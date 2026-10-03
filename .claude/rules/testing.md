# Testing strategy

Tests exist to prove that the product works for a user, not to pin down how
the code is written. Optimise for confidence per line of test code and for
the freedom to refactor.

## Every change: verify the key flows end to end

Before calling a change done, exercise the flows it touches against a real
daemon and a real VM — not mocks. Run the relevant subset of `tests/`
(`uv run python -m pytest tests/ -m "not slow"`), and when the change is
user-facing (CLI, TUI, SDK, HTTP API) also drive it by hand the way a user
would and report what you saw.

Key flows:

1. **Daemon lifecycle** — `flint start` comes up healthy, golden snapshot ready,
   `/health` reports the backend.
2. **Sandbox lifecycle** — create → run command → pause → resume → kill; it
   disappears from the list.
3. **Exec** — stdout/stderr/exit code, env vars, cwd, streaming callbacks, PTY.
4. **Files & workspace** — write/read/list/stat/delete; workspace survives
   pause/resume and is isolated between sandboxes.
5. **Isolation & network** — network policy round-trips, credential headers
   are injected by the proxy and never visible inside the VM.
6. **Templates & agents** — build a template, boot from it; agent build/deploy
   (`tests/test_agents.py`, slow — run when touching agents or templates).
7. **Observability** — events and logs WebSockets deliver lifecycle events.

A run where tests are **skipped** is not a pass. The `sandbox` fixture skips
when VM creation isn't available; check the summary line and say so if VM
tests didn't actually run. Both backends (Firecracker, Cloud Hypervisor) run
in CI — if a change is backend-specific, test that backend.

## What to write

- **Prefer extending an existing e2e test** for a new feature's happy path
  over adding a new file. Assert on what a user observes (API responses,
  command output, files in the VM), never on internal calls or call order.
- **Unit tests only for real logic** — parsing, policy evaluation, state
  machines, registry resolution: pure functions with interesting inputs.
  If a test needs heavy mocking to exist, it's testing implementation; don't
  write it.
- **No regression tests.** Don't add a test per bug fix that re-asserts the
  specific broken detail. Fix the bug, verify the flow end to end, move on.
  If the bug exposed a gap in a key flow, strengthen that flow's test instead.
- **Delete tests that block a legitimate change** rather than contorting the
  code to keep them green — as long as the key flows are still covered.

## How to write it

- One behaviour per test, named after the behaviour (`test_workspace_persists_across_pause_resume`).
- Tests must be independent and order-free: each creates and kills its own sandbox.
- No `sleep` for synchronisation — poll for a condition with a deadline.
- Failure messages should explain themselves: include the command output or
  daemon log tail, so a CI failure is diagnosable without a rerun.
- A test you haven't seen fail proves nothing — when adding one, break the
  code briefly to confirm it catches the failure.
