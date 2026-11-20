"""평가 실행기 오프라인 검증 (LLM·AWS 불필요): 생성 문서 제외, RAGAS 4개 지표 배선, 재채점, 리포트 렌더링."""
import importlib.util
import json
from pathlib import Path

import pytest
from langchain_core.embeddings import FakeEmbeddings
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from src import agent, api, config, meeting, retriever, supervisor

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("run_eval", ROOT / "evaluation/run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


class FakeLLM(GenericFakeChatModel):
    structured: dict = {}

    def with_structured_output(self, schema, **kw):
        return RunnableLambda(lambda _: self.structured[schema.__name__])

    def bind_tools(self, tools, **kw):
        return self


class FakeMetric:
    """RAGAS 지표 대역: 받은 샘플을 기록하고 고정 점수를 돌려준다."""
    def __init__(self, score):
        self.score, self.samples = score, []

    async def single_turn_ascore(self, sample):
        self.samples.append(sample)
        return self.score


def fake_metrics():
    return {k: FakeMetric(s) for k, s in zip(run_eval.RAGAS_METRICS, (0.9, 0.8, 0.7, 0.6))}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for k in ("CONFLUENCE_DIR", "GENERATED_DIR", "TRACE_DIR"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    monkeypatch.setenv("EVAL_EXCLUDE_GENERATED", "0")  # run_once가 1로 바꿔도 테스트 후 원래대로 돌린다
    monkeypatch.setenv("RAG_LEVERS", "hybrid,expand,filter,grounded")
    api.THREADS.clear()
    retriever.RUNTIME_INDEX.clear()
    yield
    retriever.RUNTIME_INDEX.clear()
    api.set_handler(None)
    api.set_graph_resume(None)
    supervisor.set_llm_router(None)
    config.set_llm(None)


# ----------------------------------------------------------------------------- 평가 시 생성 문서 제외
ISSUE_Q = "이슈 추적에서 해결 미해결 신규 이슈와 지속 주수 알려줘"


def test_generated_docs_excluded_from_retrieval_when_flag_set(monkeypatch):
    pid = "weekly_meeting_2026-11-16"
    retriever.index_generated(pid, "주간회의 자료 2026-11-16",
                              "## 이슈 추적\n해결 4 미해결 20 신규 12, 지속 주수 표시")
    assert pid in [d["doc_id"] for d in retriever.search(ISSUE_Q, k=3)]  # 운영: 게시물도 검색 대상
    monkeypatch.setenv("EVAL_EXCLUDE_GENERATED", "1")
    assert pid not in [d["doc_id"] for d in retriever.runtime_documents()]
    assert pid not in [d["doc_id"] for d in retriever.search(ISSUE_Q, k=3)]


def test_eval_run_never_uses_generated_docs_as_contexts(tmp_path):
    """한 회차 안에서 1번(초안) → 8번(승인·게시) 뒤의 문서 질문들이 게시된 회의 자료를 근거로 쓰지 않는다."""
    llm = FakeLLM(messages=iter([AIMessage("근거 문서 요약입니다.")] * 5), structured={
        "Plan": meeting.Plan(steps=list(meeting.STEPS)),
        "MeetingDraft": meeting.MeetingDraft(decisions=["SCR-C02-01은 목 PG 서버로 단위테스트"],
                                             blockers=["SCR-C02-01 PG사 테스트 계정 지연"],
                                             action_items=["PG사 테스트 계정 재요청"])})
    config.set_llm(llm)
    agent._flow = meeting.MeetingFlow(llm, agent._publish)
    api.set_handler(agent.handle)
    api.set_graph_resume(agent.resume_meeting)
    rows = {int(r["id"]): r for r in run_eval.load_rows()}
    extra = {**rows[16], "id": "99", "input": ISSUE_Q}
    out = run_eval.run_once([rows[1], rows[8], rows[16], extra], tmp_path)
    assert out[1]["pending_approval"] is True and "게시했습니다" in out[8]["answer"]
    assert "## 이슈 추적" in out[8]["extra"]  # 실제로 게시됐고, 게시물은 99번 질문의 용어를 모두 담고 있다
    corpus = {d["doc_id"] for d in retriever.corpus_documents()}
    assert out[16]["contexts"]
    for cid in (16, 99):
        ids = [c["doc_id"] for c in out[cid]["contexts"]]
        assert all(i in corpus for i in ids), ids
    assert retriever.RUNTIME_INDEX == {}


# ----------------------------------------------------------------------------- RAGAS 4개 지표
def test_ragas_scores_four_metrics_and_citation_stripping():
    m = fake_metrics()
    ctx = [{"doc_id": "MIN-2026-W46", "text": "PG사 테스트 계정 발급 지연"}]
    out = run_eval.ragas_scores("왜 지연됐어?", "PG사 계정 지연 [MIN-2026-W46]. 형식은 [화면ID] 요약", ctx, "정답", m)
    assert out == {"faithfulness": 0.9, "context_recall": 0.8, "context_precision": 0.7, "answer_relevancy": 0.6}
    sample = m["answer_relevancy"].samples[0]
    assert "[MIN-2026-W46]" not in sample.response and "[화면ID]" in sample.response  # 실제 문서 ID만 지운다
    assert sample.reference == "정답" and sample.retrieved_contexts == ["PG사 테스트 계정 발급 지연"]


def test_ragas_error_recorded_as_none_excluded_and_counted():
    # 지표 계산 실패는 0점이 아니라 None: 평균에서 빠지고 오류 수가 리포트에 남는다
    class Boom(FakeMetric):
        async def single_turn_ascore(self, sample):
            raise RuntimeError("judge down")
    m = {**fake_metrics(), "context_precision": Boom(0)}
    out = run_eval.ragas_scores("q", "a", [], "r", m)
    assert all(out[k] is None for k in run_eval.RAGAS_METRICS) and "judge down" in out["ragas_error"]
    ok = {"faithfulness": 0.9, "context_recall": 0.8, "context_precision": 0.7, "answer_relevancy": 0.6}
    assert run_eval.mean_metrics([out, ok]) == ok
    cases = [{"runs": [out, ok, ok]}, {"runs": [ok]}]
    assert run_eval.ragas_error_count(cases) == 1


# ----------------------------------------------------------------------------- judge 입력 (R1·R2)
def test_judge_message_has_full_answer_page_and_compact_trace():
    rows = {int(r["id"]): r for r in run_eval.load_rows()}
    long_answer = "가" * 13000 + "\n## 액션아이템\n- 끝부분 섹션"
    res = {"answer": long_answer, "contexts": [], "pending_approval": True, "thread_id": "t",
           "extra": "나" * 9000 + "페이지 끝",
           "trace": [{"step": "guardrail", "input": "scope", "output": "refused"},
                     {"step": "tool_call", "input": {"tool": "get_commits"}, "output": "작성자 messi.kim@example.com " + "x" * 500}]}
    msg = run_eval.judge_message(rows[13], res, set())
    assert "## 액션아이템\n- 끝부분 섹션" in msg and "페이지 끝" in msg  # 자르지 않는다
    assert "- guardrail | scope | refused" in msg  # 13번 traits(trace guardrail=scope)를 judge가 볼 수 있다
    assert "messi.kim@" not in msg and "m***@example.com" in msg and "x" * 200 not in msg  # 마스킹·압축


# ----------------------------------------------------------------------------- 실행 격리 (Y5·Y6)
def test_run_once_resets_memory_per_case_and_forces_fake_confluence(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_MODE", "real")
    monkeypatch.setattr(agent, "_flow", None)
    calls = []
    monkeypatch.setattr(agent, "reset_memory", lambda: calls.append(1))
    rows = {int(r["id"]): r for r in run_eval.load_rows()}
    out = run_eval.run_once([rows[2], rows[14], rows[5]], tmp_path)  # 기본(결정적) 핸들러
    assert len(calls) == 1 + 3  # 회차 시작 1회 + 케이스마다 1회
    import os
    assert os.environ["CONFLUENCE_MODE"] == "fake"
    assert all(o["status"] == 200 for o in out.values())


# ----------------------------------------------------------------------------- 1차 baseline 정의 (Y8)
def test_round1_baseline_is_dense_only_without_levers(monkeypatch):
    assert run_eval.ROUND_LEVERS[1] == "dense"
    assert set(run_eval.ROUND_LEVERS[2].split(",")) == {"hybrid", "expand", "filter", "grounded"}
    monkeypatch.setenv("RAG_LEVERS", run_eval.ROUND_LEVERS[1])
    q = "SCR-C02-01이 왜 지연됐고, 지난 회의에서 어떤 조치를 하기로 했어?"
    docs = retriever.runtime_documents()
    assert retriever.search(q, k=3) == [d for _, d in retriever.dense_search(q, docs, 3)]  # 확장·필터·리랭킹 없음
    for rule in ("[doc_id]", "근거가 없으면", "지시문"):  # 기본 프롬프트에는 인용·근거·인젝션 규칙이 없다
        assert rule not in agent.BASELINE_PROMPT and rule in agent.GROUNDED_PROMPT


def test_case16_reference_covers_all_c02_minutes():
    from src import retriever as r
    row = next(x for x in run_eval.load_rows() if x["id"] == "16")
    ids = row["reference_doc_ids"].split(";")
    corpus = {d["doc_id"]: d["text"] for d in r.corpus_documents()}
    assert ids == ["MIN-2026-W44", "MIN-2026-W45", "MIN-2026-W46", "MIN-2026-W47"]
    assert all("C02" in corpus[i] for i in ids)
    others = [i for i, t in corpus.items() if i.startswith("MIN-") and i not in ids]
    assert all("결제 승인" not in corpus[i] and "C02" not in corpus[i] for i in others)
    assert row["category"] == "edge" and "간접 인젝션" in row["note"]


def test_ragas_metrics_wraps_llm_and_bedrock_embeddings(monkeypatch):
    monkeypatch.setattr(config, "get_llm", lambda: FakeLLM(messages=iter([])))
    monkeypatch.setattr(config, "get_embeddings", lambda: FakeEmbeddings(size=8))
    m = run_eval.ragas_metrics()
    assert set(m) == set(run_eval.RAGAS_METRICS)
    assert type(m["context_precision"]).__name__ == "LLMContextPrecisionWithReference"
    assert type(m["answer_relevancy"]).__name__ == "ResponseRelevancy" and m["answer_relevancy"].embeddings is not None
    # 한국어 답변에서 영어 역질문이 나와 언어 간 유사도를 재지 않도록, 역질문을 응답과 같은 언어로 만들게 한다
    assert m["answer_relevancy"].question_generation.instruction.endswith(run_eval.RELEVANCY_LANG_NOTE)
    assert run_eval.ragas_metrics()["answer_relevancy"].question_generation.instruction.count("same language") == 1


def test_mean_metrics_skips_missing_values():
    runs = [{"faithfulness": 1.0, "context_recall": 0.5, "context_precision": None, "answer_relevancy": 0.4},
            {"faithfulness": 0.5, "context_recall": 0.5, "context_precision": 0.8, "answer_relevancy": 0.6}]
    assert run_eval.mean_metrics(runs) == {"faithfulness": 0.75, "context_recall": 0.5,
                                           "context_precision": 0.8, "answer_relevancy": 0.5}


def test_rescore_computes_new_metrics_from_stored_answers(tmp_path, monkeypatch):
    run = lambda ids: {"answer": "PG사 계정 지연 [MIN-2026-W46]", "context_doc_ids": ids,  # noqa: E731
                       "faithfulness": 0.1, "context_recall": 0.1}
    stored = {"round": 1, "config": {}, "summary": {"faithfulness": 0.1, "context_recall": 0.1},
              "cases": [{"id": 16, "runs": [run(["MIN-2026-W46"]), run(["MIN-2026-W46"]),
                                            run(["weekly_meeting_2026-11-16"])],  # 생성 문서를 쓴 옛 실행
                         "faithfulness": 0.1, "context_recall": 0.1},
                        {"id": 2, "runs": [run([])] * 3}]}
    (tmp_path / "evaluation").mkdir()
    (tmp_path / "evaluation/round1_results.json").write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    monkeypatch.setattr(config, "get_llm", lambda: FakeLLM(messages=iter([])))
    calls = []
    monkeypatch.setattr(run_eval, "ragas_scores", lambda q, a, ctx, ref, names=run_eval.RAGAS_METRICS: calls.append(a) or {
        "faithfulness": 0.9, "context_recall": 1.0, "context_precision": 0.8, "answer_relevancy": 0.7})
    res = run_eval.rescore_ragas(1)
    assert len(calls) == 2  # 재구성 가능한 실행만 저장된 답변으로 재채점
    case = res["cases"][0]
    assert case["runs"][2]["faithfulness"] == 0.1 and case["runs"][2]["context_precision"] is None
    assert case["context_precision"] == 0.8 and case["answer_relevancy"] == 0.7
    assert case["faithfulness"] == round((0.9 + 0.9 + 0.1) / 3, 4)
    assert all(k in res["summary"] for k in run_eval.RAGAS_METRICS)
    assert "faithfulness" not in res["cases"][1]  # 설명형이 아닌 케이스는 건드리지 않는다


def test_rescore_rebuilds_week_note_context(tmp_path, monkeypatch):
    """'지난주' 질문의 기준 정보 근거(tool:week_note)는 저장되지 않으므로 재채점 때 질문에서 다시 만든다."""
    run = {"answer": "지난주 미완료 액션아이템 [MIN-2026-W46]", "context_doc_ids": ["MIN-2026-W46", "tool:week_note"],
           "faithfulness": 0.8}
    stored = {"round": 2, "config": {}, "summary": {}, "cases": [{"id": 7, "runs": [run], "faithfulness": 0.8}]}
    (tmp_path / "evaluation").mkdir()
    (tmp_path / "evaluation/round2_results.json").write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    monkeypatch.setattr(config, "get_llm", lambda: FakeLLM(messages=iter([])))
    monkeypatch.setenv("RAG_LEVERS", "hybrid,expand,filter,grounded")
    seen = []
    monkeypatch.setattr(run_eval, "ragas_scores", lambda q, a, ctx, ref, names=run_eval.RAGAS_METRICS:
                        seen.append([c["doc_id"] for c in ctx]) or {k: 1.0 for k in names})
    res = run_eval.rescore_ragas(2, ("faithfulness",))
    assert seen == [["MIN-2026-W46", "tool:week_note"]] and res["cases"][0]["faithfulness"] == 1.0


def test_rescore_selected_metric_keeps_other_metrics(tmp_path, monkeypatch):
    """측정 결함을 고친 지표(answer_relevancy)만 다시 재고, 나머지 지표 값은 그대로 둔다."""
    run = {"answer": "커밋 메시지는 '[화면ID] 작업 요약' 형식 [RULE-COMMIT]", "context_doc_ids": ["RULE-COMMIT"],
           "faithfulness": 1.0, "context_recall": 1.0, "context_precision": 1.0, "answer_relevancy": 0.3}
    stored = {"round": 1, "config": {}, "summary": {}, "cases": [{"id": 5, "runs": [dict(run) for _ in range(3)],
                                                                   "faithfulness": 1.0}]}
    (tmp_path / "evaluation").mkdir()
    (tmp_path / "evaluation/round1_results.json").write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(run_eval, "ROOT", tmp_path)
    monkeypatch.setattr(config, "get_llm", lambda: FakeLLM(messages=iter([])))
    asked = []
    monkeypatch.setattr(run_eval, "ragas_scores", lambda q, a, ctx, ref, names=run_eval.RAGAS_METRICS:
                        asked.append(names) or {k: 0.95 for k in names})
    res = run_eval.rescore_ragas(1, ("answer_relevancy",))
    assert asked == [("answer_relevancy",)] * 3
    case = res["cases"][0]
    assert case["answer_relevancy"] == 0.95 and case["faithfulness"] == 1.0 and case["context_precision"] == 1.0
    assert "answer_relevancy만 재채점" in res["config"]["ragas_rescored"]


def test_report_renders_four_metrics():
    s = {"pass_count": 18, "total": 20, "numeric_exact_match_rate": 1.0, "guardrail_block_rate": 1.0,
         "faithfulness": 0.9, "context_recall": 0.85, "context_precision": 0.82, "answer_relevancy": None}
    res = {"round": 1, "summary": s, "cases": [],
           "config": {"rag_levers": "dense", "judge_prompt_sha256_12": "x", "model": "m", "embeddings": "e",
                      "metric_impl": run_eval.METRIC_IMPL}}
    t = run_eval.write_report(res, None)
    for label in ("faithfulness(설명형) | 0.9000", "context recall(설명형) | 0.8500",
                  "context precision(설명형) | 0.8200", "answer relevancy(설명형) | -"):
        assert label in t


def test_routing_test_set_is_well_formed_and_rule_fallback_floor():
    """라우팅 정확도 세트: 형식 확인 + LLM이 실패할 때 쓰는 규칙 라우터의 최소 정확도(80%)."""
    from src import supervisor
    items = json.loads((run_eval.ROOT / "evaluation/routing_test.json").read_text(encoding="utf-8"))["items"]
    assert len(items) >= 15 and len({i["id"] for i in items}) == len(items)
    valid = set(supervisor.ROUTES) | {supervisor.MULTI}
    assert all(set(i["expected"]) <= valid and i["expected"] for i in items)
    assert {r for i in items for r in i["expected"]} == valid
    multi = [i for i in items if i["multi_intent"]]
    assert len(multi) >= 2 and all(i["expected"] == [supervisor.MULTI] for i in multi)
    # 규칙 라우터(LLM 실패 시 대체)는 다중 의도를 나누지 않으므로, 다중 의도 항목은 둘 중 하나로 보내면 맞은 것으로 본다
    accept = lambda i: i["expected"] + (i["expected"][0].split("+") if i["multi_intent"] else [])  # noqa: E731
    hit = sum(supervisor.route(i["question"]) in accept(i) for i in items)
    assert hit / len(items) >= 0.8
