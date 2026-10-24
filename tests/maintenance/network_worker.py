"""Runs inside one simulated compute host's network namespace."""

import sys

from flint.core._migration_network import MigrationNetwork
from flint.core._netns import ensure_host_firewall

VM = "07ce885b-bf8a-41c3-a9c5-6dc9f09cba11"
role = sys.argv[1]


class Network(MigrationNetwork):
    @property
    def namespace(self):
        return "compute-" + role


net = Network(
    VM,
    42,
    "10.90.0.1" if role != "destination" else "10.90.0.2",
    "10.90.0.3",
    "wg-flint",
    1500,
)
if role == "gateway":
    ensure_host_firewall()
    net.setup_gateway(True)
else:
    net.setup_compute(True)
