import json

from core.query_evaluation import evaluate_full_case


class FakeEngine:
    def __init__(self, result):
        self.result = result
        self.last_prompt_info = {"llama_response": {"confidence": 0.8}}

    def execute(self, question):
        return self.result


def test_live_result_values_can_be_checked_without_leaking_them_in_report():
    engine = FakeEngine({"sql": "SELECT ...", "columns": ["code"],
                         "rows": [{"code": "RESTRICTED-2"}, {"code": "RESTRICTED-1"}],
                         "row_count": 2})
    case = {"id": "private-1", "question": "A private operational question",
            "expected_rows": [{"code": "RESTRICTED-1"}, {"code": "RESTRICTED-2"}]}
    report = evaluate_full_case(engine, case)
    assert report["passed"] is True
    assert "RESTRICTED" not in json.dumps(report)
    assert "private operational" not in json.dumps(report).lower()
    assert evaluate_full_case(engine, {**case, "require_row_order": True})["passed"] is False
    assert evaluate_full_case(engine, {**case, "expected_rows": [{"code": "WRONG"}]})["passed"] is False


def test_scalar_and_no_sql_expectations():
    scalar = FakeEngine({"sql": "SELECT ...", "columns": ["n"], "rows": [{"n": 17}], "row_count": 1})
    case = {"id": "scalar", "question": "How many?", "expected_scalar": 17}
    assert evaluate_full_case(scalar, case)["passed"] is True
    assert evaluate_full_case(scalar, {**case, "expected_scalar": 18})["passed"] is False
    refusal = FakeEngine({"sql": None, "columns": [], "rows": [], "row_count": 0})
    assert evaluate_full_case(refusal, {"id": "ambiguous", "question": "Unclear", "expect_no_sql": True})["passed"] is True
    assert evaluate_full_case(scalar, {"id": "ambiguous", "question": "Unclear", "expect_no_sql": True})["passed"] is False
