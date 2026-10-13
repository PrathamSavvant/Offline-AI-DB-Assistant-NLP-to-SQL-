import pytest

from agent.database_intelligence_agent import DatabaseIntelligenceAgent
from agent.llama_sql_agent import LlamaSQLAgent
from core.join_path_finder import JoinPathFinder
from core.query_plan import QueryPlanBuilder
from core.request_router import RequestRouter
from core.relationship_builder import RelationshipBuilder
from services.sql_reasoning_engine import SQLReasoningEngine, SQLValidationError, SQLValidator


SCHEMA = {
    "database": "company",
    "tables": [
        {
            "name": "parents",
            "primary_key": ["tenant_id", "parent_id"],
            "columns": [{"name": "tenant_id"}, {"name": "parent_id"}, {"name": "name"}],
            "foreign_keys": [],
        },
        {
            "name": "children",
            "columns": [
                {"name": "tenant_id"}, {"name": "parent_id"},
                {"name": "child_id"}, {"name": "status"},
            ],
            "foreign_keys": [
                {
                    "table": "children", "column": "tenant_id",
                    "references_table": "parents", "references_column": "tenant_id",
                    "constraint_name": "fk_child_parent", "ordinal_position": 1,
                },
                {
                    "table": "children", "column": "parent_id",
                    "references_table": "parents", "references_column": "parent_id",
                    "constraint_name": "fk_child_parent", "ordinal_position": 2,
                },
            ],
        },
        {
            "name": "isolated",
            "columns": [{"name": "parent_id"}],
            "foreign_keys": [],
        },
    ],
}


class StaticGenerator:
    def __init__(self, context):
        self.context = context

    def build_context(self, question):
        return {"question": question, **self.context}


class SpyLLM:
    def __init__(self, decision=None):
        self.calls = 0
        self.prompts = []
        self.decision = decision or {
            "sql": "SELECT child_id FROM children LIMIT 5",
            "answer": "ok", "confidence": 1.0, "needs_clarification": False,
        }

    def generate_structured_sql(self, prompt):
        self.calls += 1
        self.prompts.append(prompt)
        return self.decision


class Prompt:
    def build(self, context):
        return "selected=" + ",".join(context["query_plan"]["selected_tables"])


def _plan_context(selected, join_plan, **extra):
    return {
        "selected_tables": selected,
        "scored_tables": [{"table": table, "matches": [f"table_name:{table}"]} for table in selected],
        "join_plan": join_plan,
        "bridge_tables": [table for table in join_plan.get("included_tables", []) if table not in join_plan.get("requested_tables", [])],
        "prompt": "## Database Schema\nselected only\n## SQL Query:\n",
        **extra,
    }


def test_database_intelligence_agent_creates_ready_plan_without_an_llm():
    join = {
        "requested_tables": ["children"], "included_tables": ["children"],
        "join_edges": [], "unreachable_tables": [], "all_joins_verified": True,
    }
    agent = DatabaseIntelligenceAgent(
        StaticGenerator(_plan_context(["children"], join)),
        query_plan_builder=QueryPlanBuilder(SCHEMA),
    )
    context = agent.plan("  LIST   children  ")
    assert context["query_plan"]["status"] == "ready"
    assert context["query_plan"]["original_question"] == "  LIST   children  "
    assert context["query_plan"]["normalized_question"] == "list children"
    assert context["query_plan"]["database_identity"] == "company"


def test_database_intelligence_agent_removes_pending_runtime_evidence():
    join = {"requested_tables": ["children"], "included_tables": ["children"], "join_edges": [], "unreachable_tables": [], "all_joins_verified": True}
    agent = DatabaseIntelligenceAgent(
        StaticGenerator(_plan_context(
            ["children"], join,
            pending_knowledge={"children": "never"},
            knowledge_matches=[
                {"table": "children", "source": "approved_knowledge"},
                {"table": "isolated", "source": "pending_knowledge"},
            ],
        )),
        query_plan_builder=QueryPlanBuilder(SCHEMA),
    )
    context = agent.plan("children")
    assert "pending_knowledge" not in context
    assert context["query_plan"]["knowledge_matches"] == [
        {"table": "children", "source": "approved_knowledge"}
    ]


def test_query_plan_fails_when_no_verified_path_exists():
    join = {
        "requested_tables": ["children", "isolated"], "included_tables": ["children"],
        "join_edges": [], "unreachable_tables": ["isolated"], "all_joins_verified": True,
    }
    plan = QueryPlanBuilder(SCHEMA).build(_plan_context(["children"], join)).to_dict()
    assert plan["status"] == "failed"
    assert "No verified foreign-key path" in plan["failure_reason"]


def test_valid_fk_path_alone_cannot_make_query_plan_ready():
    join = {
        "requested_tables": ["children", "parents"],
        "included_tables": ["children", "parents"],
        "join_edges": [{"from": "children", "to": "parents", "verified": True}],
        "unreachable_tables": [], "all_joins_verified": True,
    }
    context = _plan_context(["children", "parents"], join)
    context["scored_tables"] = [
        {"table": "children", "matches": ["fallback_importance"]},
        {"table": "parents", "matches": ["importance_tiebreak (+0.9)"]},
    ]
    plan = QueryPlanBuilder(SCHEMA).build(context).to_dict()
    assert plan["status"] == "failed"
    assert "No sufficiently grounded database entity" in plan["failure_reason"]


def test_query_plan_is_ambiguous_when_predicates_are_ambiguous():
    join = {"requested_tables": ["children"], "included_tables": ["children"], "join_edges": [], "unreachable_tables": [], "all_joins_verified": True}
    plan = QueryPlanBuilder(SCHEMA).build(_plan_context(
        ["children"], join,
        predicate_manifest={"filters": [], "ambiguities": [{"value": "open", "reason": "multiple_table_candidates"}]},
    )).to_dict()
    assert plan["status"] == "ambiguous"
    assert plan["unresolved_terms"] == ["open"]


def test_llama_sql_agent_receives_ready_plan_and_only_composes_sql():
    llm = SpyLLM()
    agent = LlamaSQLAgent(Prompt(), llm)
    prompt, decision = agent.compose({"query_plan": {"status": "ready", "selected_tables": ["children"]}})
    assert prompt == "selected=children"
    assert decision["sql"].startswith("SELECT")
    assert llm.calls == 1


@pytest.mark.parametrize("status", ["failed", "ambiguous"])
def test_llama_sql_agent_rejects_non_ready_plans(status):
    llm = SpyLLM()
    with pytest.raises(ValueError, match="ready Query Plan"):
        LlamaSQLAgent(Prompt(), llm).compose({"query_plan": {"status": status, "selected_tables": []}})
    assert llm.calls == 0


def test_reasoning_engine_does_not_call_llama_for_failed_plan():
    class Context:
        def build(self, question):
            return {"selected_tables": ["children"], "query_plan": {"status": "failed", "failure_reason": "No path"}}

    llm = SpyLLM()
    engine = SQLReasoningEngine(Context(), Prompt(), SQLValidator(), None, llm, {})
    result = engine.execute("question")
    assert result["sql"] is None
    assert result["query_plan"]["status"] == "failed"
    assert llm.calls == 0


@pytest.mark.parametrize("question", [
    "hello", "hi", "hey", "thanks", "thank you", "who are you", "what can you do",
    "show me", "give me data", "tell me something",
])
def test_non_database_intent_skips_planning_llama_and_execution(question):
    class Context:
        calls = 0

        def build(self, _question):
            self.calls += 1
            raise AssertionError("database planning must be skipped")

    class Executor:
        calls = 0

        def __call__(self):
            self.calls += 1
            raise AssertionError("SQL execution must be skipped")

    context = Context()
    executor = Executor()
    llm = SpyLLM()
    diagnostics = {}
    engine = SQLReasoningEngine(
        context, Prompt(), SQLValidator(), executor, llm, diagnostics,
        intent_router=RequestRouter(),
    )
    result = engine.execute(question)
    assert result["selected_tables"] == []
    assert result["llama_sql_called"] is False
    assert result["sql_executed"] is False
    assert context.calls == llm.calls == executor.calls == 0
    assert diagnostics["database_planning_skipped"] is True


def test_ungrounded_nonsense_stops_before_llama_and_execution():
    class Context:
        def build(self, question):
            return {
                "question": question,
                "selected_tables": [],
                "query_plan": {
                    "status": "failed", "selected_tables": [],
                    "failure_reason": "No sufficiently grounded database entity was found.",
                },
            }

    class Executor:
        calls = 0

        def __call__(self):
            self.calls += 1
            raise AssertionError("SQL execution must be skipped")

    llm = SpyLLM()
    executor = Executor()
    diagnostics = {}
    result = SQLReasoningEngine(
        Context(), Prompt(), SQLValidator(), executor, llm, diagnostics,
        intent_router=RequestRouter(),
    ).execute("random nonsense text")
    assert result["query_plan"]["status"] == "failed"
    assert result["selected_tables"] == []
    assert llm.calls == executor.calls == 0
    assert diagnostics["llama_sql_called"] is False
    assert diagnostics["sql_executed"] is False


def test_composite_fk_is_one_verified_relationship():
    relationships = RelationshipBuilder(SCHEMA).get_fk_only_relationships()
    assert len(relationships) == 1
    assert relationships[0]["from_columns"] == ["tenant_id", "parent_id"]
    assert relationships[0]["to_columns"] == ["tenant_id", "parent_id"]
    assert relationships[0]["database"] == "company"
    assert relationships[0]["verified"] is True


def test_composite_fk_join_path_preserves_all_column_pairs():
    plan = JoinPathFinder(RelationshipBuilder(SCHEMA)).find_multi_table_join(["children", "parents"])
    edge = plan["join_edges"][0]
    assert edge["constraint_name"] == "fk_child_parent"
    assert edge["from_columns"] == ["tenant_id", "parent_id"]
    assert " AND " in plan["sql"]


def _validator_context():
    tables = {table["name"]: table for table in SCHEMA["tables"]}
    edge = JoinPathFinder(RelationshipBuilder(SCHEMA)).find_multi_table_join(["children", "parents"])["join_edges"][0]
    return {
        "selected_tables": ["children", "parents"],
        "tables_by_name": tables,
        "join_plan": {"included_tables": ["children", "parents"], "join_edges": [edge]},
        "query_plan": {"resolved_predicates": []},
    }


def test_validator_accepts_complete_composite_fk():
    sql = "SELECT c.child_id FROM children c JOIN parents p ON c.tenant_id = p.tenant_id AND c.parent_id = p.parent_id"
    assert "LIMIT" in SQLValidator().validate(sql, _validator_context()).upper()


def test_validator_rejects_incomplete_composite_fk():
    sql = "SELECT c.child_id FROM children c JOIN parents p ON c.tenant_id = p.tenant_id"
    with pytest.raises(SQLValidationError, match="incomplete"):
        SQLValidator().validate(sql, _validator_context())


def test_validator_rejects_join_when_plan_has_no_fk():
    context = _validator_context()
    context["join_plan"]["join_edges"] = []
    with pytest.raises(SQLValidationError, match="no verified FK"):
        SQLValidator().validate(
            "SELECT c.child_id FROM children c JOIN parents p ON c.parent_id = p.parent_id",
            context,
        )


def test_validator_rejects_same_named_columns_without_planned_relationship():
    tables = {table["name"]: table for table in SCHEMA["tables"]}
    context = {
        "selected_tables": ["parents", "isolated"],
        "tables_by_name": tables,
        "join_plan": {"included_tables": ["parents", "isolated"], "join_edges": []},
    }
    with pytest.raises(SQLValidationError, match="no verified FK"):
        SQLValidator().validate(
            "SELECT p.name FROM parents p JOIN isolated i ON p.parent_id = i.parent_id",
            context,
        )


def test_validator_enforces_resolved_predicate():
    context = {
        "selected_tables": ["children"],
        "tables_by_name": {"children": SCHEMA["tables"][1]},
        "join_plan": {"included_tables": ["children"], "join_edges": []},
        "query_plan": {"resolved_predicates": [{"table": "children", "column": "status", "operator": "=", "value": "OPEN"}]},
    }
    with pytest.raises(SQLValidationError, match="omitted required resolved predicate"):
        SQLValidator().validate("SELECT child_id FROM children", context)
    assert "LIMIT" in SQLValidator().validate(
        "SELECT child_id FROM children WHERE children.status = 'OPEN'", context
    ).upper()


def test_validator_rejects_unplanned_table_and_mutation():
    context = {
        "selected_tables": ["children"],
        "tables_by_name": {table["name"]: table for table in SCHEMA["tables"]},
        "join_plan": {"included_tables": ["children"], "join_edges": []},
    }
    with pytest.raises(SQLValidationError, match="outside approved context"):
        SQLValidator().validate("SELECT parent_id FROM parents", context)
    with pytest.raises(SQLValidationError, match="Only a single SELECT"):
        SQLValidator().validate("DELETE FROM children", context)


def test_fk_planner_reports_isolated_table_and_ambiguous_paths():
    finder = JoinPathFinder(RelationshipBuilder(SCHEMA))
    assert finder.find_multi_table_join(["children", "isolated"], allow_semantic_fallback=False)["unreachable_tables"] == ["isolated"]

    ambiguous_schema = {
        "database": "company",
        "tables": [
            {"name": "a", "columns": [{"name": "left_id"}, {"name": "right_id"}], "foreign_keys": [
                {"table": "a", "column": "left_id", "references_table": "b", "references_column": "id", "constraint_name": "fk_left"},
                {"table": "a", "column": "right_id", "references_table": "b", "references_column": "id", "constraint_name": "fk_right"},
            ]},
            {"name": "b", "columns": [{"name": "id"}], "foreign_keys": []},
        ],
    }
    ambiguous = JoinPathFinder(RelationshipBuilder(ambiguous_schema)).find_multi_table_join(
        ["a", "b"], allow_semantic_fallback=False, fail_on_ambiguous_paths=True
    )
    assert ambiguous["ambiguous_paths"]
    assert ambiguous["join_edges"] == []


def test_self_referencing_fk_remains_verified_without_extra_table():
    schema = {"database": "company", "tables": [{
        "name": "employees", "columns": [{"name": "id"}, {"name": "manager_id"}],
        "foreign_keys": [{
            "table": "employees", "column": "manager_id", "references_table": "employees",
            "references_column": "id", "constraint_name": "fk_manager",
        }],
    }]}
    relationships = RelationshipBuilder(schema).get_fk_only_relationships()
    assert relationships[0]["from_table"] == relationships[0]["to_table"] == "employees"
    assert relationships[0]["verified"] is True
