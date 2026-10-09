"""Path 2 of §14.3: browse the market by region and crop, and make an offer on a result.

Most of this path is open to anyone: a trader can pick a region, pick a crop and read the
results without an account. The gate is one screen further in — the "Make offer" step — because
a listing is worth showing a stranger and a bid is not. The offer the path builds is placed on
the chosen listing under the same rules the app applies.
"""

from __future__ import annotations

from packages.ussd.session import SessionState
from packages.ussd.trees.model import (
    Action,
    Node,
    choices,
    index_choice,
    positive_amount,
)


def _after_detail(text: str, session: SessionState) -> str | None:
    value = text.strip()
    if value == "1":
        return "offer_quantity"
    if value == "2" and session.context.get("has_more"):
        return "browse_results_next"
    return None


NODES: dict[str, Node] = {
    "browse_region_menu": Node(
        id="browse_region_menu",
        prompt=("ussd_region_menu", "ussd_rows"),
        next=index_choice("browse_crop_menu", "region_codes"),
        prefetch=("browse_regions",),
        action=Action("select_region"),
    ),
    "browse_crop_menu": Node(
        id="browse_crop_menu",
        prompt=("ussd_crop_menu", "ussd_rows"),
        next=index_choice("browse_results", "crop_ids"),
        prefetch=("browse_crops",),
        action=Action("select_crop"),
    ),
    "browse_results": Node(
        id="browse_results",
        prompt=("ussd_rows",),
        next=index_choice("browse_result_detail", "listing_ids"),
        prefetch=("browse_results",),
        action=Action("select_listing", indexed=True),
    ),
    "browse_result_detail": Node(
        id="browse_result_detail",
        prompt=("ussd_result_detail", "ussd_result_options"),
        next=_after_detail,
        prefetch=("browse_result_detail",),
    ),
    "browse_results_next": Node(
        id="browse_results_next",
        prompt=("ussd_rows",),
        next=index_choice("browse_result_detail", "listing_ids"),
        prefetch=("browse_results",),
        arrival_action=Action("page_next"),
    ),
    "offer_quantity": Node(
        id="offer_quantity",
        prompt=("ussd_quantity_prompt",),
        next=positive_amount("offer_price"),
        requires_account=True,
        error_reason_id="error_digits",
    ),
    "offer_price": Node(
        id="offer_price",
        prompt=("ussd_price_prompt",),
        next=positive_amount("offer_confirm"),
        arrival_action=Action("set_quantity"),
        error_reason_id="error_digits",
    ),
    "offer_confirm": Node(
        id="offer_confirm",
        prompt=("ussd_offer_confirm",),
        next=choices({"1": "offer_done"}),
        arrival_action=Action("set_price"),
    ),
    "offer_done": Node(
        id="offer_done",
        prompt=("ussd_offer_placed",),
        terminal=True,
        arrival_action=Action("offer_place", entity_from="selected_listing_id"),
    ),
}
