from pathlib import Path

from core.defined_knowledge import DefinedKnowledgeRepository
from core.rag_layer import LexicalRetriever
from services.sql_reasoning_engine import PromptBuilder


def test_table_context_and_abbreviation_are_the_simple_approved_model(tmp_path: Path):
    repo = DefinedKnowledgeRepository(tmp_path / "defined.json")
    repo.update_table("finance_payment_recoveries", {"definition": "Finance payment recovery records used to track recovered customer payments."})
    repo.upsert_business_term("finance_payment_recoveries", {"abbreviation": "ADM", "meaning": "Administration"})
    repo.upsert_business_term("finance_payment_recoveries", {"abbreviation": "BAL", "meaning": "Balance"})
    entry = repo.table("finance_payment_recoveries")
    assert entry["definition"].startswith("Finance payment")
    assert entry["abbreviations"]["ADM"]["meaning"] == "Administration"
    retriever = LexicalRetriever({"finance_payment_recoveries": {"name": "finance_payment_recoveries", "columns": []}}, {}, {}, defined_knowledge=repo.all())
    assert retriever.score_table("finance_payment_recoveries", "recovered customer payments")["score"] > 0
    prompt = PromptBuilder().build({"question": "show ADM recoveries", "prompt": "## Tables\n`finance_payment_recoveries`\n## SQL Query:\n", "selected_tables": ["finance_payment_recoveries"], "_defined_knowledge": repo})
    assert "Meaning: Finance payment recovery records" in prompt
    assert "ADM = Administration" in prompt
    assert "BAL = Balance" not in prompt


def test_prompt_excludes_unselected_knowledge(tmp_path: Path):
    repo = DefinedKnowledgeRepository(tmp_path / "defined.json")
    repo.update_table("selected", {"definition": "Selected table context."})
    repo.update_table("unselected", {"definition": "This must not reach the prompt."})
    prompt = PromptBuilder().build({"prompt": "## SQL Query:\n", "selected_tables": ["selected"], "_defined_knowledge": repo})
    assert "Selected table context" in prompt
    assert "must not reach" not in prompt


def test_prompt_diagnostics_and_pending_context_isolation(tmp_path: Path):
    approved = DefinedKnowledgeRepository(tmp_path / "approved.json")
    approved.update_table("recoveries", {"definition": "Approved recovery context."})
    context = {"question": "show recovery", "prompt": "## SQL Query:\n", "selected_tables": ["recoveries"], "_defined_knowledge": approved}
    PromptBuilder().build(context)
    assert context["performance"]["knowledge_entries_injected"] == 1
    assert context["performance"]["knowledge_chars"] > 0
    assert context["performance"]["final_prompt_chars"] > context["performance"]["base_prompt_chars"]
    # A separate pending store is deliberately never supplied to PromptBuilder.
    assert "Pending" not in PromptBuilder().build({"question": "show recovery", "prompt": "## SQL Query:\n", "selected_tables": [], "_defined_knowledge": DefinedKnowledgeRepository(tmp_path / "empty.json")})


def test_llama_prompt_places_knowledge_between_schema_and_request(tmp_path: Path):
    repo = DefinedKnowledgeRepository(tmp_path / "defined.json")
    repo.update_table("recoveries", {"definition": "Tracks recovered customer payments."})
    base = "## Database Task\nSQL\n## Database Schema\n`recoveries`\n## User Request\nshow recoveries\n## SQL Generation Rules\nSELECT only\n## SQL Query:\n"
    context = {"question": "show recoveries", "prompt": base, "selected_tables": ["recoveries"], "_defined_knowledge": repo}
    prompt = PromptBuilder().build(context)
    assert prompt.index("## Database Schema") < prompt.index("## Relevant Table Knowledge") < prompt.index("## User Request") < prompt.index("## SQL Generation Rules")
    assert context["performance"]["instruction_chars"] >= 0
