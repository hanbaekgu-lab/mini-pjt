"""진척 계산 도구 테스트: 사전 고정 정답(data/fixtures/expected_progress.json)과 100% 일치 검증."""
import json
from pathlib import Path

import pytest

from src import tools

EXPECTED = json.loads((Path(tools.FIXTURE_DIR) / "expected_progress.json").read_text(encoding="utf-8"))


def test_reference_week_fixed():
    w = tools.load_reference_week()
    assert (w["start"], w["end"]) == ("2026-11-16", "2026-11-22")


@pytest.mark.parametrize("tid", sorted(EXPECTED["tasks"]))
def test_task_progress_and_remaining_exact(tid):
    r = tools.calc_progress(task_id=tid)
    exp = EXPECTED["tasks"][tid]
    assert r["progress"] == exp["progress"]
    assert r["remaining_mm"] == exp["remaining_mm"]
    assert tools.calc_remaining_mm(task_id=tid)["remaining_mm"] == exp["remaining_mm"]


@pytest.mark.parametrize("dev", sorted(EXPECTED["developers"]))
def test_developer_progress_and_remaining_exact(dev):
    r = tools.calc_progress(developer_id=dev)
    exp = EXPECTED["developers"][dev]
    assert (r["progress"], r["remaining_mm"]) == (exp["progress"], exp["remaining_mm"])
    assert tools.calc_remaining_mm(developer_id=dev)["remaining_mm"] == exp["remaining_mm"]


def test_team_progress_exact():
    r = tools.calc_progress()
    assert (r["progress"], r["remaining_mm"]) == (EXPECTED["team"]["progress"], EXPECTED["team"]["remaining_mm"])


def test_all_developers_and_tasks_covered():
    devs = {d["developer_id"] for d in tools.load_developers() if d["role"] == "developer"}
    assert devs == set(EXPECTED["developers"]) and len(devs) == 6
    assert {t["task_id"] for t in tools.load_tasks()} == set(EXPECTED["tasks"])


def test_unlinked_commits_exact():
    unl = tools.check_mismatch()["unlinked_commits"]
    assert {c["hash"] for c in unl} == set(EXPECTED["unlinked_hashes"])
    reasons = {c["hash"]: c["reason"] for c in unl}
    assert reasons["c022"] == "no_or_malformed_task_id"   # ID 없음
    assert reasons["c021"] == "no_or_malformed_task_id"   # TASK-16 형식 오류
    assert reasons["c028"] == "no_or_malformed_task_id"   # TASK020 형식 오류
    assert reasons["c035"] == "no_or_malformed_task_id"   # TASK-0123 형식 오류
    assert reasons["c032"] == "unknown_task_id"


def test_malformed_done_marks_not_counted_and_after_week_excluded():
    # c021(TASK-16-4 done, 형식 오류)과 c016(기준 주 이후)은 진척에 반영되지 않는다
    assert tools.calc_progress(task_id="TASK-016")["subtasks_done"] == [1, 2, 3]
    assert tools.calc_progress(task_id="TASK-019")["subtasks_done"] == []


def test_mismatch_warnings_exact():
    warns = tools.check_mismatch()["mismatch_warnings"]
    assert {w["task_id"]: w["code"] for w in warns} == EXPECTED["mismatch_tasks"]


def test_status_wins_over_commits():
    t7 = tools.calc_progress(task_id="TASK-007")
    assert t7["progress"] == 1.0 and t7["subtasks_done"] == [1, 2, 3]
    t17 = tools.calc_progress(task_id="TASK-017")
    assert t17["progress"] == 0.0 and t17["commit_count_total"] == 1


def test_commit_count_not_in_progress():
    # TASK-015: 2번 하위 작업에 커밋 2건이어도 진척률은 2/5
    t = tools.calc_progress(task_id="TASK-015")
    assert t["commit_count_total"] == 3 and t["progress"] == 0.4


def test_unknown_task_returns_error_not_number():
    r = tools.calc_progress(task_id="TASK-099")
    assert r["error"] == "not_found" and "progress" not in r


def test_no_email_in_tool_output():
    rep = tools.build_progress_report()
    assert "@" not in json.dumps(rep, ensure_ascii=False)
    assert "@" not in tools.format_progress_report(rep)


def test_team_report_text_contains_everything():
    text = tools.format_progress_report(tools.build_progress_report())
    for tid in EXPECTED["tasks"]:
        assert tid in text
    for dev, e in EXPECTED["developers"].items():
        assert f"({dev}): 진척률 {e['progress']*100:.2f}%, 남은 {e['remaining_mm']:.2f}MM" in text
    for h in EXPECTED["unlinked_hashes"]:
        assert h in text
    warn_section = text.split("## 경고")[1].split("## 미연결")[0]
    for tid in EXPECTED["mismatch_tasks"]:
        assert tid in warn_section
    assert "미연결 커밋" in text and "불일치" in text


def test_developer_report_scoped_to_self():
    rep = tools.build_progress_report(developer_id="dev01")
    assert {t["task_id"] for t in rep["progress"]["tasks"]} == {"TASK-012", "TASK-013"}
    assert rep["unlinked_commits"] == [] and rep["mismatch_warnings"] == []
    rep4 = tools.build_progress_report(developer_id="dev04")
    assert [c["hash"] for c in rep4["unlinked_commits"]] == ["c021", "c022"]
    assert "박지단" not in tools.format_progress_report(rep)


def test_task_warning_lookup():
    assert tools.build_progress_report(task_id="TASK-012")["mismatch_warnings"] == []
    assert tools.build_progress_report(task_id="TASK-007")["mismatch_warnings"][0]["code"] == tools.WARN_STATUS_DONE
