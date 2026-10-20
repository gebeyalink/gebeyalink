"""Pin the served OpenAPI schema as a reviewable file.

The file this module writes, ``apps/api/openapi.snapshot.json``, is the API's description of
itself: generated from the route table and the request and response models, and from nothing
else. A CI job regenerates it and compares byte-for-byte, so a change to the served contract
arrives as a diff a reviewer reads rather than something a client discovers.

Determinism is the whole point, so everything that could make two runs on two machines
disagree is fixed here:

* **Inert settings, no real values.** The application is built from a placeholder profile, so
  no environment variable is read and no secret can reach the file. The placeholders are
  deliberately, visibly fake - a secret scanner must see a placeholder, not something that
  looks like a leaked credential.
* **An explicit newline.** The file is opened with ``newline=""`` and the serialized text ends
  in exactly one ``"\\n"``, so no platform can translate the ending.
* **Sorted keys and a fixed indent.** The same schema serializes to the same bytes.
* **Two hashes.** ``write_snapshot`` returns the baseline-method hash and the file's SHA-256.
  The first is the compact, key-sorted digest of the whole document, recorded once when this
  pin landed and used only to prove the schema itself did not change. The second is the digest
  of the bytes on disk.

The baseline-method hash is not a CI gate. CI compares the written file to the tracked one;
the hash answers one question, once: did pinning the schema change the schema?
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from pydantic import SecretStr

from apps.api.core.config import Settings
from apps.api.main import create_app

#: The baseline-method hash: the SHA-256 of the compact, key-sorted OpenAPI document under an
#: inert settings profile, recorded when the pin landed. It proves the schema did not change;
#: it is not a value CI checks.
BASELINE_METHOD_HASH = "ddbb82c01bb69d05bcde3b1c3da41520b44bea12987ebef5c884ec1683241fb3"


def _inert_settings() -> Settings:
    """The placeholder configuration the document is generated from.

    Every value is a placeholder. None is read from the environment, none is usable, and the
    signing material and the gateway secret are the same obvious string so a scanner reads
    them as fixtures rather than as credentials.
    """
    return Settings(
        database_url="postgresql+psycopg://snapshot@localhost:1/snapshot",
        redis_url="redis://localhost:1/0",
        signing_private_key=SecretStr("snapshot-placeholder"),
        signing_public_key=SecretStr("snapshot-placeholder"),
        gateway_secret=SecretStr("snapshot-placeholder"),
        terms_version="snapshot",
    )


def _document() -> dict[str, object]:
    """Build the application on the inert profile and return its OpenAPI document."""
    return create_app(_inert_settings()).openapi()


def _baseline_method_hash(document: dict[str, object]) -> str:
    """The compact, key-sorted digest of the whole document."""
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode("utf-8")).hexdigest()


def baseline_method_hash() -> str:
    """The baseline-method hash of the current schema. Not a CI gate."""
    return _baseline_method_hash(_document())


def write_snapshot(path: str | Path) -> tuple[str, str]:
    """Write the pinned document to ``path`` and return its two hashes.

    Returns ``(baseline_method_hash, file_sha256)``: the one-time schema proof and the digest
    of the bytes written. The bytes are ``json.dumps(..., indent=2, sort_keys=True,
    ensure_ascii=False)`` followed by a single newline, written through a handle opened with
    ``newline=""`` so the newline reaches the file exactly as it was written.
    """
    document = _document()
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return _baseline_method_hash(document), hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pin the served OpenAPI schema as a file.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="print the baseline-method hash")
    action.add_argument("--write", metavar="PATH", help="write the snapshot to PATH")
    args = parser.parse_args(argv)

    if args.check:
        digest = baseline_method_hash()
        print("baseline-method hash:", digest)
        if digest == BASELINE_METHOD_HASH:
            print("matches the recorded baseline")
            return 0
        print("differs from the recorded baseline:", BASELINE_METHOD_HASH)
        return 1

    method_hash, file_hash = write_snapshot(args.write)
    print("wrote:", args.write)
    print("baseline-method hash:", method_hash)
    print("file sha256:", file_hash)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
