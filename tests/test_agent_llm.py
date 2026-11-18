"""LLM 에이전트 계층 테스트 (가짜 채팅 모델 주입, AWS 불필요).

Supervisor LLM 라우터(LCEL+Pydantic), 문서 Q&A(RAG), 진척 ReAct 에이전트, Plan-Execute + interrupt() HITL을 검증한다.
"""
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from src import agent, api, config, confluence, meeting, supervisor


class FakeLLM(GenericFakeChatModel):
    """메시지 목록을 차례로 돌려주고, 구조화 출력은 스키마 이름별 고정 객체를 돌려주는 가짜 모델."""
    structured: dict = {}

    def with_structured_output(self, schema, **kw):
        return RunnableLambda(lambda _: self.structured[schema.__name__])

    def bind_tools(self, tools, **kw):
        return self


def fake(messages=(), **structured):
    return FakeLLM(messages=iter(list(messages)), structured=structured)


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_DIR", str(tmp_path / "pages"))
    monkeypatch.setenv("GENERATED_DIR", str(tmp_path / "generated"))
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "trace"))
    monkeypatch.setenv("RAG_LEVERS", "hybrid,expand,filter,grounded")
    api.THREADS.clear()
    yield
    api.set_handler(None)
    api.set_graph_resume(None)
    supervisor.set_llm_router(None)
    config.set_llm(None)


def wire(llm):
    config.set_llm(llm)
    agent._flow = meeting.MeetingFlow(llm, agent._publish)
    agent.reset()
    api.set_handler(agent.handle)
    api.set_graph_resume(agent.resume_meeting)


def test_llm_router_uses_structured_output():
    llm = fake(RouteDecision=supervisor.RouteDecision(route="docs_qa", reason="문서 질문"))
    assert supervisor.make_llm_router(llm)("TASK-012가 왜 지연됐어?") == "docs_qa"


def test_docs_qa_llm_answer_with_contexts():
    config.set_llm(fake([AIMessage("인증 키 발급 지연이 블로커다. [MIN-2026-W46]")]))
    out = agent.answer_docs_qa("TASK-012가 왜 지연됐고, 지난 회의에서 어떤 조치를 하기로 했어?", "pm_pl", "pm01")
    assert "[MIN-2026-W46]" in out["answer"]
    assert any(c["doc_id"] == "MIN-2026-W46" for c in out["contexts"])
    steps = [t["step"] for t in out["trace"]]
    assert "tool_call" in steps and "llm_answer" in steps


def test_progress_react_calls_tools_and_keeps_tool_numbers():
    calls = [{"name": "calc_progress", "args": {}, "id": "1"}, {"name": "calc_remaining_mm", "args": {}, "id": "2"}]
    config.set_llm(fake([AIMessage("", tool_calls=calls), AIMessage("이번 주 본인 업무를 요약했습니다.")]))
    agent.reset()
    out = agent.answer_progress("이번주 내 진척률이랑 남은 MM 알려줘", "developer", "dev01")
    tools_called = [t["input"]["tool"] for t in out["trace"] if t["step"] == "tool_call"]
    assert sorted(tools_called) == ["calc_progress", "calc_remaining_mm"]  # 병렬 실행이라 순서 무관
    assert "[도구 계산 결과]" in out["answer"] and "진척률" in out["answer"]
    assert "박서연" not in out["answer"]  # 개발자 본인 범위만


def test_developer_tool_scope_enforced_in_code():
    called = []
    calc = agent._progress_tools("developer", "dev01", called)[0]
    from src import tools
    other = next(t["task_id"] for t in tools.load_tasks() if t["owner"] != "dev01")
    assert calc.invoke({"task_id": other}) == "해당 정보는 조회 권한이 없습니다"


def _meeting_llm():
    return fake(Plan=meeting.Plan(steps=["draft_material", "collect_progress"]),  # 순서·누락은 코드가 보완
                MeetingDraft=meeting.MeetingDraft(decisions=["TASK-012는 목 서버로 진행"],
                                                  blockers=["TASK-012 인증 키 지연"],
                                                  action_items=["TASK-012 인증 키 재요청(PL)"]),
                RouteDecision=supervisor.RouteDecision(route="meeting_material", reason="회의 자료"))


def test_meeting_plan_execute_interrupt_and_approve():
    llm = _meeting_llm()
    wire(llm)
    supervisor.set_llm_router(supervisor.make_llm_router(llm))
    client = TestClient(api.app)
    d = client.post("/query", json={"question": "이번 주 회의 자료 만들어서 Confluence에 올려줘",
                                    "user_id": "pm01", "role": "pm_pl"}).json()
    assert d["pending_approval"] is True
    assert all(h in d["answer"] for h in ("## 결정사항", "## 블로커", "## 액션아이템", "## 진척 현황"))
    plan = next(t for t in d["trace"] if t["step"] == "plan")
    assert plan["output"] == list(meeting.STEPS)
    assert confluence.list_pages() == []  # 승인 전 게시 없음
    # 개발자는 승인할 수 없다
    dev = client.post("/query", json={"question": "승인", "user_id": "dev01", "role": "developer",
                                      "thread_id": d["thread_id"], "approval": "approve"}).json()
    assert dev["answer"] == "해당 정보는 조회 권한이 없습니다" and confluence.list_pages() == []
    ok = client.post("/query", json={"question": "승인", "user_id": "pm01", "role": "pm_pl",
                                     "thread_id": d["thread_id"], "approval": "approve"}).json()
    assert "게시했습니다" in ok["answer"] and len(confluence.list_pages()) == 1
    assert any(t["step"] == "confluence_publish" for t in ok["trace"])
    # 장기 메모리에 이번 주 액션아이템 저장
    items = agent._flow.store.search(("action_items",))
    assert items and "TASK-012 인증 키 재요청(PL)" in items[0].value["items"]


def test_meeting_reject_not_published():
    llm = _meeting_llm()
    wire(llm)
    supervisor.set_llm_router(supervisor.make_llm_router(llm))
    client = TestClient(api.app)
    d = client.post("/query", json={"question": "이번 주 회의 자료 만들어줘", "user_id": "pm01", "role": "pm_pl"}).json()
    r = client.post("/query", json={"question": "반려", "user_id": "pm01", "role": "pm_pl",
                                    "thread_id": d["thread_id"], "approval": "reject"}).json()
    assert "게시하지 않았습니다" in r["answer"] and confluence.list_pages() == []
