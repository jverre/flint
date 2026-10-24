"""The rootfs pool never hands out an entry whose files are gone."""

from __future__ import annotations

import os

from flint.core import _pool


def test_an_entry_that_lost_its_files_is_not_claimed(tmp_path, monkeypatch, caplog):
    src = tmp_path / "golden-rootfs.ext4"
    src.write_bytes(b"rootfs")
    sources = {"rootfs.ext4": str(src)}
    good, lost = tmp_path / "pool" / "good", tmp_path / "pool" / "lost"
    for d in (good, lost):
        os.makedirs(d)
    (good / "rootfs.ext4").write_bytes(b"rootfs")
    entry = lambda d: {"id": d.name, "dir_path": str(d), "template_id": "default", "state": "ready",  # noqa: E731
                       "fingerprint": _pool._fingerprint(sources)}
    monkeypatch.setattr(_pool, "_pool_dir", str(tmp_path / "pool"))
    monkeypatch.setattr(_pool, "_pool_sources", lambda _t: sources)
    monkeypatch.setattr(_pool, "_pool_entries", [entry(lost), entry(good)])

    assert _pool._claim_pool_entry("default") is None  # the first ready one lost its rootfs
    assert "lost ['rootfs.ext4']" in caplog.text
    assert not lost.exists()
    assert _pool._claim_pool_entry("default") == str(good)
