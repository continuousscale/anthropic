"""Filename construction under the BizBox convention.

The convention is `YYYY-MM-DD_Subject_Descriptor` with `_v2`, `_v3` appended
only on a genuine collision. Three rules drive everything here:

1.  The model supplies facts; this module supplies the name. The classifier
    never returns a filename, so it cannot invent one that breaks convention.
2.  A filename is public surface. It shows up in link previews, share
    notifications and folder listings, so no full account number, SSN or EIN
    ever reaches one.
3.  A name that cannot be built correctly is not built at all. Missing facts
    produce a blocker, never a placeholder like "Unknown" or today's date.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from .config import Config, DocumentType
from .models import DocumentFacts

# `2026-08-14_ThirdHorizon_SOW_signed_v2.pdf` - date, subject, then a
# descriptor that may itself contain underscores, with an optional version.
CONFORMING_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}"          # date
    r"_[A-Za-z0-9&\-]+"            # subject
    r"_[A-Za-z0-9\-]+(?:_[A-Za-z0-9\-]+)*"  # descriptor parts
    r"(?:_v\d+)?$"                 # optional version
)

# Types whose filing is scoped to a specific account, where the redacted
# account number is what distinguishes two otherwise identical statements.
_ACCOUNT_SCOPED_PATH_MARKERS = ("Accounts/", "Cards/", "Processors/")


class NameError_(ValueError):
    """Raised when a conforming name cannot be constructed from the facts."""


def is_conforming(filename: str) -> bool:
    """True if a filename already follows the convention.

    Used to skip files on re-runs, which is what makes the pipeline safely
    idempotent: a second pass over the same folder is a no-op.
    """
    return bool(CONFORMING_RE.match(Path(filename).stem))


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
    out = re.sub(r"_{2,}", "_", out).strip("_-. ")
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


def build_descriptor(
    facts: DocumentFacts, doc_type: DocumentType, config: Config
) -> str:
    """`SOW` + `signed` -> `SOW_signed`, plus a redacted account when scoped."""
    parts: list[str] = [doc_type.descriptor]

    qualifier = facts.qualifier
    # A signature is a fact worth carrying in the name, and the classifier
    # reports it separately from the free-text qualifier.
    if facts.is_signed and (not qualifier or "sign" not in qualifier.lower()):
        qualifier = "signed"
    if qualifier:
        cleaned = scrub(qualifier.strip().lower(), config)
        cleaned = re.sub(r"[^a-z0-9]+", "", cleaned)
        if cleaned:
            parts.append(cleaned)

    if config.privacy.redact_account_numbers and doc_type.path:
        if any(marker in doc_type.path for marker in _ACCOUNT_SCOPED_PATH_MARKERS):
            redacted = redact_account(facts.account_number, config.privacy.account_number_style)
            if redacted:
                parts.append(redacted)

    return "_".join(p for p in parts if p)


def choose_date(facts: DocumentFacts, config: Config) -> tuple[str | None, list[str]]:
    """Format the identifying date, or explain why there isn't one.

    Never falls back to today's date or to the file's modified time: a wrong
    date in a filename is worse than an absent one, because it silently
    misfiles the document into the wrong period.
    """
    notes: list[str] = []
    if not facts.document_date:
        return None, ["No date could be read from the document."]
    try:
        parsed = _dt.date.fromisoformat(facts.document_date)
    except ValueError:
        return None, [f"Unparseable date from classifier: {facts.document_date!r}."]

    if parsed > _dt.date.today() + _dt.timedelta(days=1):
        notes.append(f"Date {parsed.isoformat()} is in the future; verify before filing.")
    return parsed.strftime(config.naming.date_format), notes


def build_name(
    facts: DocumentFacts,
    doc_type: DocumentType,
    config: Config,
    original_filename: str,
) -> tuple[str, list[str]]:
    """Render the full filename, extension included.

    Raises NameError_ when a required component is missing, so the caller can
    turn that into a review blocker instead of writing a malformed name.
    """
    notes: list[str] = []

    date_str, date_notes = choose_date(facts, config)
    notes.extend(date_notes)
    if not date_str:
        raise NameError_("no usable document date")

    subject, subject_notes = resolve_subject(facts, config)
    notes.extend(subject_notes)
    if not subject:
        # The convention has three slots; a document with no counterparty uses
        # our own short name, which is how governance documents are named.
        subject = config.organization.short_name
        notes.append(f"No counterparty on the document; used '{subject}'.")

    descriptor = build_descriptor(facts, doc_type, config)
    if not descriptor:
        raise NameError_("could not build a descriptor")

    stem = config.naming.template.format(
        date=date_str, subject=subject, descriptor=descriptor
    )
    stem = scrub(stem, config)

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


def version_for_collision(name: str, taken: set[str]) -> str:
    """Append `_v2`, `_v3`, ... until the name is free in its destination.

    The convention's version suffix exists for exactly this, and it is the
    reason the agent never needs to overwrite a file to complete a rename.
    """
    if name not in taken:
        return name

    path = Path(name)
    stem, ext = path.stem, path.suffix

    base = re.sub(r"_v\d+$", "", stem)
    version = 2
    while f"{base}_v{version}{ext}" in taken:
        version += 1
    return f"{base}_v{version}{ext}"
