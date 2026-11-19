"""진척 계산 도구 테스트: 생성기가 따로 계산한 정답(data/fixtures/expected_progress.json)과 100% 일치 검증."""
import json
from pathlib import Path

import pytest

from src import tools

EXPECTED = json.loads((Path(tools.FIXTURE_DIR) / "expected_progress.json").read_text(encoding="utf-8"))
AGG_KEYS = ("progress", "remaining_mm", "screens_done", "screens_total")


def test_project_and_reference_week_fixed():
    p = tools.load_project()
    assert (p["name"], p["start"], p["end"]) == ("플랫폼 전환 프로젝트", "2026-03-02", "2027-02-26")
    w = tools.load_reference_week()
    assert (w["start"], w["end"]) == ("2026-11-16", "2026-11-22")


def test_menu_hierarchy_and_team():
    screens = tools.load_screens()
    assert len(screens) == 170
    assert len({s["large_code"] for s in screens}) == 8
    assert len({s["medium_code"] for s in screens}) == 34
    for s in screens:  # 화면 ID = 대메뉴·중메뉴·소메뉴 코드
        assert s["screen_id"].startswith(f"SCR-{s['medium_code']}-") and s["medium_code"][0] == s["large_code"]
    roles = [d["role"] for d in tools.load_developers()]
    assert (roles.count("pm"), roles.count("pl"), roles.count("developer")) == (1, 2, 7)
    parts = tools.load_project()["parts"]
    for s in screens:  # 파트 = 대메뉴 담당 PL, 담당 개발자는 같은 파트
        assert s["large_code"] in parts[s["part"]]["large_menus"]
        dev = next(d for d in tools.load_developers() if d["developer_id"] == s["owner"])
        assert dev["part"] == s["part"] and dev["lead"] == parts[s["part"]]["lead"]


@pytest.mark.parametrize("sid", sorted(EXPECTED["screens"]))
def test_screen_progress_and_remaining_exact(sid):
    r = tools.calc_progress(screen_id=sid)
    exp = EXPECTED["screens"][sid]
    assert (r["progress"], r["remaining_mm"]) == (exp["progress"], exp["remaining_mm"])


@pytest.mark.parametrize("dev", sorted(EXPECTED["developers"]))
def test_developer_progress_and_remaining_exact(dev):
    r = tools.calc_progress(developer_id=dev)
    assert {k: r[k] for k in AGG_KEYS} == EXPECTED["developers"][dev]
    assert tools.calc_remaining_mm(developer_id=dev)["remaining_mm"] == EXPECTED["developers"][dev]["remaining_mm"]


@pytest.mark.parametrize("part", sorted(EXPECTED["parts"]))
def test_part_progress_exact(part):
    r = tools.calc_progress(part=part)
    assert {k: r[k] for k in AGG_KEYS} == EXPECTED["parts"][part]


def test_team_progress_exact():
    r = tools.calc_progress()
    assert {k: r[k] for k in AGG_KEYS} == EXPECTED["team"]


def test_menu_rollups_exact():
    rep = tools.build_progress_report()
    assert {m["code"]: {k: m[k] for k in AGG_KEYS} for m in rep["large_menus"]} == EXPECTED["large_menus"]
    assert {m["code"]: {k: m[k] for k in AGG_KEYS} for m in rep["medium_menus"]} == EXPECTED["medium_menus"]
    assert {str(p["no"]): p["done"] for p in rep["phases"]} == EXPECTED["phases"]


def test_delayed_screens_exact():
    rep = tools.build_progress_report()
    assert sorted(s["screen_id"] for s in rep["delayed"]) == EXPECTED["delayed_screens"]
    ends = [s["planned_end"] for s in rep["delayed"]]
    assert ends == sorted(ends)  # 계획 종료일이 이른 순


def test_unlinked_commits_exact():
    unl = tools.check_mismatch()["unlinked_commits"]
    assert {c["hash"]: c["reason"] for c in unl} == EXPECTED["unlinked"]
    assert set(EXPECTED["unlinked"].values()) == {"no_or_malformed_screen_id", "unknown_screen_id"}


@pytest.mark.parametrize("msg,expected", [
    ("[SCR-A01-03] 요약", ("SCR-A01-03", None, False)),
    ("[SCR-A01-03-2][done] 개발 완료", ("SCR-A01-03", 2, True)),
    ("[SCR-A1-03] 형식 오류", (None, None, False)),
    ("[SCR-A01-3][done] 형식 오류", (None, None, False)),
    ("[SCRA01-03] 형식 오류", (None, None, False)),
    ("[scr-a01-03] 소문자", (None, None, False)),
    ("[SCR-A01-033] 자릿수 초과", (None, None, False)),
    ("[SCR-A01-03][done] 단계 번호 없음", ("SCR-A01-03", None, False)),
])
def test_commit_message_parsing(msg, expected):
    p = tools.parse_commit_message(msg)
    assert (p["screen_id"], p["phase_no"], p["done"]) == expected


def test_after_week_commits_excluded():
    hashes = {c["hash"] for c in tools.get_commits()["linked"]}
    assert EXPECTED["after_week_hashes"] and not hashes & set(EXPECTED["after_week_hashes"])


def test_mismatch_warnings_exact():
    warns = tools.check_mismatch()["mismatch_warnings"]
    assert sorted([w["screen_id"], w["code"]] for w in warns) == EXPECTED["mismatch"]


def test_status_wins_over_commits():
    codes = {sid: code for sid, code in EXPECTED["mismatch"]}
    done = next(s for s, c in codes.items() if c == tools.WARN_STATUS_DONE)
    r = tools.calc_progress(screen_id=done)
    assert r["progress"] == 1.0 and r["phases_remaining"] == []
    not_started = next(s for s, c in codes.items() if c == tools.WARN_NOT_STARTED)
    r = tools.calc_progress(screen_id=not_started)
    assert r["progress"] == 0.0 and r["commit_count_total"] >= 1
    bad = next(s for s, c in codes.items() if c == tools.WARN_BAD_SUBTASK)
    assert 5 not in tools.calc_progress(screen_id=bad)["phases_done"]  # 없는 단계 완료 표시는 무시


def test_unknown_screen_returns_error_not_number():
    r = tools.calc_progress(screen_id="SCR-Z01-01")
    assert r["error"] == "not_found" and "progress" not in r


def test_no_email_in_tool_output():
    for rep in (tools.build_progress_report(), tools.build_progress_report(part="PL1"),
                tools.build_progress_report(developer_id="dev01")):
        assert "@" not in json.dumps(rep, ensure_ascii=False)
        assert "@" not in tools.format_progress_report(rep)


def test_team_report_text_shows_rollups_not_all_screens():
    text = tools.format_progress_report(tools.build_progress_report())
    for dev, e in EXPECTED["developers"].items():
        assert f"({dev}): 진척률 {e['progress']*100:.2f}%, 남은 {e['remaining_mm']:.2f}MM" in text
    for code, e in EXPECTED["large_menus"].items():
        assert f"- {code} " in text and f"진척률 {e['progress']*100:.2f}%, 남은 {e['remaining_mm']:.2f}MM" in text
    for h in EXPECTED["unlinked"]:
        assert h in text
    warn_section = text.split("## 경고")[1].split("## 미연결")[0]
    for sid, _ in EXPECTED["mismatch"]:
        assert sid in warn_section
    shown = {s for s in EXPECTED["screens"] if s in text}
    assert len(shown) < 40  # 170개 화면을 모두 나열하지 않는다


def test_developer_report_scoped_to_self():
    rep = tools.build_progress_report(developer_id="dev01")
    owned = {s["screen_id"] for s in tools.load_screens() if s["owner"] == "dev01"}
    assert {s["screen_id"] for s in rep["progress"]["screens"]} == owned
    assert all(c["developer_id"] == "dev01" for c in rep["unlinked_commits"])
    assert all(w["screen_id"] in owned for w in rep["mismatch_warnings"])
    text = tools.format_progress_report(rep)
    assert "박지단" not in text and "developers" not in rep


def test_part_report_scoped_to_part():
    rep = tools.build_progress_report(part="PL2")
    assert {m["code"] for m in rep["large_menus"]} == {"E", "F", "G", "H"}
    assert {d["developer_id"] for d in rep["developers"]} == {"dev05", "dev06", "dev07"}
    members = {"dev05", "dev06", "dev07"}
    assert rep["unlinked_commits"] and all(c["developer_id"] in members for c in rep["unlinked_commits"])
    assert "김메시" not in tools.format_progress_report(rep)


def test_part_report_has_medium_menus_and_plan_vs_actual():
    rep = tools.build_progress_report(part="PL1")
    text = tools.format_progress_report(rep)
    sch = EXPECTED["schedule"]
    meds = sorted(c for c in EXPECTED["medium_menus"] if c[0] in "ABCD")
    assert [m["code"] for m in rep["medium_menus"]] == meds
    section = text.split("## 중메뉴별", 1)[1].split("## 단계별", 1)[0]
    for c in meds:
        e = EXPECTED["medium_menus"][c]
        assert f"- {c} " in section and f"진척률 {e['progress']*100:.2f}%, 남은 {e['remaining_mm']:.2f}MM" in section
        assert f"계획 {sch['medium_menus'][c]['planned_progress']*100:.2f}%" in section
    assert not any(f"- {c} " in section for c in EXPECTED["medium_menus"] if c[0] in "EFGH")
    p = sch["parts"]["PL1"]
    assert (f"일정 경과율 {sch['elapsed']*100:.2f}%, 계획 진척률 {p['planned_progress']*100:.2f}%, "
            f"실적 진척률 {p['progress']*100:.2f}%, 차이 {p['gap_pp']:+.2f}%p ({p['schedule_status']})") in text


def test_screen_warning_lookup():
    sid = EXPECTED["mismatch"][0][0]
    assert tools.build_progress_report(screen_id=sid)["mismatch_warnings"][0]["code"] == EXPECTED["mismatch"][0][1]


def test_user_scope_by_role():
    assert tools.user_scope("pm", "pm01") == {}
    assert tools.user_scope("pl", "pl01") == {"part": "PL1"}
    assert tools.user_scope("developer", "dev03") == {"developer_id": "dev03"}


def test_generator_is_deterministic(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_fixtures", Path(tools.FIXTURE_DIR) / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "OUT", tmp_path)
    mod.main()
    for name in ("project.json", "developers.json", "screens.json", "commits.json", "meeting_history.json",
                 "expected_progress.json"):
        assert (tmp_path / name).read_text(encoding="utf-8") == (Path(tools.FIXTURE_DIR) / name).read_text(encoding="utf-8")
