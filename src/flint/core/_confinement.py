"""Launch a parser in an empty filesystem, then discard host privileges."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import threading


def supervised_spawn(factory):
    """Keep the spawning task alive for Linux PR_SET_PDEATHSIG.

    A request thread is not a suitable parent: the kernel kills a child with
    PDEATHSIG when that particular task exits, even if the daemon is alive.
    """
    ready = threading.Event()
    result = []
    errors = []

    def supervise():
        try:
            process = factory()
            result.append(process)
        except BaseException as exc:
            errors.append(exc)
        finally:
            ready.set()
        if result:
            result[0].wait()

    threading.Thread(target=supervise, daemon=True, name="confined-process").start()
    ready.wait()
    if errors:
        raise errors[0]
    return result[0]


def parser_command(uid: int, gid: int, *, readonly: dict[str, str], writable: dict[str, str],
                   command: list[str]) -> list[str]:
    if uid == 0 or gid == 0:
        raise ValueError("Host parsers must have an unprivileged identity")
    args = ["bwrap", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--new-session", "--die-with-parent", "--clearenv",
            "--cap-drop", "ALL", "--cap-add", "CAP_SETUID", "--cap-add", "CAP_SETGID",
            "--cap-add", "CAP_SETPCAP", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--chmod", "1777", "/tmp",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", "/tmp", "--setenv", "PYTHONNOUSERSITE", "1"]
    # Bind only the interpreter, its installed dependencies and public TLS roots.
    for path in dict.fromkeys(("/usr", "/lib", "/lib64", sys.prefix, sys.base_prefix,
                               "/etc/ssl/certs", "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf")):
        if os.path.exists(path):
            args += ["--ro-bind", path, path]
    for source, target in readonly.items():
        args += ["--ro-bind", source, target]
    for source, target in writable.items():
        args += ["--bind", source, target]
    args += ["--chmod", "0755", "/etc", "--chmod", "0755", "/etc/ssl"]
    # bwrap copies ancestor permissions when constructing mountpoints. A venv
    # below /root or a private home needs traversable *synthetic* parents; no
    # other files from that home are exposed and the host modes stay untouched.
    for parent in reversed(Path(sys.prefix).parents):
        path = str(parent)
        if path != "/" and not any(path == p or path.startswith(p + "/") for p in ("/usr", "/lib", "/lib64")):
            args += ["--chmod", "0755", path]
    # The only remaining capabilities belong to this fixed launcher. setpriv
    # clears groups and the capability bounding set before the parser execs.
    return args + ["--", "/usr/bin/setpriv", f"--reuid={uid}", f"--regid={gid}",
                   "--clear-groups", "--no-new-privs", "--bounding-set=-all",
                   "--inh-caps=-all", "--ambient-caps=-all", "--", *command]
