"""The Claude call that reads a document and reports what it is.

Two decisions shape this module:

*   The model returns *facts*, never a filename or a folder. Naming and routing
    are deterministic code driven by config, so a convention change is a config
    edit rather than a prompt rewrite, and the model cannot invent a name that
    breaks the convention.
*   The system prompt is byte-stable across every file in a run and is cached.
    The type catalog and entity list are long; without caching, each file would
    re-pay for them. With it, only the document itself is new input.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import anthropic

from .config import Config
from .models import DocumentFacts

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"

# Classification is a bounded reading task, not open-ended reasoning, and it
# runs once per file across potentially thousands of files. Medium effort is
# the cost-conscious setting for this workload shape; raise it with
# --effort if a folder of hard scans is classifying poorly.
DEFAULT_EFFORT = "medium"


class SupportsParse(Protocol):
    """The slice of the Anthropic client this module uses, so tests can fake it."""

    @property
    def messages(self) -> Any: ...


def build_system_prompt(config: Config, corrections: list[dict[str, str]] | None = None) -> str:
    """Assemble the classifier's instructions from config.

    Kept byte-stable for a given config so the prompt cache hits on every file
    after the first.
    """
    org = config.organization
    former = ", ".join(f.name for f in org.former_names) or "none"

    lines: list[str] = [
        (f"You identify business documents for {org.legal_name}, a solo consulting "
        f"firm in Washington State taxed as an S corporation."),
        "",
        ("Your job is to read a document and report what it is. You do not choose "
        "filenames or folders - other code does that from the facts you report."),
        "",
        "## Context you need",
        "",
        f"- Our business is {org.legal_name}.",
        (f"- It was formerly named: {former}. This was a rename, not a new entity: "
        f"same EIN, same company. Documents naming the old entity are correct as "
        f"filed. Report the name exactly as printed and do not normalize it."),
    ]

    for rel in org.related_entities:
        lines.append(
            f"- '{rel.name}' is a different, {rel.status} entity. If a document "
            f"names it, say so in your concerns."
        )

    lines += [
        "",
        "## Known counterparties",
        "",
        ("Report `counterparty` as printed on the document. These are the names "
        "already known to the filing system, for your reference:"),
        "",
    ]
    for group, members in config.entities.items():
        names = ", ".join(e.subject for e in members)
        lines.append(f"- {group.replace('_', ' ')}: {names}")

    lines += [
        "",
        "## Document type catalog",
        "",
        "Choose exactly one `document_type` id from this list:",
        "",
    ]
    for dt in config.document_types:
        note = f" ({dt.note.strip()})" if dt.note else ""
        lines.append(f"- `{dt.id}`: {dt.label}{note}")

    lines += [
        "",
        ("These types are deliberately NOT filed by this system. If a document is "
        "one of them, return its id as the document_type and explain in concerns:"),
        "",
    ]
    for et in config.excluded_types:
        lines.append(f"- `{et.id}`: {et.label} - {et.reason.strip()}")

    lines += [
        "- `unknown`: nothing in the catalog fits this document.",
        "",
        "## How to read a document",
        "",
        ("1. The existing filename is unreliable and is often something like "
        "`IMG_4471.pdf` or `scan (2).pdf`. Read the document itself. Where the "
        "filename and the contents disagree, the contents win."),
        "2. Prefer the date printed on the document over any date in the filename.",
        ("3. `document_date` and `period_year` are different things. A return filed "
        "in March 2026 covering tax year 2025 has document_date 2026-03-15 and "
        "period_year 2025. A statement for August 2026 has both in 2026."),
        ("4. Distinguish a registration from a return: registering an agency account "
        "is a `registration`; every filing made afterward is a return."),
        ("5. Distinguish reusable collateral from a client deliverable: if it carries "
        "a specific client's numbers, it is a deliverable."),
        ("6. Be calibrated about confidence. A blurry scan whose total you cannot "
        "read is not a 0.95. Under 0.6 means a human should look at it, and that "
        "is a useful answer, not a failure."),
        ("7. Never guess a date you cannot see. Return null instead. A wrong date "
        "silently misfiles the document into the wrong year."),
    ]

    if corrections:
        lines += [
            "",
            "## Corrections from previous runs",
            "",
            "A human corrected these classifications. Apply the same judgement:",
            "",
        ]
        for c in corrections:
            lines.append(
                f"- A document described as \"{c['summary']}\" was classified "
                f"`{c['was']}` but is actually `{c['corrected_to']}`."
            )

    return "\n".join(lines)


class Classifier:
    """Classifies one file at a time against the configured catalog."""

    def __init__(
        self,
        config: Config,
        client: SupportsParse | None = None,
        corrections: list[dict[str, str]] | None = None,
        effort: str = DEFAULT_EFFORT,
        model: str = MODEL,
    ) -> None:
        self.config = config
        self.client = client or anthropic.Anthropic()
        self.effort = effort
        self.model = model
        self.system_prompt = build_system_prompt(config, corrections)
        self.usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }

    def classify(
        self, filename: str, content_blocks: list[dict[str, Any]]
    ) -> DocumentFacts:
        """Return the facts read off one document.

        Raises anthropic errors through to the caller, which records them
        against the file rather than aborting the batch.
        """
        blocks: list[dict[str, Any]] = [
            *content_blocks,
            {
                "type": "text",
                "text": (
                    f"The file is currently named `{filename}`. Treat that name as "
                    f"unreliable evidence. Identify the document from its contents "
                    f"and report the facts."
                ),
            },
        ]

        response = self.client.messages.parse(
            model=self.model,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": self.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": blocks}],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            output_format=DocumentFacts,
        )

        self._record_usage(response)

        facts = response.parsed_output
        if facts is None:
            raise RuntimeError(
                f"Classifier returned no parsed output for {filename} "
                f"(stop_reason={getattr(response, 'stop_reason', None)})"
            )
        return facts

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if not usage:
            return
        for key in self.usage:
            self.usage[key] += getattr(usage, key, 0) or 0

    def usage_summary(self) -> str:
        cached = self.usage["cache_read_input_tokens"]
        fresh = self.usage["input_tokens"]
        total_in = cached + fresh
        pct = (cached / total_in * 100) if total_in else 0.0
        return (
            f"{total_in:,} input tokens ({pct:.0f}% served from cache), "
            f"{self.usage['output_tokens']:,} output tokens"
        )
