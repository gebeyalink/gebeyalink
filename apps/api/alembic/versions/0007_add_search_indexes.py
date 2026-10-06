"""The access paths a search takes: listings by state, and woredas by boundary.

FR-401's filters and FR-402's orders are all predicates over `listings.status` first,
because a buyer may only be shown two of its seven values (FR-404 keeps `DRAFT`, `PAUSED`,
`EXPIRED`, `CLOSED` and `SUSPENDED` out, and expiry is read rather than written, so the
query carries the clock as well as the column). Every index below therefore starts with
`status`: an index that did not would serve a query the API never makes.

Each one ends in the columns its order needs. A btree can only be used for an ordering it
actually provides, so `created_at` or `unit_price_santim` without `id` behind it would
leave Postgres sorting every matching row to break the ties that two listings posted in
the same instant will always produce - and ties are the ordinary case here, not a corner.

    ix_listings_status_created_at_id             the unfiltered market, newest first
    ix_listings_status_crop_id_region_code...    FR-401's crop and region, newest first
    ix_listings_status_woreda_id_created_at_id   one woreda, newest first
    ix_listings_status_unit_price_santim_id      price_asc / price_desc
    ix_listings_status_harvest_date_id           harvest_date_asc
    ix_woredas_geog                              the radius filter, by geography

The geography index is a GiST over `woredas.geog` even though every boundary the platform
holds is still NULL: the column exists, PostGIS gives it an operator class, and creating
the index now means the day the boundaries arrive the radius filter has somewhere to land
rather than a migration of its own to wait for.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_listings_status_created_at_id"),
        "listings",
        ["status", "created_at", "id"],
    )
    op.create_index(
        op.f("ix_listings_status_crop_id_region_code_created_at_id"),
        "listings",
        ["status", "crop_id", "region_code", "created_at", "id"],
    )
    op.create_index(
        op.f("ix_listings_status_woreda_id_created_at_id"),
        "listings",
        ["status", "woreda_id", "created_at", "id"],
    )
    op.create_index(
        op.f("ix_listings_status_unit_price_santim_id"),
        "listings",
        ["status", "unit_price_santim", "id"],
    )
    op.create_index(
        op.f("ix_listings_status_harvest_date_id"),
        "listings",
        ["status", "harvest_date", "id"],
    )
    op.create_index(
        op.f("ix_woredas_geog"),
        "woredas",
        ["geog"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_woredas_geog"), table_name="woredas")
    op.drop_index(op.f("ix_listings_status_harvest_date_id"), table_name="listings")
    op.drop_index(op.f("ix_listings_status_unit_price_santim_id"), table_name="listings")
    op.drop_index(op.f("ix_listings_status_woreda_id_created_at_id"), table_name="listings")
    op.drop_index(
        op.f("ix_listings_status_crop_id_region_code_created_at_id"), table_name="listings"
    )
    op.drop_index(op.f("ix_listings_status_created_at_id"), table_name="listings")
