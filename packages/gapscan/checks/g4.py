"""G4 — a requirement's test exercising an endpoint the contract does not define.

The contract is the source of truth for what exists. A test that drives ``/v1/something``
which the contract never defines is either testing an invented endpoint or testing the wrong
one; either way the "it works" it reports is evidence about something that is not the
contract. This is the check that makes "frontend against a mock, backend against nothing"
visible.

The paths are read from the test source, where the calls are written inline
(``client.post("/v1/orders")``). The contract's ``servers`` block carries the ``/v1`` base, so
the base is stripped before matching, and a ``{param}`` segment matches any single segment, so
``/v1/orders/{id}/confirm`` in a test resolves to ``/orders/{order_id}/confirm`` in the
contract.
"""

from __future__ import annotations

import re

from packages.gapscan.config import Context, Finding, Severity

CHECK = "G4"

#: A versioned API path literal in test source: ``"/v1/orders"`` or ``f"/v1/orders/{id}"``.
PATH_LITERAL = re.compile(r"""["'](/v1/[A-Za-z0-9_\-/{}]*)["']""")

#: The base every operation in the contract is written relative to, from its ``servers`` block.
BASE_PREFIX = "/v1"

#: Only requirement tests are in this check's remit, and a requirement test is named for the
#: requirement: ``test_FR_9002_listing.py``. The selection reads the *file name*, not the source
#: text, so a file that merely contains the characters ``test_FR_...`` inside a string — the
#: tool's own test, which builds requirement tests as fixture data — is not mistaken for one.
REQUIREMENT_TEST_NAME = re.compile(r"^test_([A-Z]+)_(\d+[A-Z]?)_")

#: A path segment that announces the endpoint deliberately does not exist. A test that pushes
#: at ``/v1/auth/does-not-exist`` is proving the error envelope, not claiming an endpoint; the
#: check reads the announcement rather than inventing an exemption list per test file.
PROBE_SEGMENT = re.compile(
    r"(does[-_]?not[-_]?exist|nonexistent|not[-_]?found|missing|unknown|bogus|invalid|no[-_]?such)",
    re.I,
)


def check(context: Context) -> list[Finding]:
    """Every versioned path a requirement test drives must exist in the contract."""
    if not context.operations:
        return []
    contract_paths = [_normalise(path) for _, path, _ in context.operations]
    findings: list[Finding] = []
    for path in context.test_files:
        requirement = _requirement_of(path.name)
        if requirement is None:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in PATH_LITERAL.finditer(line):
                candidate = match.group(1)
                if _is_probe(candidate) or _matches_contract(candidate, contract_paths):
                    continue
                findings.append(
                    Finding(
                        check=CHECK,
                        severity=Severity.BLOCKING,
                        requirement=requirement,
                        message=f"calls '{candidate}' — not found in docs/openapi.yaml",
                        evidence=f"{path.as_posix()}:{line_number}",
                    )
                )
    return findings


def _matches_contract(candidate: str, contract_paths: list[str]) -> bool:
    """Whether a candidate path could be one of the contract's, allowing f-string variables.

    ``f"/v1/listings/{listing_id}/pause"`` and ``f"/v1/listings/{listing_id}/{move}"`` are
    indistinguishable to a scanner: both interpolate a variable in the last segment. The
    contract resolves it — ``/listings/{id}/pause`` has ``pause`` as a literal — but which value
    ``move`` holds is a runtime fact. The same ambiguity runs the other way: a test that passes
    a real UUID writes ``/v1/listings/1b9d…/pause`` where the contract writes
    ``/listings/{id}/pause``. So a ``{}`` on either side matches a literal on the other. A
    candidate misses only when no contract path has the same segment count and, under that
    relaxation, the same shape.
    """
    candidate_segments = _normalise(candidate).split("/")
    for contract_path in contract_paths:
        contract_segments = contract_path.split("/")
        if len(contract_segments) != len(candidate_segments):
            continue
        if all(
            contract == candidate_segment or contract == "{}" or candidate_segment == "{}"
            for contract, candidate_segment in zip(
                contract_segments, candidate_segments, strict=True
            )
        ):
            return True
    return False


def _is_probe(path: str) -> bool:
    """Whether a path names itself as deliberately-absent."""
    return any(PROBE_SEGMENT.search(segment) for segment in path.split("/") if segment)


def _requirement_of(name: str) -> str | None:
    """The requirement a requirement test is named for, or None if it is not one."""
    match = REQUIREMENT_TEST_NAME.match(name)
    return f"{match.group(1)}-{match.group(2)}" if match else None


def _normalise(path: str) -> str:
    """A path with its version base stripped and its parameters collapsed to ``{}``."""
    trimmed = path[len(BASE_PREFIX) :] if path.startswith(BASE_PREFIX) else path
    segments: list[str] = []
    for segment in trimmed.split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            segments.append("{}")
        else:
            segments.append(segment)
    return "/".join(segments) or "/"
