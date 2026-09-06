"""Core data models shared across the pipeline.

`DocumentFacts` is the structured output contract for the classifier: it is
handed to the Claude API as the response schema, so every field description
here is read by the model. Changes to the wording change classifier behavior.
"""

from __future__ import annotations

import datetime as _dt
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Disposition(str, Enum):
    """What the pipeline decided to do with a file."""

    AUTO = "auto"           # confident enough to act without review
    REVIEW = "review"       # proposal queued for a human
    UNIDENTIFIED = "unidentified"  # not confident enough to propose anything
    SKIP_CONFORMING = "skip_conforming"  # already named correctly
    EXCLUDED = "excluded"   # a type the BizBox deliberately does not file
    DUPLICATE = "duplicate"  # byte-identical to a file already filed
    ERROR = "error"


class DocumentFacts(BaseModel):
    """What the classifier reads off the face of a document.

    Every field is evidence, not a decision. The naming and routing layers turn
    these facts into a filename and a path; the model does not choose either.
    """

    document_type: str = Field(
        description=(
            "The id of the single best-matching document type from the catalog "
            "in the system prompt. Use the exact id string. If nothing in the "
            "catalog fits, use 'unknown'."
        )
    )
    document_date: str | None = Field(
        default=None,
        description=(
            "The date that identifies this document, as YYYY-MM-DD. Prefer the "
            "date printed on the document over any date in the filename: the "
            "statement period end for a statement, the signature date for a "
            "signed agreement, the issue date for an invoice, the filing date "
            "for a return. Null if no date can be read."
        ),
    )
    period_year: int | None = Field(
        default=None,
        description=(
            "The fiscal or calendar year this document belongs to, which is not "
            "always the year of document_date - a return filed in March 2026 "
            "for tax year 2025 has period_year 2025. Null if not applicable."
        ),
    )
    counterparty: str | None = Field(
        default=None,
        description=(
            "The other party as printed on the document: the client, bank, "
            "vendor, or agency. Use the name as written, not an abbreviation "
            "you invent. Null if the document has no counterparty."
        ),
    )
    entity_named: str | None = Field(
        default=None,
        description=(
            "The name this document uses for our own business, exactly as "
            "printed. Important: some documents still say 'Andover Consulting, "
            "LLC'. Report what is printed - do not normalize it."
        ),
    )
    account_number: str | None = Field(
        default=None,
        description=(
            "Any account, policy, or card number printed on the document, in "
            "full. It is redacted downstream before it reaches a filename. Null "
            "if none."
        ),
    )
    qualifier: str | None = Field(
        default=None,
        description=(
            "One short lowercase word adding a distinguishing detail when the "
            "type alone is ambiguous: 'signed', 'executed', 'draft', 'amended', "
            "'void'. Null if nothing meaningful to add."
        ),
    )
    is_signed: bool = Field(
        default=False,
        description="True only if the document bears an actual signature or e-signature certificate.",
    )
    summary: str = Field(
        description="One sentence, under 20 words, describing what this document is."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How confident you are in document_type and document_date together. "
            "Be honest and calibrated. Use below 0.6 when the document is "
            "unreadable, ambiguous, or unlike anything in the catalog. Reserve "
            "above 0.9 for documents whose type and date are unmistakable."
        ),
    )
    reasoning: str = Field(
        description=(
            "One or two sentences naming the specific evidence you used - the "
            "letterhead, a form number, a total, a period label. Cite what you "
            "actually saw."
        )
    )
    concerns: list[str] = Field(
        default_factory=list,
        description=(
            "Anything a human should know before this is filed: the document is "
            "a scan of a scan, the date is handwritten, it names the old entity, "
            "it looks personal rather than business, it may be privileged. Empty "
            "list if nothing stands out."
        ),
    )


class FileRef(BaseModel):
    """A file in the source storage, before any decision is made about it."""

    id: str
    name: str
    mime_type: str
    size: int = 0
    parent_id: str | None = None
    parent_path: str | None = None
    modified_time: _dt.datetime | None = None
    content_hash: str | None = None
    web_link: str | None = None


class Proposal(BaseModel):
    """A single proposed rename-and-file action, before it is applied."""

    file: FileRef
    disposition: Disposition
    facts: DocumentFacts | None = None

    proposed_name: str | None = None
    proposed_path: str | None = None
    retention: str | None = None
    legal_weight: bool = False
    alert: bool = False

    # Populated when the proposal cannot be completed as-is.
    blockers: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    # Set by the reviewer when a human edits or rejects the proposal.
    reviewed_by: str | None = None
    review_action: Literal["approved", "edited", "rejected"] | None = None

    @property
    def is_actionable(self) -> bool:
        return (
            self.proposed_name is not None
            and not self.blockers
            and self.disposition in (Disposition.AUTO, Disposition.REVIEW)
        )

    @property
    def renames(self) -> bool:
        return self.proposed_name is not None and self.proposed_name != self.file.name

    @property
    def moves(self) -> bool:
        return (
            self.proposed_path is not None
            and self.file.parent_path is not None
            and self.proposed_path != self.file.parent_path
        )


class AppliedAction(BaseModel):
    """The record written to the journal when a proposal is applied.

    Holds enough prior state to reverse the action exactly.
    """

    batch_id: str
    file_id: str
    prior_name: str
    prior_parent_id: str | None
    prior_parent_path: str | None
    new_name: str
    new_parent_id: str | None
    new_parent_path: str | None
    applied_at: _dt.datetime
    undone_at: _dt.datetime | None = None
