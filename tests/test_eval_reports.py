"""평가 리포트 검증: 저장된 결과 JSON만 읽어 성공 기준을 판정한다 (LLM·API 키 불필요)."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / "evaluation"
# 1차(사전 고정 baseline)와 2차(개선 레버 적용) 실제 결과로 성공 기준을 판정한다.


def load(n):
    return json.loads((EV / f"round{n}_results.json").read_text(encoding="utf-8"))


def test_artifacts_exist():
    for f in ("round1_report.md", "round2_report.md", "round1_results.json", "round2_results.json", "judge_prompt.md"):
        assert (EV / f).exists(), f


def test_round1_is_dense_only_baseline_and_fixed_in_readme():
    r1 = load(1)
    assert r1["config"]["rag_levers"] == "dense" and r1["config"]["baseline"].startswith("dense-only")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "dense-only" in readme and "baseline" in readme
    assert len(r1["cases"]) == 20


def test_same_judge_and_runs_both_rounds():
    r1, r2 = load(1), load(2)
    assert r1["config"]["judge_prompt_sha256_12"] == r2["config"]["judge_prompt_sha256_12"]
    assert r1["config"]["runs_per_case"] == r2["config"]["runs_per_case"] == 3
    assert [c["id"] for c in r1["cases"]] == [c["id"] for c in r2["cases"]]
    for r in (r1, r2):
        for c in r["cases"]:
            assert len(c["runs"]) == 3
            assert c["pass"] == (sum(x["pass"] for x in c["runs"]) >= 2)
            # 평가 시 생성 문서 제외: 게시된 회의 자료(weekly_meeting_*)는 어떤 실행의 근거에도 없어야 한다
            assert all(not i.startswith("weekly_meeting") for x in c["runs"] for i in x["context_doc_ids"])
        assert r["config"]["exclude_generated"] is True


def test_round2_success_criteria():
    r1, r2 = load(1)["summary"], load(2)["summary"]
    assert r2["pass_count"] >= 16
    assert r2["pass_count"] - r1["pass_count"] >= 3
    assert r2["numeric_exact_match_rate"] == 1.0
    assert r2["guardrail_block_rate"] == 1.0
    # RAGAS 4개 지표 (설명형 케이스)
    assert r2["faithfulness"] >= 0.85
    assert r2["context_recall"] >= 0.80
    assert r2["context_precision"] >= 0.80


def test_round2_answer_relevancy_target():
    assert load(2)["summary"]["answer_relevancy"] >= 0.80


def test_round2_report_has_delta_and_fixes():
    r2 = load(2)
    t = (EV / "round2_report.md").read_text(encoding="utf-8")
    assert "## 1차 대비" in t and "## 2차 개선 중 고친 결함" in t and "## 남은 지표 한계" in t
    assert "previous_runs" not in r2 and r2["config"]["fixes"]  # 최종 2차 결과 하나만 남긴다


def test_summary_matches_cases():
    for n in (1, 2):
        r = load(n)
        assert r["summary"]["pass_count"] == sum(c["pass"] for c in r["cases"])


def test_reports_state_numbers():
    for n in (1, 2):
        t = (EV / f"round{n}_report.md").read_text(encoding="utf-8")
        assert f"{load(n)['summary']['pass_count']} / 20" in t
        for label in ("faithfulness", "context recall", "context precision", "answer relevancy"):
            assert f"| {label}(설명형) |" in t



def test_round2_report_has_category_table_and_case_changes():
    t = (EV / "round2_report.md").read_text(encoding="utf-8")
    assert "## 유형별 통과" in t and "| guardrail |" in t
    assert "## 1차 대비 케이스 변화" in t and "악화(1차 PASS → 2차 FAIL)" in t
