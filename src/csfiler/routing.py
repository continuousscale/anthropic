"""Destination resolution: which BizBox folder a classified document belongs in.

This is the half that Financial Cents' renamer does not do. Renaming a file
`2026-08-31_Mercury_Statement_x3210.pdf` while leaving it in the Drive inbox
still leaves someone to file it. The routing rules in the config carry each
document type to an exact folder path.

A rule that cannot resolve every token in its path produces a blocker rather
than a partial path. Filing a document into the wrong folder is worse than
leaving it in the inbox, because it will not be found again by anyone looking
in the right place.
"""

from __future__ import annotations

import datetime as _dt
import re
from string import Formatter

from .config import Config, DocumentType
from .models import DocumentFacts

_FORMATTER = Formatter()


def path_tokens(template: str) -> list[str]:
    """The `{token}` names a path template needs filled."""
    return [f for _, f, _, _ in _FORMATTER.parse(template) if f]


def resolve_year(facts: DocumentFacts) -> int | None:
    """Fiscal period year, preferring the explicit period over the doc date.

    A return filed 2026-03-15 for tax year 2025 files under 2025. The
    classifier reports these separately for exactly this reason.
    """
    if facts.period_year:
        return facts.period_year
    if facts.document_date:
        try:
            return _dt.date.fromisoformat(facts.document_date).year
        except ValueError:
            return None
    return None


def resolve_path(
    facts: DocumentFacts,
    doc_type: DocumentType,
    config: Config,
) -> tuple[str | None, list[str], list[str]]:
    """Resolve a document type's path template against the facts.

    Returns (path, blockers, notes). A non-empty blockers list means the file
    must not be moved: the caller sends it to review with the reason attached.
    """
    blockers: list[str] = []
    notes: list[str] = []

    if not doc_type.path:
        return None, [f"Document type '{doc_type.id}' has no filing rule."], notes

    entity = config.resolve_entity(facts.counterparty)
    year = resolve_year(facts)
    values: dict[str, str] = {}

    for token in path_tokens(doc_type.path):
        if token == "client":
            # `{client}` is stricter than `{subject}`: it must be a counterparty
            # configured as a client, not merely a name we could pascal-case.
            if entity and config.entity_group(entity.subject) == "clients":
                values["client"] = entity.subject
            elif facts.counterparty:
                blockers.append(
                    f"'{facts.counterparty}' is not a known client. Add it to "
                    f"config under entities.clients, or file this by hand."
                )
            else:
                blockers.append("No client named on the document.")

        elif token == "subject":
            if entity:
                values["subject"] = entity.subject
            elif facts.counterparty:
                blockers.append(
                    f"'{facts.counterparty}' is not a known counterparty, so "
                    f"there is no folder for it yet."
                )
            else:
                blockers.append("No counterparty named on the document.")

        elif token == "year":
            if year:
                values["year"] = str(year)
            else:
                blockers.append("No year could be determined for the filing period.")

        elif token == "month":
            if facts.document_date:
                try:
                    values["month"] = f"{_dt.date.fromisoformat(facts.document_date).month:02d}"
                except ValueError:
                    blockers.append("Document date is unparseable.")
            else:
                blockers.append("No document date for the month folder.")

        elif token == "date":
            if facts.document_date:
                values["date"] = facts.document_date
            else:
                blockers.append("No document date.")

        else:
            blockers.append(f"Path template uses unknown token '{{{token}}}'.")

    if blockers:
        return None, blockers, notes

    path = doc_type.path.format(**values)

    for protected in config.drive.protected_paths:
        if path == protected or path.startswith(protected.rstrip("/") + "/"):
            blockers.append(
                f"Destination '{path}' is under protected path '{protected}'. "
                f"The agent does not write there."
            )
            return None, blockers, notes

    if doc_type.note:
        notes.append(doc_type.note)

    return path, blockers, notes


def entity_flags(facts: DocumentFacts, config: Config) -> tuple[list[str], bool]:
    """Checks tied to the live entity situation.

    Returns (notes, force_review). The name change and the dormant second
    entity are live issues in this business, so documents touching either are
    surfaced rather than filed silently.
    """
    notes: list[str] = []
    force_review = False

    former = config.matches_former_name(facts.entity_named)
    if former:
        notes.append(
            f"Document names the business as '{facts.entity_named}' "
            f"({former.name}). Same EIN, same entity - correct as filed. Add to "
            f"the register at 01 > Name Change & Reinstatement (ACTIVE ISSUE)."
        )

    related = config.matches_related_entity(facts.entity_named)
    if related:
        notes.append(
            f"Document names '{related.name}' ({related.status}). This is a "
            f"different entity and does not belong in this BizBox without a decision."
        )
        if related.handling == "review":
            force_review = True

    return notes, force_review


def looks_personal(facts: DocumentFacts) -> bool:
    """Heuristic backstop for personal records reaching a business folder."""
    haystack = " ".join(filter(None, [facts.summary, facts.reasoning, *facts.concerns])).lower()
    return bool(re.search(r"\b(joint return|form 1040|personal|spouse|household)\b", haystack))
