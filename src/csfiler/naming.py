"""Filename construction from the firm's configured convention.

A convention is an ordered list of fields, a separator, and a per-field rule
for what to do when the fact a field needs is not on the document. All of it
comes from config: reordering the fields reorders the filename, and no part of
the convention is written into this module. `_v2`, `_v3` are appended only on a
genuine collision.

Three rules drive everything here:

1.  The model supplies facts; this module supplies the name. The classifier
    never returns a filename, so it cannot invent one that breaks convention.
2.  A filename is public surface. It shows up in link previews, share
    notifications and folder listings, so no full account number, SSN or EIN
    ever reaches one.
3.  A field that cannot be filled follows its own `on_missing` policy, and
    `block` is the honest default for anything load-bearing. A guessed date is
    worse than no name at all, because it silently misfiles the document into
    the wrong year - so the date field blocks rather than guessing, while a
    missing qualifier simply drops out.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from .config import Config, DocumentType, NameField
from .models import DocumentFacts


def conforming_pattern(config: Config) -> re.Pattern[str]:
    """Build the "already named correctly" test from the configured fields.

    Derived from the convention rather than hardcoded, so a firm that reorders
    its fields or switches separator still gets correct skip-on-rerun behavior.
    """
    sep = re.escape(config.naming.sep)
    chunks: list[str] = []
    for spec in config.naming.fields:
        if spec.field == "date":
            fmt = spec.format or "%Y-%m-%d"
            chunk = re.escape(fmt)
            for code, pattern in (
                (re.escape("%Y"), r"\d{4}"),
                (re.escape("%m"), r"\d{2}"),
                (re.escape("%d"), r"\d{2}"),
            ):
                chunk = chunk.replace(code, pattern)
        elif spec.field == "year":
            chunk = r"\d{4}"
        else:
            chunk = r"[A-Za-z0-9&.\-]+"
        # Every field but a blocking one may be absent from a given name.
        chunks.append(chunk if spec.on_missing == "block" else f"(?:{chunk})?")

    body = f"{sep}?".join(chunks)
    return re.compile(rf"^{body}(?:{sep}v\d+)?$")

# Types whose filing is scoped to a specific account, where the redacted
# account number is what distinguishes two otherwise identical statements.
_ACCOUNT_SCOPED_PATH_MARKERS = ("Accounts/", "Cards/", "Processors/")


class NameError_(ValueError):
    """Raised when a conforming name cannot be constructed from the facts."""


def is_conforming(filename: str, config: Config) -> bool:
    """True if a filename already follows the configured convention.

    Used to skip files on re-runs, which is what makes the pipeline safely
    idempotent: a second pass over the same folder is a no-op and costs
    nothing, because it never reaches the API.
    """
    return bool(conforming_pattern(config).match(Path(filename).stem))


def pascal(value: str) -> str:
    """`Third Horizon Partners` -> `ThirdHorizonPartners`."""
    parts = re.split(r"[^A-Za-z0-9]+", value)
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def redact_account(account: str | None, style: str = "x{last4}") -> str | None:
    """Reduce an account number to its last four digits.

    `****1234`, `1234-5678-9012-3456` and `Acct 987654321` all reduce to the
    last four digits present. Returns None when there are not at least four.
    """
    if not account:
        return None
    digits = re.sub(r"\D", "", account)
    if len(digits) < 4:
        return None
    return style.format(last4=digits[-4:])


def scrub(text: str, config: Config) -> str:
    """Remove anything the privacy config forbids in a filename."""
    out = text
    for _name, pattern in config.privacy.compiled():
        out = pattern.sub("", out)
    for banned in config.naming.banned_tokens:
        out = re.sub(rf"(?<![A-Za-z]){re.escape(banned)}(?![A-Za-z])", "", out, flags=re.IGNORECASE)
    sep = config.naming.sep
    if sep.strip():
        out = re.sub(re.escape(sep) + r"{2,}", sep, out)
    out = out.strip("_-. ")
    return out


def resolve_subject(facts: DocumentFacts, config: Config) -> tuple[str | None, list[str]]:
    """Pick the subject token, preferring a known entity over free text.

    Returns (subject, notes). A counterparty that resolves to a configured
    entity uses that entity's canonical token verbatim, which is what keeps
    `Third Horizon`, `Third Horizon Partners` and `3rd Horizon` all filing to
    the same folder.
    """
    notes: list[str] = []
    entity = config.resolve_entity(facts.counterparty)
    if entity:
        return entity.subject, notes

    if facts.counterparty:
        guess = pascal(scrub(facts.counterparty, config))
        if guess:
            notes.append(
                f"'{facts.counterparty}' is not a known counterparty; used "
                f"'{guess}'. Add it to config to file it consistently."
            )
            return guess, notes

    # No counterparty at all is normal for governance and IP documents.
    return None, notes


def resolve_descriptor(doc_type: DocumentType) -> str | None:
    """The descriptor configured for this document type."""
    return doc_type.descriptor or None


def resolve_qualifier(facts: DocumentFacts, config: Config) -> str | None:
    """`signed`, `draft`, `amended` - one lowercase word, or nothing."""
    qualifier = facts.qualifier
    # A signature is a fact worth carrying in the name, and the classifier
    # reports it separately from the free-text qualifier.
    if facts.is_signed and (not qualifier or "sign" not in qualifier.lower()):
        qualifier = "signed"
    if not qualifier:
        return None
    cleaned = re.sub(r"[^a-z0-9]+", "", scrub(qualifier.strip().lower(), config))
    return cleaned or None


def _format_date(facts: DocumentFacts, fmt: str) -> tuple[str | None, list[str]]:
    """Format the identifying date, or explain why there isn't one.

    Never falls back to today's date or to the file's modified time: a wrong
    date in a filename silently misfiles the document into the wrong period.
    """
    if not facts.document_date:
        return None, ["No date could be read from the document."]
    try:
        parsed = _dt.date.fromisoformat(facts.document_date)
    except ValueError:
        return None, [f"Unparseable date from classifier: {facts.document_date!r}."]

    notes: list[str] = []
    if parsed > _dt.date.today() + _dt.timedelta(days=1):
        notes.append(f"Date {parsed.isoformat()} is in the future; verify before filing.")
    return parsed.strftime(fmt), notes


def _entity_in_group(facts: DocumentFacts, config: Config, group: str) -> str | None:
    """The counterparty token, but only if it belongs to the named group."""
    entity = config.resolve_entity(facts.counterparty)
    if entity and config.entity_group(entity.subject) == group:
        return entity.subject
    return None


def resolve_field(
    spec: "NameField",
    facts: DocumentFacts,
    doc_type: DocumentType,
    config: Config,
) -> tuple[str | None, list[str]]:
    """Produce one field's text, or None when the fact is not available."""
    notes: list[str] = []
    name = spec.field

    if name == "date":
        return _format_date(facts, spec.format or "%Y-%m-%d")

    if name == "year":
        year = facts.period_year
        if not year and facts.document_date:
            try:
                year = _dt.date.fromisoformat(facts.document_date).year
            except ValueError:
                year = None
        return (str(year) if year else None), notes

    if name == "subject":
        subject, subject_notes = resolve_subject(facts, config)
        return subject, subject_notes

    if name == "client":
        return _entity_in_group(facts, config, "clients"), notes

    if name == "bank":
        return _entity_in_group(facts, config, "financial_institutions"), notes

    if name == "account":
        if spec.account_scoped_only and not _is_account_scoped(doc_type):
            return None, notes
        if not config.privacy.redact_account_numbers:
            return (facts.account_number or None), notes
        return redact_account(
            facts.account_number, spec.format or config.privacy.account_number_style
        ), notes

    if name == "doc_type":
        return resolve_descriptor(doc_type), notes

    if name == "qualifier":
        return resolve_qualifier(facts, config), notes

    if name == "entity":
        return (pascal(facts.entity_named) if facts.entity_named else None), notes

    return None, [f"No resolver for name field {name!r}."]


def _is_account_scoped(doc_type: DocumentType) -> bool:
    return bool(doc_type.path) and any(
        marker in doc_type.path for marker in _ACCOUNT_SCOPED_PATH_MARKERS
    )


def build_name(
    facts: DocumentFacts,
    doc_type: DocumentType,
    config: Config,
    original_filename: str,
) -> tuple[str, list[str]]:
    """Render the filename by walking the configured fields in order.

    Each field that cannot be filled applies its own `on_missing` policy, so a
    firm decides per field whether a gap blocks the name, is skipped, or is
    marked with a placeholder. Raises NameError_ when a `block` field is
    missing, which the caller turns into a review blocker.
    """
    notes: list[str] = []
    parts: list[str] = []

    for spec in config.naming.fields:
        value, field_notes = resolve_field(spec, facts, doc_type, config)
        notes.extend(field_notes)

        if value:
            parts.append(str(value))
            continue

        if spec.on_missing == "block":
            raise NameError_(f"no {spec.field} available")
        if spec.on_missing == "use_org":
            parts.append(config.organization.short_name)
            notes.append(
                f"No {spec.field} on the document; used "
                f"'{config.organization.short_name}'."
            )
        elif spec.on_missing == "placeholder":
            parts.append(spec.placeholder)
            notes.append(f"No {spec.field} on the document; marked '{spec.placeholder}'.")
        # "skip" contributes nothing and closes the gap.

    if not parts:
        raise NameError_("no fields could be filled")

    stem = scrub(config.naming.sep.join(parts), config)
    if not stem:
        raise NameError_("the name was empty after scrubbing")

    ext = Path(original_filename).suffix.lower()
    max_stem = config.naming.max_filename_length - len(ext)
    if len(stem) > max_stem:
        stem = stem[:max_stem].rstrip("_-. ")
        notes.append(f"Name truncated to {config.naming.max_filename_length} characters.")

    name = f"{stem}{ext}"

    # Belt and braces: verify nothing forbidden survived construction.
    for pattern_name, pattern in config.privacy.compiled():
        if pattern.search(name):
            raise NameError_(f"generated name contains forbidden {pattern_name}")

    return name, notes


def version_for_collision(name: str, taken: set[str], sep: str = "_") -> str:
    """Append `_v2`, `_v3`, ... until the name is free in its destination.

    The convention's version suffix exists for exactly this, and it is the
    reason the agent never needs to overwrite a file to complete a rename.
    """
    if name not in taken:
        return name

    path = Path(name)
    stem, ext = path.stem, path.suffix

    base = re.sub(re.escape(sep) + r"v\d+$", "", stem)
    version = 2
    while f"{base}{sep}v{version}{ext}" in taken:
        version += 1
    return f"{base}{sep}v{version}{ext}"
