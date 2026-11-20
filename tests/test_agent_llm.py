"""LLM 에이전트 계층 테스트 (가짜 채팅 모델 주입, AWS 불필요).

Supervisor LLM 라우터(LCEL+Pydantic), 문서 Q&A(RAG), 진척 ReAct 에이전트, Plan-Execute + interrupt() HITL을 검증한다.
"""
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from helpers import post_query
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


def test_docs_qa_last_week_resolves_to_previous_week_minutes():
    """'지난주' 질문: 코드가 기준 주(2026-11-16) 직전 주 회의록(MIN-2026-W46)을 골라 LLM 문맥과 [기준 정보]로 넘긴다.
    (LLM은 오늘 날짜를 모르므로 이 안내가 없으면 회의록이 '지난주'인지 확인하지 못해 근거 없음으로 답했다)"""
    seen = []

    def llm(prompt):
        seen.append(prompt.to_string())
        return AIMessage("PG사 테스트 계정 발급 재요청(강앙리)이 미완료다. [MIN-2026-W46]")
    config.set_llm(RunnableLambda(llm))
    out = agent.answer_docs_qa("지난주 액션아이템 중에 아직 완료 안 된 게 뭐야?", "pm", "pm01")
    ids = [c["doc_id"] for c in out["contexts"]]
    # 검색 1순위는 지난주 회의록이고, 답이 기댄 기준 정보(지난주 = W46)는 검색 문서 뒤에 근거로 함께 돌려준다
    assert ids[0] == "MIN-2026-W46" and ids[-1] == "tool:week_note" and not {"MIN-2026-W45", "MIN-2026-W47"} & set(ids)
    assert "MIN-2026-W46(2026-11-13)" in out["contexts"][-1]["text"]
    prompt = seen[0]
    assert "[기준 정보]" in prompt and "2026-11-09(월) 주" in prompt and "MIN-2026-W46(2026-11-13)" in prompt
    # 액션아이템 줄(상태 포함)이 가드레일 정리 후에도 그대로 LLM 문맥에 들어간다
    assert "PG사 테스트 계정 발급 재요청(강앙리) - 미완료" in prompt
    assert "회원 데이터 이관 검증 결과 공유(서살라) - 미완료" in prompt


def test_docs_qa_week_note_only_with_filter_lever(monkeypatch):
    # 1차 baseline(dense)은 사전 고정 설정이라 기준 정보를 넣지 않는다
    monkeypatch.setenv("RAG_LEVERS", "dense")
    from src import retriever
    assert retriever.week_note("지난주 액션아이템 뭐야?") == ""
    assert retriever.week_context("지난주 액션아이템 뭐야?") is None


def test_week_context_only_for_relative_week_questions():
    from src import retriever
    assert retriever.week_context("지난 회의에서 어떤 조치를 하기로 했어?")["doc_id"] == "tool:week_note"
    # 상대 주차가 없는 질문은 기준 정보를 답에 쓰지 않으므로 근거에 넣지 않는다
    assert retriever.week_context("커밋 메시지는 어떤 형식으로 써야 해?") is None
    config.set_llm(fake([AIMessage("커밋 메시지는 '[화면ID] 작업 요약' 형식이다. [RULE-COMMIT]")]))
    out = agent.answer_docs_qa("커밋 메시지는 어떤 형식으로 써야 해?", "developer", "dev01")
    assert all(not c["doc_id"].startswith("tool:") for c in out["contexts"])


def test_grounded_prompt_keeps_document_action_item_status():
    # 이미 완료된 액션아이템을 '~하기로 했다'로 쓰던 오류(3번) 방지
    assert "완료·미완료 상태를 문서 표기 그대로" in agent.GROUNDED_PROMPT
    assert "완료·미완료" not in agent.BASELINE_PROMPT  # 1차 baseline은 사전 고정 그대로
    # 두 가지 이상을 묻는 질문은 각 부분을 질문의 대상·핵심어로 시작해 답하되, 목록·표 형식은 유지한다
    assert "각 부분을 질문의 대상과 핵심어를 넣은 문장으로 시작" in agent.GROUNDED_PROMPT
    assert "목록·표 형식은 그대로" in agent.GROUNDED_PROMPT and "각 부분" not in agent.BASELINE_PROMPT


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
    d = post_query(client, {"question": "이번 주 회의 자료 만들어서 Confluence에 올려줘",
                                    "user_id": "pm01", "role": "pm"}).json()
    assert d["pending_approval"] is True
    assert all(h in d["answer"] for h in ("## 결정사항", "## 블로커", "## 액션아이템", "## 진척 현황"))
    plan = next(t for t in d["trace"] if t["step"] == "plan")
    assert plan["output"] == list(meeting.STEPS)
    assert confluence.list_pages() == []  # 승인 전 게시 없음
    # 개발자는 승인할 수 없다
    dev = post_query(client, {"question": "승인", "user_id": "dev01", "role": "developer",
                                      "thread_id": d["thread_id"], "approval": "approve"}).json()
    assert dev["answer"] == "해당 정보는 조회 권한이 없습니다" and confluence.list_pages() == []
    ok = post_query(client, {"question": "승인", "user_id": "pm01", "role": "pm",
                                     "thread_id": d["thread_id"], "approval": "approve"}).json()
    assert "게시했습니다" in ok["answer"] and len(confluence.list_pages()) == 1
    page = confluence.get_page(confluence.list_pages()[0])["body"]
    # 게시 꼬리말은 실제 내용과 맞아야 한다: 공용 자료라 이름·연락처·개인별 진척률이 없다
    assert page.rstrip().endswith(agent.PUBLISH_FOOTER) and "이름만 표기" not in page
    assert not [x for d in tools.load_developers() for x in (d["name"], d["email"]) if x in page]
    assert "@" not in page
    assert ok["contexts"] == [] and dev["contexts"] == []  # R3: 승인·거절 응답에 초안 contexts 없음
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
    d = post_query(client, {"question": "이번 주 회의 자료 만들어줘", "user_id": "pm01", "role": "pm"}).json()
    r = post_query(client, {"question": "반려", "user_id": "pm01", "role": "pm",
                                    "thread_id": d["thread_id"], "approval": "reject"}).json()
    assert "게시하지 않았습니다" in r["answer"] and confluence.list_pages() == []


def test_progress_followup_question_forces_fresh_tool_calls():
    """같은 대화의 다른 질문에서 LLM이 도구 없이 이전 답을 재사용하면, 도구를 다시 부르게 하고 최신 결과만 쓴다."""
    first = [{"name": "calc_progress", "args": {}, "id": "1"}]
    again = [{"name": "calc_progress", "args": {}, "id": "2"}]
    config.set_llm(fake([AIMessage("", tool_calls=first), AIMessage("PL1 파트 진척을 요약했습니다."),
                         AIMessage("앞선 답과 같이 중메뉴 15개입니다."),  # 도구 없이 재사용(잘못된 개수)
                         AIMessage("", tool_calls=again), AIMessage("최신 도구 결과의 중메뉴별·단계별 현황입니다.")]))
    agent.reset()
    agent.answer_progress("내 파트 진척 알려줘", "pl", "pl01")
    out = agent.answer_progress("내 파트 중메뉴별·단계별 현황 알려줘", "pl", "pl01")
    assert [t["input"]["tool"] for t in out["trace"] if t["step"] == "tool_call"] == ["calc_progress"]
    assert out["answer"].startswith("최신 도구 결과") and "중메뉴 15개" not in out["answer"]
    assert "질문마다 도구를 새로 호출" in agent.PROGRESS_SYSTEM and "최신 도구 결과" in agent.PROGRESS_SYSTEM


def test_progress_memory_is_per_thread():
    # Y15: 대화 기억은 사용자별이 아니라 thread별 (같은 사용자의 다른 thread와 섞이지 않음)
    config.set_llm(fake([AIMessage("첫 답"), AIMessage("", tool_calls=[{"name": "calc_progress", "args": {}, "id": "1"}]),
                         AIMessage("둘째 답")]))
    agent.reset()
    agent.answer_progress("내 진척 알려줘", "developer", "dev01", thread_id="t1")
    cfg = lambda t: {"configurable": {"thread_id": f"progress-{t}"}}  # noqa: E731
    assert agent._memory.get(cfg("t1")) is not None and agent._memory.get(cfg("t2")) is None


@pytest.mark.parametrize("role,user,part,ok", [
    ("pm", "pm01", "PL2", True), ("pl", "pl02", "PL2", True), ("pl", "pl01", "PL2", False),
    ("developer", "dev01", "PL1", False),
])
def test_part_argument_scope(role, user, part, ok):
    # Y16: 진척 도구 part 인자 (PM 전체, PL 본인 파트, 개발자 불가)
    calc, remain = agent._progress_tools(role, user, [])[:2]
    out = calc.invoke({"part": part})
    assert (f"{part} 파트 합계" in out) is ok and (out == api.DENY) is (not ok)
    assert (remain.invoke({"part": part}) == api.DENY) is (not ok)


def test_team_report_prints_part_rows():
    exp = tools.build_progress_report(part="PL2")["progress"]
    text = tools.format_progress_report(tools.build_progress_report())
    assert "## 파트별" in text
    assert f"- PL2 파트: 진척률 {exp['progress'] * 100:.2f}%, 남은 {exp['remaining_mm']:.2f}MM" in text


@pytest.mark.parametrize("user,role,expect", [
    ("pm01", "pm", None),                       # PM: 전체 미연결 커밋
    ("pl01", "pl", {"dev01", "dev02", "dev03"}),  # PL1: 본인 파트 구성원 커밋만
    ("dev01", "developer", {"dev01"}),            # 개발자: 본인 커밋만
])
def test_appended_report_lists_unlinked_commit_authors_masked_in_scope(user, role, expect):
    """[도구 계산 결과]의 미연결 커밋에도 get_commits 도구처럼 작성자 이름·이메일을 싣는다(출력 단계 마스킹).
    답변 서술과 근거 표가 같은 정보를 보여 줘야 judge가 이메일을 지어낸 것으로 보지 않는다."""
    calls = [{"name": "get_commits", "args": {}, "id": "1"}]
    llm = fake([AIMessage("", tool_calls=calls), AIMessage("미연결 커밋을 정리했습니다."),
                AIMessage("", tool_calls=calls), AIMessage("미연결 커밋을 정리했습니다.")],
               RouteDecision=supervisor.RouteDecision(route="progress", reason="커밋"))
    wire(llm)
    supervisor.set_llm_router(supervisor.make_llm_router(llm))
    ans = post_query(TestClient(api.app), {"question": "화면 ID 없이 올라온 커밋들 있어?",
                                           "user_id": user, "role": role}).json()["answer"]
    section = ans[ans.index("## 미연결 커밋"):]
    devs = {d["developer_id"]: d for d in tools.load_developers()}
    unlinked = tools.get_commits()["unlinked"]
    visible = [c for c in unlinked if expect is None or c["developer_id"] in expect]
    assert visible and len(visible) < len(unlinked) or expect is None
    for c in unlinked:
        d = devs[c["developer_id"]]
        masked = f"{d['name']} <{d['email'][0]}***@{d['email'].split('@')[1]}>"
        assert (f"- {c['hash']} {masked}" in section) is (c in visible)
        assert d["email"] not in ans  # 원문 이메일은 어디에도 없다


def test_meeting_draft_falls_back_when_structured_output_fails():
    """LLM 구조화 출력이 실패(예: 출력 토큰 한도로 잘려 필드 누락)해도 요청 전체가 실패하지 않고 결정적 초안으로 대신한다."""
    flow = meeting.MeetingFlow(fake(Plan=meeting.Plan(steps=list(meeting.STEPS))), lambda t, b: "p")  # MeetingDraft 없음 → 실패
    tid, st = flow.start()
    assert all(h in st["body"] for h in ("## 결정사항", "## 블로커", "## 액션아이템"))
    assert any(t["step"] == "llm_draft" and t["output"].startswith("fallback:default_draft") for t in st["log"])


def test_meeting_material_report_has_no_commit_authors():
    # 회의 자료(공용 문서) 입력·초안에는 미연결 커밋 작성자 이름·이메일을 싣지 않는다
    flow = meeting.MeetingFlow(_meeting_llm(), lambda t, b: "p")
    text = flow._collect_progress({"log": []})["report_text"]
    assert "c0436" in text and "@" not in text
    assert not [d["name"] for d in tools.load_developers() if d["name"] in text]


def test_check_previous_meeting_reads_last_week_page_via_mcp():
    pages = []
    body = "# 주간회의 자료 2026-11-09\n\n## 액션아이템\n- [ ] SCR-C02-01-3 완료(강앙리)"
    flow = meeting.MeetingFlow(_meeting_llm(), lambda t, b: "p", lambda pid: pages.append(pid) or {"body": body})
    st = flow._check_previous_meeting({"log": []})
    assert pages == ["weekly_meeting_2026-11-09"]
    step = next(t for t in st["log"] if t["input"]["tool"] == "get_page")
    assert step["output"] == "mcp:weekly_meeting_2026-11-09"
    assert "SCR-C02-01-3 완료" in st["previous_page"] and "강앙리" not in st["previous_page"]


def test_check_previous_meeting_falls_back_to_snapshot():
    def missing(pid):
        raise RuntimeError("page not found")
    flow = meeting.MeetingFlow(_meeting_llm(), lambda t, b: "p", missing)
    st = flow._check_previous_meeting({"log": []})
    step = next(t for t in st["log"] if t["input"]["tool"] == "get_page")
    assert step["output"].startswith("snapshot:2026-11-09") and st["previous_page"]


def test_check_previous_meeting_reads_committed_page_through_real_mcp(monkeypatch):
    # 실제 stdio MCP 서버로 커밋된 지난주 페이지(data/confluence_pages)를 읽는다
    from pathlib import Path
    monkeypatch.setenv("CONFLUENCE_DIR", str(Path(agent.__file__).resolve().parent.parent / "data/confluence_pages"))
    flow = meeting.MeetingFlow(_meeting_llm(), lambda t, b: "p", agent._read_page)
    st = flow._check_previous_meeting({"log": []})
    step = next(t for t in st["log"] if t["input"]["tool"] == "get_page")
    assert step["output"] == "mcp:weekly_meeting_2026-11-09" and st["previous_page"]


def test_progress_tools_accept_developer_name_from_llm():
    """ReAct LLM은 개발자 ID를 모르므로 developer_id 자리에 이름을 넣는다. 도구가 이름을 ID로 바꿔 범위를 검사한다."""
    by_id = lambda role, uid, dev: {t.name: t for t in agent._progress_tools(role, uid, [])}["calc_progress"].invoke(  # noqa: E731
        {"developer_id": dev})
    pm = by_id("pm", "pm01", "정카카")
    assert pm == by_id("pm", "pm01", "dev05") and "찾을 수 없" not in pm
    assert by_id("pl", "pl01", "박지단님") == by_id("pl", "pl01", "dev02")      # PL1 구성원은 이름으로도 조회
    assert by_id("pl", "pl01", "한피구") == agent.guardrails.DENY                  # 다른 파트 구성원은 이름이어도 거절
    assert by_id("developer", "dev01", "박지단") == agent.guardrails.DENY


def test_bedrock_clients_have_explicit_timeout_and_retries():
    c = config.boto_config()
    assert c.read_timeout == 120 and c.connect_timeout == 10
    assert c.retries == {"max_attempts": 3, "mode": "standard"}


def test_progress_tool_rejects_malformed_screen_id_and_agent_recovers():
    """형식이 틀린 화면 ID는 도구 실행 전에 검증 오류로 LLM에 돌아가고, LLM이 고쳐 다시 호출한다."""
    bad = [{"name": "calc_progress", "args": {"screen_id": "scr-a01"}, "id": "1"}]
    good = [{"name": "calc_progress", "args": {"screen_id": "SCR-A01-03"}, "id": "2"}]
    config.set_llm(fake([AIMessage("", tool_calls=bad), AIMessage("", tool_calls=good), AIMessage("SCR-A01-03 현황입니다.")]))
    agent.reset()
    out = agent.answer_progress("SCR-A01-03 진척 알려줘", "pm", "pm01")
    assert [t["input"]["tool"] for t in out["trace"] if t["step"] == "tool_call"] == ["calc_progress"]  # 실제 실행은 1번
    assert "SCR-A01-03" in out["answer"]


def test_progress_agent_tool_calls_are_capped():
    """도구를 끝없이 부르는 LLM이어도 한 번 실행에서 TOOL_CALL_LIMIT까지만 실행된다."""
    loop = [AIMessage("", tool_calls=[{"name": "calc_progress", "args": {}, "id": f"c{i}"}]) for i in range(15)]
    config.set_llm(fake(loop + [AIMessage("요약입니다.")]))
    agent.reset()
    out = agent.answer_progress("내 진척률 알려줘", "developer", "dev01")
    n = sum(1 for t in out["trace"] if t["step"] == "tool_call")
    assert 1 <= n <= agent.TOOL_CALL_LIMIT and "[도구 계산 결과]" in out["answer"]


def test_meeting_edit_then_approve_publishes_edited_title_and_body():
    """HITL 수정 후 승인: PL이 제목·본문을 고쳐 승인하면 고친 내용으로 게시하고, 구성원 이름은 지우고 연락처는 가린다."""
    llm = _meeting_llm()
    wire(llm)
    supervisor.set_llm_router(supervisor.make_llm_router(llm))
    client = TestClient(api.app)
    d = post_query(client, {"question": "이번 주 회의 자료 만들어줘", "user_id": "pl01", "role": "pl"}).json()
    body = "## 결정사항\n- 결제 화면은 강앙리 확인 후 배포 (문의 ang.kang@example.com)\n\n## 액션아이템\n- [ ] 배포 체크리스트 점검"
    ok = post_query(client, {"question": "고쳐서 승인", "user_id": "pl01", "role": "pl", "thread_id": d["thread_id"],
                             "approval": "edit", "edits": {"title": "주간회의 자료 2026-11-16 (수정)", "body": body}}).json()
    assert "수정 내용을 반영해 승인되어" in ok["answer"] and ok["pending_approval"] is False and ok["contexts"] == []
    assert any(t["step"] == "hitl_edit" and t["output"] == "title,body" for t in ok["trace"])
    page = confluence.get_page(confluence.list_pages()[0])
    assert page["title"] == "주간회의 자료 2026-11-16 (수정)"
    assert "배포 체크리스트 점검" in page["body"] and "강앙리" not in page["body"] and "ang.kang@" not in page["body"]
    assert page["body"].rstrip().endswith(agent.PUBLISH_FOOTER)
    snap = {it.key: it.value for it in agent._flow.snapshots()}["2026-11-16"]
    assert snap["action_items"] == ["배포 체크리스트 점검"]  # 수정본의 액션아이템이 다음 주 '지난주'가 된다
    again = post_query(client, {"question": "다시", "user_id": "pl01", "role": "pl", "thread_id": d["thread_id"],
                                "approval": "approve"}).json()
    assert again["answer"] == ok["answer"] and len(confluence.list_pages()) == 1  # 멱등


def test_meeting_edit_by_developer_is_denied():
    llm = _meeting_llm()
    wire(llm)
    supervisor.set_llm_router(supervisor.make_llm_router(llm))
    client = TestClient(api.app)
    d = post_query(client, {"question": "이번 주 회의 자료 만들어줘", "user_id": "pm01", "role": "pm"}).json()
    r = post_query(client, {"question": "고쳐서 승인", "user_id": "dev01", "role": "developer", "thread_id": d["thread_id"],
                            "approval": "edit", "edits": {"body": "## 결정사항\n- 임의 수정"}}).json()
    assert r["answer"] == "해당 정보는 조회 권한이 없습니다" and confluence.list_pages() == []


# --- Replan: 단계 실패 시 다시 시도·건너뛰기·중단 ---
def _flow_with(replan_action, monkeypatch, fail_step, fail_times=99):
    llm = fake(Plan=meeting.Plan(steps=list(meeting.STEPS)),
               MeetingDraft=meeting.MeetingDraft(decisions=["결정"], blockers=["블로커"], action_items=["할 일"]),
               Replan=meeting.Replan(action=replan_action, reason="테스트"))
    flow = meeting.MeetingFlow(llm, lambda t, b: "p")
    orig, n = getattr(flow, "_" + fail_step), {"calls": 0}

    def flaky(state):
        n["calls"] += 1
        if n["calls"] <= fail_times:
            raise TimeoutError("MCP 타임아웃")
        return orig(state)
    monkeypatch.setattr(flow, "_" + fail_step, flaky)
    return flow, n


def test_replan_retry_recovers_transient_failure(monkeypatch):
    flow, n = _flow_with("retry", monkeypatch, "check_previous_meeting", fail_times=1)
    _, st = flow.start()
    assert n["calls"] == 2 and st.get("body") and not st.get("error")
    rp = [t for t in st["log"] if t["step"] == "replan"]
    assert rp[0]["output"]["action"] == "retry" and rp[0]["input"]["failed_step"] == "check_previous_meeting"


def test_replan_skips_optional_step_and_still_drafts(monkeypatch):
    flow, n = _flow_with("skip", monkeypatch, "check_previous_meeting")
    _, st = flow.start()
    assert n["calls"] == 1 and "## 결정사항" in st["body"]  # 지난주 확인 없이도 초안 작성 → 승인 대기


def test_replan_cannot_skip_required_step_and_aborts(monkeypatch):
    flow, n = _flow_with("skip", monkeypatch, "collect_progress")  # LLM이 skip이라 해도 필수 단계는 건너뛸 수 없다
    _, st = flow.start()
    assert n["calls"] == 2 and st.get("error") and not st.get("body")  # 1회 재시도 후 중단, 승인 대기 없음
    acts = [t["output"]["action"] for t in st["log"] if t["step"] == "replan"]
    assert acts == ["retry", "abort"]


def test_aborted_meeting_returns_error_without_pending(monkeypatch):
    llm = _meeting_llm()
    wire(llm)
    monkeypatch.setattr(agent._flow, "_collect_progress", lambda s: (_ for _ in ()).throw(RuntimeError("DB 없음")))
    out = agent.answer_meeting("pm")
    assert out["pending_approval"] is False and "만들지 못했습니다" in out["answer"] and "_draft" not in out


# --- 영구 저장 (STATE_DB): 재시작해도 승인 대기·장기 메모리·thread 기록 유지 ---
def test_pending_approval_survives_restart_with_state_db(tmp_path, monkeypatch):
    from src import state
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state" / "agent.sqlite"))
    state.close()
    llm = _meeting_llm()
    wire(llm)
    supervisor.set_llm_router(supervisor.make_llm_router(llm))
    client = TestClient(api.app)
    d = post_query(client, {"question": "이번 주 회의 자료 만들어줘", "user_id": "pm01", "role": "pm"}).json()
    assert d["pending_approval"] is True and confluence.list_pages() == []

    # 재시작 흉내: 메모리의 그래프·thread 기록·세션을 버리고 같은 DB에서 다시 만든다
    api.THREADS.clear()
    api.SESSIONS.clear()
    state.close()
    agent._flow = meeting.MeetingFlow(llm, agent._publish)
    agent.reset(keep_persistent=True)
    api.THREADS.update(state.load_threads())
    assert api.THREADS[d["thread_id"]]["status"] == "pending"
    assert {it.key for it in agent._flow.snapshots()} == {"2026-11-09"}  # 장기 메모리(지난주 스냅샷)도 남아 있다

    ok = post_query(client, {"question": "승인", "user_id": "pm01", "role": "pm", "thread_id": d["thread_id"],
                             "approval": "approve"}).json()  # 다시 로그인한 세션으로 승인
    assert "게시했습니다" in ok["answer"] and len(confluence.list_pages()) == 1
    assert state.load_threads()[d["thread_id"]]["status"] == "approved"
    assert {it.key for it in agent._flow.snapshots()} == {"2026-11-09", "2026-11-16"}
    state.close()


def test_state_defaults_to_memory_without_env(monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.store.memory import InMemoryStore
    from src import state
    monkeypatch.delenv("STATE_DB", raising=False)
    assert isinstance(state.checkpointer(), InMemorySaver) and isinstance(state.store(), InMemoryStore)
    assert state.load_threads() == {}
    state.save_thread("x", {"a": 1})  # 메모리 모드: 아무것도 저장하지 않는다
    assert state.load_threads() == {}


# --- 다중 의도: 진척 수치 + 문서 내용을 함께 묻는 질문 ---
def test_router_returns_multi_only_for_progress_plus_docs():
    mk = lambda **kw: supervisor.make_llm_router(fake(RouteDecision=supervisor.RouteDecision(reason="r", **kw)))  # noqa: E731
    assert mk(route="progress", also="docs_qa")("q") == supervisor.MULTI
    assert mk(route="docs_qa", also="progress")("q") == supervisor.MULTI   # 순서와 관계없이 진척 → 문서
    assert mk(route="docs_qa", also="docs_qa")("q") == "docs_qa"
    assert mk(route="meeting_material", also="progress")("q") == "meeting_material"  # 회의 자료는 묶지 않는다
    assert mk(route="docs_qa")("q") == "docs_qa"


def test_multi_intent_query_answers_progress_then_docs():
    """다중 의도: 진척 → 문서 순서로 두 핸들러를 부르고 답·근거·trace를 합친다 (가드레일은 질문 전체로 한 번)."""
    seen = []

    def handler(req, route):
        seen.append(route)
        # 질문(검색어)은 그대로 두고, 역할 안내는 focus로 넘긴다: 진척 담당은 수치만, 문서 담당은 이유·결정·조치만
        assert req.question == "SCR-C02-01 진척률이랑 지연 이유 알려줘" and req.focus == api.MULTI_HINT[route]
        doc = {"progress": ("SCR-C02-01 진척률 50%", "tool:build_progress_report"),
               "docs_qa": ("PG사 계정 지연이 원인 [MIN-2026-W46]", "MIN-2026-W46")}[route]
        return {"answer": doc[0], "contexts": [{"doc_id": doc[1], "text": doc[0]}],
                "trace": [{"step": "tool_call", "input": {"tool": route}, "output": "ok"}], "pending_approval": False}
    api.set_handler(handler)
    supervisor.set_llm_router(lambda q: supervisor.MULTI)
    d = post_query(TestClient(api.app), {"question": "SCR-C02-01 진척률이랑 지연 이유 알려줘",
                                         "user_id": "pm01", "role": "pm"}).json()
    assert seen == ["progress", "docs_qa"]
    assert d["answer"] == "[진척]\nSCR-C02-01 진척률 50%\n\n[문서 근거]\nPG사 계정 지연이 원인 [MIN-2026-W46]"
    assert next(t for t in d["trace"] if t["step"] == "supervisor_route")["output"] == supervisor.MULTI
    assert [t["input"] for t in d["trace"] if t["step"] == "multi_intent"] == ["progress", "docs_qa"]
    assert [c["doc_id"] for c in d["contexts"]] == ["tool:build_progress_report", "MIN-2026-W46"]



def test_docs_focus_goes_to_prompt_not_retrieval():
    seen = []

    def llm(prompt):
        seen.append(prompt.to_string())
        return AIMessage("PG사 테스트 계정 발급 지연 [MIN-2026-W46]")
    config.set_llm(RunnableLambda(llm))
    q = "SCR-C02-01 진척률이랑 지연 이유 알려줘"
    plain = agent.answer_docs_qa(q, "pm", "pm01")
    focused = agent.answer_docs_qa(q, "pm", "pm01", focus=api.MULTI_HINT["docs_qa"])
    assert [c["doc_id"] for c in plain["contexts"]] == [c["doc_id"] for c in focused["contexts"]]  # 검색 결과 동일
    assert "[답변 범위]" in seen[1] and "[답변 범위]" not in seen[0]


def test_multi_intent_drops_out_of_scope_no_answer_from_docs_part():
    na = agent.NO_ANSWER
    mixed = f"## 지연 화면\n\n{na}\n\n## 지난 회의 결정사항\n\n- 목 PG 서버로 단위테스트 [MIN-2026-W46]"
    assert api._drop_no_answer(mixed) == "## 지난 회의 결정사항\n\n- 목 PG 서버로 단위테스트 [MIN-2026-W46]"
    assert api._drop_no_answer(na) == na                       # 내용이 전혀 없으면 '근거 없음'은 그대로 둔다
    assert api._drop_no_answer("이유는 PG 지연 [MIN-2026-W46]") == "이유는 PG 지연 [MIN-2026-W46]"
