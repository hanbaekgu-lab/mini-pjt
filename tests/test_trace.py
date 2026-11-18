"""Trace(라우팅·도구 호출·로컬 기록)와 MCP/LLM 타임아웃·재시도 테스트."""
import json
import time

import pytest
from fastapi.testclient import TestClient

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


def post(q, user="pm01", role="pm_pl", **kw):
    return client.post("/query", json={"question": q, "user_id": user, "role": role, **kw})


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
    post("k@sds.com 진척 알려줘")
    text = "".join(f.read_text(encoding="utf-8") for f in (tmp_path / "trace").glob("*.json"))
    assert "k@sds.com" not in text


def test_retry_succeeds_after_failures():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("x")
        return "ok"
    assert middleware.call_with_retry(flaky) == "ok" and len(calls) == 3


def test_retry_timeout_gives_up(monkeypatch):
    monkeypatch.setattr(middleware, "TIMEOUT", 0.05)
    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.3)
    with pytest.raises(middleware.CallFailed):
        middleware.call_with_retry(slow)
    assert len(calls) == 3  # 최초 1 + 재시도 2


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
    assert len(attempts) == 3
    assert confluence.list_pages() == []
    assert any(t["step"] == "confluence_publish" and "error" in str(t["output"]) for t in d["trace"])


def test_llm_timeout_reports_error(monkeypatch):
    monkeypatch.setattr(middleware, "LLM_TIMEOUT", 0.05)
    n = []

    def slow_llm(req):
        n.append(1)
        time.sleep(0.3)
    api.set_handler(slow_llm)
    d = post("커밋 규칙이 뭐야?").json()
    assert d["answer"].startswith("오류") and "타임아웃" in d["answer"]
    assert len(n) == 3
    assert routes(d) == ["docs_qa"]
