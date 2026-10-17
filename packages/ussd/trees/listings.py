"""Path 1 of §14.3: a seller's own listings, the offers on one of them, and answering an offer.

The entry to this path is gated on an account, because a listing belongs to a trader and an
unregistered handset has no listings to show. Everything past the gate acts on the seller's
own rows: the offers screen is reached by choosing one of their listings, and the accept,
reject and counter moves are handed to the gateway with the offer the seller picked.
"""

from __future__ import annotations

from packages.ussd.trees.model import (
    Action,
    Node,
    choices,
    digits,
    index_choice,
    positive_amount,
)

NODES: dict[str, Node] = {
    "listings_menu": Node(
        id="listings_menu",
        prompt=("ussd_listings_menu",),
        next=choices(
            {"1": "ussd_new_listing_crop", "2": "active_listings", "3": "paused_listings"}
        ),
        requires_account=True,
        prefetch=("listing_counts",),
    ),
    "ussd_new_listing_crop": Node(
        id="ussd_new_listing_crop",
        prompt=("ussd_crop_menu", "ussd_rows"),
        next=index_choice("ussd_new_listing_quantity", "crop_ids"),
        requires_account=True,
        prefetch=("new_listing_crops",),
        action=Action("select_crop"),
        arrival_action=Action("require_listing_profile"),
    ),
    "ussd_new_listing_quantity": Node(
        id="ussd_new_listing_quantity",
        prompt=("ussd_quantity_prompt",),
        next=digits("ussd_new_listing_price"),
        error_reason_id="error_digits",
        action=Action("set_listing_quantity"),
        allows_back=False,
    ),
    "ussd_new_listing_price": Node(
        id="ussd_new_listing_price",
        prompt=("ussd_price_prompt",),
        next=digits("ussd_new_listing_harvest"),
        error_reason_id="error_digits",
        action=Action("set_listing_price"),
        allows_back=False,
    ),
    "ussd_new_listing_harvest": Node(
        id="ussd_new_listing_harvest",
        prompt=("ussd_new_listing_harvest",),
        next=choices(
            {
                "1": "ussd_new_listing_delivery",
                "2": "ussd_new_listing_delivery",
                "3": "ussd_new_listing_delivery",
                "4": "ussd_new_listing_delivery",
            }
        ),
        error_reason_id="error_invalid_choice",
        action=Action("set_listing_harvest"),
    ),
    "ussd_new_listing_delivery": Node(
        id="ussd_new_listing_delivery",
        prompt=("ussd_new_listing_delivery",),
        next=choices(
            {
                "1": "ussd_new_listing_published",
                "2": "ussd_new_listing_delivery_fee",
                "3": "ussd_new_listing_published",
            }
        ),
        error_reason_id="error_invalid_choice",
        action=Action("set_listing_delivery"),
    ),
    "ussd_new_listing_delivery_fee": Node(
        id="ussd_new_listing_delivery_fee",
        prompt=("ussd_new_listing_delivery_fee",),
        next=digits("ussd_new_listing_published"),
        error_reason_id="error_digits",
        action=Action("set_listing_delivery_fee"),
        allows_back=False,
    ),
    "ussd_new_listing_published": Node(
        id="ussd_new_listing_published",
        prompt=("ussd_new_listing_published",),
        terminal=True,
        arrival_action=Action("publish_listing"),
    ),
    "active_listings": Node(
        id="active_listings",
        prompt=("ussd_active_listings", "ussd_rows"),
        next=index_choice("offers_list", "listing_ids"),
        prefetch=("active_listings",),
        action=Action("select_listing", indexed=True),
    ),
    "paused_listings": Node(
        id="paused_listings",
        prompt=("ussd_paused_listings", "ussd_rows"),
        next=None,
        prefetch=("paused_listings",),
    ),
    "offers_list": Node(
        id="offers_list",
        prompt=("ussd_offers_header", "ussd_rows"),
        next=index_choice("offer_actions", "offer_ids"),
        prefetch=("offers_for_selected",),
        action=Action("select_offer", indexed=True),
    ),
    "offer_actions": Node(
        id="offer_actions",
        prompt=("ussd_offer_actions",),
        next=choices({"1": "accepted", "2": "rejected", "3": "counter_price"}),
    ),
    "accepted": Node(
        id="accepted",
        prompt=("ussd_offer_accepted",),
        terminal=True,
        arrival_action=Action("offer_accept", entity_from="selected_offer_id"),
    ),
    "rejected": Node(
        id="rejected",
        prompt=("ussd_offer_rejected",),
        terminal=True,
        arrival_action=Action("offer_reject", entity_from="selected_offer_id"),
    ),
    "counter_price": Node(
        id="counter_price",
        prompt=("ussd_price_prompt",),
        next=positive_amount("counter_confirm"),
        error_reason_id="error_digits",
    ),
    "counter_confirm": Node(
        id="counter_confirm",
        prompt=("ussd_offer_confirm",),
        next=choices({"1": "counter_done"}),
        arrival_action=Action("set_price"),
    ),
    "counter_done": Node(
        id="counter_done",
        prompt=("ussd_offer_placed",),
        terminal=True,
        arrival_action=Action("offer_counter", entity_from="selected_offer_id"),
    ),
}
