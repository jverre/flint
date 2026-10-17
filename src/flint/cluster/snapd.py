"""Client for this host's flint-cluster snapd (local unix socket)."""

from __future__ import annotations

import httpx


class SnapdError(RuntimeError):
    pass


class Snapd:
    def __init__(self, socket_path: str) -> None:
        self._http = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket_path), base_url="http://snapd", timeout=900.0,
        )

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        kwargs = {"json": body} if body is not None else {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise SnapdError(f"snapd unreachable: {e}") from None
        if resp.status_code >= 300:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise SnapdError(f"snapd {method} {path}: {detail}")
        return resp.json()

    def health(self) -> dict:
        return self._call("GET", "/health", timeout=5)

    def create(self, snapshot_id: str, kind: str, root: str, files: dict[str, str], *,
               upload: str = "sync", parent: str = "", meta: dict | None = None) -> dict:
        """Snapshot ``files`` (names -> paths relative to ``root``).

        upload: "sync" returns once in the object store; "async" once cached
        here (peers can fetch it immediately); "none" keeps it local.
        """
        return self._call("POST", "/v1/snapshots", {
            "id": snapshot_id, "kind": kind, "root": root, "files": files,
            "upload": upload, "parent": parent, "meta": meta or {},
        })

    def wait_uploaded(self, snapshot_id: str) -> None:
        self._call("POST", f"/v1/snapshots/{snapshot_id}/wait")

    def restore(self, snapshot_id: str, dest: str, peers: list[str] | None = None, names: list[str] | None = None) -> dict:
        return self._call("POST", "/v1/restore", {"id": snapshot_id, "dest": dest, "peers": peers or [], "names": names or []})

    def cache(self) -> dict:
        return self._call("GET", "/v1/cache", timeout=10)

    def fence(self, ttl_ms: int, vms: list[dict]) -> dict:
        return self._call("POST", "/v1/fence", {"ttl_ms": ttl_ms, "vms": vms}, timeout=5)

    def disarm(self) -> dict:
        return self._call("DELETE", "/v1/fence", timeout=5)
