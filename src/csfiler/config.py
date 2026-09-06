"""Configuration loading and entity resolution.

All domain knowledge - the naming convention, the BizBox taxonomy, the known
counterparties, the routing rules - lives in the YAML file this module loads.
Nothing here hardcodes a rule; this is only the typed view of the file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

CONFIG_FILENAME = "continuous_scale.yaml"

# Searched in order. The repo layout wins for an editable install; the CWD and
# the user config directory cover an installed copy driven from elsewhere.
CONFIG_SEARCH_PATHS = (
    Path.cwd() / "config" / CONFIG_FILENAME,
    Path.cwd() / CONFIG_FILENAME,
    Path(__file__).resolve().parents[2] / "config" / CONFIG_FILENAME,
    Path.home() / ".config" / "csfiler" / CONFIG_FILENAME,
)


def find_config() -> Path | None:
    """First existing config from the search path, or None."""
    for candidate in CONFIG_SEARCH_PATHS:
        if candidate.is_file():
            return candidate
    return None


class ForbiddenPattern(BaseModel):
    name: str
    pattern: str


class PrivacyConfig(BaseModel):
    redact_account_numbers: bool = True
    account_number_style: str = "x{last4}"
    forbid_in_filename: list[ForbiddenPattern] = Field(default_factory=list)

    def compiled(self) -> list[tuple[str, re.Pattern[str]]]:
        return [(f.name, re.compile(f.pattern)) for f in self.forbid_in_filename]


# The separator names a firm can choose between, as words rather than symbols.
SEPARATORS = {"dash": "-", "underscore": "_", "period": ".", "space": " "}

# What a field may do when the fact it needs is not on the document.
#   block       - refuse to build a name at all (right for a date: a wrong
#                 date silently misfiles the document into the wrong year)
#   skip        - leave the field out and close the gap
#   placeholder - emit a marker, so a human can spot what is missing
#   use_org     - fall back to our own short name (subject fields only)
ON_MISSING = {"block", "skip", "placeholder", "use_org"}

# Fields that may appear in a filename.
NAME_FIELDS = {
    "date",       # the document's own date
    "year",       # the fiscal period year, which is not always the date's year
    "subject",    # the counterparty, whoever it is
    "client",     # the counterparty, but only if it is a configured client
    "bank",       # the counterparty, but only if it is a financial institution
    "account",    # the account identifier, redacted to its last four digits
    "doc_type",   # the descriptor configured for the document type
    "qualifier",  # signed, draft, amended
    "entity",     # the name our own business is given on the document
}


class NameField(BaseModel):
    """One component of the filename, in the order it appears."""

    field: str
    # strftime for date, "%Y" style for year, "x{last4}" for account.
    format: str | None = None
    style: str | None = None  # "pascal" for subject-like fields
    on_missing: str = "skip"
    placeholder: str = "X"
    # Only emit this field for document types filed under a specific account,
    # where it is what distinguishes two otherwise identical statements.
    account_scoped_only: bool = False

    @field_validator("field")
    @classmethod
    def _known_field(cls, value: str) -> str:
        if value not in NAME_FIELDS:
            raise ValueError(
                f"Unknown name field {value!r}. Choose from: {sorted(NAME_FIELDS)}"
            )
        return value

    @field_validator("on_missing")
    @classmethod
    def _known_policy(cls, value: str) -> str:
        if value not in ON_MISSING:
            raise ValueError(
                f"Unknown on_missing policy {value!r}. Choose from: {sorted(ON_MISSING)}"
            )
        return value


class NamingConfig(BaseModel):
    """The firm's naming convention, as an ordered list of fields.

    Reordering `fields` reorders the filename; changing `separator` changes
    what joins them. Nothing about the convention is hardcoded in the source.
    """

    separator: str = "underscore"
    fields: list[NameField] = Field(default_factory=list)
    banned_tokens: list[str] = Field(default_factory=list)
    max_filename_length: int = 120

    @field_validator("separator")
    @classmethod
    def _known_separator(cls, value: str) -> str:
        if value not in SEPARATORS:
            raise ValueError(
                f"Unknown separator {value!r}. Choose from: {sorted(SEPARATORS)}"
            )
        return value

    @property
    def sep(self) -> str:
        return SEPARATORS[self.separator]


class ConfidenceConfig(BaseModel):
    auto_apply: float = 0.90
    review: float = 0.60


# How much of the work a human sees.
#   by_confidence - only genuinely uncertain files are queued (the default)
#   just_do_it    - apply everything the classifier is not actively unsure of
#   always_ask    - queue every proposal, however confident
REVIEW_MODES = {"by_confidence", "just_do_it", "always_ask"}


class ReviewConfig(BaseModel):
    mode: str = "by_confidence"

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        if value not in REVIEW_MODES:
            raise ValueError(
                f"Unknown review mode {value!r}. Choose from: {sorted(REVIEW_MODES)}"
            )
        return value


class DriveConfig(BaseModel):
    root_folder_name: str = "BizBox — Continuous Scale"
    protected_paths: list[str] = Field(default_factory=list)
    skip_conforming: bool = True


class DocumentType(BaseModel):
    id: str
    label: str
    descriptor: str
    path: str | None = None
    requires: list[str] = Field(default_factory=list)
    retention: str | None = None
    legal_weight: bool = False
    alert: bool = False
    note: str | None = None


class ExcludedType(BaseModel):
    id: str
    label: str
    reason: str


class Entity(BaseModel):
    subject: str
    aliases: list[str] = Field(default_factory=list)
    kind: str | None = None
    accounts: list[str] = Field(default_factory=list)
    note: str | None = None


class FormerName(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    renamed_on: str | None = None
    note: str | None = None


class RelatedEntity(BaseModel):
    name: str
    status: str
    handling: str = "review"


class Organization(BaseModel):
    legal_name: str
    short_name: str
    former_names: list[FormerName] = Field(default_factory=list)
    related_entities: list[RelatedEntity] = Field(default_factory=list)


class Config(BaseModel):
    organization: Organization
    drive: DriveConfig = Field(default_factory=DriveConfig)
    naming: NamingConfig = Field(default_factory=NamingConfig)
    privacy: PrivacyConfig = Field(default_factory=PrivacyConfig)
    confidence: ConfidenceConfig = Field(default_factory=ConfidenceConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    categories: dict[str, str] = Field(default_factory=dict)
    entities: dict[str, list[Entity]] = Field(default_factory=dict)
    document_types: list[DocumentType] = Field(default_factory=list)
    excluded_types: list[ExcludedType] = Field(default_factory=list)
    retention_policy: dict[str, str] = Field(default_factory=dict)

    # ---- lookups ---------------------------------------------------------

    def type_by_id(self, type_id: str) -> DocumentType | None:
        for dt in self.document_types:
            if dt.id == type_id:
                return dt
        return None

    def excluded_by_id(self, type_id: str) -> ExcludedType | None:
        for et in self.excluded_types:
            if et.id == type_id:
                return et
        return None

    def all_entities(self) -> list[Entity]:
        return [e for group in self.entities.values() for e in group]

    def entity_group(self, subject: str) -> str | None:
        """Which group ('clients', 'vendors', ...) an entity belongs to."""
        for group, members in self.entities.items():
            for e in members:
                if e.subject == subject:
                    return group
        return None

    def resolve_entity(self, raw: str | None) -> Entity | None:
        """Map a name as printed on a document to a canonical entity.

        Matching is deliberately conservative: exact match on the canonical
        subject or a configured alias, case- and punctuation-insensitive. A
        near-miss returns None so the file goes to review rather than being
        filed under a guessed counterparty.
        """
        if not raw:
            return None
        needle = _normalize(raw)
        if not needle:
            return None
        for entity in self.all_entities():
            candidates = [entity.subject, *entity.aliases]
            if any(_normalize(c) == needle for c in candidates):
                return entity
        return None

    def matches_former_name(self, raw: str | None) -> FormerName | None:
        """True when a document names the business by its pre-rename name."""
        if not raw:
            return None
        needle = _normalize(raw)
        for former in self.organization.former_names:
            if any(_normalize(c) == needle for c in [former.name, *former.aliases]):
                return former
            # A document may print "Andover Consulting, LLC dba ..." - substring
            # match on the alias catches that without matching the new name.
            for alias in [former.name, *former.aliases]:
                if _normalize(alias) and _normalize(alias) in needle:
                    return former
        return None

    def matches_related_entity(self, raw: str | None) -> RelatedEntity | None:
        if not raw:
            return None
        needle = _normalize(raw)
        for rel in self.organization.related_entities:
            if _normalize(rel.name) in needle:
                return rel
        return None


def _normalize(value: str) -> str:
    """Lowercase, strip punctuation and entity suffixes, collapse whitespace."""
    text = value.lower().strip()
    text = re.sub(r"[,\.]", " ", text)
    text = re.sub(r"\b(llc|inc|incorporated|corp|corporation|ltd|co|company)\b", " ", text)
    text = re.sub(r"[^a-z0-9&]+", "", text)
    return text


def load_config(path: str | Path | None = None) -> Config:
    """Load and validate the configuration file."""
    if path:
        resolved = Path(path)
        if not resolved.is_file():
            raise FileNotFoundError(f"Config not found at {resolved}.")
    else:
        found = find_config()
        if not found:
            searched = "\n  ".join(str(p) for p in CONFIG_SEARCH_PATHS)
            raise FileNotFoundError(
                f"Could not find {CONFIG_FILENAME}. Pass --config explicitly, or "
                f"place it at one of:\n  {searched}"
            )
        resolved = found
    raw: dict[str, Any] = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    return Config.model_validate(raw)
