#!/usr/bin/env python3
"""Measure one long-lived TCP echo connection while maintenance runs.

Run the probe inside the VM against an external echo service, and perform
maintenance from the control plane. --action is for trusted staging runners
that can reach both the workload endpoint and the maintenance target.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import socket
import subprocess
import threading
import time


def rehearse(
    host: str,
    port: int,
    duration: float,
    interval: float,
    timeout: float,
    actions: list[str],
) -> dict:
    report = {
        "requests": 0,
        "request_failures": 0,
        "connection_resets": 0,
        "maximum_pause_ms": 0.0,
        "actions": [],
    }
    stop = threading.Event()
    ready = threading.Event()
    start = time.monotonic()

    def workload():
        conn = None
        seq = 0
        try:
            while not stop.is_set() and time.monotonic() - start < duration:
                sent = time.monotonic()
                try:
                    if conn is None:
                        conn = socket.create_connection((host, port), timeout)
                        conn.settimeout(timeout)
                    payload = (f"flint-maintenance:{seq}:" + "x" * 48 + "\n").encode()
                    conn.sendall(payload)
                    received = bytearray()
                    while len(received) < len(payload):
                        data = conn.recv(len(payload) - len(received))
                        if not data:
                            raise ConnectionResetError("echo connection closed")
                        received.extend(data)
                    if received != payload:
                        raise ValueError("echo payload changed")
                    report["requests"] += 1
                    ready.set()
                except (OSError, ValueError) as exc:
                    report["request_failures"] += 1
                    if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
                        report["connection_resets"] += 1
                    if conn:
                        conn.close()
                    conn = None
                finally:
                    report["maximum_pause_ms"] = max(
                        report["maximum_pause_ms"], (time.monotonic() - sent) * 1000
                    )
                seq += 1
                stop.wait(interval)
        finally:
            if conn:
                conn.close()

    thread = threading.Thread(target=workload, daemon=True)
    thread.start()
    try:
        if not ready.wait(min(timeout, duration)):
            raise RuntimeError("workload did not become healthy before maintenance")
        for action in actions:
            t0 = time.monotonic()
            result = subprocess.run(
                shlex.split(action),
                timeout=max(1, duration - (time.monotonic() - start)),
            )
            report["actions"].append(
                {
                    "command": action,
                    "returncode": result.returncode,
                    "duration_ms": (time.monotonic() - t0) * 1000,
                }
            )
            if result.returncode:
                break
        thread.join(timeout=max(0, duration - (time.monotonic() - start)) + timeout + 1)
    finally:
        stop.set()
        thread.join(timeout=timeout + 1)
    report["duration_s"] = time.monotonic() - start
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--max-pause-ms", type=float, default=1000)
    parser.add_argument("--action", action="append", default=[])
    parser.add_argument("--output", type=Path, default=Path("maintenance-result.json"))
    args = parser.parse_args()
    if (
        min(args.duration, args.interval, args.timeout, args.max_pause_ms) <= 0
        or not 1 <= args.port <= 65535
    ):
        parser.error("durations, pause budget and port must be positive and valid")
    result = rehearse(
        args.host, args.port, args.duration, args.interval, args.timeout, args.action
    )
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    if (
        result["request_failures"]
        or result["connection_resets"]
        or not result["requests"]
        or result["maximum_pause_ms"] > args.max_pause_ms
        or any(a["returncode"] for a in result["actions"])
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
