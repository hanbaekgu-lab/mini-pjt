"""Trace(라우팅·도구 호출·로컬 기록)와 MCP/LLM 타임아웃·재시도 테스트."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from helpers import post_query
from src import api, confluence, mcp_client, middleware

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_DIR", str(tmp_path / "pages"))
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "trace"))
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    api.THREADS.clear()
    api.set_handler(None)
    yield
    api.set_handler(None)


def post(q, user="pm01", role="pm", **kw):
    return post_query(client, {"question": q, "user_id": user, "role": role, **kw})


def routes(d):
    return [t["output"] for t in d["trace"] if t["step"] == "supervisor_route"]


def test_progress_route_and_tool_call():
    d = post("이번 주 진척률 알려줘").json()
    assert routes(d) == ["progress"]
    assert any(t["step"] == "tool_call" for t in d["trace"])


def test_meeting_route():
    d = post("이번 주 회의 자료 초안 만들어줘").json()
    assert routes(d) == ["meeting_material"]


def test_docs_qa_route():
    assert routes(post("커밋 규칙이 뭐야?").json()) == ["docs_qa"]


def test_route_on_guardrail_and_resume():
    d = post("회의 자료 초안", user="dev01", role="developer").json()
    assert routes(d) == ["meeting_material"]
    p = post("이번 주 회의 자료 초안").json()
    r = post("승인", thread_id=p["thread_id"], approval="approve").json()
    assert routes(r) == ["meeting_material"]


def test_local_trace_log_without_langsmith_key(tmp_path):
    post("이번 주 진척률 알려줘")
    files = list((tmp_path / "trace").glob("*.json"))
    assert files
    rec = json.loads(files[0].read_text(encoding="utf-8"))
    assert rec["route"] == "progress" and rec["trace"]


def test_trace_pii_masked_in_log(tmp_path):
    post("k@example.com 진척 알려줘")
    text = "".join(f.read_text(encoding="utf-8") for f in (tmp_path / "trace").glob("*.json"))
    assert "k@example.com" not in text


def test_retry_succeeds_after_failure():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("x")
        return "ok"
    assert middleware.call_with_retry(flaky) == "ok" and len(calls) == 2  # 재시도는 최대 1회


def test_retry_at_most_once():
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("x")
    with pytest.raises(middleware.CallFailed):
        middleware.call_with_retry(boom)
    assert len(calls) == 2 and middleware.MAX_RETRIES == 1


def test_timeout_not_retried(monkeypatch):
    # Y17: 타임아웃이 난 호출은 아직 실행 중일 수 있어 다시 부르지 않는다
    monkeypatch.setattr(middleware, "TIMEOUT", 0.05)
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
    with pytest.raises(middleware.CallFailed) as e:
        middleware.call_with_retry(slow)
    assert len(calls) == 1 and e.value.timed_out


def test_mcp_inner_wait_shorter_than_outer_timeout():
    assert mcp_client.TIMEOUT == 10.0 and middleware.TIMEOUT == 15.0 and mcp_client.TIMEOUT < middleware.TIMEOUT


def test_mcp_timeout_reports_error_and_no_publish(monkeypatch):
    monkeypatch.setattr(middleware, "TIMEOUT", 0.05)
    attempts = []

    def hang(*a, **k):
        attempts.append(1)
        time.sleep(0.3)
    monkeypatch.setattr(mcp_client, "call_tool", hang)
    p = post("이번 주 회의 자료 초안").json()
    d = post("승인", thread_id=p["thread_id"], approval="approve").json()
    assert "게시 실패" in d["answer"] and "타임아웃" in d["answer"]
    assert len(attempts) == 1  # 첫 게시가 끝나지 않았을 수 있으므로 다시 게시하지 않는다
    assert confluence.list_pages() == []
    assert any(t["step"] == "confluence_publish" and "error" in str(t["output"]) for t in d["trace"])


def test_create_page_error_not_retried(monkeypatch):
    attempts = []

    def fail(*a, **k):
        attempts.append(1)
        raise mcp_client.MCPError("server down")
    monkeypatch.setattr(mcp_client, "call_tool", fail)
    p = post("이번 주 회의 자료 초안").json()
    d = post("승인", thread_id=p["thread_id"], approval="approve").json()
    assert "게시 실패" in d["answer"] and len(attempts) == 1


def test_llm_timeout_reports_error_and_rotates_memory_key(monkeypatch):
    monkeypatch.setattr(middleware, "LLM_TIMEOUT", 0.05)
    n, keys = [], []

    def slow_llm(req, route):
        n.append(1)
        time.sleep(0.3)
    api.set_handler(slow_llm)
    d = post("커밋 규칙이 뭐야?").json()
    assert d["answer"].startswith("오류") and "타임아웃" in d["answer"]
    assert len(n) == 1  # 타임아웃은 재시도하지 않는다
    assert routes(d) == ["docs_qa"]
    api.set_handler(lambda req, route: keys.append(req.thread_id) or {"answer": "ok", "contexts": [], "trace": []})
    post("커밋 규칙이 뭐야?", thread_id=d["thread_id"])
    assert keys == [f"{d['thread_id']}#1"]  # 타임아웃 뒤 같은 thread는 새 대화 기억 키를 쓴다


# --- Y7: 라우팅은 신원·가드레일 뒤, 요청당 1회, 캐시 없음 ---
def test_trace_order_guardrail_then_route():
    d = post("이번 주 진척률 알려줘").json()
    steps = [t["step"] for t in d["trace"]]
    assert steps[:2] == ["guardrail", "supervisor_route"]


def test_router_not_called_for_blocked_or_unauthenticated_and_not_cached():
    from src import supervisor
    calls = []
    supervisor.set_llm_router(lambda q: calls.append(q) or "docs_qa")
    try:
        assert client.post("/query", json={"question": "커밋 규칙이 뭐야?"}).status_code == 401
        blocked = post("박지단 진척률 알려줘", user="dev01", role="developer").json()
        assert blocked["answer"] == api.DENY and calls == []
        assert routes(blocked) == [] and [t["step"] for t in blocked["trace"]] == ["guardrail"]
        post("커밋 규칙이 뭐야?")
        post("커밋 규칙이 뭐야?")
        assert calls == ["커밋 규칙이 뭐야?"] * 2  # 같은 질문도 요청마다 라우팅 (실행 간 캐시 없음)
    finally:
        supervisor.set_llm_router(None)


# --- 요청 단위 LLM 사용량(호출 수·지연시간·토큰) ---
def _usage_llm(n):
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from src import tracing
    msgs = [AIMessage("ok", usage_metadata={"input_tokens": 10, "output_tokens": 3, "total_tokens": 13}) for _ in range(n)]
    return GenericFakeChatModel(messages=iter(msgs), callbacks=[tracing.USAGE_HANDLER])


def test_usage_collected_across_worker_threads():
    from src import middleware, tracing
    llm = _usage_llm(2)
    tok = tracing.start_usage()
    llm.invoke("a")
    middleware.call_with_retry(llm.invoke, "b", timeout=5)  # 작업 스레드에서 부른 호출도 같은 요청으로 집계
    u = tracing.end_usage(tok)
    assert u["llm_calls"] == 2 and u["input_tokens"] == 20 and u["output_tokens"] == 6 and u["latency_s"] >= 0


def test_usage_outside_request_is_ignored_and_empty_request_returns_none():
    from src import tracing
    _usage_llm(1).invoke("x")  # 집계가 열리지 않은 호출은 무시
    tok = tracing.start_usage()
    assert tracing.end_usage(tok) is None


def test_query_trace_ends_with_llm_usage_when_llm_called():
    from fastapi.testclient import TestClient
    from helpers import post_query
    from src import api
    llm = _usage_llm(1)
    api.set_handler(lambda req, route: {"answer": llm.invoke(req.question).content, "contexts": [], "trace": [],
                                        "pending_approval": False})
    try:
        d = post_query(TestClient(api.app), {"question": "이번 주 진척 알려줘", "user_id": "pm01"}).json()
    finally:
        api.set_handler(None)
    last = d["trace"][-1]
    assert last["step"] == "llm_usage" and last["output"]["llm_calls"] == 1 and last["output"]["input_tokens"] == 10


def test_progress_events_from_llm_and_tools_reach_sink():
    from src import agent, tracing
    got = []
    tok = tracing.start_events(lambda k, d: got.append((k, d)))
    try:
        _usage_llm(1).invoke("x")
        agent._progress_tools("pm", "pm01", [])[0].invoke({})
    finally:
        tracing.end_events(tok)
    kinds = [k for k, _ in got]
    assert kinds[:2] == ["llm_start", "llm_end"] and ("tool_call", {"tool": "calc_progress"}) in got
    tracing.emit("route", {"output": "x"})  # 수신자가 없으면 아무 일도 없다
    assert len(got) == 3
