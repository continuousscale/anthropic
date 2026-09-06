import pytest

from csfiler.naming import (
    NameError_,
    build_name,
    is_conforming,
    pascal,
    redact_account,
    version_for_collision,
)


def test_renames_a_camera_filename(config, facts_factory):
    name, _ = build_name(
        facts_factory(is_signed=True), config.type_by_id("sow"), config, "IMG_4471.pdf"
    )
    assert name == "2026-08-14_ThirdHorizon_SOW_signed.pdf"


def test_client_aliases_collapse_to_one_subject(config, facts_factory):
    for alias in ["Third Horizon", "Third Horizon Partners", "3rd Horizon"]:
        name, _ = build_name(
            facts_factory(counterparty=alias), config.type_by_id("sow"), config, "a.pdf"
        )
        assert name.startswith("2026-08-14_ThirdHorizon_")


def test_account_number_is_redacted_to_last_four(config, facts_factory):
    facts = facts_factory(
        document_type="bank_statement",
        counterparty="Mercury Bank",
        account_number="8123456789012345",
        document_date="2026-08-31",
    )
    name, _ = build_name(facts, config.type_by_id("bank_statement"), config, "scan (2).pdf")
    assert name == "2026-08-31_Mercury_Statement_x2345.pdf"
    assert "8123456789012345" not in name


def test_full_account_number_never_survives_into_a_name(config, facts_factory):
    """A long digit run is forbidden outright, wherever it came from."""
    facts = facts_factory(counterparty="Acme 123456789012 Holdings")
    name, _ = build_name(facts, config.type_by_id("sow"), config, "a.pdf")
    assert "123456789012" not in name


def test_banned_convention_words_are_stripped(config, facts_factory):
    facts = facts_factory(qualifier="final")
    name, _ = build_name(facts, config.type_by_id("sow"), config, "a.pdf")
    assert "final" not in name.lower()


def test_missing_date_blocks_rather_than_guessing(config, facts_factory):
    with pytest.raises(NameError_):
        build_name(facts_factory(document_date=None), config.type_by_id("sow"), config, "a.pdf")


def test_conformance_detection_makes_reruns_idempotent():
    assert is_conforming("2026-08-14_ThirdHorizon_SOW_signed.pdf")
    assert is_conforming("2026-08-31_Mercury_Statement_x2345_v2.pdf")
    assert not is_conforming("IMG_4471.pdf")
    assert not is_conforming("scan (2).pdf")
    assert not is_conforming("Third Horizon SOW final.pdf")


def test_collision_versions_instead_of_overwriting():
    taken = {"a.pdf", "a_v2.pdf", "a_v3.pdf"}
    assert version_for_collision("a.pdf", taken) == "a_v4.pdf"
    assert version_for_collision("b.pdf", taken) == "b.pdf"


def test_redact_account_handles_masked_and_short_inputs():
    assert redact_account("****4471") == "x4471"
    assert redact_account("1234-5678-9012-3456") == "x3456"
    assert redact_account("12") is None
    assert redact_account(None) is None


def test_pascal_case():
    assert pascal("Third Horizon Partners") == "ThirdHorizonPartners"
    assert pascal("good-skin clinics") == "GoodSkinClinics"
