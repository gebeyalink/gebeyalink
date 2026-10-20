"""Request and response bodies for a tier-upgrade submission.

Three shapes: what the trader sends, and the small receipt they get back. The request mirrors
``docs/openapi.yaml``'s ``/me/kyc`` body exactly; the response is deliberately minimal — the
case's identity and state, not the evidence — because a submission's whole outcome is "your
case is waiting", and echoing the uploaded addresses back would be a copy of data the client
already holds.

``doc_images`` are URIs, not bytes. What a submission carries is the list of addresses the
client's own photo storage handed back; fetching and hosting the image is the media flow's
business (ADR-0006, a later slice). At most three, per §11.1's "≤3 photos".

The tier and document vocabularies are the model's enums rather than a second list here, so a
value the API accepts is a value the column will store. A request naming a tier or a document
kind the platform does not define is refused by the shape before the service is reached.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.api.models.kyc_case import KycDocType, KycStatus, KycTier

#: §11.1: at most three photographs per case. The rule is the case's own, so it lives beside
#: the shape that states it rather than in the handler that happens to enforce it.
MAX_DOC_IMAGES = 3

#: The tax number's ceiling, per §11.1's TIN field. A TIN is short; thirty characters is
#: generous and still bounded, which is what a request field wants to be.
MAX_TIN_LENGTH = 30

#: The schemes a document address may use. A document image is fetched from somewhere and
#: hosted by the client, so the address has to be a web one; anything else is a value that
#: would fail at the fetch rather than at the shape, and a shape is the cheaper place to
#: refuse it.
_URI_SCHEMES = ("http://", "https://")


class KycSubmissionBody(BaseModel):
    """A trader asking to be moved to a higher tier, with the document that justifies it."""

    model_config = ConfigDict(extra="forbid")

    tier_requested: KycTier = Field(
        description="The tier being asked for; must be higher than the one the account holds.",
    )
    doc_type: KycDocType = Field(description="Which identity document is being submitted.")
    doc_images: list[str] = Field(
        min_length=0,
        max_length=MAX_DOC_IMAGES,
        description="Where the document images are hosted; at most three.",
    )
    # Optional, and only meaningful for a business account: §11.1's BUSINESS_LICENSE path.
    # Where the profile's own field is written from, in the same transaction as the case.
    tax_identification_number: str | None = Field(
        default=None,
        max_length=MAX_TIN_LENGTH,
        description="TIN for a business account; written to the profile with the case.",
    )

    @field_validator("doc_images")
    @classmethod
    def _each_image_is_a_uri(cls, values: list[str]) -> list[str]:
        """Every address is a web address the platform could fetch.

        A scheme check rather than a full parser: the point is that a *document* is at an
        address reachable over the web, and refusing anything that is not is the whole of
        what this owes. The check runs per item so the caller is told the field, not merely
        that something in a list was wrong.
        """
        for value in values:
            if not value.startswith(_URI_SCHEMES) or len(value) <= len("https://"):
                raise ValueError("must_be_a_web_uri")
        return values


class KycCaseResponse(BaseModel):
    """A submitted case, as the trader who sent it may read it back.

    The receipt rather than the case: enough to quote in a support message and to poll the
    outcome by, and no copy of the evidence the sender already holds.
    """

    # Read off the written row rather than a hand-built dict, so the fields the response names
    # are the fields the row holds and the two cannot drift.
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: UUID
    status: KycStatus
    tier_requested: KycTier
    doc_type: KycDocType
    created_at: datetime
