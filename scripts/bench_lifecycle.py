"""Measure sandbox lifecycle latency against a running daemon.

Runs N cycles of create -> exec -> (dirty memory -> pause -> resume -> verify
memory) x rounds -> kill and prints p50/p90/max per phase, as seen by an SDK user.

    uv run python scripts/bench_lifecycle.py -n 10 --dirty-mb 24

Point it at a non-default daemon with FLINT_PORT / FLINT_STATE_DIR.
"""

from __future__ import annotations

import argparse
import statistics
import time
from collections import defaultdict

from flint import Sandbox


def _pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))]


def run_cycle(template_id: str, dirty_mb: int, rounds: int) -> dict[str, list[float]]:
    t: dict[str, list[float]] = defaultdict(list)

    def timed(phase, fn):
        t0 = time.monotonic()
        result = fn()
        t[phase].append(time.monotonic() - t0)
        return result

    sb = timed("create", lambda: Sandbox(template_id=template_id))
    try:
        res = timed("first_exec", lambda: sb.commands.run("echo ready"))
        assert res.stdout.strip() == "ready", res

        digests: dict[str, str] = {}
        for i in range(rounds):
            if dirty_mb:
                # Touch guest memory so pause has real state to write out.
                res = sb.commands.run(
                    "mkdir -p /run/bench && (mountpoint -q /run/bench || mount -t tmpfs -o size=100m tmpfs /run/bench) && "
                    f"dd if=/dev/urandom of=/run/bench/blob{i} bs=1M count={dirty_mb} 2>/dev/null && md5sum /run/bench/blob{i}",
                    timeout=120,
                )
                assert res.exit_code == 0, res
                digests[f"blob{i}"] = res.stdout.split()[0]

            timed("pause", sb.pause)
            timed("resume", sb.resume)

            res = timed("exec_after_resume", lambda: sb.commands.run("cd /run/bench 2>/dev/null; md5sum blob* 2>/dev/null; echo ok"))
            seen = {line.split()[1]: line.split()[0] for line in res.stdout.splitlines() if "blob" in line}
            assert seen == digests, f"guest memory changed across pause/resume: {seen} != {digests}"
    finally:
        timed("kill", sb.kill)
    return t


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-n", "--iterations", type=int, default=10)
    parser.add_argument("--template", default="default")
    parser.add_argument("--dirty-mb", type=int, default=24, help="guest memory to dirty before pause (0 to skip)")
    parser.add_argument("--rounds", type=int, default=2, help="pause/resume rounds per sandbox")
    parser.add_argument("--warmup", type=int, default=1)
    args = parser.parse_args()

    for _ in range(args.warmup):
        run_cycle(args.template, args.dirty_mb, args.rounds)

    samples: dict[str, list[float]] = defaultdict(list)
    for _ in range(args.iterations):
        for phase, values in run_cycle(args.template, args.dirty_mb, args.rounds).items():
            samples[phase].extend(v * 1000 for v in values)

    print(f"{'phase':<20}{'p50 ms':>10}{'p90 ms':>10}{'max ms':>10}{'mean ms':>10}")
    for phase, values in samples.items():
        print(
            f"{phase:<20}{_pct(values, 0.5):>10.1f}{_pct(values, 0.9):>10.1f}"
            f"{max(values):>10.1f}{statistics.mean(values):>10.1f}"
        )


if __name__ == "__main__":
    main()
