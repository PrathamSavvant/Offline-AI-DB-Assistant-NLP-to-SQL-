from core.defined_knowledge import DefinedKnowledgeRepository
from core.pending_knowledge import PendingKnowledgeRepository
from flask import Flask
from routes.knowledge import bp as knowledge_blueprint


SCHEMA = {"tables": [{"name": "work_orders", "columns": [{"name": "wo_id"}, {"name": "status"}]}]}


class Dictionary:
    def tokenize(self, value):
        return value.replace("_", " ").split()

    def get_all(self):
        return {"WO": {"expansion": "Work Order", "synonyms": [], "aliases": [], "description": "", "example": ""}}


def test_defined_knowledge_migrates_annotation_and_owned_dictionary_term(tmp_path):
    repository = DefinedKnowledgeRepository(tmp_path / "defined.json")
    repository.migrate(SCHEMA, {"work_orders": {"meaning": "Maintenance work", "aliases": ["jobs"]}}, Dictionary())
    entry = repository.table("work_orders")
    assert entry["definition"] == "Maintenance work"
    assert entry["synonyms"] == ["jobs"]
    assert entry["abbreviations"]["WO"]["meaning"] == "Work Order"


def test_defined_knowledge_business_terms_are_table_scoped(tmp_path):
    repository = DefinedKnowledgeRepository(tmp_path / "defined.json")
    term = repository.upsert_business_term("work_orders", {"abbreviation": "WO", "meaning": "Work Order", "column": "wo_id"})
    assert term["column"] == "wo_id"
    assert repository.delete_business_term("work_orders", "WO") is True


def test_pending_knowledge_migrates_and_approves_into_defined_knowledge(tmp_path):
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    pending.migrate({"WO": {"table": "work_orders", "column": "wo_id", "confidence": "low"}})
    term = pending.approve_term("WO", defined, {"meaning": "Work Order"})
    assert term["abbreviation"] == "WO"
    assert defined.table("work_orders")["abbreviations"]["WO"]["meaning"] == "Work Order"


def test_pending_term_requires_meaning_but_never_a_column(tmp_path):
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    pending.migrate({"ADM": {"table": "work_orders", "column": "legacy_column"}})
    contract = pending.business_terms()[0]
    assert "column" not in contract and "suggested_column" not in contract
    try:
        pending.approve_term("ADM", defined, {"table": "work_orders", "context": "Finance category"})
    except ValueError as exc:
        assert "meaning" in str(exc)
    else:
        raise AssertionError("empty meaning must not be accepted")
    term = pending.approve_term("ADM", defined, {"table": "work_orders", "meaning": "Administration", "context": "ignored", "column": "ignored"})
    assert term["column"] == ""
    assert "context" not in defined.table("work_orders")["abbreviations"]["ADM"]


def test_pending_table_context_is_inert_until_accepted(tmp_path):
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    pending.save_table_context("work_orders", "Tracks operational work orders.")
    assert not defined.table("work_orders")["definition"]
    accepted = pending.accept_table_context("work_orders", defined)
    assert accepted["definition"] == "Tracks operational work orders."
    assert pending.table_context("work_orders") == ""


def test_pending_tables_identify_missing_enhancement_fields(tmp_path):
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    assert pending.pending_tables(SCHEMA, defined)[0]["table"] == "work_orders"


def test_pending_tables_put_most_unidentified_tokens_first(tmp_path):
    schema = {"tables": [
        {"name": "alpha", "approx_row_count": 10, "columns": []},
        {"name": "beta", "approx_row_count": 100, "columns": []},
    ]}
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    pending.migrate({
        "A1": {"table": "alpha"}, "A2": {"table": "alpha"},
        "B1": {"table": "beta"},
    })
    rows = pending.pending_tables(schema, defined)
    assert [row["table"] for row in rows] == ["alpha", "beta"]
    assert rows[0]["unidentified_token_count"] == 2


def test_pending_tokens_keep_an_otherwise_defined_table_in_review_queue(tmp_path):
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    defined.update_table("work_orders", {"definition": "Maintenance work orders"})
    pending.migrate({"WO": {"table": "work_orders"}})
    rows = pending.pending_tables(SCHEMA, defined)
    assert rows[0]["table"] == "work_orders"
    assert rows[0]["unidentified_token_count"] == 1


def test_pending_endpoint_searches_normalized_schema_text_and_keeps_token_order(tmp_path):
    schema = {"tables": [
        {"name": "work_equipment", "columns": [{"name": "repair_status"}]},
        {"name": "equipment_repairs", "columns": [{"name": "repair_class"}]},
    ]}
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    pending.migrate({"WE1": {"table": "work_equipment"}, "WE2": {"table": "work_equipment"}, "ER": {"table": "equipment_repairs"}})

    class Cache:
        def __init__(self):
            self.schema, self.relationships, self.ranking, self.table_profiles = schema, {"relationships": []}, {"rankings": []}, {}
        def get_table(self, name):
            return next((item for item in schema["tables"] if item["name"] == name), None)

    app = Flask(__name__)
    app.extensions["services"] = {"pending_knowledge": pending, "defined_knowledge": defined, "cache": Cache()}
    app.register_blueprint(knowledge_blueprint)
    response = app.test_client().get("/api/knowledge/pending?search=work%20equipment")
    rows = response.get_json()["pending_tables"]
    assert [row["table"] for row in rows] == ["work_equipment"]
    assert rows[0]["unidentified_token_count"] == 2


def test_pending_search_prefers_table_names_and_excludes_relationship_profile_text(tmp_path):
    schema = {"tables": [
        {"name": "allergy", "columns": [{"name": "encounter_id"}]},
        {"name": "encounter", "columns": [{"name": "id"}]},
        {"name": "encounter_diagnosis", "columns": [{"name": "id"}]},
        {"name": "encounter_provider", "columns": [{"name": "id"}]},
        {"name": "background", "columns": [{"name": "id"}]},
    ]}
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    pending.migrate({
        "A1": {"table": "allergy"}, "A2": {"table": "allergy"},
        "E1": {"table": "encounter"},
    })

    class Cache:
        def __init__(self):
            self.schema = schema
            self.relationships = {"relationships": []}
            self.ranking = {"rankings": []}
            self.table_profiles = {
                "background": {"system_knowledge": {"related_table": "encounter"}},
            }

        def get_table(self, name):
            return next((item for item in schema["tables"] if item["name"] == name), None)

    app = Flask(__name__)
    app.extensions["services"] = {
        "pending_knowledge": pending, "defined_knowledge": defined, "cache": Cache(),
    }
    app.register_blueprint(knowledge_blueprint)
    client = app.test_client()

    unfiltered = client.get("/api/knowledge/pending").get_json()["pending_tables"]
    assert unfiltered[0]["table"] == "allergy"  # Highest pending-token count remains first.

    matches = client.get("/api/knowledge/pending?search=encounter").get_json()["pending_tables"]
    assert [row["table"] for row in matches] == [
        "encounter", "encounter_diagnosis", "encounter_provider", "allergy",
    ]
    assert [row["search_match"] for row in matches] == [
        "Exact table name", "Table name", "Table name", "Column: encounter_id",
    ]

    column_only = client.get("/api/knowledge/pending?search=encounter_id").get_json()["pending_tables"]
    assert [row["table"] for row in column_only] == ["allergy"]


def test_table_lifecycle_is_exclusive_and_returns_to_pending_when_cleared(tmp_path):
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")

    assert [row["table"] for row in pending.pending_tables(SCHEMA, defined)] == ["work_orders"]
    assert defined.is_table_defined("work_orders") is False

    defined.update_table("work_orders", {"definition": "Maintenance work orders"})
    assert defined.is_table_defined("work_orders") is True
    assert pending.pending_tables(SCHEMA, defined) == []

    defined.update_table("work_orders", {"definition": "", "synonyms": []})
    assert defined.is_table_defined("work_orders") is False
    assert [row["table"] for row in pending.pending_tables(SCHEMA, defined)] == ["work_orders"]


def test_synonym_or_abbreviation_alone_makes_a_table_defined(tmp_path):
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    defined.update_table("work_orders", {"synonyms": ["jobs"]})
    assert defined.is_table_defined("work_orders")
    assert pending.pending_tables(SCHEMA, defined) == []

    defined.update_table("work_orders", {"synonyms": []})
    defined.upsert_business_term("work_orders", {"abbreviation": "WO", "meaning": "Work Order"})
    assert defined.is_table_defined("work_orders")
    assert pending.pending_tables(SCHEMA, defined) == []


def test_schema_refresh_style_migration_does_not_overwrite_manual_knowledge(tmp_path):
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    defined.update_table("work_orders", {"definition": "User authored", "synonyms": ["jobs"]})
    defined.upsert_business_term("work_orders", {"abbreviation": "WO", "meaning": "Work Order"})
    changed_schema = {"tables": [{"name": "work_orders", "columns": [{"name": "new_column"}]}]}
    defined.migrate(changed_schema, {}, Dictionary())
    assert defined.table("work_orders") == {
        "definition": "User authored", "synonyms": ["jobs"],
        "abbreviations": {"WO": {"abbreviation": "WO", "meaning": "Work Order", "column": ""}},
    }


def test_pending_table_context_direct_acceptance(tmp_path):
    """Requirement A: Direct acceptance from textarea/request without prior save."""
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")

    result = pending.accept_table_context(
        "work_orders", defined, inline_context="Direct inline definition from textarea."
    )
    assert result["definition"] == "Direct inline definition from textarea."
    assert defined.table("work_orders")["definition"] == "Direct inline definition from textarea."
    assert pending.table_context("work_orders") == ""


def test_pending_table_context_save_then_accept(tmp_path):
    """Requirement B: Save context as pending draft, then accept without inline context."""
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")

    pending.save_table_context("work_orders", "Saved draft definition")
    assert pending.table_context("work_orders") == "Saved draft definition"
    assert not defined.table("work_orders")["definition"]  # Inert before acceptance

    accepted = pending.accept_table_context("work_orders", defined)
    assert accepted["definition"] == "Saved draft definition"
    assert defined.table("work_orders")["definition"] == "Saved draft definition"
    assert pending.table_context("work_orders") == ""  # Draft cleared after acceptance


def test_pending_table_context_empty_rejected(tmp_path):
    """Requirement C: Empty/whitespace inline context is rejected and nothing becomes approved."""
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")

    try:
        pending.accept_table_context("work_orders", defined, inline_context="   ")
    except ValueError as exc:
        assert "context" in str(exc).lower()
    else:
        raise AssertionError("Empty context must raise ValueError")
    assert not defined.table("work_orders")["definition"]


def test_pending_table_context_inline_precedence(tmp_path):
    """Requirement D: Inline context takes precedence over a previously saved pending draft."""
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")

    pending.save_table_context("work_orders", "Draft A")
    overridden = pending.accept_table_context(
        "work_orders", defined, inline_context="Inline B"
    )
    assert overridden["definition"] == "Inline B"
    assert defined.table("work_orders")["definition"] == "Inline B"
    assert pending.table_context("work_orders") == ""


def test_pending_context_isolation_from_prompt_and_retrieval(tmp_path):
    """Requirement E: Unaccepted pending context must not appear in retrieval or Llama prompt."""
    from core.rag_layer import LexicalRetriever
    from services.sql_reasoning_engine import PromptBuilder

    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")
    pending.save_table_context("work_orders", "SECRET UNAPPROVED PENDING CONTEXT")

    # 1. Retrieval lexical retriever does not see pending
    retriever = LexicalRetriever(
        {"work_orders": {"name": "work_orders", "columns": []}}, {}, {},
        defined_knowledge=defined.all()
    )
    score = retriever.score_table("work_orders", "SECRET UNAPPROVED PENDING CONTEXT")["score"]
    assert score == 0.0

    # 2. PromptBuilder does not inject pending
    context = {
        "question": "show work orders",
        "prompt": "## Database Schema\n`work_orders`\n## SQL Query:\n",
        "selected_tables": ["work_orders"],
        "_defined_knowledge": defined,
    }
    prompt = PromptBuilder().build(context)
    assert "SECRET UNAPPROVED PENDING CONTEXT" not in prompt
    assert "Relevant Table Knowledge" not in prompt


def test_approved_context_integration_in_retrieval_and_prompt(tmp_path):
    """Requirement F: Accepted context is immediately available in retrieval and prompt."""
    from core.rag_layer import LexicalRetriever
    from services.sql_reasoning_engine import PromptBuilder

    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")

    # Accept context
    pending.accept_table_context("work_orders", defined, inline_context="Maintenance work orders for factory assets.")

    # 1. Available in retrieval scoring
    retriever = LexicalRetriever(
        {"work_orders": {"name": "work_orders", "columns": []}}, {}, {},
        defined_knowledge=defined.all()
    )
    score = retriever.score_table("work_orders", "factory assets")["score"]
    assert score > 0.0

    # 2. Injected into PromptBuilder under ## Relevant Table Knowledge
    context = {
        "question": "show factory assets",
        "prompt": "## Database Schema\n`work_orders`\n## SQL Query:\n",
        "selected_tables": ["work_orders"],
        "_defined_knowledge": defined,
    }
    prompt = PromptBuilder().build(context)
    assert "## Relevant Table Knowledge" in prompt
    assert "Table: `work_orders`" in prompt
    assert "Meaning: Maintenance work orders for factory assets." in prompt


def test_approve_api_route_supports_inline_table_context(tmp_path):
    """Integration: approve API route handles inline table_context and returns 200/400."""
    schema = {"tables": [{"name": "departments", "columns": [{"name": "dept_no"}]}]}
    defined = DefinedKnowledgeRepository(tmp_path / "defined.json")
    pending = PendingKnowledgeRepository(tmp_path / "pending.json")

    class Cache:
        def __init__(self):
            self.schema, self.relationships, self.ranking, self.table_profiles = schema, {"relationships": []}, {"rankings": []}, {}
            self.knowledge_dir = tmp_path
            self.inverted_index = type("InvIdx", (), {"update_manual_knowledge": lambda self, t, k: None})()
        def get_table(self, name):
            return next((item for item in schema["tables"] if item["name"] == name), None)
        def get_table_names(self):
            return [t["name"] for t in schema["tables"]]
        def get_relationships_for_table(self, name):
            return []

    app = Flask(__name__)
    app.extensions["services"] = {
        "pending_knowledge": pending,
        "defined_knowledge": defined,
        "cache": Cache(),
    }
    app.register_blueprint(knowledge_blueprint)
    client = app.test_client()

    # Direct accept without prior save
    res = client.post("/api/knowledge/pending/approve", json={
        "type": "table_context",
        "table": "departments",
        "context": "Company departments and divisions.",
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["table"] == "departments"
    assert data["knowledge"]["definition"] == "Company departments and divisions."
    assert defined.table("departments")["definition"] == "Company departments and divisions."

    # Empty context returns 400
    res_err = client.post("/api/knowledge/pending/approve", json={
        "type": "table_context",
        "table": "departments",
        "context": "   ",
    })
    assert res_err.status_code == 400
    assert "error" in res_err.get_json()
