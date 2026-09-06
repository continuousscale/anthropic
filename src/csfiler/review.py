"""Local review queue.

Financial Cents offers a global choice between hands-free renaming and
approving everything. This queue is per-file instead: anything the classifier
was confident about is already applied, and what lands here is only what it was
unsure of, or what tripped a rule - an unknown client, a document naming the
old entity, a possible personal record.

Rejecting a proposal and correcting its type is recorded, and those corrections
are replayed into the classifier's prompt on later runs.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import load_config
from .journal import Journal

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>csfiler review queue</title>
<style>
  :root {{ color-scheme: light dark; --bg:#fbfaf8; --fg:#1a1a19; --muted:#6b6a67;
           --line:#e3e0da; --card:#fff; --accent:#7a5c3e; --warn:#8a5a00; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#191918; --fg:#eeece7; --muted:#9a9792; --line:#33312e;
             --card:#222120; --accent:#c9a37a; --warn:#d0a24a; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:2rem 1.5rem 4rem; background:var(--bg); color:var(--fg);
          font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  .wrap {{ max-width:56rem; margin:0 auto; }}
  h1 {{ font-size:1.35rem; margin:0 0 .25rem; }}
  .sub {{ color:var(--muted); margin-bottom:2rem; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
           padding:1.1rem 1.25rem; margin-bottom:1rem; }}
  .old {{ color:var(--muted); text-decoration:line-through; font-family:ui-monospace,monospace;
          font-size:.85rem; }}
  .row {{ display:flex; gap:.75rem; align-items:baseline; flex-wrap:wrap; }}
  label {{ display:block; font-size:.75rem; text-transform:uppercase; letter-spacing:.05em;
           color:var(--muted); margin:.85rem 0 .3rem; }}
  input[type=text] {{ width:100%; padding:.5rem .6rem; border:1px solid var(--line);
    border-radius:6px; background:var(--bg); color:var(--fg);
    font-family:ui-monospace,SFMono-Regular,monospace; font-size:.87rem; }}
  .why {{ font-size:.87rem; color:var(--muted); margin:.6rem 0 0; }}
  .flag {{ color:var(--warn); }}
  .pill {{ display:inline-block; font-size:.7rem; padding:.15rem .5rem; border-radius:99px;
           border:1px solid var(--line); color:var(--muted); }}
  .actions {{ margin-top:1rem; display:flex; gap:.5rem; }}
  button {{ font:inherit; padding:.45rem .95rem; border-radius:6px; cursor:pointer;
            border:1px solid var(--line); background:var(--card); color:var(--fg); }}
  button.primary {{ background:var(--accent); border-color:var(--accent); color:#fff; }}
  .empty {{ text-align:center; color:var(--muted); padding:4rem 0; }}
  .banner {{ background:var(--card); border:1px solid var(--warn); color:var(--warn);
             border-radius:8px; padding:.8rem 1rem; margin-bottom:1rem; font-size:.9rem; }}
  code {{ font-size:.85rem; }}
</style>
<div class="wrap">
  <h1>Review queue</h1>
  <p class="sub">{count} proposal(s) waiting. Everything the agent was confident
  about has already been handled — these are the ones it wants a second opinion on.</p>
  {cards}
</div>
"""

CARD = """
<form class="card" method="post" action="/resolve">
  <input type="hidden" name="proposal_id" value="{pid}">
  <div class="row">
    <span class="old">{old_name}</span>
    <span class="pill">{doc_type} · confidence {confidence}</span>
  </div>
  <label>Proposed name</label>
  <input type="text" name="proposed_name" value="{new_name}">
  <label>Destination folder</label>
  <input type="text" name="proposed_path" value="{new_path}">
  <label>Document type (correct it if wrong)</label>
  <input type="text" name="document_type" value="{doc_type}">
  <p class="why"><strong>Why:</strong> {reasoning}</p>
  {flags}
  <div class="actions">
    <button class="primary" name="action" value="approved">Approve</button>
    <button name="action" value="edited">Save edits</button>
    <button name="action" value="rejected">Reject</button>
  </div>
</form>
"""


def create_app(
    db_path: str | Path,
    config_path: str | Path | None = None,
    batch_id: str | None = None,
) -> FastAPI:
    app = FastAPI(title="csfiler review")
    config = load_config(config_path)

    def journal() -> Journal:
        return Journal(db_path)

    def valid_category(path: str) -> bool:
        """BizBox rule 1: the category list is closed.

        A typo in an edited destination would otherwise create a fourteenth
        top-level folder, which is exactly what the system forbids.
        """
        head = path.split("/")[0].strip()
        return any(
            head == f"{number} {name}" for number, name in config.categories.items()
        )

    @app.get("/", response_class=HTMLResponse)
    def index(error: str = "") -> str:
        with journal() as j:
            rows = j.pending_review(batch_id)
            cards = "".join(_card(row) for row in rows)
            if not rows:
                cards = (
                    '<p class="empty">Nothing waiting. Run '
                    "<code>csfiler scan</code> to look for more.</p>"
                )
            banner = f'<p class="banner">{html.escape(error)}</p>' if error else ""
            return PAGE.format(count=len(rows), cards=banner + cards)

    @app.post("/resolve")
    def resolve(
        proposal_id: str = Form(...),
        action: str = Form(...),
        proposed_name: str = Form(""),
        proposed_path: str = Form(""),
        document_type: str = Form(""),
    ) -> RedirectResponse:
        if action != "rejected" and proposed_path and not valid_category(proposed_path):
            known = ", ".join(
                f"{n} {name}" for n, name in sorted(config.categories.items())
            )
            return RedirectResponse(
                "/?error="
                + quote(
                    f"'{proposed_path}' does not start with one of the thirteen "
                    f"BizBox categories, so it was not saved. Expected one of: {known}"
                ),
                status_code=303,
            )

        with journal() as j:
            row = j.get_proposal(proposal_id)
            if row:
                # A changed document type is a teaching signal worth keeping.
                facts = json.loads(row["facts"]) if row["facts"] else {}
                original_type = facts.get("document_type")
                if document_type and original_type and document_type != original_type:
                    j.record_correction(
                        was_type=original_type,
                        corrected_to=document_type,
                        summary=facts.get("summary", row["file_name"]),
                    )
            j.resolve_proposal(
                proposal_id,
                action,
                proposed_name=proposed_name or None,
                proposed_path=proposed_path or None,
            )
        return RedirectResponse("/", status_code=303)

    return app


def _card(row: Any) -> str:
    facts = json.loads(row["facts"]) if row["facts"] else {}
    blockers = json.loads(row["blockers"] or "[]")
    notes = json.loads(row["notes"] or "[]")

    flags = ""
    if blockers or notes:
        items = "".join(
            f'<li class="flag">{html.escape(b)}</li>' for b in blockers
        ) + "".join(f"<li>{html.escape(n)}</li>" for n in notes)
        flags = f'<ul class="why">{items}</ul>'

    return CARD.format(
        pid=html.escape(row["id"]),
        old_name=html.escape(row["file_name"]),
        new_name=html.escape(row["proposed_name"] or ""),
        new_path=html.escape(row["proposed_path"] or ""),
        doc_type=html.escape(facts.get("document_type", "unknown")),
        confidence=f"{facts.get('confidence', 0):.2f}",
        reasoning=html.escape(facts.get("reasoning", "—")),
        flags=flags,
    )
