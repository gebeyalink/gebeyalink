"""The worker process's model registry: every table its tasks can touch is loaded.

A worker task that reads or writes an order reaches every table that order's foreign keys
name. SQLAlchemy resolves those keys across the whole ``MetaData`` when it flushes, so a
process that has loaded only the models its own imports happened to reach fails at the first
write with ``NoReferencedTableError`` — which is exactly what the worker process did before it
loaded the full model set. The shipment sweep settled nothing, forever, while the heartbeat
worked because it only touches the store.

The check has to run in fresh interpreters. This suite's ``conftest`` imports
``apps.api.main``, which loads every router, service, and model, so the metadata of the
interpreter running the tests already holds every table: a check made in-process would pass
whether or not the worker registers anything. That is why the defect survived the in-process
sweep tests, and it is why the worker's own registry is only visible from a process that
imports the worker and nothing else.

To avoid a second source of truth, the test compares two clean processes: one imports only
``apps.api.worker``, the other imports the full ``apps.api.main``. Their ``Base.metadata.tables``
must be identical. If the app knows a table and the worker does not load it, the difference
is named on failure.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# SPEC A20: importing in fresh processes needs no I/O.
pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Imports the worker and prints the tables it registered, without importing the application
#: or the test harness. ``TABLES=`` prefixes the line so an incidental write to stdout during
#: import cannot be mistaken for the registry.
_WORKER_PROBE = (
    "import apps.api.worker;"
    "from apps.api.db.base import Base;"
    "print('TABLES=' + ','.join(sorted(Base.metadata.tables)))"
)

#: Imports the full app to capture the complete registry as the source of truth.
_APP_PROBE = (
    "import apps.api.main;"
    "from apps.api.db.base import Base;"
    "print('TABLES=' + ','.join(sorted(Base.metadata.tables)))"
)


def _tables_from_probe(code: str) -> frozenset[str]:
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        "probe failed in a fresh process:\n" f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    line = next(
        (line for line in result.stdout.splitlines() if line.startswith("TABLES=")),
        None,
    )
    assert line is not None, f"the probe printed no registry:\n{result.stdout}"
    return frozenset(line.removeprefix("TABLES=").split(","))


def test_the_worker_process_registers_every_model() -> None:
    """A process that imports the worker alone has the same registry as the app."""
    worker_tables = _tables_from_probe(_WORKER_PROBE)
    app_tables = _tables_from_probe(_APP_PROBE)

    missing_in_worker = app_tables - worker_tables
    missing_in_app = worker_tables - app_tables

    assert worker_tables == app_tables, (
        "worker registry differs from app registry\n"
        f"- in app but not in worker: {sorted(missing_in_worker)}\n"
        f"- in worker but not in app: {sorted(missing_in_app)}"
    )
