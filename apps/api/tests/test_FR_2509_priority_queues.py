"""FR-2509 — every message has a delivery lane, and the lane is data, not code.

The registry YAML is the classification: each template declares the priority it sends
in, priority_for() reads that declaration back, and nothing in Python decides Q1/Q2/Q3
a second time. The product rules are pinned here — auth goes first, order and offer news
second, digests last, with nothing left over — and a template that sidesteps the registry
is refused with its name in the failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from packages.shared.i18n.templates import load_templates
from packages.shared.messaging.queues import SmsPriority, priority_for

pytestmark = pytest.mark.unit

_NO_PRIORITY_YAML = """\
- id: offer_expired_reminder
  channel: sms
  review_status: unreviewed
  placeholders: [order_code]
  variants:
    am: "ትዕዛዝ አልቋል"
    om: "Yeroon kee darbee jira"
    en: "Your offer has expired"
"""

_DUPLICATE_ID_YAML = """\
- id: auth_otp
  channel: sms
  priority: Q1
  review_status: unreviewed
  placeholders: [code]
  variants:
    am: "ኮድዎ {code}"
    om: "Koodii kee {code}"
    en: "Your code {code}"
- id: auth_otp
  channel: sms
  priority: Q1
  review_status: unreviewed
  placeholders: [code]
  variants:
    am: "ኮድዎ {code}"
    om: "Koodii kee {code}"
    en: "Your code {code}"
"""

_UNKNOWN_PRIORITY_YAML = """\
- id: offer_expired_reminder
  channel: sms
  priority: Q9
  review_status: unreviewed
  placeholders: [order_code]
  variants:
    am: "ትዕዛዝ አልቋል"
    om: "Yeroon kee darbee jira"
    en: "Your offer has expired"
"""

_ANOTHER_CHANNEL_YAML = """\
- id: welcome_digest
  channel: push
  priority: Q3
  review_status: unreviewed
  placeholders: [crop]
  variants:
    am: "እንኳን ደህና መጡ {crop}"
    om: "Baga nagaan dhufte {crop}"
    en: "Welcome {crop}"
"""

_USSD_CHANNEL_YAML = """\
- id: ussd_menu
  channel: ussd
  priority: Q2
  review_status: unreviewed
  placeholders: [ussd]
  variants:
    am: "ዝርዝር {ussd}"
    om: "Tarree {ussd}"
    en: "Menu {ussd}"
"""

_MISSING_AM_YAML = """\
- id: order_funded
  channel: sms
  priority: Q2
  review_status: unreviewed
  placeholders: [order_code]
  variants:
    om: "Ajajni kee {order_code} gabaabbatee jira"
    en: "Your order {order_code} has been funded"
"""

_UNKNOWN_VARIANT_LANGUAGE_YAML = """\
- id: order_funded
  channel: sms
  priority: Q2
  review_status: unreviewed
  placeholders: [order_code]
  variants:
    am: "ትዕዛዝዎ {order_code} ተረጋግጧል"
    om: "Ajajni kee {order_code} gabaabbatee jira"
    en: "Your order {order_code} has been funded"
    xx: "Not a language we speak"
"""

_NOT_A_MAPPING_ENTRY_YAML = """\
- just a template name
"""

_INVALID_YAML = """\
- id: auth_otp
  channel: sms
  priority: Q1
  placeholders: [code]
  variants: [am, om, en
"""

_NOT_A_LIST_YAML = "just a string, not a list\n"

_ID_NOT_A_STRING_YAML = """\
- id: 12345
  channel: sms
  priority: Q1
  review_status: unreviewed
  placeholders: [code]
  variants:
    am: "ኮድዎ {code}"
    om: "Koodii kee {code}"
    en: "Your code {code}"
"""

_PRIORITY_NOT_A_STRING_YAML = """\
- id: auth_otp
  channel: sms
  priority: [Q1]
  review_status: unreviewed
  placeholders: [code]
  variants:
    am: "ኮድዎ {code}"
    om: "Koodii kee {code}"
    en: "Your code {code}"
"""

_PLACEHOLDERS_NOT_A_LIST_YAML = """\
- id: auth_otp
  channel: sms
  priority: Q1
  review_status: unreviewed
  placeholders: code
  variants:
    am: "ኮድዎ {code}"
    om: "Koodii kee {code}"
    en: "Your code {code}"
"""

_VARIANTS_NOT_A_MAPPING_YAML = """\
- id: auth_otp
  channel: sms
  priority: Q1
  review_status: unreviewed
  placeholders: [code]
  variants: [am, om, en]
"""

_VARIANT_TEXT_NOT_A_STRING_YAML = """\
- id: auth_otp
  channel: sms
  priority: Q1
  review_status: unreviewed
  placeholders: [code]
  variants:
    am: [x]
    om: "Koodii kee {code}"
    en: "Your code {code}"
"""


def _write_registry(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_every_template_in_the_registry_declares_a_priority() -> None:
    """The classification is exhaustive: nothing in the registry is unclassified."""
    registry = load_templates()
    assert registry, "the template registry must not be empty"
    for template_id, template in registry.items():
        assert isinstance(
            template.priority, SmsPriority
        ), f"{template_id} has no usable priority: {template.priority!r}"


def test_priority_for_returns_the_priority_the_yaml_declares() -> None:
    """The classification is data-driven: it echoes the declared lane back verbatim."""
    for template_id, template in load_templates().items():
        assert priority_for(template_id) is template.priority


def test_the_classified_lanes_match_the_product_rules() -> None:
    """Q1 auth and action tokens, Q2 order-and-offer, Q3 digests — every id in one set.

    Spelled out rather than inferred from a prefix: a reply whose id happens to start with
    `offer_` is not order-and-offer traffic by that accident, and a new id added without a
    lane decision must fail here rather than fall through a pattern that happens to match.
    """
    registry = load_templates()
    q1_ids = frozenset({"auth_otp", "action_token_invalid"})
    q3_ids = frozenset({"price_digest_daily", "saved_search_digest"})
    q2_ids = frozenset(
        {
            # USSD menu strings are declared Q2 even though no USSD reply is ever queued: the
            # registry requires every message to name a lane, so this classification is inert
            # rather than meaningful here — the lane exists for the registry, not for the path.
            "help",
            "register_prompt",
            "price",
            "price_unavailable",
            "post_success",
            "post_failure",
            "offer_placed",
            "cancel_confirmed",
            "status",
            "payout_history_empty",
            "profile_incomplete_for_order",
            "order_funded",
            "order_ready_for_pickup",
            "order_shipped",
            "order_delivered",
            "offer_received",
            "offer_accepted",
            "offer_rejected",
            "ussd_greeting",
            "ussd_language_menu",
            "ussd_resume_prompt",
            "ussd_main_menu",
            "ussd_listings_menu",
            "ussd_new_listing_placeholder",
            "ussd_active_listings",
            "ussd_paused_listings",
            "ussd_no_listings",
            "ussd_rows",
            "ussd_listing_row",
            "ussd_offers_header",
            "ussd_offer_line",
            "ussd_offer_actions",
            "ussd_offer_accepted",
            "ussd_offer_rejected",
            "ussd_crop_menu",
            "ussd_region_menu",
            "ussd_result_detail",
            "ussd_result_options",
            "ussd_quantity_prompt",
            "ussd_price_prompt",
            "ussd_offer_confirm",
            "ussd_offer_placed",
            "ussd_orders_menu",
            "ussd_orders_awaiting",
            "ussd_orders_in_progress",
            "ussd_orders_completed",
            "ussd_no_orders",
            "ussd_order_row",
            "ussd_order_buyer_view",
            "ussd_enter_pickup_code",
            "ussd_delivered",
            "ussd_price_board",
            "ussd_price_row",
            "ussd_price_unavailable",
            "ussd_help",
            "register_prompt_ussd",
            "error_invalid_choice",
            "error_digits",
            "error_wrong_code",
            "error_unavailable",
            "error_profile_incomplete",
            "ussd_no_results",
        }
    )
    expected = dict.fromkeys(q1_ids, SmsPriority.Q1)
    expected |= dict.fromkeys(q3_ids, SmsPriority.Q3)
    expected |= dict.fromkeys(q2_ids, SmsPriority.Q2)
    assert set(registry) == set(expected), sorted(set(registry) ^ set(expected))
    for template_id, template in registry.items():
        assert template.priority is expected[template_id], (template_id, template.priority)


def test_a_template_with_no_priority_is_refused_and_named(tmp_path: Path) -> None:
    """A template that never declares a lane fails, with its name in the error."""
    _write_registry(tmp_path, _NO_PRIORITY_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    message = str(failure.value)
    assert "offer_expired_reminder" in message
    assert "priority" in message


def test_an_unknown_priority_value_is_refused_and_named(tmp_path: Path) -> None:
    _write_registry(tmp_path, _UNKNOWN_PRIORITY_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    message = str(failure.value)
    assert "offer_expired_reminder" in message
    assert "Q9" in message


def test_a_duplicate_template_id_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _DUPLICATE_ID_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "duplicate template id 'auth_otp'" in str(failure.value)


def test_a_channel_this_registry_does_not_carry_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _ANOTHER_CHANNEL_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "push" in str(failure.value)


def test_a_ussd_template_loads(tmp_path: Path) -> None:
    """USSD shares this registry and its budgets: a menu prompt is a message by the same rules."""
    _write_registry(tmp_path, _USSD_CHANNEL_YAML)
    registry = load_templates(tmp_path)
    assert registry["ussd_menu"].channel == "ussd"


def test_a_template_missing_a_required_variant_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _MISSING_AM_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "am" in str(failure.value)


def test_a_variant_in_an_unknown_language_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _UNKNOWN_VARIANT_LANGUAGE_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "xx" in str(failure.value)


def test_a_non_mapping_entry_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _NOT_A_MAPPING_ENTRY_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "must be a mapping" in str(failure.value)


def test_a_yaml_file_that_cannot_be_parsed_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _INVALID_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "could not be read as YAML" in str(failure.value)


def test_a_yaml_file_that_is_not_a_list_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _NOT_A_LIST_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "expected a list of templates" in str(failure.value)


def test_an_id_that_is_not_a_string_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _ID_NOT_A_STRING_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "id must be a non-empty string" in str(failure.value)


def test_a_priority_that_is_not_a_string_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _PRIORITY_NOT_A_STRING_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "priority must be a string" in str(failure.value)


def test_placeholders_that_are_not_a_list_are_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _PLACEHOLDERS_NOT_A_LIST_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "placeholders must be a non-empty list" in str(failure.value)


def test_variants_that_are_not_a_mapping_are_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _VARIANTS_NOT_A_MAPPING_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "variants must map a language to its text" in str(failure.value)


def test_a_variant_whose_text_is_not_a_string_is_refused(tmp_path: Path) -> None:
    _write_registry(tmp_path, _VARIANT_TEXT_NOT_A_STRING_YAML)
    with pytest.raises(ValueError) as failure:
        load_templates(tmp_path)
    assert "must all be non-empty strings" in str(failure.value)


def test_an_unknown_template_id_is_refused_and_named() -> None:
    with pytest.raises(KeyError) as failure:
        priority_for("no_such_template")
    assert "no_such_template" in str(failure.value)
