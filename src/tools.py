"""진척 계산 도구 (결정적 함수). 숫자는 모두 여기서 계산하고 LLM은 설명만 한다.

- 커밋-업무 매핑: 메시지의 업무 ID (TASK-\\d{3}); 없거나 형식 오류면 '미연결'
- 하위 작업 완료: ``[TASK-012-3][done]`` 형식의 완료 표시
- 진척률: 업무 = 완료 하위 작업 / 전체 하위 작업, 개발자·팀 = 계획 MM 가중 평균
- 남은 MM = 계획 MM x (1 - 진척률)
- 업무 상태와 커밋이 불일치하면 업무 상태를 우선하고 경고로 표시
- 내부 처리는 실제 이메일을 쓰며, 이 모듈의 출력에는 이메일을 싣지 않는다 (마스킹은 미들웨어 담당)
"""
from __future__ import annotations

import json
import os
import re
from fractions import Fraction
from pathlib import Path

FIXTURE_DIR = Path(os.environ.get("FIXTURE_DIR", Path(__file__).resolve().parent.parent / "data" / "fixtures"))

# 업무 ID: 앞뒤가 영숫자/하이픈이 아니어야 하고 숫자는 정확히 3자리. 선택적 하위 번호(-N).
TASK_REF_RE = re.compile(r"(?<![A-Za-z0-9-])TASK-(\d{3})(?:-(\d+))?(?![A-Za-z0-9])")
DONE_RE = re.compile(r"\[done\]", re.IGNORECASE)

STATUS_DONE = "완료"
STATUS_NOT_STARTED = "미착수"

WARN_STATUS_DONE = "STATUS_DONE_SUBTASKS_INCOMPLETE"
WARN_SUBTASKS_DONE = "SUBTASKS_DONE_STATUS_NOT_DONE"
WARN_NOT_STARTED = "NOT_STARTED_BUT_HAS_COMMITS"
WARN_BAD_SUBTASK = "DONE_MARK_UNKNOWN_SUBTASK"


def _load(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def load_reference_week() -> dict:
    return _load("reference_week.json")


def load_developers() -> list[dict]:
    return _load("developers.json")


def load_tasks() -> list[dict]:
    return _load("tasks.json")


def load_commits() -> list[dict]:
    return _load("commits.json")


def parse_commit_message(message: str) -> dict:
    """커밋 메시지에서 업무 ID·하위 번호·완료 표시를 추출한다. 형식 오류면 task_id=None."""
    m = TASK_REF_RE.search(message)
    if not m:
        return {"task_id": None, "subtask_no": None, "done": False}
    sub = int(m.group(2)) if m.group(2) else None
    done = bool(DONE_RE.search(message)) and sub is not None
    return {"task_id": f"TASK-{m.group(1)}", "subtask_no": sub, "done": done}


def get_commits() -> dict:
    """기준 주 종료일까지의 커밋을 업무에 연결하고 미연결 커밋을 분리한다."""
    week = load_reference_week()
    tasks = {t["task_id"] for t in load_tasks()}
    email_to_dev = {d["email"].lower(): d for d in load_developers()}
    linked, unlinked = [], []
    for c in load_commits():
        if c["date"] > week["end"]:
            continue  # 기준 주 이후 커밋은 제외
        dev = email_to_dev.get(c["author_email"].lower())
        p = parse_commit_message(c["message"])
        rec = {
            "hash": c["hash"],
            "date": c["date"],
            "developer_id": dev["developer_id"] if dev else None,
            "message": c["message"],
            "in_week": week["start"] <= c["date"] <= week["end"],
            **p,
        }
        if p["task_id"] is None:
            rec["reason"] = "no_or_malformed_task_id"
            unlinked.append(rec)
        elif p["task_id"] not in tasks:
            rec["reason"] = "unknown_task_id"
            unlinked.append(rec)
        else:
            linked.append(rec)
    return {"linked": linked, "unlinked": unlinked}


def _r4(x: Fraction) -> float:
    return round(float(x), 4)


def _r2(x: Fraction) -> float:
    return round(float(x), 2)


def _task_calc() -> dict:
    """업무별 정확 분수 계산 결과와 경고."""
    commits = get_commits()
    result = {}
    for t in load_tasks():
        tid = t["task_id"]
        mine = [c for c in commits["linked"] if c["task_id"] == tid]
        done_set, warnings = set(), []
        for c in mine:
            if c["done"]:
                if 1 <= c["subtask_no"] <= t["subtask_count"]:
                    done_set.add(c["subtask_no"])
                else:
                    warnings.append({"code": WARN_BAD_SUBTASK, "task_id": tid, "hash": c["hash"],
                                     "message": f"{tid}에 없는 하위 작업 {c['subtask_no']}번 완료 표시 (무시)"})
        total = t["subtask_count"]
        commit_ratio = Fraction(len(done_set), total)
        status = t["status"]
        mm = Fraction(str(t["plan_mm"]))
        if status == STATUS_DONE:
            ratio = Fraction(1)  # 업무 상태 우선
            if len(done_set) < total:
                missing = sorted(set(range(1, total + 1)) - done_set)
                warnings.append({"code": WARN_STATUS_DONE, "task_id": tid,
                                 "message": f"{tid} 상태는 '완료'이나 완료 표시 없는 하위 작업 {missing} (상태 기준 100% 적용)"})
        else:
            ratio = commit_ratio
            if status == STATUS_NOT_STARTED and mine:
                warnings.append({"code": WARN_NOT_STARTED, "task_id": tid,
                                 "message": f"{tid} 상태는 '미착수'이나 커밋 {len(mine)}건 존재 (상태 기준 0% 적용)"})
                ratio = Fraction(0)
            elif len(done_set) == total:
                warnings.append({"code": WARN_SUBTASKS_DONE, "task_id": tid,
                                 "message": f"{tid} 하위 작업은 모두 완료됐으나 상태는 '{status}'"})
        result[tid] = {
            "task_id": tid, "title": t["title"], "owner": t["owner"], "status": status,
            "plan_mm": mm, "ratio": ratio, "remaining": mm * (1 - ratio),
            "subtasks_total": total, "subtasks_done": sorted(done_set),
            "subtasks_remaining": sorted(set(range(1, total + 1)) - done_set) if ratio < 1 else [],
            "commit_count_total": len(mine),
            "commit_count_week": sum(1 for c in mine if c["in_week"]),
            "warnings": warnings,
        }
    return result


def _public_task(x: dict) -> dict:
    return {
        "task_id": x["task_id"], "title": x["title"], "owner": x["owner"], "status": x["status"],
        "plan_mm": float(x["plan_mm"]), "progress": _r4(x["ratio"]), "remaining_mm": _r2(x["remaining"]),
        "subtasks_total": x["subtasks_total"], "subtasks_done": x["subtasks_done"],
        "subtasks_remaining": x["subtasks_remaining"],
        "commit_count_total": x["commit_count_total"], "commit_count_week": x["commit_count_week"],
    }


def _aggregate(items: list[dict]) -> dict:
    plan = sum((i["plan_mm"] for i in items), Fraction(0))
    done = sum((i["plan_mm"] * i["ratio"] for i in items), Fraction(0))
    ratio = done / plan if plan else Fraction(0)
    return {"plan_mm": float(plan), "progress": _r4(ratio), "remaining_mm": _r2(plan - done)}


def calc_progress(developer_id: str | None = None, task_id: str | None = None) -> dict:
    """진척률 계산. 인자 없음=팀 전체, developer_id=개발자, task_id=업무 단건. 없는 대상은 error 반환."""
    calc = _task_calc()
    if task_id is not None:
        if task_id not in calc:
            return {"error": "not_found", "message": f"{task_id} 업무를 찾을 수 없습니다"}
        return {"scope": "task", **_public_task(calc[task_id])}
    if developer_id is not None:
        if developer_id not in {d["developer_id"] for d in load_developers()}:
            return {"error": "not_found", "message": "대상을 찾을 수 없습니다"}
        mine = [v for v in calc.values() if v["owner"] == developer_id]
        return {"scope": "developer", "developer_id": developer_id,
                **_aggregate(mine), "tasks": [_public_task(v) for v in mine]}
    return {"scope": "team", **_aggregate(list(calc.values())),
            "tasks": [_public_task(v) for v in calc.values()]}


def calc_remaining_mm(developer_id: str | None = None, task_id: str | None = None) -> dict:
    """남은 MM = 계획 MM x (1 - 진척률)."""
    p = calc_progress(developer_id=developer_id, task_id=task_id)
    if "error" in p:
        return p
    return {k: p[k] for k in ("scope", "task_id", "developer_id", "plan_mm", "remaining_mm") if k in p}


def check_mismatch() -> dict:
    """업무 상태-커밋 불일치 경고와 미연결 커밋 목록."""
    commits = get_commits()
    warnings = [w for v in _task_calc().values() for w in v["warnings"]]
    unlinked = [{"hash": c["hash"], "date": c["date"], "developer_id": c["developer_id"],
                 "message": c["message"], "reason": c["reason"]} for c in commits["unlinked"]]
    return {"mismatch_warnings": warnings, "unlinked_commits": unlinked}


def build_progress_report(developer_id: str | None = None, task_id: str | None = None) -> dict:
    """진척 질의 응답용 묶음: 업무별·개발자별 진척 + 불일치 경고 + 미연결 커밋.

    developer_id 지정 시 해당 개발자 범위만 (경고·미연결도 본인 업무/커밋으로 한정).
    """
    week = load_reference_week()
    prog = calc_progress(developer_id=developer_id, task_id=task_id)
    if "error" in prog:
        return prog
    mm = check_mismatch()
    report = {"reference_week": week["label"], "progress": prog}
    if developer_id is None and task_id is None:
        devs = []
        for d in load_developers():
            if d["role"] != "developer":
                continue
            p = calc_progress(developer_id=d["developer_id"])
            devs.append({"developer_id": d["developer_id"], "name": d["name"], "progress": p["progress"],
                         "plan_mm": p["plan_mm"], "remaining_mm": p["remaining_mm"]})
        report["developers"] = devs
        warnings, unlinked = mm["mismatch_warnings"], mm["unlinked_commits"]
    elif task_id is not None:
        warnings = [w for w in mm["mismatch_warnings"] if w["task_id"] == task_id]
        unlinked = []
    else:
        own = {t["task_id"] for t in prog["tasks"]}
        warnings = [w for w in mm["mismatch_warnings"] if w["task_id"] in own]
        unlinked = [c for c in mm["unlinked_commits"] if c["developer_id"] == developer_id]
    report["mismatch_warnings"] = warnings
    report["unlinked_commits"] = unlinked
    return report


def format_progress_report(report: dict) -> str:
    """결정적 텍스트 렌더링 (LLM 없이도 숫자가 그대로 표시됨)."""
    if "error" in report:
        return report["message"]
    names = {d["developer_id"]: d["name"] for d in load_developers()}
    p = report["progress"]
    lines = [f"[기준 주 {report['reference_week']}] 진척 현황"]
    if p["scope"] == "task":
        tasks = [p]
    else:
        tasks = p["tasks"]
        lines.append(f"- 범위 합계: 진척률 {p['progress']*100:.2f}%, 계획 {p['plan_mm']:.2f}MM, 남은 {p['remaining_mm']:.2f}MM")
    if "developers" in report:
        lines.append("## 개발자별")
        for d in report["developers"]:
            lines.append(f"- {d['name']}({d['developer_id']}): 진척률 {d['progress']*100:.2f}%, 남은 {d['remaining_mm']:.2f}MM")
    lines.append("## 업무별")
    for t in tasks:
        rest = ",".join(f"{t['task_id']}-{n}" for n in t["subtasks_remaining"]) or "없음"
        lines.append(f"- {t['task_id']} {t['title']} ({names.get(t['owner'], t['owner'])}, {t['status']}): "
                     f"진척률 {t['progress']*100:.2f}%, 남은 {t['remaining_mm']:.2f}MM, 남은 하위 작업 {rest}, "
                     f"커밋 {t['commit_count_week']}건(이번 주)/{t['commit_count_total']}건(누적)")
    lines.append("## 경고: 상태-커밋 불일치")
    lines += [f"- {w['message']}" for w in report["mismatch_warnings"]] or ["- 없음"]
    lines.append("## 미연결 커밋 (업무 ID 없음·형식 오류)")
    lines += [f"- {c['hash']} {names.get(c['developer_id'], '알 수 없음')} {c['date']} \"{c['message']}\""
              for c in report["unlinked_commits"]] or ["- 없음"]
    return "\n".join(lines)


STATUS_ICON = {"완료": "✅ 완료", "진행중": "🟡 진행중", "미착수": "⚪ 미착수"}
UNLINKED_REASON = {"no_or_malformed_task_id": "업무 ID 없음·형식 오류", "unknown_task_id": "없는 업무 ID"}


def progress_bar(p: float, width: int = 10) -> str:
    n = round(p * width)
    return "█" * n + "░" * (width - n)


def format_progress_markdown(report: dict) -> str:
    """회의 자료용 표 렌더링 (팀 전체 보고서). 숫자는 도구 계산값 그대로."""
    names = {d["developer_id"]: d["name"] for d in load_developers()}
    p = report["progress"]
    warns, unlinked = report["mismatch_warnings"], report["unlinked_commits"]
    L = [f"기준 주: {report['reference_week']}", "",
         "| 전체 진척률 | 진행 | 계획 | 남은 공수 | 불일치 경고 | 미연결 커밋 |",
         "|---:|---|---:|---:|---:|---:|",
         f"| **{p['progress']*100:.2f}%** | `{progress_bar(p['progress'])}` | {p['plan_mm']:.2f}MM | "
         f"{p['remaining_mm']:.2f}MM | {len(warns)}건 | {len(unlinked)}건 |", "",
         "### 개발자별", "", "| 개발자 | 진척률 | 진행 | 남은 공수 |", "|---|---:|---|---:|"]
    L += [f"| {d['name']} | {d['progress']*100:.2f}% | `{progress_bar(d['progress'])}` | {d['remaining_mm']:.2f}MM |"
          for d in report.get("developers", [])]
    L += ["", "### 업무별", "",
          "| 업무 | 담당 | 상태 | 진척률 | 진행 | 남은 공수 | 남은 하위 작업 | 커밋(이번 주/누적) |",
          "|---|---|---|---:|---|---:|---|---:|"]
    for t in p["tasks"]:
        rest = ", ".join(f"{t['task_id']}-{n}" for n in t["subtasks_remaining"]) or "-"
        L.append(f"| {t['task_id']} {t['title']} | {names.get(t['owner'], t['owner'])} | "
                 f"{STATUS_ICON.get(t['status'], t['status'])} | {t['progress']*100:.2f}% | `{progress_bar(t['progress'])}` | "
                 f"{t['remaining_mm']:.2f}MM | {rest} | {t['commit_count_week']}/{t['commit_count_total']} |")
    L += ["", "### ⚠️ 상태-커밋 불일치", ""]
    L += (["| 업무 | 내용 |", "|---|---|"] + [f"| {w['task_id']} | {w['message']} |" for w in warns]) if warns else ["없음"]
    L += ["", "### 🔗 미연결 커밋", ""]
    L += (["| 커밋 | 작성자 | 일자 | 메시지 | 사유 |", "|---|---|---|---|---|"]
          + [f"| {c['hash']} | {names.get(c['developer_id'], '알 수 없음')} | {c['date']} | `{c['message']}` | "
             f"{UNLINKED_REASON.get(c['reason'], c['reason'])} |" for c in unlinked]) if unlinked else ["없음"]
    return "\n".join(L)
