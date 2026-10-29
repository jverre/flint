"""Talking to the guest agent (flintd) in a sandbox: bounded HTTP, a typed client, the terminal bridge."""

from .agent import ExecResult, GuestAgent
from .http import GuestAgentError
from .terminal import bridge_terminal

__all__ = ["ExecResult", "GuestAgent", "GuestAgentError", "bridge_terminal"]
