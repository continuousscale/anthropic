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
from pydantic import BaseModel, Field

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


class NamingConfig(BaseModel):
    template: str = "{date}_{subject}_{descriptor}"
    date_format: str = "%Y-%m-%d"
    separator: str = "_"
    banned_tokens: list[str] = Field(default_factory=list)
    max_filename_length: int = 120
    subject_style: str = "pascal"
    descriptor_style: str = "snake_parts"


class ConfidenceConfig(BaseModel):
    auto_apply: float = 0.90
    review: float = 0.60


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
