"""평가 리포트 검증: 저장된 결과 JSON만 읽어 성공 기준을 판정한다 (LLM·API 키 불필요)."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / "evaluation"
# 저장된 1·2차 결과는 도메인 재설계(플랫폼 전환·화면 170개) 이전 데이터로 만든 것이다. 재평가 전까지 결과 판정은 건너뛴다.
STALE = pytest.mark.skip(reason="도메인 재설계 후 평가 재실행 전")


def load(n):
    return json.loads((EV / f"round{n}_results.json").read_text(encoding="utf-8"))


def test_artifacts_exist():
    for f in ("round1_report.md", "round2_report.md", "round1_results.json", "round2_results.json", "judge_prompt.md"):
        assert (EV / f).exists(), f


@STALE
def test_round1_is_dense_only_baseline_and_fixed_in_readme():
    r1 = load(1)
    assert r1["config"]["rag_levers"] == "dense" and r1["config"]["baseline"] == "dense-only"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "dense-only" in readme and "baseline" in readme
    assert len(r1["cases"]) == 20


@STALE
def test_same_judge_and_runs_both_rounds():
    r1, r2 = load(1), load(2)
    assert r1["config"]["judge_prompt_sha256_12"] == r2["config"]["judge_prompt_sha256_12"]
    assert r1["config"]["runs_per_case"] == r2["config"]["runs_per_case"] == 3
    assert [c["id"] for c in r1["cases"]] == [c["id"] for c in r2["cases"]]
    for r in (r1, r2):
        for c in r["cases"]:
            assert len(c["runs"]) == 3
            assert c["pass"] == (sum(x["pass"] for x in c["runs"]) >= 2)


@STALE
def test_round2_success_criteria():
    r1, r2 = load(1)["summary"], load(2)["summary"]
    assert r2["pass_count"] >= 16
    assert r2["pass_count"] - r1["pass_count"] >= 3
    assert r2["numeric_exact_match_rate"] == 1.0
    assert r2["guardrail_block_rate"] == 1.0
    assert r2["faithfulness"] >= 0.85
    assert r2["context_recall"] >= 0.80


@STALE
def test_summary_matches_cases():
    for n in (1, 2):
        r = load(n)
        assert r["summary"]["pass_count"] == sum(c["pass"] for c in r["cases"])


@STALE
def test_reports_state_numbers():
    for n in (1, 2):
        t = (EV / f"round{n}_report.md").read_text(encoding="utf-8")
        assert f"{load(n)['summary']['pass_count']} / 20" in t
