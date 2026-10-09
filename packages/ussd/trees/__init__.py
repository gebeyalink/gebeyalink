"""The menu trees, joined into the one mapping the engine walks.

Each path module declares its own nodes; this module is where they become one graph and where
the graph is checked against the registry before a call can ever reach it. A node that names a
template the registry does not hold is a programming error, and the only honest place to find
it is here, at build, rather than by rendering a blank line to a trader halfway through a
listing.
"""

from __future__ import annotations

from packages.shared.i18n.templates import load_templates
from packages.ussd.render import UnknownTemplateError
from packages.ussd.trees import browse, listings, main, orders
from packages.ussd.trees.model import Node

_MODULES = (main, listings, browse, orders)


def build_nodes() -> dict[str, Node]:
    """Every node in every tree, refusing duplicates and unknown templates by name."""
    nodes: dict[str, Node] = {}
    for module in _MODULES:
        for node_id, node in module.NODES.items():
            if node_id != node.id:
                raise ValueError(f"node keyed {node_id!r} declares id {node.id!r}")
            if node_id in nodes:
                raise ValueError(f"duplicate node id {node_id!r}")
            nodes[node_id] = node
    _validate(nodes)
    return nodes


def _validate(nodes: dict[str, Node]) -> None:
    registry = load_templates()
    for node in nodes.values():
        for template_id in node.prompt:
            if template_id not in registry:
                raise UnknownTemplateError(template_id)
        if node.error_reason_id is not None and node.error_reason_id not in registry:
            raise UnknownTemplateError(node.error_reason_id)
    for node in nodes.values():
        for template_id in (*node.prompt,):
            template = registry[template_id]
            if template.channel != "ussd":
                raise ValueError(
                    f"node {node.id!r} renders {template_id!r}, which is not a USSD template"
                )
