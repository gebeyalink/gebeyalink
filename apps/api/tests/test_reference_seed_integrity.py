"""Seed integrity for the reference tables (SPEC 11.1, 27.1, 27.2).

These read the database directly. The endpoints are covered on their own terms elsewhere;
what matters here is that the rows the migration shipped are the rows everything else was
written against. Reading them here rather than through the API is deliberate: asking the
code under test whether the code under test seeded correctly proves very little.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal

import psycopg
import pytest

pytestmark = pytest.mark.usefixtures("migrated_database")

# The published taxonomy, transcribed. Written out in full rather than checked as a count,
# because a count cannot tell a missing faba bean from a spurious peanut.
TAXONOMY: tuple[tuple[int, str, str, str], ...] = (
    (1, "Teff", "ጤፍ", "CEREALS"),
    (2, "Wheat", "ስንዴ", "CEREALS"),
    (3, "Barley", "ገብስ", "CEREALS"),
    (4, "Maize", "በቆሎ", "CEREALS"),
    (5, "Sorghum", "ማሽላ", "CEREALS"),
    (6, "Chickpea", "ሽምብራ", "PULSES"),
    (7, "Lentil", "ምስር", "PULSES"),
    (8, "Faba bean", "በቀላ", "PULSES"),
    (9, "White pea bean", "አተር", "PULSES"),
    (10, "Niger seed", "ኑግ", "OILSEEDS"),
    (11, "Sesame", "ሰሊጥ", "OILSEEDS"),
    (12, "Linseed", "አሥላስ", "OILSEEDS"),
    (13, "Coffee (red cherry)", "ቡና", "COFFEE"),
    (14, "Avocado", "አቮካዶ", "FRUITS"),
    (15, "Mango", "ማንጎ", "FRUITS"),
    (16, "Banana", "ሙዝ", "FRUITS"),
    (17, "Onion", "ሽንኩርት", "VEGETABLES"),
    (18, "Tomato", "ቲማቲም", "VEGETABLES"),
    (19, "Potato", "ድንች", "VEGETABLES"),
    (20, "Cattle (live)", "ተራራ ወታደር/በሬ", "LIVESTOCK"),
)


@pytest.fixture()
def reference_rows(postgres_url: str) -> Iterator[Callable[[str], list[tuple[object, ...]]]]:
    """Run one read-only statement against the database this test run migrated."""
    sync_url: str = postgres_url.replace("postgresql+psycopg://", "postgresql://")

    def _rows(statement: str) -> list[tuple[object, ...]]:
        with psycopg.connect(sync_url, autocommit=True) as connection:
            cursor = connection.execute(statement)
            assert cursor.description is not None
            return list(cursor.fetchall())

    yield _rows


def test_thirteen_regions_are_seeded(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    codes = [row[0] for row in reference_rows("SELECT code FROM regions ORDER BY code")]
    assert codes == ["AA", "AF", "AM", "BE", "DD", "GA", "HA", "OR", "SI", "SN", "SO", "SW", "TI"]


def test_every_region_is_named_in_the_language_the_region_list_supplies(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    unnamed = reference_rows("SELECT code FROM regions WHERE coalesce(name_en, '') = ''")
    assert unnamed == []


def test_region_names_with_no_sourced_translation_are_left_empty(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    """The region list publishes English only, so the other two columns wait for review."""
    rows = reference_rows("SELECT code, name_am, name_om FROM regions ORDER BY code")
    assert [row for row in rows if row[1] is not None or row[2] is not None] == []


def test_the_seeded_crops_are_the_published_taxonomy(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    rows = reference_rows("SELECT id, name_en, name_am, category::text FROM crops ORDER BY id")
    assert rows == [tuple(row) for row in TAXONOMY]


def test_the_taxonomy_identifiers_are_the_published_numbers_one_through_twenty(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    ids = [row[0] for row in reference_rows("SELECT id FROM crops ORDER BY id")]
    assert ids == list(range(1, 21))


def test_every_crop_is_named_in_the_languages_the_taxonomy_supplies(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    unnamed = reference_rows(
        "SELECT id FROM crops WHERE coalesce(name_en, '') = '' OR coalesce(name_am, '') = ''"
    )
    assert unnamed == []


def test_the_languages_with_no_sourced_names_are_left_empty_rather_than_guessed(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    """A wrong translation in a trilingual product costs more trust than a missing one.

    The taxonomy publishes English and Amharic only, so Oromo is empty for every crop and
    Tigrinya for all but the one the taxonomy itself supplies.
    """
    rows = reference_rows("SELECT id, name_om, name_ti FROM crops ORDER BY id")
    authored = [row for row in rows if row[1] is not None]
    assert authored == []
    assert [row[0] for row in rows if row[2] is not None] == [20]


def test_the_twentieth_crop_is_livestock_measured_in_units_and_inactive(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    """Cattle is counted, not weighed, so it must not appear where weight is expected."""
    rows = reference_rows("SELECT name_en, base_unit::text, active FROM crops WHERE id = 20")
    assert rows == [("Cattle (live)", "UNIT", False)]


def test_cattle_is_the_only_crop_left_inactive(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    rows = reference_rows("SELECT id FROM crops WHERE active IS NOT TRUE")
    assert [row[0] for row in rows] == [20]


def test_every_active_kilogram_crop_can_be_priced_in_quintals(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    """A quintal is a hundred kilograms for every crop weighed in kilograms.

    Written per crop rather than worked out at read time, so the conversion is looked up
    from one table like any other instead of special-cased in a price calculation.
    """
    rows = reference_rows(
        "SELECT crop_id, unit_code::text, kg_equivalent FROM crop_units ORDER BY crop_id"
    )
    assert rows == [(crop_id, "QUINTAL", Decimal("100.00")) for crop_id in range(1, 20)]


def test_sack_and_joniya_are_offered_as_units_but_carry_no_unsourced_weight(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    unsourced = reference_rows(
        "SELECT unit_code::text FROM crop_units WHERE unit_code::text <> 'QUINTAL'"
    )
    assert unsourced == []
    available = reference_rows(
        "SELECT e.enumlabel FROM pg_enum e "
        "JOIN pg_type t ON t.oid = e.enumtypid "
        "WHERE t.typname = 'crop_unit_code' ORDER BY e.enumsortorder"
    )
    assert [row[0] for row in available] == ["QUINTAL", "SACK", "JONIYA"]


def test_the_pilot_woreda_is_seeded_without_a_boundary(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    rows = reference_rows("SELECT name, region_code, zone, geog FROM woredas")
    assert rows == [("Adaa", "OR", "East Shewa", None)]


def test_the_woreda_boundary_column_is_real_geometry_rather_than_text(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    """A point-in-woreda test needs geometry with a spatial reference, not a string."""
    rows = reference_rows(
        "SELECT format_type(a.atttypid, a.atttypmod) "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "WHERE c.relname = 'woredas' AND a.attname = 'geog'"
    )
    assert rows == [("geography(MultiPolygon,4326)",)]


def test_woredas_name_a_region_that_exists(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    orphans = reference_rows("""
        SELECT w.name FROM woredas w
        LEFT JOIN regions r ON r.code = w.region_code
        WHERE r.code IS NULL
        """)
    assert orphans == []


def test_crop_units_name_a_crop_that_exists(
    reference_rows: Callable[[str], list[tuple[object, ...]]],
) -> None:
    orphans = reference_rows("""
        SELECT u.crop_id FROM crop_units u
        LEFT JOIN crops c ON c.id = u.crop_id
        WHERE c.id IS NULL
        """)
    assert orphans == []
