"""Shared volumes: storage mounted into running sandboxes.

Mounting a volume puts the whole volume, or one ``subpath`` of it, at
``path`` inside a running sandbox, read-write or read-only, at any point in
the sandbox's life and as many times as you like. The guest mounts it over
NFS from Flint's host NFS server, which authorizes each mount by the
sandbox's bridge IP and revokes it on unmount.

One writer per subtree: a read-write mount is refused while another
read-write mount of the same volume overlaps it (same subpath, or one inside
the other). Read-only mounts are unlimited. So a parent can mount a volume
read-only while each child writes its own subpath.

Two kinds, served the same way from ``DAEMON_DIR/volumes/<id>``:

- ``image`` (the default where the host supports NBD): a lazy copy-on-write
  block image (``services/r2nfs/blockstore``) whose ext4 filesystem the NFS
  server mounts there. It has a size, and supports snapshots (read-only
  copies, cheap: they share every block) and forks (writable copies).
- ``dir``: a plain host directory.

Exclusive mounts (image volumes only) give one sandbox the volume as a raw
disk: the host serves the image over NBD and the sandbox's own kernel mounts
the ext4, so file operations run at local-disk speed. Nobody else can mount
the volume meanwhile; others read it through snapshots. A volume a sandbox
has had as a raw disk is "guest-written": its filesystem is untrusted, so
the host never mounts it again. From then on it, and its forks, can only be
mounted exclusively, and its snapshots are mounted read-only as block
devices in each reader's own guest.

Shared mounts are for sharing files between sandboxes, not for hot
workloads: every metadata operation crosses to the host, so package installs
and databases belong on an exclusive volume or the sandbox's own disk.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass

from . import _nfs_server
from .config import BRIDGE_IP, NBD_PORT, NBD_RO_PORT, R2NFS_PORT, log
from ._nfs_server import VOLUMES_DIR
from .types import SandboxState


_VOLUMES_TABLE = """
CREATE TABLE IF NOT EXISTS volumes (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at REAL NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'dir',
    size_gib   INTEGER,
    readonly   INTEGER NOT NULL DEFAULT 0,
    source     TEXT,
    guest_written INTEGER NOT NULL DEFAULT 0,
    tenant     TEXT NOT NULL DEFAULT 'default'
);
"""
_SCHEMA = _VOLUMES_TABLE + """
CREATE UNIQUE INDEX IF NOT EXISTS volumes_tenant_name ON volumes (tenant, name);
CREATE TABLE IF NOT EXISTS volume_mounts (
    id         TEXT PRIMARY KEY,
    volume_id  TEXT NOT NULL,
    vm_id      TEXT NOT NULL,
    path       TEXT NOT NULL,
    subpath    TEXT NOT NULL,
    readonly   INTEGER NOT NULL,
    client_ip  TEXT NOT NULL,
    created_at REAL NOT NULL,
    transport  TEXT NOT NULL DEFAULT 'nfs',
    device     TEXT,
    UNIQUE (vm_id, path)
);
"""

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

DEFAULT_SIZE_GIB = 10  # sparse: an image only uses what is written to it
_MAX_SIZE_GIB = 16384
_VOLUME_COLUMNS = "id, name, created_at, kind, size_gib, readonly, source, guest_written, tenant"
_MOUNT_COLUMNS = "id, volume_id, vm_id, path, subpath, readonly, client_ip, created_at, transport, device"

# Readers revalidate attributes every second so they see other sandboxes'
# writes promptly; a writer owns its subtree, so it can cache longer.
_NFS_OPTIONS = f"vers=3,proto=tcp,port={R2NFS_PORT},mountport={R2NFS_PORT},mountproto=tcp,nolock,soft,timeo=100,retrans=3"
_READER_OPTIONS = "ro,actimeo=1,lookupcache=positive"
_WRITER_OPTIONS = "rw,actimeo=30"

# Where a block mount's filesystem is mounted in the guest; `path` is a bind
# mount of it (or of its subpath).
_GUEST_DISKS_DIR = "/run/flint/disks"


class VolumeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Volume:
    id: str
    name: str
    created_at: float
    kind: str = "dir"
    size_gib: int | None = None
    readonly: bool = False
    source: str | None = None  # volume this is a snapshot or fork of
    guest_written: bool = False  # a sandbox has had it as a raw disk
    tenant: str = "default"  # only this tenant's sandboxes can mount it

    def to_dict(self) -> dict:
        return asdict(self)


def _volume(row) -> Volume:
    return Volume(row[0], row[1], row[2], row[3], row[4], bool(row[5]), row[6], bool(row[7]), row[8])


@dataclass
class Mount:
    id: str
    volume_id: str
    vm_id: str
    path: str
    subpath: str
    readonly: bool
    client_ip: str
    created_at: float
    transport: str = "nfs"  # "nfs" (shared) or "block" (NBD, mounted by the guest)
    device: str | None = None  # the guest's NBD device, for block mounts

    @property
    def exclusive(self) -> bool:
        return self.transport == "block" and not self.readonly

    @property
    def guest_dir(self) -> str:
        return f"{_GUEST_DISKS_DIR}/{self.id}"

    def to_dict(self) -> dict:
        d = asdict(self)
        del d["client_ip"], d["device"], d["transport"]
        d["exclusive"] = self.exclusive
        return d


def _clean_path(path: object) -> str:
    if not isinstance(path, str) or not path.startswith("/"):
        raise VolumeError(400, "'path' must be an absolute path")
    path = posixpath.normpath(path)
    if path == "/":
        raise VolumeError(400, "Can't mount over /")
    return path


def _clean_subpath(subpath: object) -> str:
    if not isinstance(subpath, str):
        raise VolumeError(400, "'subpath' must be a string")
    subpath = posixpath.normpath("/" + subpath).lstrip("/")
    if ".." in subpath.split("/"):
        raise VolumeError(400, "'subpath' can't contain '..'")
    return subpath


def _overlaps(a: str, b: str) -> bool:
    """True if subpath a is b, or one contains the other ("" is the whole volume)."""
    if not a or not b or a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


class Volumes:
    """Volume CRUD plus mounting into sandboxes. ``manager`` is the SandboxManager."""

    def __init__(self, conn: sqlite3.Connection, manager, state_store=None) -> None:
        self._conn = conn
        self._manager = manager
        self._state_store = state_store
        self._lock = threading.Lock()
        with self._lock:
            # Volumes used to be unattachable raw images; keep any old rows aside.
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(volumes)")}
            if "image_path" in cols:
                self._conn.execute("ALTER TABLE volumes RENAME TO volumes_legacy_images")
            elif cols:
                for col, decl in (("kind", "TEXT NOT NULL DEFAULT 'dir'"), ("size_gib", "INTEGER"),
                                  ("readonly", "INTEGER NOT NULL DEFAULT 0"), ("source", "TEXT"),
                                  ("guest_written", "INTEGER NOT NULL DEFAULT 0"),
                                  ("tenant", "TEXT NOT NULL DEFAULT 'default'")):
                    if col not in cols:
                        self._conn.execute(f"ALTER TABLE volumes ADD COLUMN {col} {decl}")
                self._drop_global_name_uniqueness()
            mount_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(volume_mounts)")}
            if mount_cols:
                for col, decl in (("transport", "TEXT NOT NULL DEFAULT 'nfs'"), ("device", "TEXT")):
                    if col not in mount_cols:
                        self._conn.execute(f"ALTER TABLE volume_mounts ADD COLUMN {col} {decl}")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        os.makedirs(VOLUMES_DIR, exist_ok=True)

    def _drop_global_name_uniqueness(self) -> None:
        """Names used to be unique across the host; now they are per tenant.
        SQLite can't drop a column constraint, so rebuild the table."""
        for idx in self._conn.execute("PRAGMA index_list(volumes)").fetchall():
            if idx[3] != "u":  # origin: only the inline UNIQUE constraint
                continue
            cols = [c[2] for c in self._conn.execute(f"PRAGMA index_info('{idx[1]}')")]
            if cols == ["name"]:
                self._conn.execute("ALTER TABLE volumes RENAME TO volumes_before_tenants")
                self._conn.executescript(_VOLUMES_TABLE)
                self._conn.execute(f"INSERT INTO volumes ({_VOLUME_COLUMNS}) "
                                   f"SELECT {_VOLUME_COLUMNS} FROM volumes_before_tenants")
                self._conn.execute("DROP TABLE volumes_before_tenants")
                return

    # ── Volumes ──────────────────────────────────────────────────────────
    # ``tenants`` scopes a lookup to the volumes of those tenants (None: all).

    @staticmethod
    def _scope(tenants: tuple[str, ...] | None) -> tuple[str, list]:
        if tenants is None:
            return "", []
        return f" AND tenant IN ({', '.join('?' * len(tenants))})", list(tenants)

    def list(self, tenants: tuple[str, ...] | None = None) -> list[Volume]:
        where, args = self._scope(tenants)
        rows = self._conn.execute(
            f"SELECT {_VOLUME_COLUMNS} FROM volumes WHERE 1{where} ORDER BY created_at DESC", args,
        ).fetchall()
        return [_volume(r) for r in rows]

    def get(self, ref: str, tenants: tuple[str, ...] | None = None) -> Volume | None:
        """Look a volume up by ID or name."""
        where, args = self._scope(tenants)
        rows = self._conn.execute(
            f"SELECT {_VOLUME_COLUMNS} FROM volumes WHERE (id = ? OR name = ?){where}", [ref, ref, *args],
        ).fetchall()
        if len(rows) > 1:
            byid = [r for r in rows if r[0] == ref]
            if not byid:
                raise VolumeError(409, f"Several tenants have a volume named '{ref}'; use its ID")
            rows = byid
        return _volume(rows[0]) if rows else None

    def _require(self, ref: str, tenants: tuple[str, ...] | None = None) -> Volume:
        vol = self.get(ref, tenants)
        if vol is None:
            raise VolumeError(404, f"Volume '{ref}' not found")
        return vol

    def create(self, name: object, size_gib: object = None, tenant: str = "default") -> Volume:
        """Create an empty volume: an image of ``size_gib`` if the host supports images, else a directory."""
        if size_gib is not None and (not isinstance(size_gib, int) or isinstance(size_gib, bool)
                                     or not 1 <= size_gib <= _MAX_SIZE_GIB):
            raise VolumeError(400, f"'size_gib' must be an integer from 1 to {_MAX_SIZE_GIB}")
        if _nfs_server.images_available():
            return self._add(name, kind="image", size_gib=size_gib or DEFAULT_SIZE_GIB, tenant=tenant)
        if size_gib is not None:
            raise VolumeError(503, "Sized volumes need block images, which this host can't serve (no NBD)")
        return self._add(name, kind="dir", tenant=tenant)

    def snapshot(self, ref: str, name: object, tenants: tuple[str, ...] | None = None) -> Volume:
        """A read-only copy of the volume as it is now. Cheap: it shares every block."""
        return self._copy(ref, name, readonly=True, tenants=tenants)

    def fork(self, ref: str, name: object, tenants: tuple[str, ...] | None = None) -> Volume:
        """A writable copy of the volume as it is now, independent from then on."""
        return self._copy(ref, name, readonly=False, tenants=tenants)

    def _copy(self, ref: str, name: object, readonly: bool, tenants: tuple[str, ...] | None) -> Volume:
        src = self._require(ref, tenants)
        if src.kind != "image":
            raise VolumeError(400, f"Volume '{src.name}' is a plain directory; only image volumes can be copied")
        owner = next((m for m in self._mounts_where("volume_id = ?", src.id) if m.exclusive), None)
        if owner is None:
            return self._add(name, kind="image", size_gib=src.size_gib, readonly=readonly, source=src, tenant=src.tenant)
        # A sandbox owns the filesystem: have it flush and hold writes while
        # the copy is taken, so the copy is consistent.
        if self._manager.get_entry(owner.vm_id) is None:
            raise VolumeError(409, f"Volume '{src.name}' is mounted exclusively by paused sandbox "
                                   f"{owner.vm_id}; resume it to copy the volume")
        self._agent_json(owner.vm_id, "/fs/freeze", {"path": owner.guest_dir})
        try:
            return self._add(name, kind="image", size_gib=src.size_gib, readonly=readonly, source=src, tenant=src.tenant)
        finally:
            self._agent_json(owner.vm_id, "/fs/thaw", {"path": owner.guest_dir})

    def _add(self, name: object, *, kind: str, size_gib: int | None = None,
             readonly: bool = False, source: Volume | None = None, tenant: str = "default") -> Volume:
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise VolumeError(400, "'name' must be 1-63 letters, digits, '.', '_' or '-'")
        vol = Volume(id="vol-" + uuid.uuid4().hex[:12], name=name, created_at=time.time(), kind=kind,
                     size_gib=size_gib, readonly=readonly, source=source.id if source else None,
                     guest_written=bool(source and source.guest_written), tenant=tenant)
        with self._lock:
            if self.get(name, (tenant,)) is not None:
                raise VolumeError(409, f"Volume '{name}' already exists")
            try:
                if kind == "image":
                    _nfs_server.create_image(vol.id, (size_gib or 0) << 30, source.id if source else "", readonly)
                    if not vol.guest_written:
                        _nfs_server.mount_image(vol.id)
                else:
                    os.makedirs(os.path.join(VOLUMES_DIR, vol.id), mode=0o755)
            except _nfs_server.NfsServerError as e:
                self._discard(vol)
                raise VolumeError(500, f"Creating volume '{name}' failed: {e}") from e
            self._conn.execute(
                f"INSERT INTO volumes ({_VOLUME_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (vol.id, vol.name, vol.created_at, vol.kind, vol.size_gib, int(vol.readonly), vol.source,
                 int(vol.guest_written), vol.tenant),
            )
            self._conn.commit()
        log.info("Created %s volume %s (%s)%s", kind, vol.id, name, f" from {source.id}" if source else "")
        return vol

    def delete(self, ref: str, tenants: tuple[str, ...] | None = None) -> Volume:
        with self._lock:
            vol = self._require(ref, tenants)
            if self._mounts_where("volume_id = ?", vol.id):
                raise VolumeError(409, f"Volume '{vol.name}' is mounted; unmount it first")
            self._conn.execute("DELETE FROM volumes WHERE id = ?", (vol.id,))
            self._conn.commit()
        self._discard(vol)
        log.info("Deleted volume %s (%s)", vol.id, vol.name)
        return vol

    @staticmethod
    def _discard(vol: Volume) -> None:
        if vol.kind == "image":
            try:
                _nfs_server.delete_image(vol.id)  # unmounts it first
            except _nfs_server.NfsServerError as e:
                log.warning("Failed to delete image %s: %s", vol.id, e)
        # Wiped like a sandbox's files; never follows the symlinks sandboxes
        # may have left in it. (The storage server wipes image volumes.)
        from ._wipe import wipe_tree
        wipe_tree(os.path.join(VOLUMES_DIR, vol.id))

    # ── Mounts ───────────────────────────────────────────────────────────

    def _mounts_where(self, where: str, *args) -> list[Mount]:
        rows = self._conn.execute(
            f"SELECT {_MOUNT_COLUMNS} FROM volume_mounts WHERE {where} ORDER BY created_at", args,
        ).fetchall()
        return [Mount(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6], r[7], r[8], r[9]) for r in rows]

    def list_mounts(self, vm_id: str) -> list[Mount]:
        return self._mounts_where("vm_id = ?", vm_id)

    def mount(self, vm_id: str, volume: object, path: object, subpath: object = "",
              readonly: object = False, exclusive: object = False) -> Mount:
        if not isinstance(volume, str) or not volume:
            raise VolumeError(400, "'volume' is required")
        if not isinstance(readonly, bool) or not isinstance(exclusive, bool):
            raise VolumeError(400, "'readonly' and 'exclusive' must be booleans")
        if exclusive and readonly:
            raise VolumeError(400, "Exclusive mounts are read-write; mount a snapshot for a read-only disk")
        path = _clean_path(path)
        subpath = _clean_subpath(subpath)
        entry = self._manager.get_entry(vm_id)
        if entry is None:
            raise VolumeError(409, f"Sandbox {vm_id} is not running")
        # Only the sandbox's own tenant's volumes: another tenant's don't exist for it.
        tenant = (entry.tenant or "default",)
        self._require(volume, tenant)
        client_ip = (entry.backend_metadata or {}).get("veth_ip", "")
        if not client_ip:
            raise VolumeError(409, "Sandbox has no network path to the host's volume server "
                                   "(sandboxes created with allow_internet_access=False can't mount volumes)")

        with self._lock:
            vol = self._require(volume, tenant)
            if vol.readonly and not readonly:
                raise VolumeError(400, f"Volume '{vol.name}' is a read-only snapshot; mount it with readonly=True")
            if self._mounts_where("vm_id = ? AND path = ?", vm_id, path):
                raise VolumeError(409, f"Something is already mounted at {path}")
            others = self._mounts_where("volume_id = ?", vol.id)
            holder = next((o for o in others if o.exclusive), None)
            if holder:
                raise VolumeError(409, f"Volume '{vol.name}' is mounted exclusively by sandbox {holder.vm_id} "
                                       f"at {holder.path}; snapshot it to read it elsewhere")
            if exclusive:
                if vol.kind != "image":
                    raise VolumeError(400, f"Volume '{vol.name}' is a plain directory; only image volumes "
                                           "can be mounted exclusively")
                if others:
                    raise VolumeError(409, f"Volume '{vol.name}' is mounted by sandbox {others[0].vm_id} "
                                           f"at {others[0].path}; an exclusive mount needs it to itself")
                transport = "block"
            elif vol.guest_written:
                if not vol.readonly:
                    raise VolumeError(409, f"Volume '{vol.name}' has been a sandbox's exclusive disk, so only "
                                           "exclusive mounts can use it now; snapshot it to share it read-only")
                transport = "block"  # read-only, in the reader's own guest
            else:
                transport = "nfs"
                if not readonly:
                    for other in others:
                        if not other.readonly and _overlaps(subpath, other.subpath):
                            raise VolumeError(409, (
                                f"'{vol.name}:/{subpath}' overlaps '{vol.name}:/{other.subpath}', which sandbox "
                                f"{other.vm_id} has mounted read-write at {other.path}; only one writer per subtree"
                            ))
            m = Mount(
                id="mnt-" + uuid.uuid4().hex, volume_id=vol.id, vm_id=vm_id, path=path, subpath=subpath,
                readonly=readonly, client_ip=client_ip, created_at=time.time(), transport=transport,
            )
            # Insert first so the checks above see this mount while it's being set up.
            self._conn.execute(
                f"INSERT INTO volume_mounts ({_MOUNT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (m.id, m.volume_id, m.vm_id, m.path, m.subpath, int(m.readonly), m.client_ip, m.created_at,
                 m.transport, m.device),
            )
            self._conn.commit()

        try:
            if transport == "nfs":
                self._mount_nfs(m)
            else:
                self._mount_block(m, vol)
        except Exception as e:
            self._forget(m)
            if m.exclusive and not vol.guest_written:
                self._restore_to_host(vol)
            raise VolumeError(500, f"Mount failed: {e}") from e
        log.info("[%s] mounted %s:/%s at %s (%s%s)", vm_id[:8], vol.name, subpath, path,
                 "ro" if readonly else "rw", ", exclusive" if exclusive else "")
        return m

    def _mount_nfs(self, m: Mount) -> None:
        _nfs_server.register_mount(m.id, m.client_ip, m.volume_id, m.subpath, m.readonly)
        options = f"{_NFS_OPTIONS},{_READER_OPTIONS if m.readonly else _WRITER_OPTIONS}"
        # Through a shell rather than flintd's /mount/nfs: in templates
        # built before init-net.sh set PATH, flintd can't find `mount`.
        script = 'mkdir -p "$1" && mount -t nfs -o "$2" "$3" "$1"'
        self._guest_sh(m.vm_id, script, m.path, options, f"{BRIDGE_IP}:/{m.id}")

    def _mount_block(self, m: Mount, vol: Volume) -> None:
        if m.exclusive and not vol.guest_written:
            # The sandbox's kernel owns the filesystem from now on. Marked
            # first, so a failure part-way leaves it untrusted.
            with self._lock:
                self._conn.execute("UPDATE volumes SET guest_written = 1 WHERE id = ?", (vol.id,))
                self._conn.commit()
            _nfs_server.unmount_image(vol.id)
        _nfs_server.export_image(m.id, vol.id, m.client_ip, m.readonly)
        port = NBD_RO_PORT if m.readonly else NBD_PORT
        resp = self._agent_json(m.vm_id, "/disks/attach", {"address": f"{BRIDGE_IP}:{port}", "export": m.id})
        m.device = resp["device"]
        with self._lock:
            self._conn.execute("UPDATE volume_mounts SET device = ? WHERE id = ?", (m.device, m.id))
            self._conn.commit()
        # Snapshots are frozen when taken: no journal to replay (and a
        # read-only device couldn't replay it).
        opts = "ro,noload" if m.readonly else "rw,noatime"
        script = (
            'mkdir -p "$1" "$2" && mount -t ext4 -o "$4" "$3" "$1" && '
            'src="$1/$5" && { [ -z "$5" ] || [ -d "$src" ] || mkdir -p "$src"; } && '
            'mount --bind "$src" "$2"'
        )
        self._guest_sh(m.vm_id, script, m.guest_dir, m.path, m.device, opts, m.subpath)

    def unmount(self, vm_id: str, path: object) -> Mount:
        path = _clean_path(path)
        found = self._mounts_where("vm_id = ? AND path = ?", vm_id, path)
        if not found:
            raise VolumeError(404, f"No volume mounted at {path}")
        m = found[0]
        # A paused sandbox can't be reached; revoking server-side is enough,
        # its stale mount fails with errors once resumed.
        if self._manager.get_entry(vm_id) is not None:
            try:
                if m.transport == "nfs":
                    self._guest_sh(vm_id, 'umount "$1" 2>/dev/null || umount -l "$1"', path)
                else:
                    # A clean unmount flushes the filesystem to the volume; a
                    # busy one is detached lazily.
                    self._guest_sh(vm_id, 'for p in "$1" "$2"; do umount "$p" 2>/dev/null || umount -l "$p"; done; '
                                          'rmdir "$2" 2>/dev/null; true', path, m.guest_dir)
                    if m.device:
                        self._agent_json(vm_id, "/disks/detach", {"device": m.device})
            except Exception as e:
                log.warning("[%s] umount %s failed in guest: %s", vm_id[:8], path, e)
        self._forget(m)
        log.info("[%s] unmounted %s", vm_id[:8], path)
        return m

    def release_vm(self, vm_id: str) -> list[Mount]:
        """Drop every mount of a sandbox that is going away."""
        mounts = self.list_mounts(vm_id)
        for m in mounts:
            self._forget(m)
        return mounts

    def replay(self) -> None:
        """At daemon start: make sure host-served image volumes are mounted on the host,
        re-register sandbox mounts with the storage server, and drop mounts of sandboxes
        that are gone."""
        for vol in self.list():
            if vol.kind == "image" and not vol.guest_written:
                try:
                    _nfs_server.mount_image(vol.id)
                except _nfs_server.NfsServerError as e:
                    log.warning("Could not mount image volume %s (%s): %s", vol.id, vol.name, e)
        for m in self._mounts_where("1"):
            row = self._state_store.get_sandbox(m.vm_id) if self._state_store else None
            if not row or row.get("state") not in (SandboxState.RUNNING.value, SandboxState.PAUSED.value):
                self._forget(m)
                continue
            try:
                if m.transport == "nfs":
                    _nfs_server.register_mount(m.id, m.client_ip, m.volume_id, m.subpath, m.readonly)
                else:
                    _nfs_server.export_image(m.id, m.volume_id, m.client_ip, m.readonly)
            except _nfs_server.NfsServerError as e:
                log.warning("[%s] could not restore mount %s: %s", m.vm_id[:8], m.path, e)

    def _restore_to_host(self, vol: Volume) -> None:
        """After a failed first exclusive mount: if the sandbox never attached the
        disk, the host still trusts it and takes it back (the storage server
        refuses images a sandbox has written)."""
        try:
            _nfs_server.mount_image(vol.id)
        except _nfs_server.NfsServerError as e:
            log.warning("Volume %s stays exclusive-only: %s", vol.id, e)
            return
        with self._lock:
            self._conn.execute("UPDATE volumes SET guest_written = 0 WHERE id = ?", (vol.id,))
            self._conn.commit()

    def _guest_sh(self, vm_id: str, script: str, *args: str) -> None:
        """Run a shell script in the guest with positional args; raise on failure."""
        body = json.dumps({"cmd": ["/bin/sh", "-c", script, "sh", *args], "timeout": 30}).encode()
        status, resp = self._manager.agent_request(vm_id, "POST", "/exec", body, 40)
        result = json.loads(resp) if status == 200 else {}
        if result.get("exit_code") != 0:
            detail = (result.get("stderr") or result.get("stdout") or resp.decode(errors="replace")).strip()
            raise VolumeError(500, f"In sandbox: {detail}")

    def _agent_json(self, vm_id: str, path: str, body: dict) -> dict:
        """POST to the guest agent; raise with its error on failure."""
        status, resp = self._manager.agent_request(vm_id, "POST", path, json.dumps(body).encode(), 90)
        if status == 404 and b"error" not in resp:
            raise VolumeError(501, "This sandbox's guest agent predates exclusive volumes; rebuild its template")
        try:
            data = json.loads(resp)
        except ValueError:
            data = {"error": resp.decode(errors="replace")}
        if status != 200:
            raise VolumeError(500, f"In sandbox ({path}): {data.get('error', data)}")
        return data

    def _forget(self, m: Mount) -> None:
        try:
            if m.transport == "nfs":
                _nfs_server.deregister_mount(m.id)
            else:
                _nfs_server.unexport_image(m.id)
        except _nfs_server.NfsServerError as e:
            log.warning("Failed to revoke mount %s: %s", m.id, e)
        with self._lock:
            self._conn.execute("DELETE FROM volume_mounts WHERE id = ?", (m.id,))
            self._conn.commit()
