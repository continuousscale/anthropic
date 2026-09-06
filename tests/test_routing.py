from csfiler.routing import entity_flags, looks_personal, resolve_path, resolve_year


def test_client_document_routes_to_the_seven_tab_structure(config, facts_factory):
    path, blockers, _ = resolve_path(facts_factory(), config.type_by_id("sow"), config)
    assert path == "05 Clients & Revenue/Clients/ThirdHorizon/1 Agreement"
    assert not blockers


def test_invoice_and_agreement_split_across_tabs(config, facts_factory):
    inv, _, _ = resolve_path(
        facts_factory(document_type="client_invoice"), config.type_by_id("client_invoice"), config
    )
    assert inv.endswith("/4 Invoices & Payments")


def test_return_files_under_its_tax_year_not_its_filing_year(config, facts_factory):
    """Filed March 2026 for tax year 2025 belongs in the 2025 folder."""
    facts = facts_factory(
        document_type="tax_return", document_date="2026-03-15", period_year=2025, counterparty=None
    )
    path, blockers, _ = resolve_path(facts, config.type_by_id("tax_return"), config)
    assert path == "03 Tax/Tax Years/2025/1 Filed Returns"
    assert not blockers


def test_year_falls_back_to_document_date(facts_factory):
    assert resolve_year(facts_factory(document_date="2026-08-14", period_year=None)) == 2026
    assert resolve_year(facts_factory(document_date=None, period_year=None)) is None


def test_statement_routes_into_its_account_year_folder(config, facts_factory):
    facts = facts_factory(
        document_type="bank_statement", counterparty="Mercury", document_date="2026-08-31"
    )
    path, _, _ = resolve_path(facts, config.type_by_id("bank_statement"), config)
    assert path == "04 Money & Banking/Accounts/Mercury/2 Statements/2026"


def test_unknown_client_blocks_instead_of_inventing_a_folder(config, facts_factory):
    path, blockers, _ = resolve_path(
        facts_factory(counterparty="Acme Widgets"), config.type_by_id("sow"), config
    )
    assert path is None
    assert blockers and "not a known client" in blockers[0]


def test_missing_year_blocks_a_tax_filing(config, facts_factory):
    facts = facts_factory(
        document_type="tax_return", document_date=None, period_year=None, counterparty=None
    )
    path, blockers, _ = resolve_path(facts, config.type_by_id("tax_return"), config)
    assert path is None
    assert any("year" in b.lower() for b in blockers)


def test_protected_paths_are_refused(config, facts_factory, monkeypatch):
    doc_type = config.type_by_id("legal_matter_doc").model_copy(
        update={"path": "09 Legal Matters/Legal Holds/{subject}"}
    )
    facts = facts_factory(counterparty="Mercury")
    path, blockers, _ = resolve_path(facts, doc_type, config)
    assert path is None
    assert any("protected" in b for b in blockers)


def test_former_entity_name_is_flagged_not_rewritten(config, facts_factory):
    notes, force_review = entity_flags(
        facts_factory(entity_named="Andover Consulting, LLC"), config
    )
    assert notes and "Name Change" in notes[0]
    assert not force_review  # correct as filed, just needs registering


def test_dormant_sister_entity_forces_review(config, facts_factory):
    notes, force_review = entity_flags(
        facts_factory(entity_named="Sara Dickinson Global LLC"), config
    )
    assert force_review
    assert notes


def test_personal_records_are_detected(config, facts_factory):
    assert looks_personal(facts_factory(summary="A joint return for the 2025 tax year"))
    assert not looks_personal(facts_factory(summary="A statement of work"))
