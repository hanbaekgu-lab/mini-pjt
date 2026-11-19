"""게시된 회의 자료: 섹션, 도구 수치 일치, MCP 재조회, 런타임 검색 대상, 재게시 교체, 코퍼스 제외."""
import pytest
from fastapi.testclient import TestClient

from helpers import post_query
from src import api, mcp_client, meeting, retriever, tools

client = TestClient(api.app)
Q = "이번 주 회의 자료 초안 만들어줘"


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_DIR", str(tmp_path / "pages"))
    monkeypatch.setenv("GENERATED_DIR", str(tmp_path / "generated"))
    api.THREADS.clear()
    api.set_handler(None)
    retriever.RUNTIME_INDEX.clear()
    yield
    retriever.RUNTIME_INDEX.clear()


def post(**kw):
    b = {"question": Q, "user_id": "pm01", "role": "pm"}
    b.update(kw)
    return post_query(client, b).json()


def publish():
    tid = post()["thread_id"]
    d = post(thread_id=tid, approval="approve")
    assert d["pending_approval"] is False
    page_id = next(t["output"] for t in d["trace"] if t["step"] == "confluence_publish")
    return page_id


def test_material_has_sections_and_matching_numbers():
    body = meeting.build_meeting_material()
    for sec in ("## 결정사항", "## 블로커", "## 액션아이템"):
        assert sec in body
    rep = tools.build_progress_report()
    team = rep["progress"]
    assert f"**{team['progress']*100:.2f}%**" in body
    assert f"{team['remaining_mm']:.2f}MM" in body
    for m in rep["large_menus"] + rep["medium_menus"]:  # 메뉴별 표의 행마다 도구 수치가 그대로 들어간다
        row = next(ln for ln in body.splitlines() if ln.startswith(f"| {m['code']} {m['name']} |"))
        unit = "MM" if m in rep["large_menus"] else ""  # 중메뉴 표는 공수 열 머리에 MM 단위를 둔다
        assert f"| {m['progress']*100:.2f}% |" in row and f"| {m['remaining_mm']:.2f}{unit} |" in row
        assert f"| {m['screens_done']}/{m['screens_total']} |" in row
    for ph in rep["phases"]:
        assert f"| {ph['no']} {ph['name']} | {ph['done']}/{ph['total']} |" in body


def test_material_is_readable_for_170_screens():
    body = meeting.build_meeting_material()
    rep = tools.build_progress_report()
    for sec in ("### 대메뉴별", "### 중메뉴별", "### 단계별 완료 현황", "계획 대비 지연 화면"):
        assert sec in body
    section = body.split("### ⏰", 1)[1].split("### ⚠️", 1)[0]
    delayed_rows = [ln for ln in section.splitlines() if ln.startswith("| SCR-")]
    assert len(delayed_rows) == min(len(rep["delayed"]), tools.TOP_N)  # 지연 화면 상위 N개만
    assert [r.split(" ")[1] for r in delayed_rows] == [s["screen_id"] for s in rep["delayed"][:tools.TOP_N]]
    assert f"전체 {len(rep['delayed'])}개" in body
    assert len(body.splitlines()) < 250


def test_material_has_no_individual_progress():
    # 회의 자료는 참석자 전원에게 공유된다: 개인별 진척 표·담당자 열 없음, 사람 이름 옆에 진척률(%) 없음
    body = meeting.build_meeting_material()
    assert "### 개발자별" not in body and "| 담당 |" not in body and "| 작성자 |" not in body
    names = [d["name"] for d in tools.load_developers()]
    progress = body.split("## 결정사항", 1)[0]
    assert not any(n in progress for n in names)  # 진척 현황 섹션에는 사람 이름이 없다
    assert not any(n in ln and "%" in ln for ln in body.splitlines() for n in names)


def test_developer_can_read_published_page():
    # 게시된 회의 자료는 누구나 읽을 수 있다. 개발자 RAG 경로의 타인 수치 제외 가드는 그대로 둔다.
    from src import guardrails
    pid = publish()
    page = mcp_client.call_tool("get_page", {"page_id": pid})
    ctx, _ = guardrails.sanitize_contexts("developer", "dev01", [{"doc_id": pid, "text": page["body"]}])
    team = tools.build_progress_report()["progress"]
    assert "### 대메뉴별" in ctx[0]["text"] and f"**{team['progress']*100:.2f}%**" in ctx[0]["text"]


def test_published_page_retrievable_via_mcp():
    pid = publish()
    page = mcp_client.call_tool("get_page", {"page_id": pid})
    assert "## 결정사항" in page["body"] and "## 액션아이템" in page["body"]
    team = tools.build_progress_report()["progress"]
    assert f"**{team['progress']*100:.2f}%**" in page["body"]
    assert pid in [h["page_id"] for h in mcp_client.call_tool("search_pages", {"query": "블로커"})]


def test_published_page_in_runtime_index_not_in_corpus():
    pid = publish()
    assert pid in [d["doc_id"] for d in retriever.runtime_documents()]
    assert any(d["doc_id"] == pid for d in retriever.search("주간회의 자료 액션아이템"))
    assert pid not in [d["doc_id"] for d in retriever.corpus_documents()]
    assert all(d["source"] == "corpus" for d in retriever.corpus_documents())


def test_republish_replaces_without_duplicates():
    p1 = publish()
    api.THREADS.clear()
    p2 = publish()
    assert p1 == p2
    assert [d["doc_id"] for d in retriever.runtime_documents()].count(p1) == 1
    assert len(mcp_client.call_tool("search_pages", {"query": "결정사항"})) == 1
    assert len(list(retriever.generated_dir().glob("*.md"))) == 1


def test_no_person_names_anywhere_in_meeting_material():
    """회의 자료는 참석자 전원 공유 문서: 결정사항·블로커·액션아이템 어디에도 팀원 이름이 없다."""
    body = meeting.build_meeting_material()
    assert not [d["name"] for d in tools.load_developers() if d["name"] in body]
    assert meeting.strip_names("SCR-A01-03 개발 완료 (담당: 김메시님)") == "SCR-A01-03 개발 완료"


# --- 일정 대비 진척 · 지난주 대비 · 이슈 추적 (정답: 생성기가 따로 계산한 expected_progress.json)
import json
from pathlib import Path

EXPECTED = json.loads((Path(tools.FIXTURE_DIR) / "expected_progress.json").read_text(encoding="utf-8"))
SCH = EXPECTED["schedule"]
PLAN_KEYS = ("progress", "planned_progress", "gap_pp", "schedule_status")


def test_plan_vs_actual_matches_expected():
    rep = tools.build_progress_report()
    assert rep["schedule"]["elapsed"] == SCH["elapsed"] and 0.70 < SCH["elapsed"] < 0.75
    assert {k: rep["progress"][k] for k in PLAN_KEYS} == SCH["team"]
    for group in ("parts", "large_menus", "medium_menus"):
        assert {m["code"]: {k: m[k] for k in PLAN_KEYS} for m in rep[group]} == SCH[group]
    assert {s["schedule_status"] for s in SCH["large_menus"].values()} == {"정상", "주의", "지연"}
    assert tools.schedule_status(-3.0) == "정상" and tools.schedule_status(-3.01) == "주의"
    assert tools.schedule_status(-10.0) == "주의" and tools.schedule_status(-10.01) == "지연"
    body = meeting.build_meeting_material()
    t = SCH["team"]
    assert (f"| **전체** | {t['planned_progress']*100:.2f}% | **{t['progress']*100:.2f}%** | {t['gap_pp']:+.2f}%p |"
            in body)
    assert f"일정 경과율 **{SCH['elapsed']*100:.2f}%**" in body
    for code, m in SCH["large_menus"].items():
        row = next(ln for ln in body.splitlines() if ln.startswith(f"| {code} "))
        assert f"| {m['planned_progress']*100:.2f}% | {m['progress']*100:.2f}% | {m['gap_pp']:+.2f}%p |" in row


def test_planned_ratio_linear_and_clamped():
    s = {"planned_start": "2026-11-02", "planned_end": "2026-11-12"}
    assert tools.planned_ratio(s, "2026-11-01") == 0 and tools.planned_ratio(s, "2026-11-12") == 1
    assert tools.planned_ratio(s, "2026-11-07") == tools.Fraction(1, 2)
    assert all(x["planned_start"] < x["planned_end"] for x in tools.load_screens())


def test_previous_snapshot_and_week_over_week_deltas():
    prev = tools.previous_snapshot()
    exp = EXPECTED["previous_week"]
    assert prev["week"] == exp["week"] == {"start": "2026-11-09", "end": "2026-11-15"}
    assert prev["team"] == exp["team"]
    assert {k: v["progress"] for k, v in prev["large_menus"].items()} == exp["large_menus"]
    assert {k: v["progress"] for k, v in prev["medium_menus"].items()} == exp["medium_menus"]
    # 지난주 → 이번 주: 진척·완료 화면·단계 완료는 줄지 않는다
    assert all(EXPECTED["medium_menus"][k]["progress"] >= v for k, v in exp["medium_menus"].items())
    assert all(EXPECTED["phases"][k] >= v for k, v in exp["phases"].items())
    assert EXPECTED["team"]["screens_done"] - exp["team"]["screens_done"] == len(exp["completed_this_week"]) > 0
    body = meeting.build_meeting_material()
    cur = EXPECTED["team"]["progress"]
    d = round((cur - exp["team"]["progress"]) * 100, 2)
    assert f"| 전체 실적 진척률 | {exp['team']['progress']*100:.2f}% | **{cur*100:.2f}%** | ▲ +{d:.2f}%p |" in body
    for code, before in exp["large_menus"].items():
        row = next(ln for ln in body.splitlines() if ln.startswith(f"| {code} "))
        dd = round((EXPECTED["large_menus"][code]["progress"] - before) * 100, 2)
        assert f"| {before*100:.2f}% | {f'▲ +{dd:.2f}%p' if dd > 0 else '-'} |" in row
    assert "▼" in body.split("### 대메뉴별")[0]  # 계획 대비 차이 악화는 감소 표시


def test_issue_tracking_resolved_persistent_new():
    ctx = meeting.meeting_context()
    tr, exp = ctx["issues"], EXPECTED["issue_tracking"]
    assert sorted(i["id"] for i in tr["resolved"]) == exp["resolved"]
    assert sorted(i["id"] for i in tr["new"]) == exp["new"]
    assert {i["id"]: i["weeks"] for i in tr["persistent"]} == exp["persistent"]
    assert tr["resolved"] and tr["persistent"] and tr["new"]  # 해결·지속·신규가 모두 있다
    assert {i["id"] for i in tools.previous_snapshot()["issues"]} == set(EXPECTED["previous_week"]["issue_ids"])
    section = meeting.build_meeting_material().split("## 이슈 추적", 1)[1].split("## 지난주 액션아이템 이행", 1)[0]
    assert "| ✅ 해결 | " in section and f"**{len(exp['resolved'])}** |" in section
    assert "| DLY-SCR-C02-01 |" in section and "| 🔁 미해결 | 12주 |" in section


def test_previous_action_items_completion_from_data():
    ctx = meeting.meeting_context()
    got = {t: ok for t, ok, _ in ctx["prev_actions"]}
    assert got == EXPECTED["previous_action_items"]
    assert any(got.values()) and not all(got.values())
    screens = {s["screen_id"]: s for s in tools.calc_progress()["screens"]}
    assert meeting.action_status("PG사 테스트 계정 재요청", screens, set())[0] is None  # 데이터로 확인 불가
    body = meeting.build_meeting_material()
    assert "| ✅ 완료 | 단계 완료 표시 확인 |" in body and "| ⬜ 미완료 |" in body


def test_without_history_everything_is_new():
    body = meeting.build_meeting_material(load_history=False)
    assert "### 지난주 대비 변화" not in body and "모두 신규로 등록" in body
    ctx = meeting.meeting_context(load_history=False)
    assert not ctx["issues"]["resolved"] and not ctx["issues"]["persistent"]


def test_week_snapshot_feeds_next_week():
    ctx = meeting.meeting_context()
    snap = meeting.week_snapshot(ctx, ["SCR-C02-01 결제 승인 메인: SCR-C02-01-3 완료"])
    assert snap["week"]["start"] == "2026-11-16" and snap["team"]["progress"] == EXPECTED["team"]["progress"]
    nxt = meeting.track_issues(tools.current_issues(ctx["report"]), snap, "2026-11-23")
    assert not nxt["resolved"] and not nxt["new"]  # 같은 데이터면 모두 미해결로 이어진다
    assert next(i for i in nxt["persistent"] if i["id"] == "DLY-SCR-C02-01")["weeks"] == 13


def test_previous_week_page_from_snapshot():
    page = meeting.render_snapshot_page(tools.previous_snapshot())
    prev = EXPECTED["previous_week"]["team"]
    assert f"**{prev['progress']*100:.2f}%**" in page and "## 이슈 목록" in page and "## 액션아이템" in page
    assert not [d["name"] for d in tools.load_developers() if d["name"] in page]


def test_committed_meeting_pages_are_current():
    # 저장소에 커밋한 회의 자료 페이지(샘플)는 결정적 렌더 + 게시 꼬리말과 같다 (이번 주·지난주)
    from src import middleware
    from src.confluence_convert import md_to_storage
    root = Path(__file__).resolve().parent.parent / "data" / "confluence_pages"
    prev = tools.previous_snapshot()
    for week, body in (("2026-11-16", meeting.build_meeting_material()), ("2026-11-09", meeting.render_snapshot_page(prev))):
        body = middleware.mask_pii(meeting.with_footer(body))  # 실제 게시본과 같게 꼬리말 포함
        md = (root / f"{meeting.meeting_page_id(week)}.md").read_text(encoding="utf-8")
        assert md == f"# {meeting.meeting_title(week)}\n\n{body}\n"
        assert (root / f"{meeting.meeting_page_id(week)}.storage.xml").read_text(encoding="utf-8") == md_to_storage(body)



# ----------------------------------------------------------------------------- 중메뉴 구성·공수, 타임라인 (요구 2)
def _row(text: str, prefix: str) -> str:
    return next(r for r in text.splitlines() if r.startswith(prefix))


def test_screen_attributes_match_screen_definitions():
    sc = {s["screen_id"]: s for s in tools.load_screens()}
    assert {s["complexity"] for s in sc.values()} == {"상", "중", "하"}
    assert all({"상": 0.6, "하": 0.3}.get(s["complexity"], s["plan_mm"]) == s["plan_mm"] for s in sc.values())  # 상=0.6MM, 하=0.3MM
    assert sum(1 for s in sc.values() if s["crud"] == "R") > 0  # 조회 전용 화면 존재
    # 화면 정의서와 모순 없음: 결제 승인 요청·상품 등록은 생성(C), 회원 통계는 조회 전용, 권한 화면은 수정(U)
    assert sc["SCR-C02-01"]["crud"] == "CR" and sc["SCR-D01-01"]["crud"] == "CR"
    assert sc["SCR-G02-01"]["crud"] == "R" and sc["SCR-C02-02"]["crud"] == "R" and "U" in sc["SCR-H01-01"]["crud"]


def test_medium_composition_and_effort_exact():
    comp = EXPECTED["composition"]
    text = meeting.build_meeting_material()
    report = tools.build_progress_report()
    for m in report["medium_menus"]:
        e = comp["medium_menus"][m["code"]]
        assert m["crud"] == e["crud"] and m["complexity"] == e["complexity"]
        assert (m["plan_mm"], m["earned_mm"], m["remaining_mm"]) == (e["plan_mm"], e["earned_mm"], e["remaining_mm"])
        row = _row(text, f"| {m['code']} {m['name']} | ")
        assert (f"| {tools.crud_label(e['crud'])} | {tools.complexity_label(e['complexity'])} | "
                f"{e['plan_mm']:.2f} | {e['earned_mm']:.2f} | {e['remaining_mm']:.2f} |") in row
    assert tools.crud_label(comp["medium_menus"]["C02"]["crud"]) == "C2 R5 U2 D1"
    for m in report["large_menus"]:
        e = comp["large_menus"][m["code"]]
        row = _row(text, f"| {m['code']} {m['name']} | {m['part']} |")
        assert f"| {e['plan_mm']:.2f}MM | {e['earned_mm']:.2f}MM | {e['remaining_mm']:.2f}MM |" in row
    t = comp["team"]
    assert round(t["earned_mm"] + t["remaining_mm"], 2) in (round(t["plan_mm"] - 0.01, 2), t["plan_mm"])  # 반올림 오차 이내
    assert report["progress"]["earned_mm"] == t["earned_mm"] and report["progress"]["plan_mm"] == t["plan_mm"]


def test_timeline_percentages_match_tools():
    text = meeting.build_meeting_material()
    sch, team = EXPECTED["schedule"], EXPECTED["schedule"]["team"]
    block = text.split("#### 전체 기간 대비 현재 위치", 1)[1].split("```")[1]
    lines = block.strip("\n").splitlines()
    bar = lambda x: "█" * round(x * 48) + "░" * (48 - round(x * 48))  # noqa: E731
    assert f"경과  {bar(sch['elapsed'])} {sch['elapsed'] * 100:.2f}%" in lines
    assert f"계획  {bar(team['planned_progress'])} {team['planned_progress'] * 100:.2f}%" in lines
    assert f"실적  {bar(team['progress'])} {team['progress'] * 100:.2f}% (계획 대비 {team['gap_pp']:+.2f}%p)" in lines
    assert lines[1].split()[1:] == ["3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "1", "2"]
    assert "▼ 기준 주" in block and block.count("◆") == 3  # 마일스톤 2개 + 범례
    # 대메뉴 일정표: 계획 기간 월 ■, 기준 주 달 ▼, 실적/계획
    lm = {m["code"]: m for m in tools.build_progress_report()["large_menus"]}
    row = _row(text, "| 시스템관리(H) |")
    assert row.count("■") == 4 and f"{lm['H']['progress']*100:.2f}% / {lm['H']['planned_progress']*100:.2f}%" in row
    assert "**11▼**" in text and _row(text, "| ◆ 마일스톤 |").count("◆") == 2  # 2월 칸 1개 + 행 이름
    assert len(text.splitlines()) < 250 and not [d["name"] for d in tools.load_developers() if d["name"] in text]


def test_converter_fenced_code_to_code_macro():
    from src.confluence_convert import md_to_storage
    xml = md_to_storage("```\n경과  ███░ 73.48%\n<tag> x]]>y\n```\n문단")
    assert xml.startswith('<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[경과  ███░ 73.48%\n<tag> x]]]]>')
    assert xml.endswith("</ac:plain-text-body></ac:structured-macro><p>문단</p>")
    page = md_to_storage(meeting.build_meeting_material())
    assert page.count('ac:name="code"') == 1 and "<p>```</p>" not in page
