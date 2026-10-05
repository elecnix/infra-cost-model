"""Match the addresses an infrastructure export declares against a cost model.

A node's ``resourceAddress`` names one logical address. Real exports rarely
match a model one to one: Terraform's address carries the module path, such as
``module.app.module.ecs.aws_ecs_service.api``, and one node often stands for
several resources on purpose, such as four load balancers billing at the same
hourly rate. A node lists those extra addresses in ``covers``, as exact strings
or glob patterns, and an address is costed when any node's address or pattern
reaches it (#449).

A pattern that reaches nothing is stale. It claims a resource the export does
not have, so it is reported rather than quietly accepted.
"""

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Iterable


@dataclass(frozen=True)
class StalePattern:
    """A `covers` pattern that matches no address in the export."""

    node: str
    pattern: str


@dataclass
class CoverageResult:
    """The three-way diff between the export's addresses and the model's claims."""

    #: Addresses the model costs.
    matched: frozenset[str]
    #: Addresses in the export that no node claims.
    uncosted: frozenset[str]
    #: Addresses a node names that the export lacks, when no `covers` pattern
    #: of that node reaches any address the export has.
    orphaned: frozenset[str]
    #: `covers` patterns that match nothing, sorted by node then pattern.
    stale_patterns: list[StalePattern]


def _covers_patterns(node_data: dict) -> list[str]:
    """The `covers` entries of a node, keeping the string ones."""
    covers = node_data.get("covers")
    if not isinstance(covers, list):
        return []
    return [entry for entry in covers if isinstance(entry, str)]


def match_coverage(nodes: dict, iac_addresses: Iterable[str]) -> CoverageResult:
    """Diff the addresses an export declares against the nodes that claim them.

    Args:
        nodes: The model's `nodes` mapping, address to node definition.
        iac_addresses: The addresses extracted from the infrastructure export.

    Returns:
        The costed addresses, the uncosted ones, the orphaned ones, and the
        `covers` patterns that reach nothing.
    """
    iac = set(iac_addresses)
    matched: set[str] = set()
    orphaned: set[str] = set()
    stale: list[StalePattern] = []

    for node_name, node_data in nodes.items():
        if not isinstance(node_data, dict):
            continue

        address = node_data.get("resourceAddress")
        own_address_found = isinstance(address, str) and address in iac
        if own_address_found:
            matched.add(address)

        any_cover_hit = False
        for pattern in _covers_patterns(node_data):
            hits = {addr for addr in iac if fnmatchcase(addr, pattern)}
            if hits:
                matched |= hits
                any_cover_hit = True
            else:
                stale.append(StalePattern(node=str(node_name), pattern=pattern))

        # A node is orphaned only when the export lacks its own address and no
        # `covers` pattern of that node reaches any address the export has.
        if isinstance(address, str) and not own_address_found and not any_cover_hit:
            orphaned.add(address)

    return CoverageResult(
        matched=frozenset(matched),
        uncosted=frozenset(iac - matched),
        orphaned=frozenset(orphaned),
        stale_patterns=sorted(stale, key=lambda s: (s.node, s.pattern)),
    )