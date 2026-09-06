"""Tests for the review queue web UI."""

import pytest

pytest.importorskip("httpx", reason="TestClient needs httpx")

from fastapi.testclient import TestClient  # noqa: E402

from csfiler.journal import Journal  # noqa: E402
from csfiler.models import Disposition, DocumentFacts, FileRef, Proposal  # noqa: E402
from csfiler.review import create_app  # noqa: E402


@pytest.fixture
def queued(tmp_path):
    """One proposal sitting in the review queue."""
    db = tmp_path / "j.db"
    journal = Journal(db)
    batch = journal.start_batch("Inbox", "scan")
    proposal_id = journal.record_proposal(
        batch,
        Proposal(
            file=FileRef(
                id="f1", name="IMG_1.pdf", mime_type="application/pdf", parent_path="Inbox"
            ),
            disposition=Disposition.REVIEW,
            facts=DocumentFacts(
                document_type="proposal",
                summary="A scope document.",
                confidence=0.72,
                reasoning="Titled 'Proposal' with a pricing table.",
            ),
            proposed_name="2026-01-01_ThirdHorizon_Proposal.pdf",
            proposed_path="05 Clients & Revenue/Clients/ThirdHorizon/2 Proposal & Scope",
        ),
    )
    journal.close()
    return db, proposal_id


def test_queue_lists_pending_proposals(queued):
    db, _ = queued
    response = TestClient(create_app(db)).get("/")
    assert response.status_code == 200
    assert "IMG_1.pdf" in response.text
    assert "2026-01-01_ThirdHorizon_Proposal.pdf" in response.text
    assert "pricing table" in response.text  # the reasoning is shown


def test_approving_records_the_decision(queued):
    db, proposal_id = queued
    client = TestClient(create_app(db))
    response = client.post(
        "/resolve",
        data={
            "proposal_id": proposal_id,
            "action": "approved",
            "proposed_name": "2026-01-01_ThirdHorizon_Proposal.pdf",
            "proposed_path": "05 Clients & Revenue/Clients/ThirdHorizon/2 Proposal & Scope",
            "document_type": "proposal",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert Journal(db).get_proposal(proposal_id)["review_action"] == "approved"


def test_a_path_outside_the_thirteen_categories_is_refused(queued):
    """BizBox rule 1: the category list is closed, so a typo cannot create a 14th."""
    db, proposal_id = queued
    client = TestClient(create_app(db))
    response = client.post(
        "/resolve",
        data={
            "proposal_id": proposal_id,
            "action": "edited",
            "proposed_name": "a.pdf",
            "proposed_path": "99 Invented Category/Somewhere",
            "document_type": "proposal",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    # Nothing was saved.
    assert Journal(db).get_proposal(proposal_id)["review_action"] is None


def test_correcting_the_type_is_remembered_for_later_runs(queued):
    db, proposal_id = queued
    client = TestClient(create_app(db))
    client.post(
        "/resolve",
        data={
            "proposal_id": proposal_id,
            "action": "edited",
            "proposed_name": "2026-01-01_ThirdHorizon_SOW.pdf",
            "proposed_path": "05 Clients & Revenue/Clients/ThirdHorizon/1 Agreement",
            "document_type": "sow",  # a human says it was really an SOW
        },
        follow_redirects=False,
    )
    corrections = Journal(db).recent_corrections()
    assert corrections == [
        {"was": "proposal", "corrected_to": "sow", "summary": "A scope document."}
    ]


def test_learned_corrections_reach_the_next_run_prompt(queued, config):
    """A correction is not just logged - it changes the next classification."""
    from csfiler.classify import build_system_prompt

    db, proposal_id = queued
    TestClient(create_app(db)).post(
        "/resolve",
        data={
            "proposal_id": proposal_id,
            "action": "edited",
            "proposed_name": "x.pdf",
            "proposed_path": "05 Clients & Revenue/Clients/ThirdHorizon/1 Agreement",
            "document_type": "sow",
        },
        follow_redirects=False,
    )
    prompt = build_system_prompt(config, corrections=Journal(db).recent_corrections())
    assert "Corrections from previous runs" in prompt
    assert "is actually `sow`" in prompt


def test_rejecting_does_not_require_a_valid_path(queued):
    db, proposal_id = queued
    response = TestClient(create_app(db)).post(
        "/resolve",
        data={
            "proposal_id": proposal_id,
            "action": "rejected",
            "proposed_name": "",
            "proposed_path": "",
            "document_type": "proposal",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert Journal(db).get_proposal(proposal_id)["review_action"] == "rejected"
