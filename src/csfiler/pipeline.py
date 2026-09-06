"""Orchestration: scan proposes, apply writes, undo reverses.

The split matters. `scan` performs no writes of any kind - it reads files,
classifies them, and returns proposals. `apply` takes proposals a human (or a
confidence threshold) has cleared and is the only place a file is renamed or
moved. That is why a first run against a real Drive is safe by construction
rather than by remembering to pass a flag.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .classify import Classifier
from .config import Config
from .extract import Unreadable, build_content_blocks
from .journal import Journal
from .models import AppliedAction, Disposition, DocumentFacts, FileRef, Proposal
from .naming import NameError_, build_name, is_conforming, version_for_collision
from .routing import entity_flags, looks_personal, resolve_path
from .storage import Storage

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, FileRef], None]


@dataclass
class ApplyResult:
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)


class Pipeline:
    def __init__(
        self,
        config: Config,
        storage: Storage,
        classifier: Classifier,
        journal: Journal,
    ) -> None:
        self.config = config
        self.storage = storage
        self.classifier = classifier
        self.journal = journal
        # Names claimed earlier in this same batch, so two files proposed into
        # one folder in a single run cannot collide with each other.
        self._reserved: dict[str, set[str]] = {}

    # ---- scan ------------------------------------------------------------

    def scan(
        self,
        folder: str,
        recursive: bool = True,
        limit: int | None = None,
        progress: ProgressFn | None = None,
    ) -> tuple[str, list[Proposal]]:
        """Read a folder and propose an action for every file. Writes nothing."""
        batch_id = self.journal.start_batch(source=folder or "<root>", mode="scan")
        proposals: list[Proposal] = []

        for index, ref in enumerate(self.storage.list_files(folder, recursive=recursive)):
            if limit is not None and index >= limit:
                break
            if progress:
                progress("scanning", ref)
            proposal = self._propose(ref)
            proposals.append(proposal)
            self.journal.record_proposal(batch_id, proposal)

        self.journal.finish_batch(batch_id, summarize(proposals))
        return batch_id, proposals

    def _propose(self, ref: FileRef) -> Proposal:
        """Decide what should happen to one file."""
        if self.config.drive.skip_conforming and is_conforming(ref.name):
            return Proposal(
                file=ref,
                disposition=Disposition.SKIP_CONFORMING,
                notes=["Already follows the naming convention."],
            )

        try:
            data, content_hash = self.storage.download(ref)
        except Exception as exc:
            return Proposal(
                file=ref, disposition=Disposition.ERROR, blockers=[f"Download failed: {exc}"]
            )
        ref.content_hash = content_hash

        seen = self.journal.seen_before(content_hash)
        if seen and seen["file_id"] != ref.id:
            return Proposal(
                file=ref,
                disposition=Disposition.DUPLICATE,
                notes=[
                    (f"Byte-identical to '{seen['name']}' already filed at "
                    f"{seen['path'] or 'an earlier location'}. Not filed again.")
                ],
            )

        try:
            blocks, modality = build_content_blocks(ref.name, ref.mime_type, data)
        except Unreadable as exc:
            return Proposal(
                file=ref, disposition=Disposition.ERROR, blockers=[f"Cannot read file: {exc}"]
            )

        try:
            facts = self.classifier.classify(ref.name, blocks)
        except Exception as exc:
            log.exception("Classification failed for %s", ref.name)
            return Proposal(
                file=ref, disposition=Disposition.ERROR, blockers=[f"Classification failed: {exc}"]
            )

        proposal = self._plan(ref, facts)
        proposal.notes.append(f"Read as {modality}.")

        # Only remember files we are actually prepared to file; an errored or
        # unidentified file should be reconsidered on the next run.
        if proposal.disposition in (Disposition.AUTO, Disposition.REVIEW):
            self.journal.remember_file(content_hash, ref.id, ref.name, ref.parent_path)

        return proposal

    def _plan(self, ref: FileRef, facts: DocumentFacts) -> Proposal:
        """Turn facts into a concrete name, path and disposition."""
        notes: list[str] = []
        blockers: list[str] = []

        excluded = self.config.excluded_by_id(facts.document_type)
        if excluded:
            return Proposal(
                file=ref,
                disposition=Disposition.EXCLUDED,
                facts=facts,
                notes=[excluded.reason.strip()],
            )

        doc_type = self.config.type_by_id(facts.document_type)
        if not doc_type:
            return Proposal(
                file=ref,
                disposition=Disposition.UNIDENTIFIED,
                facts=facts,
                blockers=[
                    f"No filing rule for document type '{facts.document_type}'."
                    if facts.document_type != "unknown"
                    else "Could not identify what this document is."
                ],
            )

        if facts.confidence < self.config.confidence.review:
            return Proposal(
                file=ref,
                disposition=Disposition.UNIDENTIFIED,
                facts=facts,
                blockers=[
                    (f"Confidence {facts.confidence:.2f} is below the "
                    f"{self.config.confidence.review:.2f} threshold.")
                ],
            )

        flag_notes, force_review = entity_flags(facts, self.config)
        notes.extend(flag_notes)

        if looks_personal(facts):
            notes.append(
                "This looks like a personal record. The BizBox holds business "
                "records only - a joint 1040 belongs in the personal Nokbox."
            )
            force_review = True

        # Name
        proposed_name: str | None = None
        try:
            proposed_name, name_notes = build_name(facts, doc_type, self.config, ref.name)
            notes.extend(name_notes)
        except NameError_ as exc:
            blockers.append(f"Cannot build a conforming name: {exc}.")

        # Path
        proposed_path, path_blockers, path_notes = resolve_path(facts, doc_type, self.config)
        blockers.extend(path_blockers)
        notes.extend(path_notes)

        # Collision handling, against both the destination folder and names
        # already claimed earlier in this batch.
        if proposed_name and proposed_path:
            taken = self._taken_names(proposed_path)
            versioned = version_for_collision(proposed_name, taken)
            if versioned != proposed_name:
                notes.append(
                    f"A file named '{proposed_name}' already exists there; "
                    f"versioned to '{versioned}' rather than overwriting."
                )
                proposed_name = versioned
            self._reserved.setdefault(proposed_path, set()).add(proposed_name)

        notes.extend(facts.concerns)

        if blockers:
            disposition = Disposition.REVIEW
        elif force_review or facts.concerns:
            disposition = Disposition.REVIEW
        elif facts.confidence >= self.config.confidence.auto_apply:
            disposition = Disposition.AUTO
        else:
            disposition = Disposition.REVIEW

        return Proposal(
            file=ref,
            disposition=disposition,
            facts=facts,
            proposed_name=proposed_name,
            proposed_path=proposed_path,
            retention=doc_type.retention,
            legal_weight=doc_type.legal_weight,
            alert=doc_type.alert,
            blockers=blockers,
            notes=notes,
        )

    def _taken_names(self, path: str) -> set[str]:
        taken = set(self._reserved.get(path, set()))
        folder_id = self.storage.peek_folder(path)
        if folder_id:
            taken |= self.storage.names_in(folder_id)
        return taken

    # ---- apply -----------------------------------------------------------

    def apply(
        self,
        proposals: Iterable[Proposal],
        batch_id: str,
        progress: ProgressFn | None = None,
    ) -> ApplyResult:
        """Rename and move files. The only method here that writes."""
        result = ApplyResult()

        for proposal in proposals:
            if not proposal.is_actionable:
                result.skipped += 1
                continue
            if not (proposal.renames or proposal.moves):
                result.skipped += 1
                continue

            ref = proposal.file
            if progress:
                progress("applying", ref)

            prior_name = ref.name
            prior_parent_id = ref.parent_id
            prior_parent_path = ref.parent_path
            new_parent_id = prior_parent_id
            # Ids can change as a result of these operations (a local path
            # does; a Drive id does not), so carry the current one forward.
            current_id = ref.id
            renamed = False

            try:
                if proposal.renames and proposal.moves and proposal.proposed_path:
                    # One operation, so the new name is never transiently
                    # applied inside the source folder - where an already
                    # correctly-named file may be sitting under it.
                    new_parent_id = self.storage.ensure_folder(proposal.proposed_path)
                    current_id = self.storage.rename_and_move(
                        current_id,
                        proposal.proposed_name,  # type: ignore[arg-type]
                        new_parent_id,
                        prior_parent_id,
                    )
                elif proposal.renames and proposal.proposed_name:
                    current_id = self.storage.rename(current_id, proposal.proposed_name)
                    renamed = True
                elif proposal.moves and proposal.proposed_path:
                    new_parent_id = self.storage.ensure_folder(proposal.proposed_path)
                    current_id = self.storage.move(current_id, new_parent_id, prior_parent_id)

                self.journal.record_action(
                    AppliedAction(
                        batch_id=batch_id,
                        # The id as it stands after the operation, so undo can
                        # find the file without re-listing the folder.
                        file_id=current_id,
                        prior_name=prior_name,
                        prior_parent_id=prior_parent_id,
                        prior_parent_path=prior_parent_path,
                        new_name=proposal.proposed_name or prior_name,
                        new_parent_id=new_parent_id,
                        new_parent_path=proposal.proposed_path or prior_parent_path,
                        applied_at=_dt.datetime.now(_dt.timezone.utc),
                    )
                )
                result.applied += 1

            except Exception as exc:
                log.exception("Failed to apply %s", prior_name)
                result.failed += 1
                result.errors.append((prior_name, str(exc)))
                if renamed:
                    # Put the name back so a partial failure leaves no trace.
                    try:
                        self.storage.rename(current_id, prior_name)
                    except Exception:
                        result.errors.append(
                            (prior_name, ("rename succeeded but rollback failed - "
                                         f"file is now named {proposal.proposed_name}"))
                        )

        return result

    # ---- undo ------------------------------------------------------------

    def undo(self, batch_id: str, progress: ProgressFn | None = None) -> ApplyResult:
        """Reverse every action in a batch, newest first."""
        result = ApplyResult()

        for row in self.journal.undoable_actions(batch_id):
            try:
                current_id = row["file_id"]
                moved = (
                    row["new_parent_id"] != row["prior_parent_id"] and row["prior_parent_id"]
                )
                renamed_back = row["new_name"] != row["prior_name"]

                if moved and renamed_back:
                    self.storage.rename_and_move(
                        current_id,
                        row["prior_name"],
                        row["prior_parent_id"],
                        row["new_parent_id"],
                    )
                elif moved:
                    self.storage.move(
                        current_id, row["prior_parent_id"], row["new_parent_id"]
                    )
                elif renamed_back:
                    self.storage.rename(current_id, row["prior_name"])
                self.journal.mark_undone(row["id"])
                result.applied += 1
            except Exception as exc:
                log.exception("Undo failed for %s", row["new_name"])
                result.failed += 1
                result.errors.append((row["new_name"], str(exc)))

        return result


def summarize(proposals: Iterable[Proposal]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in proposals:
        counts[p.disposition.value] = counts.get(p.disposition.value, 0) + 1
    return counts
