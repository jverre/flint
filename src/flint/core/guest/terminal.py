"""The interactive terminal: a client WebSocket bridged to a PTY shell in the guest."""

from __future__ import annotations

import asyncio

from ..config import TERM_COLS, TERM_ROWS
from .agent import GuestAgent

_SIGKILL = 9


async def bridge_terminal(agent: GuestAgent, websocket) -> None:
    """Run ``/bin/sh -i`` on a PTY in the guest and relay it over ``websocket``
    (a Starlette WebSocket): its output as binary frames, binary frames back
    as its input. The shell is killed when either side goes away."""
    await websocket.accept()
    pid = await agent.start_process(["/bin/sh", "-i"], pty=True, cols=TERM_COLS, rows=TERM_ROWS)
    if pid is None:
        await websocket.close(code=1011, reason="Failed to create PTY process")
        return
    try:
        async with agent.process_output(pid) as output:
            async def to_client() -> None:
                async for data in output:
                    await websocket.send_bytes(data)

            async def to_guest() -> None:
                while True:
                    await agent.send_input(pid, await websocket.receive_bytes())

            tasks = [asyncio.create_task(to_client()), asyncio.create_task(to_guest())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await agent.signal(pid, _SIGKILL)
