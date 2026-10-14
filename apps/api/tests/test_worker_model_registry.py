"""The worker process's model registry: every table its tasks can touch is loaded.

A worker task that reads or writes an order reaches every table that order's foreign keys
name. SQLAlchemy resolves those keys across the whole ``MetaData`` when it flushes, so a
process that has loaded only the models its own imports happened to reach fails at the first
write with ``NoReferencedTableError`` — which is exactly what the worker process did before it
loaded the full model set. The shipment sweep settled nothing, forever, while the heartbeat
worked because it only touches the store.

The check has to run in a fresh interpreter. This suite's ``conftest`` imports
``apps.api.main``, which loads every router, service, and model, so the metadata of the
interpreter running the tests already holds every table: a check made in-process would pass
whether or not the worker registers anything. That is why the defect survived the in-process
sweep tests, and it is why the worker's own registry is only visible from a process that
imports the worker and nothing else.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# SPEC A20: importing the worker only, in a fresh process, is fast and needs no I/O.
pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Every table the models define, and therefore every table a worker task's foreign keys can
#: name. The worker's own task today reads and writes ``orders``, the ``listings`` they were
#: accepted from, the ``crops`` whose holding window decides a shipment, and the
#: ``quantity_reservations`` a settlement consumes; the keys on those rows reach ``users``,
#: ``regions``, and ``woredas`` as well. The set is stated in full rather than trimmed to the
#: sweep's own columns because the registry is the worker's, not one task's: the same defect
#: would meet any future task that touches a table this process had not yet loaded.
MODEL_TABLES = frozenset(
    {
        "action_tokens",
        "crop_units",
        "crops",
        "listing_photos",
        "listings",
        "offers",
        "orders",
        "price_indices",
        "profiles",
        "quantity_reservations",
        "regions",
        "users",
        "woredas",
    }
)

#: Imports the worker and prints the tables it registered, without importing the application
#: or the test harness. ``TABLES=`` prefixes the line so an incidental write to stdout during
#: import cannot be mistaken for the registry.
_PROBE = (
    "import apps.api.worker;"
    "from apps.api.db.base import Base;"
    "print('TABLES=' + ','.join(sorted(Base.metadata.tables)))"
)


def _tables_registered_by_the_worker_alone() -> frozenset[str]:
    """Run one fresh interpreter that imports the worker and report its registered tables."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        "importing apps.api.worker failed in a fresh process:\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    line = next(
        (line for line in result.stdout.splitlines() if line.startswith("TABLES=")),
        None,
    )
    assert line is not None, f"the probe printed no registry:\n{result.stdout}"
    return frozenset(line.removeprefix("TABLES=").split(","))


def test_the_worker_process_registers_every_model() -> None:
    """A process that imports the worker alone has the whole model registry in its metadata."""
    registered = _tables_registered_by_the_worker_alone()

    missing = MODEL_TABLES - registered
    assert not missing, (
        "a process that imports only apps.api.worker did not register: "
        f"{sorted(missing)}; registered: {sorted(registered)}"
    )
