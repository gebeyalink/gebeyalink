"""The snapshot generator writes the same bytes every time, and exactly the bytes promised.

The CI job compares a fresh regeneration to the tracked file, so the one property that makes
that comparison meaningful is determinism. These checks hold the three promises the generator
makes about its output: two runs are identical, the file ends in exactly one newline, and no
platform's line-ending translation has leaked a carriage return into it. The two CLI entry
points the CI job runs are checked here as well, so the command in the workflow is exercised.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from apps.api.snapshot import BASELINE_METHOD_HASH, main, write_snapshot

pytestmark = pytest.mark.unit


def test_two_runs_produce_identical_bytes(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    write_snapshot(first)
    write_snapshot(second)
    assert first.read_bytes() == second.read_bytes()


def test_snapshot_ends_with_exactly_one_newline(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    write_snapshot(snapshot)
    raw = snapshot.read_bytes()
    assert raw.endswith(b"\n")
    assert not raw.endswith(b"\n\n")


def test_snapshot_contains_no_carriage_returns(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    write_snapshot(snapshot)
    assert b"\r" not in snapshot.read_bytes()


def test_reported_file_hash_matches_the_bytes_written(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    _, file_hash = write_snapshot(snapshot)
    assert file_hash == hashlib.sha256(snapshot.read_bytes()).hexdigest()


def test_check_mode_reports_the_recorded_baseline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--check"]) == 0
    assert BASELINE_METHOD_HASH in capsys.readouterr().out


def test_write_mode_writes_the_file_and_succeeds(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.json"
    assert main(["--write", str(snapshot)]) == 0
    assert snapshot.exists()
