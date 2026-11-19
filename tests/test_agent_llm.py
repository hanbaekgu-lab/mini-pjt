"""LLM 에이전트 계층 테스트 (가짜 채팅 모델 주입, AWS 불필요).

Supervisor LLM 라우터(LCEL+Pydantic), 문서 Q&A(RAG), 진척 ReAct 에이전트, Plan-Execute + interrupt() HITL을 검증한다.
"""
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from src import agent, api, config, confluence, meeting, supervisor, tools


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
    assert supervisor.make_llm_router(llm)("SCR-C02-01이 왜 지연됐어?") == "docs_qa"


def test_docs_qa_llm_answer_with_contexts():
    config.set_llm(fake([AIMessage("PG사 테스트 계정 발급 지연이 블로커다. [MIN-2026-W46]")]))
    out = agent.answer_docs_qa("SCR-C02-01이 왜 지연됐고, 지난 회의에서 어떤 조치를 하기로 했어?", "pl", "pl01")
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
    assert "박지단" not in out["answer"]  # 개발자 본인 범위만


def test_developer_tool_scope_enforced_in_code():
    called = []
    calc = agent._progress_tools("developer", "dev01", called)[0]
    from src import tools
    other = next(s["screen_id"] for s in tools.load_screens() if s["owner"] != "dev01")
    assert calc.invoke({"screen_id": other}) == "해당 정보는 조회 권한이 없습니다"


def test_pl_tool_scope_enforced_in_code():
    from src import tools
    calc = agent._progress_tools("pl", "pl01", [])[0]
    own = next(s["screen_id"] for s in tools.load_screens() if s["part"] == "PL1")
    other = next(s["screen_id"] for s in tools.load_screens() if s["part"] == "PL2")
    assert calc.invoke({"screen_id": other}) == "해당 정보는 조회 권한이 없습니다"
    assert own in calc.invoke({"screen_id": own})
    whole = calc.invoke({})
    assert "PL1 파트 합계" in whole and "윤베일" not in whole


@pytest.mark.parametrize("role,user,target,ok", [
    ("pm", "pm01", "dev06", True), ("pl", "pl01", "dev02", True), ("pl", "pl01", "dev06", False),
    ("developer", "dev01", "dev01", True), ("developer", "dev01", "dev02", False),
])
def test_individual_progress_tool_scope(role, user, target, ok):
    # 개인 진척은 요청 시에만: PM 누구나, PL 본인 파트 구성원, 개발자 본인만
    calc, remain = agent._progress_tools(role, user, [])[:2]
    out = calc.invoke({"developer_id": target})
    assert (f"({target}) 담당 합계" in out) is ok
    assert (out == "해당 정보는 조회 권한이 없습니다") is (not ok)
    assert (remain.invoke({"developer_id": target}) == "해당 정보는 조회 권한이 없습니다") is (not ok)


def _meeting_llm():
    return fake(Plan=meeting.Plan(steps=["draft_material", "collect_progress"]),  # 순서·누락은 코드가 보완
                MeetingDraft=meeting.MeetingDraft(decisions=["SCR-C02-01은 목 PG 서버로 단위테스트"],
                                                  blockers=["SCR-C02-01 PG사 테스트 계정 지연"],
                                                  action_items=["PG사 테스트 계정 재요청(강앙리)"]),
                RouteDecision=supervisor.RouteDecision(route="meeting_material", reason="회의 자료"))


def test_meeting_plan_execute_interrupt_and_approve():
    llm = _meeting_llm()
    wire(llm)
    supervisor.set_llm_router(supervisor.make_llm_router(llm))
    client = TestClient(api.app)
    d = client.post("/query", json={"question": "이번 주 회의 자료 만들어서 Confluence에 올려줘",
                                    "user_id": "pm01", "role": "pm"}).json()
    assert d["pending_approval"] is True
    assert all(h in d["answer"] for h in ("## 결정사항", "## 블로커", "## 액션아이템", "## 진척 현황"))
    plan = next(t for t in d["trace"] if t["step"] == "plan")
    assert plan["output"] == list(meeting.STEPS)
    assert confluence.list_pages() == []  # 승인 전 게시 없음
    # 개발자는 승인할 수 없다
    dev = client.post("/query", json={"question": "승인", "user_id": "dev01", "role": "developer",
                                      "thread_id": d["thread_id"], "approval": "approve"}).json()
    assert dev["answer"] == "해당 정보는 조회 권한이 없습니다" and confluence.list_pages() == []
    ok = client.post("/query", json={"question": "승인", "user_id": "pm01", "role": "pm",
                                     "thread_id": d["thread_id"], "approval": "approve"}).json()
    assert "게시했습니다" in ok["answer"] and len(confluence.list_pages()) == 1
    assert any(t["step"] == "confluence_publish" for t in ok["trace"])
    # 장기 메모리: 지난주 스냅샷으로 초기화 → 게시 후 이번 주 스냅샷 저장 (다음 주 회의의 '지난주')
    snaps = {it.key: it.value for it in agent._flow.snapshots()}
    assert set(snaps) == {"2026-11-09", "2026-11-16"}
    now = snaps["2026-11-16"]
    assert "PG사 테스트 계정 재요청" in now["action_items"]  # 이름은 지워져 저장
    team = tools.build_progress_report()["progress"]
    assert now["team"]["progress"] == team["progress"] and now["team"]["planned_progress"] == team["planned_progress"]
    ids = {i["id"] for i in now["issues"]}
    assert "DLY-SCR-C02-01" in ids and "DLY-SCR-G03-01" not in ids  # 해결된 이슈는 다음 주로 넘기지 않는다
    assert next(i for i in now["issues"] if i["id"] == "DLY-SCR-C02-01")["since"] == "2026-08-31"  # 처음 발생 주 유지
    prev_step = [t for t in d["trace"] if t["input"] == {"tool": "memory_store", "plan_step": "check_previous_meeting"}]
    assert prev_step and prev_step[0]["output"].startswith("store:2026-11-09")
    assert any(t["step"] == "memory_store" and t["output"] == "saved" for t in ok["trace"])
    # 지난주 대비·이슈 추적 섹션이 초안에 들어가고, LLM 초안의 이름은 지워진다
    assert "### 지난주 대비 변화" in d["answer"] and "## 이슈 추적" in d["answer"] and "강앙리" not in d["answer"]


def test_meeting_store_seeded_once_and_reset():
    flow = meeting.MeetingFlow(_meeting_llm(), lambda t, b: "p")
    st = flow._check_previous_meeting({"log": []})
    assert st["previous"]["week"]["start"] == "2026-11-09" and "초기화" in st["previous_source"]
    assert flow._check_previous_meeting({"log": []})["previous_source"] == "store:2026-11-09"  # 두 번째는 Store에서
    assert "## 블로커" in st["previous_minutes"] and st["log"][-1]["output"] == "MIN-2026-W46"
    flow.reset_store()
    assert flow.snapshots() == []


def test_meeting_reject_not_published():
    llm = _meeting_llm()
    wire(llm)
    supervisor.set_llm_router(supervisor.make_llm_router(llm))
    client = TestClient(api.app)
    d = client.post("/query", json={"question": "이번 주 회의 자료 만들어줘", "user_id": "pm01", "role": "pm"}).json()
    r = client.post("/query", json={"question": "반려", "user_id": "pm01", "role": "pm",
                                    "thread_id": d["thread_id"], "approval": "reject"}).json()
    assert "게시하지 않았습니다" in r["answer"] and confluence.list_pages() == []
