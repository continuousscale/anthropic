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


def test_conformance_detection_makes_reruns_idempotent(config):
    assert is_conforming("2026-08-14_ThirdHorizon_SOW_signed.pdf", config)
    assert is_conforming("2026-08-31_Mercury_Statement_x2345_v2.pdf", config)
    assert not is_conforming("IMG_4471.pdf", config)
    assert not is_conforming("scan (2).pdf", config)
    assert not is_conforming("Third Horizon SOW final.pdf", config)


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


# --- the convention builder ------------------------------------------------


def _convention(config, separator, specs):
    """Swap in a different naming convention, leaving the rest of config alone."""
    from csfiler.config import NameField, NamingConfig

    config.naming = NamingConfig(
        separator=separator,
        banned_tokens=config.naming.banned_tokens,
        fields=[NameField(**s) for s in specs],
    )
    return config


def test_reordering_fields_reorders_the_filename(config, facts_factory):
    _convention(config, "underscore", [
        {"field": "doc_type", "on_missing": "block"},
        {"field": "subject", "on_missing": "use_org"},
        {"field": "date", "format": "%Y-%m-%d", "on_missing": "block"},
    ])
    name, _ = build_name(facts_factory(), config.type_by_id("sow"), config, "a.pdf")
    assert name == "SOW_ThirdHorizon_2026-08-14.pdf"


def test_separator_is_configurable(config, facts_factory):
    for separator, char in [("dash", "-"), ("period", "."), ("space", " ")]:
        _convention(config, separator, [
            {"field": "date", "format": "%Y-%m-%d", "on_missing": "block"},
            {"field": "subject", "on_missing": "use_org"},
            {"field": "doc_type", "on_missing": "block"},
        ])
        name, _ = build_name(facts_factory(), config.type_by_id("sow"), config, "a.pdf")
        assert name == f"2026-08-14{char}ThirdHorizon{char}SOW.pdf"


def test_per_field_date_format(config, facts_factory):
    _convention(config, "dash", [
        {"field": "date", "format": "%Y-%m", "on_missing": "block"},
        {"field": "doc_type", "on_missing": "block"},
    ])
    name, _ = build_name(facts_factory(), config.type_by_id("sow"), config, "a.pdf")
    assert name == "2026-08-SOW.pdf"


def test_missing_field_can_skip_close_the_gap(config, facts_factory):
    _convention(config, "underscore", [
        {"field": "date", "format": "%Y-%m-%d", "on_missing": "block"},
        {"field": "qualifier", "on_missing": "skip"},
        {"field": "doc_type", "on_missing": "block"},
    ])
    name, _ = build_name(
        facts_factory(qualifier=None, is_signed=False), config.type_by_id("sow"), config, "a.pdf"
    )
    assert name == "2026-08-14_SOW.pdf"  # no doubled separator where the gap was


def test_missing_field_can_leave_a_placeholder(config, facts_factory):
    _convention(config, "dash", [
        {"field": "date", "format": "%Y-%m", "on_missing": "block"},
        {"field": "bank", "on_missing": "placeholder", "placeholder": "X"},
        {"field": "doc_type", "on_missing": "block"},
    ])
    name, notes = build_name(
        facts_factory(counterparty=None), config.type_by_id("sow"), config, "a.pdf"
    )
    assert name == "2026-08-X-SOW.pdf"
    assert any("marked 'X'" in n for n in notes)


def test_a_blocking_field_refuses_the_name(config, facts_factory):
    _convention(config, "underscore", [
        {"field": "client", "on_missing": "block"},
        {"field": "doc_type", "on_missing": "block"},
    ])
    with pytest.raises(NameError_):
        build_name(
            facts_factory(counterparty="Nobody In Particular"),
            config.type_by_id("sow"), config, "a.pdf",
        )


def test_client_and_bank_fields_only_match_their_own_group(config, facts_factory):
    """`bank` must not pick up a client, nor `client` a bank."""
    _convention(config, "dash", [
        {"field": "bank", "on_missing": "placeholder", "placeholder": "NOBANK"},
        {"field": "doc_type", "on_missing": "block"},
    ])
    name, _ = build_name(
        facts_factory(counterparty="Third Horizon"), config.type_by_id("sow"), config, "a.pdf"
    )
    assert name == "NOBANK-SOW.pdf"

    _convention(config, "dash", [
        {"field": "bank", "on_missing": "block"},
        {"field": "doc_type", "on_missing": "block"},
    ])
    name, _ = build_name(
        facts_factory(counterparty="Mercury Bank"),
        config.type_by_id("bank_statement"), config, "a.pdf",
    )
    assert name == "Mercury-Statement.pdf"


def test_reproduces_the_reference_tools_example_convention(config, facts_factory):
    """`2026-03-Chase-4567-Statement.pdf` - the format the concept documents."""
    from csfiler.config import Entity

    config.entities["financial_institutions"].append(
        Entity(subject="Chase", aliases=["JPMorgan Chase"])
    )
    _convention(config, "dash", [
        {"field": "date", "format": "%Y-%m", "on_missing": "block"},
        {"field": "bank", "on_missing": "placeholder"},
        {"field": "account", "format": "{last4}", "on_missing": "skip"},
        {"field": "doc_type", "on_missing": "block"},
    ])
    facts = facts_factory(
        document_type="bank_statement", document_date="2026-03-31",
        counterparty="Chase", account_number="****4567",
    )
    name, _ = build_name(
        facts, config.type_by_id("bank_statement"), config, "scan-final-FINAL(2).pdf"
    )
    assert name == "2026-03-Chase-4567-Statement.pdf"


def test_conformance_follows_a_changed_convention(config, facts_factory):
    """Reruns stay idempotent after the firm changes its convention."""
    _convention(config, "dash", [
        {"field": "date", "format": "%Y-%m", "on_missing": "block"},
        {"field": "subject", "on_missing": "use_org"},
        {"field": "doc_type", "on_missing": "block"},
    ])
    name, _ = build_name(facts_factory(), config.type_by_id("sow"), config, "a.pdf")
    assert is_conforming(name, config)
    # A name in the *old* underscore convention is not conforming under this one.
    assert not is_conforming("2026-08-14_ThirdHorizon_SOW_signed.pdf", config)
