"""Generic regression tests for changing, opaque-name operational databases."""

import pytest

from config.settings import settings
from core.defined_knowledge import DefinedKnowledgeRepository
from core.query_plan import QueryPlanBuilder
from core.value_resolver import ValueResolver
from services.sql_reasoning_engine import PromptBuilder, SQLValidationError, SQLValidator


SCHEMA = {"tables": [
    {"name": "x_01", "primary_key": ["n"], "columns": [
        {"name": "n", "is_primary_key": True}, {"name": "s", "datatype": "varchar"},
    ]},
    {"name": "x_02", "primary_key": ["j"], "columns": [
        {"name": "j", "is_primary_key": True}, {"name": "n"},
        {"name": "active_flag", "datatype": "varchar"},
    ]},
]}


class ChangingAdapter:
    def __init__(self):
        self.calls = 0
        self.rows = [{"value": "UNDER REPAIR"}]
        self.fail = False

    def execute(self, _sql, _params):
        self.calls += 1
        if self.fail:
            raise RuntimeError("transient lookup failure")
        return self.rows


def test_observed_values_expire_and_empty_or_failed_lookups_are_not_retained(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("core.value_resolver.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "VALUE_LOOKUP_CACHE_TTL_SECONDS", 10)
    adapter = ChangingAdapter()
    resolver = ValueResolver(SCHEMA, database_adapter=adapter)
    assert resolver._observed_matches("x_01", "s", "under repair") == ["UNDER REPAIR"]
    adapter.rows = [{"value": "AWAITING COLLECTION"}]
    assert resolver._observed_matches("x_01", "s", "under repair") == ["UNDER REPAIR"]
    assert adapter.calls == 1
    clock[0] = 111.0
    assert resolver._observed_matches("x_01", "s", "under repair") == ["AWAITING COLLECTION"]
    assert adapter.calls == 2
    adapter.rows = []
    resolver.refresh()
    assert resolver._observed_matches("x_01", "s", "under repair") == []
    adapter.rows = [{"value": "READY"}]
    assert resolver._observed_matches("x_01", "s", "under repair") == ["READY"]
    adapter.fail = True
    resolver.refresh()
    assert resolver._observed_matches("x_01", "s", "under repair") == []
    adapter.fail = False
    assert resolver._observed_matches("x_01", "s", "under repair") == ["READY"]


def test_observed_value_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "VALUE_LOOKUP_CACHE_MAX_ENTRIES", 2)
    adapter = ChangingAdapter()
    resolver = ValueResolver(SCHEMA, database_adapter=adapter)
    for phrase in ("one", "two", "three"):
        resolver._observed_matches("x_01", "s", phrase)
    assert len(resolver._observed_value_cache) == 2


def _plan_context(question, repository, selected=None):
    selected = selected or ["x_01", "x_02"]
    return {
        "question": question,
        "selected_tables": selected,
        "_defined_knowledge": repository,
        "presentation_context": {"entities": [
            {"table": "x_01", "primary_entity": True, "primary_key": ["n"], "identity_columns": ["n"], "display_columns": ["s"]},
            {"table": "x_02", "primary_entity": False, "primary_key": ["j"], "identity_columns": ["j"], "display_columns": []},
        ]},
        "join_plan": {"base_table": "x_01", "requested_tables": selected, "included_tables": selected,
                      "join_edges": [{"from": "x_02", "from_column": "n", "to": "x_01", "to_column": "n", "verified": True}],
                      "all_joins_verified": True},
        "predicate_manifest": {"filters": []},
        "scored_tables": [{"table": table, "matches": ["table_name"]} for table in selected],
    }


def test_approved_current_rule_enters_plan_and_prompt(tmp_path):
    repository = DefinedKnowledgeRepository(tmp_path / "knowledge.json")
    repository.update_table("x_01", {"record_grain": "one equipment item", "record_role": "entity"})
    repository.update_table("x_02", {"record_grain": "one movement event", "record_role": "event_history",
                                     "current_record": {"column": "active_flag", "value": "Y"}})
    context = _plan_context("How many equipment items are currently active?", repository)
    plan = QueryPlanBuilder(SCHEMA).build(context).to_dict()
    assert plan["status"] == "ready"
    assert plan["record_grain"] == "one equipment item"
    assert {"table": "x_02", "column": "active_flag", "operator": "=", "value": "Y", "source": "approved_current_record_rule"} in plan["resolved_predicates"]
    context["query_plan"] = plan
    context["prompt"] = "## SQL Query:\n"
    prompt = PromptBuilder().build(context)
    assert "one equipment item" in prompt
    assert "Count distinct primary-entity keys" in prompt
    assert "`x_02.active_flag` = 'Y'" in prompt


def test_current_history_without_approved_rule_fails_before_llm(tmp_path):
    repository = DefinedKnowledgeRepository(tmp_path / "knowledge.json")
    repository.update_table("x_02", {"record_role": "event_history"})
    plan = QueryPlanBuilder(SCHEMA).build(_plan_context("Where is it currently?", repository))
    assert plan.status == "failed"
    assert "current-record rule" in plan.failure_reason
    location_plan = QueryPlanBuilder(SCHEMA).build(_plan_context("Where is this equipment?", repository))
    assert location_plan.status == "failed"


def test_joined_entity_count_rejects_fanout_and_requires_current_filter(tmp_path):
    repository = DefinedKnowledgeRepository(tmp_path / "knowledge.json")
    repository.update_table("x_02", {"record_role": "event_history", "current_record": {"column": "active_flag", "value": "Y"}})
    context = _plan_context("How many equipment items are currently active?", repository)
    context["query_plan"] = QueryPlanBuilder(SCHEMA).build(context).to_dict()
    unsafe = "SELECT COUNT(*) FROM x_01 JOIN x_02 ON x_02.n = x_01.n WHERE x_02.active_flag = 'Y'"
    with pytest.raises(SQLValidationError, match="COUNT\\(DISTINCT"):
        SQLValidator().validate(unsafe, context)
    missing_current = "SELECT COUNT(DISTINCT x_01.n) FROM x_01 JOIN x_02 ON x_02.n = x_01.n"
    with pytest.raises(SQLValidationError, match="omitted required resolved predicate"):
        SQLValidator().validate(missing_current, context)
    valid = missing_current + " WHERE x_02.active_flag = 'Y'"
    assert SQLValidator().validate(valid, context).startswith("SELECT COUNT(DISTINCT")
    bypassed = missing_current + " WHERE x_02.active_flag = 'Y' OR 1 = 1"
    with pytest.raises(SQLValidationError, match="required WHERE condition"):
        SQLValidator().validate(bypassed, context)


def test_record_rule_validation_and_persistence(tmp_path):
    repository = DefinedKnowledgeRepository(tmp_path / "knowledge.json")
    with pytest.raises(ValueError):
        repository.update_table("x_02", {"record_role": "guessed"})
    with pytest.raises(ValueError):
        repository.update_table("x_02", {"current_record": {"column": "active_flag"}})
    repository.update_table("x_02", {"record_role": "event_history", "current_record": {"column": "active_flag", "value": "Y"}})
    reloaded = DefinedKnowledgeRepository(tmp_path / "knowledge.json")
    assert reloaded.table("x_02")["current_record"] == {"column": "active_flag", "value": "Y"}
