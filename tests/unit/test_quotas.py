"""Tenant quotas: which creates fit, and creates in flight count."""

from __future__ import annotations

import os

import pytest

from flint.core._quotas import QuotaExceeded, Quotas, Usage, over


def test_a_create_fits_until_a_limit_is_reached():
    limits = {"max_vms": 2, "max_memory_mib": 512}
    assert over(limits, Usage(1, 1, 128), Usage(1, 1, 128)) is None
    assert "2 VMs" in over(limits, Usage(2, 2, 256), Usage(1, 1, 128))
    assert "MiB of memory" in over(limits, Usage(1, 1, 400), Usage(1, 1, 128))
    assert over({"max_vms": 0}, Usage(1000), Usage(1)) is None  # 0 = no limit


def test_creates_in_flight_count_against_the_quota(tmp_path):
    quotas = Quotas(os.path.join(tmp_path, "flint.db"))
    quotas.set("acme", {"max_vms": 2})
    with quotas.reserve("acme", lambda: Usage(1), Usage(1)):
        with pytest.raises(QuotaExceeded, match="acme"):
            with quotas.reserve("acme", lambda: Usage(1), Usage(1)):
                pass
        with quotas.reserve("globex", lambda: Usage(1), Usage(1)):  # another tenant isn't affected
            pass
    with quotas.reserve("acme", lambda: Usage(1), Usage(1)):  # released once the first create ended
        pass


def test_limits_change_one_at_a_time(tmp_path):
    quotas = Quotas(os.path.join(tmp_path, "flint.db"))
    quotas.set("acme", {"max_vms": 3, "max_vcpus": 6})
    assert quotas.set("acme", {"max_vms": 5}) == {"max_vms": 5, "max_vcpus": 6, "max_memory_mib": 0, "max_volume_gib": 0}
    for bad in ({"max_vms": -1}, {"max_vms": "3"}, {"max_disks": 1}):
        with pytest.raises(ValueError):
            quotas.set("acme", bad)
