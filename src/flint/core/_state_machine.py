"""State machine for sandbox lifecycle transitions."""

from __future__ import annotations

from .types import SandboxState

# Valid transitions: from_state -> set of allowed to_states
_TRANSITIONS: dict[SandboxState, set[SandboxState]] = {
    SandboxState.STARTING: {SandboxState.RUNNING, SandboxState.ERROR, SandboxState.DEAD},
    SandboxState.RUNNING:  {SandboxState.PAUSED, SandboxState.ERROR, SandboxState.DEAD},
    SandboxState.PAUSED:   {SandboxState.RUNNING, SandboxState.ERROR, SandboxState.DEAD},
    SandboxState.ERROR:    {SandboxState.DEAD},
    SandboxState.DEAD:     set(),  # terminal state
}


def validate_transition(from_state: SandboxState, to_state: SandboxState) -> bool:
    """Return True if transitioning from from_state to to_state is valid."""
    allowed = _TRANSITIONS.get(from_state, set())
    return to_state in allowed
