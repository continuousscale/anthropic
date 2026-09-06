"""Durable record of everything the agent proposed, applied, and could reverse.

Every applied action stores the file's prior name and prior parent, which is
what makes `csfiler undo` exact rather than best-effort. Renaming a thousand
files is only safe if putting them all back is one command.

The same store holds content hashes (so a document that was already filed is
recognized instead of filed twice) and human corrections (which are fed back
into the classifier's prompt on later runs).
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable

from .models import AppliedAction, Disposition, DocumentFacts, FileRef, Proposal

DEFAULT_DB_PATH = Path(".csfiler") / "journal.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id           TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    source       TEXT NOT NULL,
    mode         TEXT NOT NULL,
    summary      TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id             TEXT PRIMARY KEY,
    batch_id       TEXT NOT NULL REFERENCES batches(id),
    file_id        TEXT NOT NULL,
    file_name      TEXT NOT NULL,
    parent_id      TEXT,
    parent_path    TEXT,
    mime_type      TEXT,
    disposition    TEXT NOT NULL,
    facts          TEXT,
    proposed_name  TEXT,
    proposed_path  TEXT,
    blockers       TEXT NOT NULL DEFAULT '[]',
    notes          TEXT NOT NULL DEFAULT '[]',
    review_action  TEXT,
    reviewed_at    TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    id                 TEXT PRIMARY KEY,
    batch_id           TEXT NOT NULL REFERENCES batches(id),
    file_id            TEXT NOT NULL,
    prior_name         TEXT NOT NULL,
    prior_parent_id    TEXT,
    prior_parent_path  TEXT,
    new_name           TEXT NOT NULL,
    new_parent_id      TEXT,
    new_parent_path    TEXT,
    applied_at         TEXT NOT NULL,
    undone_at          TEXT
);

CREATE TABLE IF NOT EXISTS corrections (
    id             TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    was_type       TEXT NOT NULL,
    corrected_to   TEXT NOT NULL,
    summary        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_files (
    content_hash  TEXT PRIMARY KEY,
    file_id       TEXT NOT NULL,
    name          TEXT NOT NULL,
    path          TEXT,
    first_seen    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proposals_batch ON proposals(batch_id);
CREATE INDEX IF NOT EXISTS idx_actions_batch ON actions(batch_id);
CREATE INDEX IF NOT EXISTS idx_actions_file ON actions(file_id);
"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class Journal:
    """SQLite-backed audit log. Safe to open concurrently for reads."""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- batches ---------------------------------------------------------

    def start_batch(self, source: str, mode: str) -> str:
        batch_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.conn.execute(
            "INSERT INTO batches (id, started_at, source, mode) VALUES (?,?,?,?)",
            (batch_id, _now(), source, mode),
        )
        return batch_id

    def finish_batch(self, batch_id: str, summary: dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE batches SET finished_at=?, summary=? WHERE id=?",
            (_now(), json.dumps(summary), batch_id),
        )

    def list_batches(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM batches ORDER BY started_at DESC LIMIT ?", (limit,)
            )
        )

    # ---- proposals -------------------------------------------------------

    def record_proposal(self, batch_id: str, proposal: Proposal) -> str:
        proposal_id = uuid.uuid4().hex
        self.conn.execute(
            """INSERT INTO proposals
               (id, batch_id, file_id, file_name, parent_id, parent_path, mime_type,
                disposition, facts, proposed_name, proposed_path, blockers, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                proposal_id,
                batch_id,
                proposal.file.id,
                proposal.file.name,
                proposal.file.parent_id,
                proposal.file.parent_path,
                proposal.file.mime_type,
                proposal.disposition.value,
                proposal.facts.model_dump_json() if proposal.facts else None,
                proposal.proposed_name,
                proposal.proposed_path,
                json.dumps(proposal.blockers),
                json.dumps(proposal.notes),
            ),
        )
        return proposal_id

    def pending_review(self, batch_id: str | None = None) -> list[sqlite3.Row]:
        """Proposals awaiting a human decision."""
        sql = (
            "SELECT * FROM proposals WHERE disposition='review' AND review_action IS NULL"
        )
        params: tuple[Any, ...] = ()
        if batch_id:
            sql += " AND batch_id=?"
            params = (batch_id,)
        return list(self.conn.execute(sql + " ORDER BY file_name", params))

    def resolve_proposal(
        self,
        proposal_id: str,
        action: str,
        proposed_name: str | None = None,
        proposed_path: str | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE proposals
               SET review_action=?, reviewed_at=?,
                   proposed_name=COALESCE(?, proposed_name),
                   proposed_path=COALESCE(?, proposed_path)
               WHERE id=?""",
            (action, _now(), proposed_name, proposed_path, proposal_id),
        )

    def get_proposal(self, proposal_id: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        return cur.fetchone()

    def load_proposals(
        self, batch_id: str, dispositions: Iterable[str] | None = None
    ) -> list[tuple[str, Proposal]]:
        """Rebuild proposals from a previous scan, so apply can run separately.

        Returns (proposal_id, Proposal) pairs. A proposal a reviewer edited
        comes back with the edited name and path, since those columns are
        updated in place by `resolve_proposal`.
        """
        sql = "SELECT * FROM proposals WHERE batch_id=?"
        params: list[Any] = [batch_id]
        if dispositions:
            placeholders = ",".join("?" for _ in dispositions)
            sql += f" AND disposition IN ({placeholders})"
            params.extend(dispositions)

        out: list[tuple[str, Proposal]] = []
        for row in self.conn.execute(sql + " ORDER BY file_name", params):
            out.append((row["id"], _row_to_proposal(row)))
        return out

    # ---- actions and undo -----------------------------------------------

    def record_action(self, action: AppliedAction) -> None:
        self.conn.execute(
            """INSERT INTO actions
               (id, batch_id, file_id, prior_name, prior_parent_id, prior_parent_path,
                new_name, new_parent_id, new_parent_path, applied_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex,
                action.batch_id,
                action.file_id,
                action.prior_name,
                action.prior_parent_id,
                action.prior_parent_path,
                action.new_name,
                action.new_parent_id,
                action.new_parent_path,
                action.applied_at.isoformat(),
            ),
        )

    def undoable_actions(self, batch_id: str) -> list[sqlite3.Row]:
        """Actions from a batch that have not already been reversed.

        Returned newest-first so an undo unwinds in reverse order, which
        matters when two files in one batch swapped names.
        """
        return list(
            self.conn.execute(
                "SELECT * FROM actions WHERE batch_id=? AND undone_at IS NULL "
                "ORDER BY applied_at DESC",
                (batch_id,),
            )
        )

    def rename_history(
        self, query: str | None = None, limit: int = 50
    ) -> list[sqlite3.Row]:
        """Every rename recorded, newest first, with its original name.

        The original name is never lost: it is written here before the rename
        is applied, so it survives even if the file is renamed again later.
        `query` matches either the original or the current name.
        """
        sql = "SELECT * FROM actions"
        params: list[Any] = []
        if query:
            sql += " WHERE prior_name LIKE ? OR new_name LIKE ?"
            params += [f"%{query}%", f"%{query}%"]
        sql += " ORDER BY applied_at DESC LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def original_name(self, file_id: str) -> str | None:
        """The name a file had before this agent first touched it."""
        cur = self.conn.execute(
            "SELECT prior_name FROM actions WHERE file_id=? "
            "ORDER BY applied_at ASC LIMIT 1",
            (file_id,),
        )
        row = cur.fetchone()
        return row["prior_name"] if row else None

    def mark_undone(self, action_id: str) -> None:
        self.conn.execute(
            "UPDATE actions SET undone_at=? WHERE id=?", (_now(), action_id)
        )

    # ---- duplicates ------------------------------------------------------

    def seen_before(self, content_hash: str) -> sqlite3.Row | None:
        cur = self.conn.execute(
            "SELECT * FROM seen_files WHERE content_hash=?", (content_hash,)
        )
        return cur.fetchone()

    def remember_file(
        self, content_hash: str, file_id: str, name: str, path: str | None
    ) -> None:
        self.conn.execute(
            """INSERT INTO seen_files (content_hash, file_id, name, path, first_seen)
               VALUES (?,?,?,?,?)
               ON CONFLICT(content_hash) DO NOTHING""",
            (content_hash, file_id, name, path, _now()),
        )

    # ---- corrections -----------------------------------------------------

    def record_correction(self, was_type: str, corrected_to: str, summary: str) -> None:
        """Remember that a human overrode a classification.

        These are replayed into the classifier's system prompt on later runs,
        so the same mistake is not repeated on the next quarter's documents.
        """
        self.conn.execute(
            "INSERT INTO corrections (id, created_at, was_type, corrected_to, summary) "
            "VALUES (?,?,?,?,?)",
            (uuid.uuid4().hex, _now(), was_type, corrected_to, summary),
        )

    def recent_corrections(self, limit: int = 20) -> list[dict[str, str]]:
        rows = self.conn.execute(
            "SELECT was_type, corrected_to, summary FROM corrections "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [
            {"was": r["was_type"], "corrected_to": r["corrected_to"], "summary": r["summary"]}
            for r in rows
        ]


def _row_to_proposal(row: sqlite3.Row) -> Proposal:
    """Rebuild a Proposal from its journal row."""
    facts = None
    if row["facts"]:
        facts = DocumentFacts.model_validate_json(row["facts"])
    return Proposal(
        file=FileRef(
            id=row["file_id"],
            name=row["file_name"],
            mime_type=row["mime_type"] or "application/octet-stream",
            parent_id=row["parent_id"],
            parent_path=row["parent_path"],
        ),
        disposition=Disposition(row["disposition"]),
        facts=facts,
        proposed_name=row["proposed_name"],
        proposed_path=row["proposed_path"],
        blockers=json.loads(row["blockers"] or "[]"),
        notes=json.loads(row["notes"] or "[]"),
        review_action=row["review_action"],
    )
