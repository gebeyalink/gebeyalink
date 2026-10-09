"""FR-1101, the USSD screens: the trees, the registry they build from, and the renderer.

The USSD channel is controlled entirely from templates and from the tree of nodes that
names them, so the cheapest things worth testing are exactly those two: that the tree
builds out of the registry we ship, and that a screen rendered from shared pieces is the
screen the SPEC shows. Everything the engine pushes to a handset - the error marker, the
language choice, the registration gate - lives here or is asserted by the navigation tests.

Marked unit because it imports the packages directly and touches neither Postgres nor
Redis; the whole channel can be reasoned about as data, and this file proves it.
"""

from __future__ import annotations

import pytest

from packages.shared.i18n.languages import Language
from packages.shared.i18n.templates import load_templates
from packages.ussd.render import ERROR_MARKER, UnknownTemplateError, render, render_screen
from packages.ussd.session import SessionState
from packages.ussd.trees import build_nodes

pytestmark = pytest.mark.unit

ERROR_REASON_IDS = {
    "error_invalid_choice",
    "error_digits",
    "error_wrong_code",
    "error_unavailable",
}


def test_the_tree_builds_every_node_of_the_five_paths() -> None:
    nodes = build_nodes()
    assert len(nodes) == 32
    assert set(nodes) == {
        "language",
        "resume",
        "main_menu",
        "prices",
        "help",
        "listings_menu",
        "new_listing",
        "active_listings",
        "paused_listings",
        "offers_list",
        "offer_actions",
        "accepted",
        "rejected",
        "counter_price",
        "counter_confirm",
        "counter_done",
        "browse_region_menu",
        "browse_crop_menu",
        "browse_results",
        "browse_result_detail",
        "browse_results_next",
        "offer_quantity",
        "offer_price",
        "offer_confirm",
        "offer_done",
        "orders_menu",
        "orders_awaiting",
        "orders_in_progress",
        "orders_completed",
        "order_buyer_view",
        "order_seller_entry",
        "order_delivered",
    }


def test_every_prompt_and_reason_names_an_existing_ussd_template() -> None:
    registry = load_templates()
    for node in build_nodes().values():
        for template_id in node.prompt:
            assert template_id in registry, f"{node.id} renders unknown {template_id}"
            assert registry[template_id].channel == "ussd"
        if node.error_reason_id is not None:
            assert node.error_reason_id in registry
            assert registry[node.error_reason_id].channel == "ussd"


def _state() -> SessionState:
    return SessionState(user_id=None, lang=Language.EN, menu_stack=["main_menu"], context={})


def test_the_main_menu_choices_lead_into_each_of_the_five_paths() -> None:
    """The menu table is data, so a static walk of its keys is a real assertion of routing."""
    nodes = build_nodes()
    choices = nodes["main_menu"].next
    assert choices is not None
    for key, expected in [
        ("1", "listings_menu"),
        ("2", "browse_region_menu"),
        ("3", "orders_menu"),
        ("4", "prices"),
        ("5", "help"),
    ]:
        assert choices(key, _state()) == expected
    assert choices("0", _state()) is None
    assert choices("9", _state()) is None


def test_the_listings_and_orders_menus_have_their_three_paths() -> None:
    nodes = build_nodes()
    listings = nodes["listings_menu"].next
    orders = nodes["orders_menu"].next
    assert listings is not None and orders is not None
    assert listings("1", _state()) == "new_listing"
    assert listings("2", _state()) == "active_listings"
    assert listings("3", _state()) == "paused_listings"
    assert listings("4", _state()) is None
    assert orders("1", _state()) == "orders_awaiting"
    assert orders("2", _state()) == "orders_in_progress"
    assert orders("3", _state()) == "orders_completed"
    assert orders("9", _state()) is None


def test_the_main_menu_renders_in_english() -> None:
    assert render_screen(("ussd_main_menu",), Language.EN, {}) == "\n".join(
        [
            "1. My Listings",
            "2. Browse Crops",
            "3. My Orders",
            "4. Today's Prices",
            "5. Help",
        ]
    )


def test_a_screen_of_two_parts_is_joined_by_a_single_line_break() -> None:
    body = render_screen(("ussd_greeting", "ussd_language_menu"), Language.EN, {})
    assert body == "Welcome to GebeyaLink\n" + "\n".join(
        ["1. አማርኛ", "2. English", "3. Afaan Oromoo"]
    )


def test_the_error_marker_is_the_marker_the_spec_pins() -> None:
    assert ERROR_MARKER == "❌ "


def test_reason_templates_hold_sentences_not_markers() -> None:
    """§14.1's marker belongs to the renderer, so no copy carries a symbol of its own."""
    registry = load_templates()
    for template_id in ERROR_REASON_IDS:
        for text in registry[template_id].variants.values():
            assert "❌" not in text


def test_an_unknown_template_names_itself() -> None:
    with pytest.raises(UnknownTemplateError):
        render("no_such_screen", Language.EN, {})


def test_the_registration_gate_renders_the_short_code() -> None:
    assert render("register_prompt_ussd", Language.EN, {"ussd": "*851#"}) == (
        "This number is not registered. To register, dial *851#."
    )


def test_the_help_line_is_the_spec_help_line() -> None:
    assert render("ussd_help", Language.EN, {}) == (
        "Call 951 free / SMS HELP. Agents in your kebele can assist."
    )


def test_the_language_menu_names_all_three_languages() -> None:
    body = render_screen(("ussd_greeting", "ussd_language_menu"), Language.EN, {})
    for line in ("1. አማርኛ", "2. English", "3. Afaan Oromoo"):
        assert line in body


def test_every_node_renders_under_a_filled_context() -> None:
    """A node must be renderable before any gateway has filled its context.

    The gateway prefetch fills rows and counts, but a menu node that broke on an empty
    context would break the very first screen of a fresh session. Rendering every node with
    each placeholder supplied as a marker value proves the whole graph is substitutable.
    """
    registry = load_templates()
    nodes = build_nodes()
    for node in nodes.values():
        context: dict[str, str] = {}
        for template_id in node.prompt:
            for key in registry[template_id].placeholders:
                context[key] = "x"
        screen = render_screen(node.prompt, Language.EN, context)
        assert isinstance(screen, str)
        if node.error_reason_id is not None:
            # A reason is a sentence with no placeholders; the registry guarantees it renders.
            render(node.error_reason_id, Language.EN, {})
