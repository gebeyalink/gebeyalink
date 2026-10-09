"""Path 3 of §14.3: a trader's orders, grouped by what is happening to them.

An order looks different to the two people on it, and this path never hides that. A buyer
watching a funded order sees the pickup code they will present; a seller looking at the same
order is asked to enter the code the buyer shows. The screen a row opens therefore depends on
which side of that order the dialling trader is on, which the gateway records as a role when
the row is chosen.
"""

from __future__ import annotations

from packages.ussd.session import SessionState
from packages.ussd.trees.model import Action, Node, choices


def _after_order(text: str, session: SessionState) -> str | None:
    value = text.strip()
    if not value.isdigit():
        return None
    roles = session.context.get("order_roles")
    if not isinstance(roles, list):
        return None
    position = int(value)
    if not 1 <= position <= len(roles):
        return None
    return "order_buyer_view" if roles[position - 1] == "buyer" else "order_seller_entry"


def _after_pickup(text: str, session: SessionState) -> str | None:
    return "order_delivered" if text.strip().isdigit() else None


NODES: dict[str, Node] = {
    "orders_menu": Node(
        id="orders_menu",
        prompt=("ussd_orders_menu",),
        next=choices(
            {
                "1": "orders_awaiting",
                "2": "orders_in_progress",
                "3": "orders_completed",
            }
        ),
        requires_account=True,
    ),
    "orders_awaiting": Node(
        id="orders_awaiting",
        prompt=("ussd_orders_awaiting", "ussd_rows"),
        next=None,
        prefetch=("orders_awaiting",),
    ),
    "orders_in_progress": Node(
        id="orders_in_progress",
        prompt=("ussd_orders_in_progress", "ussd_rows"),
        next=_after_order,
        prefetch=("orders_in_progress",),
        action=Action("select_order", indexed=True),
    ),
    "orders_completed": Node(
        id="orders_completed",
        prompt=("ussd_orders_completed", "ussd_rows"),
        next=None,
        prefetch=("orders_completed",),
    ),
    "order_buyer_view": Node(
        id="order_buyer_view",
        prompt=("ussd_order_buyer_view",),
        next=None,
        prefetch=("order_buyer_detail",),
    ),
    "order_seller_entry": Node(
        id="order_seller_entry",
        prompt=("ussd_enter_pickup_code",),
        next=_after_pickup,
        error_reason_id="error_digits",
    ),
    "order_delivered": Node(
        id="order_delivered",
        prompt=("ussd_delivered",),
        terminal=True,
        arrival_action=Action("delivery_confirm", entity_from="selected_order_id"),
    ),
}
