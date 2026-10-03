"""reference data: regions, crops, crop units, woredas

The rows here are reference data rather than transactional records: nobody edits them
through the API, and everything above them is written against them. They ship in the
migration so that a fresh database is immediately usable and so that every environment
starts from the same list.

Column shapes follow the core-entity definitions in the specification. Where those define
a column as an enum, the database gets a real enum type rather than free text, so that a
crop category or a local unit outside the agreed set cannot be written at all.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from apps.api.db.geo import GeographyMultiPolygon

revision: str = "0002"
down_revision: str = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Woreda boundaries are stored as PostGIS geometry, so the extension has to exist
    # before the column that uses its type. The extension belongs to the database rather
    # than to this migration, so it is left in place on downgrade for the next thing that
    # needs it.
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    regions = op.create_table(
        "regions",
        sa.Column("code", sa.String(length=2), nullable=False),
        # Only the English region names are published. Amharic and Oromo wait for a
        # reviewed list for the same reason the crop names do: a wrong translation is worse
        # than an absent one.
        sa.Column("name_en", sa.String(length=64), nullable=False),
        sa.Column("name_am", sa.String(length=64), nullable=True),
        sa.Column("name_om", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_regions")),
    )
    op.bulk_insert(
        regions,
        [
            {
                "code": "AA",
                "name_en": "Addis Ababa",
                "name_am": None,
                "name_om": None,
            },
            {"code": "AF", "name_en": "Afar", "name_am": None, "name_om": None},
            {
                "code": "AM",
                "name_en": "Amhara",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "BE",
                "name_en": "Benishangul-Gumuz",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "DD",
                "name_en": "Dire Dawa",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "GA",
                "name_en": "Gambela",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "HA",
                "name_en": "Harari",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "OR",
                "name_en": "Oromia",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "SI",
                "name_en": "Sidama",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "SN",
                "name_en": "South Ethiopia",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "SO",
                "name_en": "Somali",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "SW",
                "name_en": "South West Ethiopia",
                "name_am": None,
                "name_om": None,
            },
            {
                "code": "TI",
                "name_en": "Tigray",
                "name_am": None,
                "name_om": None,
            },
        ],
    )

    crops = op.create_table(
        "crops",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("name_en", sa.String(length=64), nullable=False),
        sa.Column("name_am", sa.String(length=64), nullable=False),
        # Oromo and Tigrinya are carried as columns but seeded empty. The published taxonomy
        # supplies English and Amharic only, and a crop name shown in the wrong language is
        # worse than one not shown at all, so both wait for a reviewed list rather than a
        # plausible guess. The one exception is Cattle, whose Tigrinya the taxonomy gives.
        # Tigrinya is also a future pilot language the crop endpoint does not expose yet.
        sa.Column("name_om", sa.String(length=64), nullable=True),
        sa.Column("name_ti", sa.String(length=64), nullable=True),
        sa.Column(
            "category",
            sa.Enum(
                "CEREALS",
                "PULSES",
                "OILSEEDS",
                "FRUITS",
                "VEGETABLES",
                "COFFEE",
                "LIVESTOCK",
                "OTHER",
                name="crop_category",
            ),
            nullable=False,
        ),
        sa.Column(
            "base_unit",
            sa.Enum("KG", "UNIT", name="crop_base_unit"),
            server_default="KG",
            nullable=False,
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        # How long an escrow on this crop is held before it can be released on its own.
        # Set per crop, because grain and leafy produce do not keep for the same time.
        sa.Column("escrow_hold_hours", sa.Integer(), server_default="72", nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crops")),
    )
    op.bulk_insert(
        crops,
        [
            {
                "id": 1,
                "name_en": "Teff",
                "name_am": "ጤፍ",
                "name_om": None,
                "name_ti": None,
                "category": "CEREALS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 2,
                "name_en": "Wheat",
                "name_am": "ስንዴ",
                "name_om": None,
                "name_ti": None,
                "category": "CEREALS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 3,
                "name_en": "Barley",
                "name_am": "ገብስ",
                "name_om": None,
                "name_ti": None,
                "category": "CEREALS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 4,
                "name_en": "Maize",
                "name_am": "በቆሎ",
                "name_om": None,
                "name_ti": None,
                "category": "CEREALS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 5,
                "name_en": "Sorghum",
                "name_am": "ማሽላ",
                "name_om": None,
                "name_ti": None,
                "category": "CEREALS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 6,
                "name_en": "Chickpea",
                "name_am": "ሽምብራ",
                "name_om": None,
                "name_ti": None,
                "category": "PULSES",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 7,
                "name_en": "Lentil",
                "name_am": "ምስር",
                "name_om": None,
                "name_ti": None,
                "category": "PULSES",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 8,
                "name_en": "Faba bean",
                "name_am": "በቀላ",
                "name_om": None,
                "name_ti": None,
                "category": "PULSES",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 9,
                "name_en": "White pea bean",
                "name_am": "አተር",
                "name_om": None,
                "name_ti": None,
                "category": "PULSES",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 10,
                "name_en": "Niger seed",
                "name_am": "ኑግ",
                "name_om": None,
                "name_ti": None,
                "category": "OILSEEDS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 11,
                "name_en": "Sesame",
                "name_am": "ሰሊጥ",
                "name_om": None,
                "name_ti": None,
                "category": "OILSEEDS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 12,
                "name_en": "Linseed",
                "name_am": "አሥላስ",
                "name_om": None,
                "name_ti": None,
                "category": "OILSEEDS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 13,
                "name_en": "Coffee (red cherry)",
                "name_am": "ቡና",
                "name_om": None,
                "name_ti": None,
                "category": "COFFEE",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 14,
                "name_en": "Avocado",
                "name_am": "አቮካዶ",
                "name_om": None,
                "name_ti": None,
                "category": "FRUITS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 15,
                "name_en": "Mango",
                "name_am": "ማንጎ",
                "name_om": None,
                "name_ti": None,
                "category": "FRUITS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 16,
                "name_en": "Banana",
                "name_am": "ሙዝ",
                "name_om": None,
                "name_ti": None,
                "category": "FRUITS",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 17,
                "name_en": "Onion",
                "name_am": "ሽንኩርት",
                "name_om": None,
                "name_ti": None,
                "category": "VEGETABLES",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 18,
                "name_en": "Tomato",
                "name_am": "ቲማቲም",
                "name_om": None,
                "name_ti": None,
                "category": "VEGETABLES",
                "base_unit": "KG",
                "active": True,
            },
            {
                "id": 19,
                "name_en": "Potato",
                "name_am": "ድንች",
                "name_om": None,
                "name_ti": None,
                "category": "VEGETABLES",
                "base_unit": "KG",
                "active": True,
            },
            {
                # Counted, not weighed. Held back from the weight-based lists until it has a
                # unit of its own to be priced in.
                "id": 20,
                "name_en": "Cattle (live)",
                "name_am": "ተራራ ወታደር/በሬ",
                "name_om": None,
                "name_ti": "ተራራ ወታደር/በሬ",
                "category": "LIVESTOCK",
                "base_unit": "UNIT",
                "active": False,
            },
        ],
    )

    crop_units = op.create_table(
        "crop_units",
        sa.Column("crop_id", sa.SmallInteger(), nullable=False),
        sa.Column(
            "unit_code",
            sa.Enum("QUINTAL", "SACK", "JONIYA", name="crop_unit_code"),
            nullable=False,
        ),
        sa.Column("kg_equivalent", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.ForeignKeyConstraint(
            ["crop_id"],
            ["crops.id"],
            name=op.f("fk_crop_units_crop_id_crops"),
        ),
        sa.PrimaryKeyConstraint("crop_id", "unit_code", name=op.f("pk_crop_units")),
    )
    # A quintal is a hundred kilograms by definition, so this conversion is arithmetic
    # rather than measurement, and it is written for every crop weighed in kilograms rather
    # than derived at read time, so that a trader pricing in quintals finds it. Sack and
    # joniya weights vary by crop and region; the unit codes exist from the start, but the
    # numbers are absent rather than guessed until they are sourced.
    op.bulk_insert(
        crop_units,
        [
            {"crop_id": crop_id, "unit_code": "QUINTAL", "kg_equivalent": 100}
            for crop_id in range(1, 20)
        ],
    )

    woredas = op.create_table(
        "woredas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("region_code", sa.String(length=2), nullable=False),
        sa.Column("zone", sa.String(length=120), nullable=False),
        sa.Column("geog", GeographyMultiPolygon(), nullable=True),
        sa.ForeignKeyConstraint(
            ["region_code"],
            ["regions.code"],
            name=op.f("fk_woredas_region_code_regions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_woredas")),
    )
    # The pilot woreda only. Its boundary is deliberately empty: a boundary dataset is
    # something to be obtained, not invented, and nothing needs it until it does.
    op.bulk_insert(
        woredas,
        [
            {
                "id": 1,
                "name": "Adaa",
                "region_code": "OR",
                "zone": "East Shewa",
                "geog": None,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("woredas")
    op.drop_table("crop_units")
    op.drop_table("crops")
    op.drop_table("regions")
    sa.Enum(name="crop_unit_code").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="crop_base_unit").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="crop_category").drop(op.get_bind(), checkfirst=True)
