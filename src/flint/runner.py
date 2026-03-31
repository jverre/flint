"""Parallel test runner using Flint sandboxes.

Shards vitest (or any test command) across multiple Firecracker microVMs
for massive parallelism with sub-100ms VM startup overhead.
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from flint.sandbox import Sandbox


# Directories/files to skip when tarring a project
_DEFAULT_EXCLUDES = {
    "node_modules",
    ".git",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".cache",
    ".turbo",
    "coverage",
    "__pycache__",
    ".venv",
    "venv",
}


@dataclass
class ShardResult:
    """Result from a single test shard."""

    shard_index: int
    total_shards: int
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    sandbox_id: str


@dataclass
class RunResult:
    """Aggregated result from all shards."""

    shards: list[ShardResult] = field(default_factory=list)
    total_duration_s: float = 0.0

    @property
    def success(self) -> bool:
        return all(s.exit_code == 0 for s in self.shards)

    @property
    def exit_code(self) -> int:
        for s in self.shards:
            if s.exit_code != 0:
                return s.exit_code
        return 0

    def summary(self) -> str:
        lines = []
        status = "PASS" if self.success else "FAIL"
        lines.append(f"\n{'='*60}")
        lines.append(f"  Test run: {status}  ({len(self.shards)} shards, {self.total_duration_s:.1f}s total)")
        lines.append(f"{'='*60}")
        for s in sorted(self.shards, key=lambda x: x.shard_index):
            icon = "+" if s.exit_code == 0 else "x"
            lines.append(
                f"  [{icon}] shard {s.shard_index}/{s.total_shards}  "
                f"exit={s.exit_code}  {s.duration_s:.1f}s  vm={s.sandbox_id[:8]}"
            )
        lines.append("")
        return "\n".join(lines)


def _create_project_tar(project_dir: str, excludes: set[str] | None = None) -> bytes:
    """Create a tar.gz archive of the project directory."""
    excludes = excludes or _DEFAULT_EXCLUDES
    buf = io.BytesIO()

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        for part in parts:
            if part in excludes:
                return None
        return info

    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(project_dir, arcname=".", filter=_filter)

    return buf.getvalue()


def _run_shard(
    shard_index: int,
    total_shards: int,
    tar_bytes: bytes,
    workdir: str,
    setup_cmd: str | None,
    test_cmd: str,
    timeout: float,
    on_output: callable | None = None,
) -> ShardResult:
    """Run a single test shard in a new sandbox."""
    sandbox = None
    start = time.monotonic()
    prefix = f"[shard {shard_index}/{total_shards}] "
    try:
        sandbox = Sandbox()
        vm_id = sandbox.id

        # Upload project archive
        sandbox.write_file("/tmp/project.tar.gz", tar_bytes)

        # Extract into workdir
        sandbox.commands.run(f"mkdir -p {workdir}", timeout=10)
        sandbox.commands.run(f"tar xzf /tmp/project.tar.gz -C {workdir}", timeout=60)
        sandbox.commands.run("rm /tmp/project.tar.gz", timeout=10)

        # Run setup command (e.g. npm ci)
        if setup_cmd:
            if on_output:
                on_output(f"{prefix}running setup: {setup_cmd}")
            result = sandbox.commands.run(f"cd {workdir} && {setup_cmd}", timeout=timeout)
            if result.exit_code != 0:
                return ShardResult(
                    shard_index=shard_index,
                    total_shards=total_shards,
                    exit_code=result.exit_code,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    duration_s=time.monotonic() - start,
                    sandbox_id=vm_id,
                )

        # Run the test command with shard flag
        shard_cmd = test_cmd.replace("{shard}", f"{shard_index}/{total_shards}")
        if on_output:
            on_output(f"{prefix}running: {shard_cmd}")
        result = sandbox.commands.run(f"cd {workdir} && {shard_cmd}", timeout=timeout)

        return ShardResult(
            shard_index=shard_index,
            total_shards=total_shards,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_s=time.monotonic() - start,
            sandbox_id=vm_id,
        )
    except Exception as exc:
        return ShardResult(
            shard_index=shard_index,
            total_shards=total_shards,
            exit_code=1,
            stdout="",
            stderr=str(exc),
            duration_s=time.monotonic() - start,
            sandbox_id=sandbox.id if sandbox else "unknown",
        )
    finally:
        if sandbox:
            try:
                sandbox.kill()
            except Exception:
                pass


def run_sharded_tests(
    *,
    project_dir: str = ".",
    shards: int = 4,
    test_cmd: str = "npx vitest run --reporter=verbose --shard={shard}",
    setup_cmd: str | None = "npm ci --prefer-offline",
    workdir: str = "/home/project",
    timeout: float = 300,
    excludes: set[str] | None = None,
    verbose: bool = True,
) -> RunResult:
    """Run tests sharded across multiple Flint sandboxes.

    Args:
        project_dir: Path to the project to test.
        shards: Number of parallel shards.
        test_cmd: Test command template. Use {shard} as placeholder for vitest's
                  --shard flag (e.g. "1/4", "2/4", etc.).
        setup_cmd: Optional setup command to run before tests (e.g. "npm ci").
        workdir: Working directory inside each VM.
        timeout: Timeout in seconds for each command.
        excludes: Directory/file names to exclude from upload.
        verbose: Print progress to stderr.

    Returns:
        RunResult with per-shard results.
    """
    project_dir = os.path.abspath(project_dir)
    run_start = time.monotonic()

    def _log(msg: str) -> None:
        if verbose:
            print(msg, file=sys.stderr, flush=True)

    # Check daemon is running
    if not Sandbox.is_daemon_running():
        _log("Error: Flint daemon is not running. Run 'flint start' first.")
        raise RuntimeError("Flint daemon is not running")

    _log(f"Packaging project: {project_dir}")
    tar_bytes = _create_project_tar(project_dir, excludes)
    tar_mb = len(tar_bytes) / (1024 * 1024)
    _log(f"Archive size: {tar_mb:.1f} MB")
    _log(f"Launching {shards} shards...")

    results: list[ShardResult] = []
    with ThreadPoolExecutor(max_workers=shards) as pool:
        futures = {
            pool.submit(
                _run_shard,
                shard_index=i + 1,
                total_shards=shards,
                tar_bytes=tar_bytes,
                workdir=workdir,
                setup_cmd=setup_cmd,
                test_cmd=test_cmd,
                timeout=timeout,
                on_output=_log,
            ): i + 1
            for i in range(shards)
        }
        for future in as_completed(futures):
            shard_result = future.result()
            results.append(shard_result)
            icon = "+" if shard_result.exit_code == 0 else "x"
            _log(
                f"  [{icon}] shard {shard_result.shard_index}/{shard_result.total_shards} "
                f"finished in {shard_result.duration_s:.1f}s"
            )

    run_result = RunResult(
        shards=results,
        total_duration_s=time.monotonic() - run_start,
    )
    _log(run_result.summary())
    return run_result
