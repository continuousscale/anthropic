"""End-to-end tests over a real local directory tree with a faked classifier."""

from pathlib import Path

import pytest

from csfiler.classify import Classifier
from csfiler.journal import Journal
from csfiler.models import Disposition
from csfiler.pipeline import Pipeline
from csfiler.storage import LocalStorage


def write_pdf(path: Path, body: str) -> None:
    """Write bytes that pass the PDF magic-byte check."""
    path.write_bytes(b"%PDF-1.4\n" + body.encode() + b"\n%%EOF")


@pytest.fixture
def workspace(tmp_path):
    """A BizBox root with an Inbox of badly named files."""
    inbox = tmp_path / "Inbox"
    inbox.mkdir()
    return tmp_path, inbox


def make_pipeline(config, root, queue, fake_client, tmp_path):
    storage = LocalStorage(root)
    classifier = Classifier(config, client=fake_client(queue))
    journal = Journal(tmp_path / "journal.db")
    return Pipeline(config, storage, classifier, journal), storage, journal


def test_scan_proposes_but_writes_nothing(config, workspace, facts_factory, fake_client, tmp_path):
    root, inbox = workspace
    write_pdf(inbox / "IMG_4471.pdf", "scan of a signed SOW")

    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(is_signed=True)], fake_client, tmp_path
    )
    _batch, proposals = pipeline.scan("Inbox")

    assert len(proposals) == 1
    p = proposals[0]
    assert p.disposition is Disposition.AUTO
    assert p.proposed_name == "2026-08-14_ThirdHorizon_SOW_signed.pdf"
    assert p.proposed_path == "05 Clients & Revenue/Clients/ThirdHorizon/1 Agreement"
    # Nothing on disk changed.
    assert (inbox / "IMG_4471.pdf").exists()
    assert not (root / "05 Clients & Revenue").exists()


def test_apply_renames_moves_and_undo_restores_exactly(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    original = inbox / "IMG_4471.pdf"
    write_pdf(original, "scan of a signed SOW")

    pipeline, _, _journal = make_pipeline(
        config, root, [facts_factory(is_signed=True)], fake_client, tmp_path
    )
    batch, proposals = pipeline.scan("Inbox")

    result = pipeline.apply(proposals, batch)
    assert result.applied == 1 and result.failed == 0

    filed = root / "05 Clients & Revenue/Clients/ThirdHorizon/1 Agreement" / \
        "2026-08-14_ThirdHorizon_SOW_signed.pdf"
    assert filed.exists()
    assert not original.exists()

    undo = pipeline.undo(batch)
    assert undo.applied == 1 and undo.failed == 0
    assert original.exists(), "undo must restore the original name and location"
    assert not filed.exists()


def test_low_confidence_is_left_alone(config, workspace, facts_factory, fake_client, tmp_path):
    root, inbox = workspace
    write_pdf(inbox / "blurry.pdf", "unreadable")

    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(confidence=0.3)], fake_client, tmp_path
    )
    _batch, proposals = pipeline.scan("Inbox")

    assert proposals[0].disposition is Disposition.UNIDENTIFIED
    assert proposals[0].proposed_name is None
    assert (inbox / "blurry.pdf").exists()


def test_mid_confidence_goes_to_review_not_auto(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "maybe.pdf", "a document")

    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(confidence=0.75)], fake_client, tmp_path
    )
    _batch, proposals = pipeline.scan("Inbox")
    assert proposals[0].disposition is Disposition.REVIEW
    assert proposals[0].proposed_name is not None  # a proposal exists, it just needs a human


def test_apply_skips_proposals_still_awaiting_review(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "maybe.pdf", "a document")

    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(confidence=0.75)], fake_client, tmp_path
    )
    batch, proposals = pipeline.scan("Inbox")
    # Reviewer has not approved it, but apply() is called on the raw scan output.
    result = pipeline.apply([p for p in proposals if p.disposition is Disposition.AUTO], batch)
    assert result.applied == 0
    assert (inbox / "maybe.pdf").exists()


def test_conforming_files_are_skipped_so_reruns_are_idempotent(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "2026-08-14_ThirdHorizon_SOW_signed.pdf", "already filed")

    pipeline, _, _ = make_pipeline(config, root, [], fake_client, tmp_path)
    _batch, proposals = pipeline.scan("Inbox")

    assert proposals[0].disposition is Disposition.SKIP_CONFORMING
    # The classifier was never called - no API spend on a second pass.
    assert pipeline.classifier.client.messages.calls == []


def test_duplicate_content_is_detected_not_filed_twice(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "a.pdf", "identical bytes")
    write_pdf(inbox / "b.pdf", "identical bytes")

    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(), facts_factory()], fake_client, tmp_path
    )
    _batch, proposals = pipeline.scan("Inbox")

    dispositions = sorted(p.disposition.value for p in proposals)
    assert "duplicate" in dispositions


def test_two_files_landing_on_one_name_get_versioned(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "one.pdf", "first distinct document")
    write_pdf(inbox / "two.pdf", "second distinct document")

    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(is_signed=True), facts_factory(is_signed=True)],
        fake_client, tmp_path,
    )
    batch, proposals = pipeline.scan("Inbox")
    names = [p.proposed_name for p in proposals]
    assert names[0] == "2026-08-14_ThirdHorizon_SOW_signed.pdf"
    assert names[1] == "2026-08-14_ThirdHorizon_SOW_signed_v2.pdf"

    pipeline.apply(proposals, batch)
    filed = root / "05 Clients & Revenue/Clients/ThirdHorizon/1 Agreement"
    assert sorted(p.name for p in filed.iterdir()) == names


def test_excluded_types_are_never_filed(config, workspace, facts_factory, fake_client, tmp_path):
    root, inbox = workspace
    (inbox / "standup.mp4.txt").write_text("transcript")

    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(document_type="meeting_recording")], fake_client, tmp_path
    )
    _batch, proposals = pipeline.scan("Inbox")

    assert proposals[0].disposition is Disposition.EXCLUDED
    assert proposals[0].proposed_name is None
    assert "not records" in proposals[0].notes[0]


def test_unknown_client_becomes_a_review_with_the_reason_attached(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "sow.pdf", "an SOW for a new client")

    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(counterparty="Acme Widgets")], fake_client, tmp_path
    )
    _batch, proposals = pipeline.scan("Inbox")

    p = proposals[0]
    assert p.disposition is Disposition.REVIEW
    assert p.proposed_path is None
    assert any("not a known client" in b for b in p.blockers)


def test_unreadable_file_is_reported_not_crashed(config, workspace, fake_client, tmp_path):
    root, inbox = workspace
    (inbox / "archive.zip").write_bytes(b"PK\x03\x04nonsense")

    pipeline, _, _ = make_pipeline(config, root, [], fake_client, tmp_path)
    _batch, proposals = pipeline.scan("Inbox")

    assert proposals[0].disposition is Disposition.ERROR
    assert proposals[0].blockers


def test_classifier_failure_does_not_abort_the_batch(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "a.pdf", "doc one")
    write_pdf(inbox / "b.pdf", "doc two")

    client = fake_client([facts_factory()])  # only one response for two files
    from csfiler.classify import Classifier as C

    storage = LocalStorage(root)
    pipeline = Pipeline(config, storage, C(config, client=client), Journal(tmp_path / "j.db"))
    _batch, proposals = pipeline.scan("Inbox")

    assert len(proposals) == 2
    assert {p.disposition for p in proposals} == {Disposition.AUTO, Disposition.ERROR}


def test_journal_records_every_proposal_for_audit(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "a.pdf", "doc")

    pipeline, _, journal = make_pipeline(config, root, [facts_factory()], fake_client, tmp_path)
    batch, _ = pipeline.scan("Inbox")

    rows = list(journal.conn.execute("SELECT * FROM proposals WHERE batch_id=?", (batch,)))
    assert len(rows) == 1
    assert rows[0]["file_name"] == "a.pdf"
    assert rows[0]["proposed_name"].endswith(".pdf")


def test_corrupt_office_file_does_not_abort_the_batch(
    config, workspace, facts_factory, fake_client, tmp_path
):
    """A truncated .docx must degrade to one errored file, not kill the run."""
    root, inbox = workspace
    (inbox / "truncated.docx").write_bytes(b"not really a docx")
    write_pdf(inbox / "good.pdf", "a readable document")

    pipeline, _, _ = make_pipeline(config, root, [facts_factory()], fake_client, tmp_path)
    _batch, proposals = pipeline.scan("Inbox")

    by_name = {p.file.name: p for p in proposals}
    assert by_name["truncated.docx"].disposition is Disposition.ERROR
    assert by_name["good.pdf"].disposition is Disposition.AUTO


def test_file_with_a_lying_pdf_extension_is_rejected_before_the_api(
    config, workspace, fake_client, tmp_path
):
    """Never spend an API call on bytes that are not the format they claim."""
    root, inbox = workspace
    (inbox / "renamed.pdf").write_bytes(b"<html>Error 404</html>")

    pipeline, _, _ = make_pipeline(config, root, [], fake_client, tmp_path)
    _batch, proposals = pipeline.scan("Inbox")

    assert proposals[0].disposition is Disposition.ERROR
    assert "not a PDF" in proposals[0].blockers[0]
    assert pipeline.classifier.client.messages.calls == []


def test_target_name_already_present_in_the_source_folder(
    config, workspace, facts_factory, fake_client, tmp_path
):
    """The rename must not collide with a correctly-named file in the inbox.

    Renaming in place and then moving fails here: the new name is briefly
    applied inside the source folder, which already holds that exact name.
    """
    root, inbox = workspace
    write_pdf(inbox / "2026-08-14_ThirdHorizon_SOW_signed.pdf", "the already-filed one")
    write_pdf(inbox / "Document(3).pdf", "a distinct second copy")

    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(is_signed=True)], fake_client, tmp_path
    )
    batch, proposals = pipeline.scan("Inbox")

    target = next(p for p in proposals if p.file.name == "Document(3).pdf")
    assert target.disposition is Disposition.AUTO

    result = pipeline.apply([target], batch)
    assert result.failed == 0, result.errors
    assert result.applied == 1

    filed = root / "05 Clients & Revenue/Clients/ThirdHorizon/1 Agreement"
    assert (filed / "2026-08-14_ThirdHorizon_SOW_signed.pdf").exists()
    # The conforming file stays untouched in the inbox.
    assert (inbox / "2026-08-14_ThirdHorizon_SOW_signed.pdf").exists()


# --- review modes ----------------------------------------------------------


def test_just_do_it_applies_what_by_confidence_would_queue(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "a.pdf", "a document")

    config.review.mode = "just_do_it"
    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(confidence=0.75)], fake_client, tmp_path
    )
    _batch, proposals = pipeline.scan("Inbox")
    assert proposals[0].disposition is Disposition.AUTO


def test_just_do_it_still_stops_on_low_confidence(
    config, workspace, facts_factory, fake_client, tmp_path
):
    """Hands-off is not unsupervised: an uncertain file still asks."""
    root, inbox = workspace
    write_pdf(inbox / "a.pdf", "a blurry document")

    config.review.mode = "just_do_it"
    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(confidence=0.30)], fake_client, tmp_path
    )
    _batch, proposals = pipeline.scan("Inbox")
    assert proposals[0].disposition is Disposition.UNIDENTIFIED


def test_just_do_it_still_stops_on_a_flagged_concern(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "a.pdf", "a document")

    config.review.mode = "just_do_it"
    pipeline, _, _ = make_pipeline(
        config, root,
        [facts_factory(confidence=0.99, concerns=["The date is handwritten."])],
        fake_client, tmp_path,
    )
    _batch, proposals = pipeline.scan("Inbox")
    assert proposals[0].disposition is Disposition.REVIEW


def test_always_ask_queues_even_a_certain_file(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "a.pdf", "an unmistakable document")

    config.review.mode = "always_ask"
    pipeline, _, _ = make_pipeline(
        config, root, [facts_factory(confidence=0.99)], fake_client, tmp_path
    )
    _batch, proposals = pipeline.scan("Inbox")
    assert proposals[0].disposition is Disposition.REVIEW
    assert proposals[0].proposed_name is not None


# --- original names --------------------------------------------------------


def test_the_original_name_is_always_recoverable(
    config, workspace, facts_factory, fake_client, tmp_path
):
    root, inbox = workspace
    write_pdf(inbox / "IMG_4471.pdf", "a signed SOW")

    pipeline, _, journal = make_pipeline(
        config, root, [facts_factory(is_signed=True)], fake_client, tmp_path
    )
    batch, proposals = pipeline.scan("Inbox")
    pipeline.apply(proposals, batch)

    history = journal.rename_history()
    assert len(history) == 1
    assert history[0]["prior_name"] == "IMG_4471.pdf"
    assert history[0]["new_name"] == "2026-08-14_ThirdHorizon_SOW_signed.pdf"

    # Searchable by either the old or the new name.
    assert journal.rename_history("IMG_4471")
    assert journal.rename_history("ThirdHorizon")
    assert journal.original_name(history[0]["file_id"]) == "IMG_4471.pdf"
