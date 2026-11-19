"""진척 계산 도구 (결정적 함수). 숫자는 모두 여기서 계산하고 LLM은 설명만 한다.

- 도메인: 플랫폼 전환 프로젝트. 대메뉴(A~H) → 중메뉴(A01..) → 소메뉴 화면(SCR-A01-03). 화면 1개 = 업무 1개
- 화면마다 단계 4개 고정: 1 분석, 2 개발, 3 단위테스트, 4 통합테스트
- 커밋-화면 매핑: 메시지의 화면 ID (SCR-<대><중2자리>-<소2자리>); 없거나 형식 오류·없는 화면이면 '미연결'
- 단계 완료: ``[SCR-A01-03-2][done]`` 형식의 완료 표시
- 진척률: 화면 = 완료 단계 / 4, 개발자·메뉴·파트·전체 = 계획 MM 가중 평균
- 남은 MM = 계획 MM x (1 - 진척률)
- 화면 상태와 커밋이 불일치하면 상태를 우선하고 경고로 표시
- 계획 대비 지연: 계획 종료일이 기준 주 종료일보다 앞인데 상태가 '완료'가 아닌 화면
- 일정 대비 진척: 일정 경과율 = 프로젝트 시작~기준 주 종료일 일수 / 전체 일수(양 끝 포함),
  계획 진척률 = 화면별 계획 시작~종료일 선형 비율(0~1)의 계획 MM 가중, 차이(%p) = 실적 - 계획,
  상태 = 차이 -3%p 이상 정상, -10%p 이상 주의, 그 미만 지연
- 이슈(회의 간 추적, 안정 ID): DLY-<화면> 계획 대비 지연, MIS-<화면> 상태-커밋 불일치, UNL-<커밋> 미연결 커밋
- 묶음 구성: CRUD 기능별 화면 수(C·R·U·D 기능을 가진 화면 수, 조회 전용 화면은 R만), 복잡도별 화면 수(상·중·하),
  계획 공수 = Σ 계획 MM, 진행 공수 = Σ 계획 MM x 진척률(완료된 만큼), 남은 공수 = 계획 - 진행
- 내부 처리는 실제 이메일을 쓰며, 이 모듈의 출력에는 이메일을 싣지 않는다 (마스킹은 미들웨어 담당)
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

FIXTURE_DIR = Path(os.environ.get("FIXTURE_DIR", Path(__file__).resolve().parent.parent / "data" / "fixtures"))

PHASES = {1: "분석", 2: "개발", 3: "단위테스트", 4: "통합테스트"}
# 화면 ID: 앞뒤가 영숫자/하이픈이 아니어야 하고 대메뉴 1자, 중·소 번호는 정확히 2자리. 선택적 단계 번호(-N).
SCREEN_REF_RE = re.compile(r"(?<![A-Za-z0-9-])SCR-([A-Z])(\d{2})-(\d{2})(?:-(\d+))?(?![A-Za-z0-9])")
# 질문 속 화면 ID 추출용 (단계 번호가 붙어 있어도 화면 ID만)
SCREEN_ID_RE = re.compile(r"(?<![A-Za-z0-9-])SCR-[A-Z]\d{2}-\d{2}(?!\d)")
DONE_RE = re.compile(r"\[done\]", re.IGNORECASE)

STATUS_DONE = "완료"
STATUS_NOT_STARTED = "미착수"

WARN_STATUS_DONE = "STATUS_DONE_SUBTASKS_INCOMPLETE"
WARN_SUBTASKS_DONE = "SUBTASKS_DONE_STATUS_NOT_DONE"
WARN_NOT_STARTED = "NOT_STARTED_BUT_HAS_COMMITS"
WARN_BAD_SUBTASK = "DONE_MARK_UNKNOWN_SUBTASK"

TOP_N = 10  # 회의 자료·답변에 보여줄 지연 화면 수
COMPLEXITY_LEVELS = ("상", "중", "하")
STATUS_OK_PP, STATUS_WARN_PP = -3.0, -10.0  # 일정 상태 기준: 실적-계획 차이(%p)


def _load(name: str):
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def load_project() -> dict:
    return _load("project.json")


def load_reference_week() -> dict:
    return load_project()["reference_week"]


def load_developers() -> list[dict]:
    return _load("developers.json")


def load_screens() -> list[dict]:
    return _load("screens.json")


def load_commits() -> list[dict]:
    return _load("commits.json")


def load_meeting_history() -> dict:
    """지난 회의 스냅샷 기록 {주 시작일: 스냅샷} (장기 메모리 초기값). 파일이 없으면 빈 기록."""
    return _load("meeting_history.json") if (FIXTURE_DIR / "meeting_history.json").exists() else {}


def previous_snapshot(week_start: str | None = None) -> dict | None:
    """기준 주보다 앞선 가장 최근 회의 스냅샷."""
    week_start = week_start or load_reference_week()["start"]
    hist = load_meeting_history()
    keys = sorted(k for k in hist if k < week_start)
    return hist[keys[-1]] if keys else None


def planned_ratio(screen: dict, as_of: str) -> Fraction:
    """계획 진척률: 계획 시작~종료일 사이 선형, 기준일 시점 0~1."""
    st, en, d = (date.fromisoformat(x) for x in (screen["planned_start"], screen["planned_end"], as_of))
    if d >= en:
        return Fraction(1)
    if d <= st:
        return Fraction(0)
    return Fraction((d - st).days, (en - st).days)


def schedule_elapsed(as_of: str | None = None) -> Fraction:
    """일정 경과율: 프로젝트 시작일~기준일 일수 / 전체 일수 (양 끝 포함)."""
    p = load_project()
    st, en = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
    d = date.fromisoformat(as_of or p["reference_week"]["end"])
    return Fraction((d - st).days + 1, (en - st).days + 1)


def schedule_status(gap_pp: float) -> str:
    return "정상" if gap_pp >= STATUS_OK_PP else "주의" if gap_pp >= STATUS_WARN_PP else "지연"


def find_screen(screen_id: str) -> dict | None:
    return next((s for s in load_screens() if s["screen_id"] == screen_id), None)


def user_scope(role: str, user_id: str) -> dict:
    """역할별 조회 범위: PM=전체({}), PL=본인 파트({'part'}), 개발자=본인({'developer_id'})."""
    if role == "developer":
        return {"developer_id": user_id}
    if role == "pl":
        dev = next((d for d in load_developers() if d["developer_id"] == user_id), None)
        return {"part": dev["part"] if dev else None}
    return {}


def screen_contact(screen_id: str) -> str | None:
    """화면 담당자 연락처 (이름 + 이메일). 이메일은 출력 단계 미들웨어가 마스킹한다. 범위 검사는 가드레일 담당."""
    s = find_screen(screen_id)
    dev = next((d for d in load_developers() if s and d["developer_id"] == s["owner"]), None)
    if dev is None:
        return None
    return f"{screen_id} 담당자: {dev['name']} / 이메일: {dev['email']} / 전화번호: 등록되어 있지 않습니다"


def in_scope(screen: dict, scope: dict) -> bool:
    if "developer_id" in scope:
        return screen["owner"] == scope["developer_id"]
    if "part" in scope:
        return screen["part"] == scope["part"]
    return True


def parse_commit_message(message: str) -> dict:
    """커밋 메시지에서 화면 ID·단계 번호·완료 표시를 추출한다. 형식 오류면 screen_id=None."""
    m = SCREEN_REF_RE.search(message)
    if not m:
        return {"screen_id": None, "phase_no": None, "done": False}
    phase = int(m.group(4)) if m.group(4) else None
    done = bool(DONE_RE.search(message)) and phase is not None
    return {"screen_id": f"SCR-{m.group(1)}{m.group(2)}-{m.group(3)}", "phase_no": phase, "done": done}


def get_commits() -> dict:
    """기준 주 종료일까지의 커밋을 화면에 연결하고 미연결 커밋을 분리한다."""
    week = load_reference_week()
    screens = {s["screen_id"] for s in load_screens()}
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
        if p["screen_id"] is None:
            rec["reason"] = "no_or_malformed_screen_id"
            unlinked.append(rec)
        elif p["screen_id"] not in screens:
            rec["reason"] = "unknown_screen_id"
            unlinked.append(rec)
        else:
            linked.append(rec)
    return {"linked": linked, "unlinked": unlinked}


def _r4(x: Fraction) -> float:
    return round(float(x), 4)


def _r2(x: Fraction) -> float:
    return round(float(x), 2)


def _screen_calc() -> dict:
    """화면별 정확 분수 계산 결과와 경고."""
    commits = get_commits()
    week_end = load_reference_week()["end"]
    by_screen: dict[str, list] = {}
    for c in commits["linked"]:
        by_screen.setdefault(c["screen_id"], []).append(c)
    result = {}
    for s in load_screens():
        sid = s["screen_id"]
        mine = by_screen.get(sid, [])
        done_set, warnings = set(), []
        for c in mine:
            if c["done"]:
                if c["phase_no"] in PHASES:
                    done_set.add(c["phase_no"])
                else:
                    warnings.append({"code": WARN_BAD_SUBTASK, "screen_id": sid, "hash": c["hash"],
                                     "message": f"{sid}에 없는 단계 {c['phase_no']}번 완료 표시 (무시)"})
        status = s["status"]
        mm = Fraction(str(s["plan_mm"]))
        if status == STATUS_DONE:
            ratio, effective = Fraction(1), set(PHASES)  # 상태 우선
            if len(done_set) < len(PHASES):
                missing = sorted(set(PHASES) - done_set)
                warnings.append({"code": WARN_STATUS_DONE, "screen_id": sid,
                                 "message": f"{sid} 상태는 '완료'이나 완료 표시 없는 단계 {missing} (상태 기준 100% 적용)"})
        elif status == STATUS_NOT_STARTED:
            ratio, effective = Fraction(0), set()
            if mine:
                warnings.append({"code": WARN_NOT_STARTED, "screen_id": sid,
                                 "message": f"{sid} 상태는 '미착수'이나 커밋 {len(mine)}건 존재 (상태 기준 0% 적용)"})
        else:
            ratio, effective = Fraction(len(done_set), len(PHASES)), done_set
            if len(done_set) == len(PHASES):
                warnings.append({"code": WARN_SUBTASKS_DONE, "screen_id": sid,
                                 "message": f"{sid} 4단계가 모두 완료됐으나 상태는 '{status}'"})
        result[sid] = {
            **s, "plan_mm": mm, "ratio": ratio, "remaining": mm * (1 - ratio), "planned": planned_ratio(s, week_end),
            "phases_done": sorted(effective), "phases_remaining": sorted(set(PHASES) - effective),
            "delayed": s["planned_end"] < week_end and status != STATUS_DONE,
            "commit_count_total": len(mine),
            "commit_count_week": sum(1 for c in mine if c["in_week"]),
            "warnings": warnings,
        }
    return result


def _public_screen(x: dict) -> dict:
    return {
        "screen_id": x["screen_id"], "title": x["title"], "large_code": x["large_code"], "large_name": x["large_name"],
        "medium_code": x["medium_code"], "medium_name": x["medium_name"], "part": x["part"], "owner": x["owner"],
        "status": x["status"], "planned_start": x["planned_start"], "planned_end": x["planned_end"],
        "delayed": x["delayed"], "planned_progress": _r4(x["planned"]),
        "plan_mm": float(x["plan_mm"]), "progress": _r4(x["ratio"]), "remaining_mm": _r2(x["remaining"]),
        "phases_done": x["phases_done"], "phases_remaining": x["phases_remaining"],
        "commit_count_total": x["commit_count_total"], "commit_count_week": x["commit_count_week"],
    }


def _aggregate(items: list[dict]) -> dict:
    plan = sum((i["plan_mm"] for i in items), Fraction(0))
    done = sum((i["plan_mm"] * i["ratio"] for i in items), Fraction(0))
    ratio = done / plan if plan else Fraction(0)
    planned = _r4(sum((i["plan_mm"] * i["planned"] for i in items), Fraction(0)) / plan) if plan else 0.0
    gap = round((_r4(ratio) - planned) * 100, 2)
    return {"plan_mm": float(plan), "progress": _r4(ratio), "remaining_mm": _r2(plan - done), "earned_mm": _r2(done),
            "crud": {op: sum(1 for i in items if op in i.get("crud", "")) for op in "CRUD"},
            "complexity": {c: sum(1 for i in items if i.get("complexity") == c) for c in COMPLEXITY_LEVELS},
            "planned_progress": planned, "gap_pp": gap, "schedule_status": schedule_status(gap),
            "screens_done": sum(1 for i in items if i["status"] == STATUS_DONE), "screens_total": len(items),
            "delayed_count": sum(1 for i in items if i["delayed"])}


def _rollup(items: list[dict], code: str, name: str) -> list[dict]:
    groups: dict[str, list] = {}
    for i in items:
        groups.setdefault(i[code], []).append(i)
    return [{"code": k, "name": v[0][name], "part": v[0]["part"], **_aggregate(v)} for k, v in sorted(groups.items())]


def calc_progress(developer_id: str | None = None, screen_id: str | None = None, part: str | None = None) -> dict:
    """진척률 계산. 인자 없음=전체, developer_id=개발자, part=파트(PL1|PL2), screen_id=화면 단건.
    없는 대상은 error 반환."""
    calc = _screen_calc()
    if screen_id is not None:
        if screen_id not in calc:
            return {"error": "not_found", "message": f"{screen_id} 화면을 찾을 수 없습니다"}
        return {"scope": "screen", **_public_screen(calc[screen_id])}
    if developer_id is not None:
        if developer_id not in {d["developer_id"] for d in load_developers()}:
            return {"error": "not_found", "message": "대상을 찾을 수 없습니다"}
        mine = [v for v in calc.values() if v["owner"] == developer_id]
        return {"scope": "developer", "developer_id": developer_id,
                **_aggregate(mine), "screens": [_public_screen(v) for v in mine]}
    if part is not None:
        if part not in load_project()["parts"]:
            return {"error": "not_found", "message": "대상을 찾을 수 없습니다"}
        mine = [v for v in calc.values() if v["part"] == part]
        return {"scope": "part", "part": part, **_aggregate(mine), "screens": [_public_screen(v) for v in mine]}
    return {"scope": "team", **_aggregate(list(calc.values())),
            "screens": [_public_screen(v) for v in calc.values()]}


def calc_remaining_mm(developer_id: str | None = None, screen_id: str | None = None, part: str | None = None) -> dict:
    """남은 MM = 계획 MM x (1 - 진척률)."""
    p = calc_progress(developer_id=developer_id, screen_id=screen_id, part=part)
    if "error" in p:
        return p
    return {k: p[k] for k in ("scope", "screen_id", "developer_id", "part", "plan_mm", "remaining_mm") if k in p}


def check_mismatch() -> dict:
    """화면 상태-커밋 불일치 경고와 미연결 커밋 목록."""
    commits = get_commits()
    warnings = [w for v in _screen_calc().values() for w in v["warnings"]]
    unlinked = [{"hash": c["hash"], "date": c["date"], "developer_id": c["developer_id"],
                 "message": c["message"], "reason": c["reason"]} for c in commits["unlinked"]]
    return {"mismatch_warnings": warnings, "unlinked_commits": unlinked}


def delayed_screens(screens: list[dict]) -> list[dict]:
    """계획 대비 지연 화면: 계획 종료일이 이른 순."""
    return sorted((s for s in screens if s["delayed"]), key=lambda s: (s["planned_end"], s["screen_id"]))


def build_progress_report(developer_id: str | None = None, screen_id: str | None = None,
                          part: str | None = None) -> dict:
    """진척 질의 응답용 묶음: 범위 진척 + (전체·파트) 메뉴별·단계별·개발자별 집계 + 지연 화면 + 경고 + 미연결 커밋.

    developer_id·part 지정 시 해당 범위만 (경고·미연결도 범위 안의 화면/커밋으로 한정).
    """
    project = load_project()
    week = project["reference_week"]
    prog = calc_progress(developer_id=developer_id, screen_id=screen_id, part=part)
    if "error" in prog:
        return prog
    mm = check_mismatch()
    report = {"project": project["name"], "month_label": project["month_label"],
              "reference_week": week["label"], "progress": prog}
    if screen_id is None:
        end = date.fromisoformat(week["end"])
        report["schedule"] = {"as_of": week["end"], "elapsed": _r4(schedule_elapsed(week["end"])),
                              "milestones": [{**m, "d_day": (date.fromisoformat(m["date"]) - end).days}
                                             for m in project.get("milestones", []) if m["date"] > week["end"]]}
    if screen_id is not None:
        report["mismatch_warnings"] = [w for w in mm["mismatch_warnings"] if w["screen_id"] == screen_id]
        report["unlinked_commits"] = []
        return report
    screens = prog["screens"]
    report["delayed"] = delayed_screens(screens)
    own = {s["screen_id"] for s in screens}
    if developer_id is None:
        calc = [v for v in _screen_calc().values() if v["screen_id"] in own]
        report["large_menus"] = _rollup(calc, "large_code", "large_name")
        report["medium_menus"] = _rollup(calc, "medium_code", "medium_name")
        report["phases"] = [{"no": n, "name": name, "done": sum(1 for s in screens if n in s["phases_done"]),
                             "total": len(screens)} for n, name in PHASES.items()]
        if part is None:
            parts = [v for v in _screen_calc().values() if v["screen_id"] in own]
            report["parts"] = _rollup(parts, "part", "part")
        devs = []
        for d in load_developers():
            if d["role"] != "developer" or (part is not None and d["part"] != part):
                continue
            p = calc_progress(developer_id=d["developer_id"])
            devs.append({"developer_id": d["developer_id"], "name": d["name"], "part": d["part"],
                         **{k: p[k] for k in ("progress", "plan_mm", "remaining_mm", "screens_done",
                                              "screens_total", "delayed_count")}})
        report["developers"] = devs
    members = {s["owner"] for s in screens}
    report["mismatch_warnings"] = [w for w in mm["mismatch_warnings"] if w["screen_id"] in own]
    report["unlinked_commits"] = [c for c in mm["unlinked_commits"]
                                  if (developer_id is None and part is None) or c["developer_id"] in members]
    return report


def _pct(x: float) -> str:
    return f"{x*100:.2f}%"


def _phase_ids(s: dict, phases: list[int]) -> str:
    if len(phases) == len(PHASES):
        return "4단계 전체(분석~통합테스트)"
    return ",".join(f"{s['screen_id']}-{n}({PHASES[n]})" for n in phases) or "없음"


def format_progress_report(report: dict, authors: bool = True) -> str:
    """결정적 텍스트 렌더링 (LLM 없이도 숫자가 그대로 표시됨). 화면 170개를 모두 나열하지 않고 집계 위주로 보여준다.
    미연결 커밋에는 작성자 이름·이메일을 적는다(이메일은 출력 단계 미들웨어가 마스킹). 보고서 범위가 곧 요청자 범위라
    PM 전체, PL 본인 파트, 개발자 본인 커밋만 나온다. 공유 문서(회의 자료) 입력은 authors=False로 작성자를 뺀다."""
    if "error" in report:
        return report["message"]
    names = {d["developer_id"]: d["name"] for d in load_developers()}
    emails = {d["developer_id"]: d["email"] for d in load_developers()}
    p = report["progress"]
    lines = [f"[기준 주 {report['reference_week']}] {report['project']} 진척 현황 ({report['month_label']})"]
    if p["scope"] == "screen":
        lines.append(f"- {p['screen_id']} {p['large_name']} > {p['medium_name']} > {p['title']} "
                     f"({names.get(p['owner'], p['owner'])}, {p['status']}, 계획 종료 {p['planned_end']}"
                     f"{', 계획 대비 지연' if p['delayed'] else ''}): 진척률 {_pct(p['progress'])}, "
                     f"남은 {p['remaining_mm']:.2f}MM, 남은 단계 {_phase_ids(p, p['phases_remaining'])}, "
                     f"커밋 {p['commit_count_week']}건(이번 주)/{p['commit_count_total']}건(누적)")
    else:
        scope = {"team": "프로젝트 전체", "part": f"{p.get('part')} 파트",
                 "developer": f"{names.get(p.get('developer_id'), '')}({p.get('developer_id')}) 담당"}[p["scope"]]
        lines.append(f"- {scope} 합계: 진척률 {_pct(p['progress'])}, 계획 {p['plan_mm']:.2f}MM, "
                     f"남은 {p['remaining_mm']:.2f}MM, 화면 완료 {p['screens_done']}/{p['screens_total']}, "
                     f"계획 대비 지연 {p['delayed_count']}개")
        sch = report["schedule"]
        lines.append(f"- 일정 대비: 일정 경과율 {_pct(sch['elapsed'])}, 계획 진척률 {_pct(p['planned_progress'])}, "
                     f"실적 진척률 {_pct(p['progress'])}, 차이 {p['gap_pp']:+.2f}%p ({p['schedule_status']})")
    if report.get("parts"):  # 전체 보고서: 파트(PL1·PL2)별 합계
        lines.append("## 파트별")
        lines += [f"- {m['code']} 파트: 진척률 {_pct(m['progress'])}, 남은 {m['remaining_mm']:.2f}MM, "
                  f"화면 완료 {m['screens_done']}/{m['screens_total']}, 지연 {m['delayed_count']}개, "
                  f"계획 {_pct(m['planned_progress'])}(차이 {m['gap_pp']:+.2f}%p, {m['schedule_status']})"
                  for m in report["parts"]]
    if "large_menus" in report:
        lines.append("## 대메뉴별")
        lines += [f"- {m['code']} {m['name']}({m['part']}): 진척률 {_pct(m['progress'])}, 남은 {m['remaining_mm']:.2f}MM, "
                  f"화면 완료 {m['screens_done']}/{m['screens_total']}, 지연 {m['delayed_count']}개, "
                  f"계획 {_pct(m['planned_progress'])}(차이 {m['gap_pp']:+.2f}%p, {m['schedule_status']})"
                  for m in report["large_menus"]]
        lines.append("## 중메뉴별")
        lines += [f"- {m['code']} {m['name']}: 진척률 {_pct(m['progress'])}, 남은 {m['remaining_mm']:.2f}MM, "
                  f"화면 완료 {m['screens_done']}/{m['screens_total']}, 지연 {m['delayed_count']}개, "
                  f"계획 {_pct(m['planned_progress'])}(차이 {m['gap_pp']:+.2f}%p)"
                  for m in report["medium_menus"]]
        lines.append("## 단계별 완료 화면")
        lines += [f"- {ph['no']} {ph['name']}: {ph['done']}/{ph['total']}" for ph in report["phases"]]
    if "developers" in report:
        lines.append("## 개발자별")
        lines += [f"- {d['name']}({d['developer_id']}): 진척률 {_pct(d['progress'])}, 남은 {d['remaining_mm']:.2f}MM, "
                  f"화면 완료 {d['screens_done']}/{d['screens_total']}" for d in report["developers"]]
    if p["scope"] == "developer":
        rest = [s for s in p["screens"] if s["phases_remaining"]]
        lines.append(f"## 남은 화면 ({len(rest)}개, 계획 종료일 순)")
        for s in sorted(rest, key=lambda s: (s["planned_end"], s["screen_id"])):
            lines.append(f"- {s['screen_id']} {s['title']} ({s['status']}, 계획 종료 {s['planned_end']}"
                         f"{', 지연' if s['delayed'] else ''}): 진척률 {_pct(s['progress'])}, 남은 {s['remaining_mm']:.2f}MM, "
                         f"남은 단계 {_phase_ids(s, s['phases_remaining'])}, 커밋 {s['commit_count_week']}건(이번 주)")
    if "delayed" in report and p["scope"] != "developer":
        d = report["delayed"]
        lines.append(f"## 계획 대비 지연 화면 (전체 {len(d)}개 중 상위 {min(len(d), TOP_N)}개)")
        lines += [f"- {s['screen_id']} {s['title']} ({names.get(s['owner'], s['owner'])}, 계획 종료 {s['planned_end']}, "
                  f"{s['status']}): 진척률 {_pct(s['progress'])}, 남은 단계 {_phase_ids(s, s['phases_remaining'])}"
                  for s in d[:TOP_N]] or ["- 없음"]
    lines.append("## 경고: 상태-커밋 불일치")
    lines += [f"- {w['message']}" for w in report["mismatch_warnings"]] or ["- 없음"]
    lines.append("## 미연결 커밋 (화면 ID 없음·형식 오류·없는 화면)")
    who = lambda c: (f" {names[c['developer_id']]} <{emails[c['developer_id']]}>" if c["developer_id"] in names  # noqa: E731
                     else " 작성자 알 수 없음") if authors else ""
    lines += [f"- {c['hash']}{who(c)} {c['date']} \"{c['message']}\" ({UNLINKED_REASON.get(c['reason'], c['reason'])})"
              for c in report["unlinked_commits"]] or ["- 없음"]
    return "\n".join(lines)


STATUS_ICON = {"완료": "✅ 완료", "진행중": "🟡 진행중", "미착수": "⚪ 미착수"}
UNLINKED_REASON = {"no_or_malformed_screen_id": "화면 ID 없음·형식 오류", "unknown_screen_id": "없는 화면 ID"}


SCHEDULE_ICON = {"정상": "🟢 정상", "주의": "🟡 주의", "지연": "🔴 지연"}


def progress_bar(p: float, width: int = 10) -> str:
    n = round(p * width)
    return "█" * n + "░" * (width - n)


def delta_pp(cur: float, prev: float | None) -> str:
    """지난주 대비 진척 증감(%p): 증가 ▲, 감소 ▼, 변화 없음 '-'. Confluence 변환기가 ▲는 초록, ▼는 빨강으로 표시한다."""
    return "-" if prev is None else delta_mark(round((cur - prev) * 100, 2))


def delta_mark(d: float, unit: str = "%p") -> str:
    return f"▲ +{d:.2f}{unit}" if d > 0 else f"▼ {d:.2f}{unit}" if d < 0 else "-"


def delta_count(cur: int, prev: int | None) -> str:
    """완료 화면·단계 수 증감 (늘어나면 좋은 값에만 쓴다)."""
    if prev is None:
        return "-"
    d = cur - prev
    return f"▲ +{d}" if d > 0 else f"▼ {d}" if d < 0 else "-"


def current_issues(report: dict) -> list[dict]:
    """회의 간 추적할 이번 주 이슈 (안정 ID). 지연 화면 → 상태-커밋 불일치 → 미연결 커밋 순."""
    out = [{"id": f"DLY-{s['screen_id']}", "kind": "지연", "target": s["screen_id"],
            "title": f"{s['screen_id']} {s['title']}: 계획 종료 {s['planned_end']} 경과, 미완료"} for s in report["delayed"]]
    for w in report["mismatch_warnings"]:
        if f"MIS-{w['screen_id']}" not in {i["id"] for i in out}:
            out.append({"id": f"MIS-{w['screen_id']}", "kind": "불일치", "target": w["screen_id"], "title": w["message"]})
    out += [{"id": f"UNL-{c['hash']}", "kind": "미연결", "target": c["hash"],
             "title": f"미연결 커밋 {c['hash']}: {UNLINKED_REASON.get(c['reason'], c['reason'])}"}
            for c in report["unlinked_commits"]]
    return out


def part_label(code: str) -> str:
    menus = load_project()["parts"][code]["large_menus"]
    return f"{code} ({menus[0]}~{menus[-1]})"


def _old(d: dict, code: str):
    return d[code]["progress"] if code in d else None


def _opt_pct(x) -> str:
    return _pct(x) if x is not None else "-"


def format_progress_markdown(report: dict, prev: dict | None = None) -> str:
    """회의 자료용 표 렌더링 (전체 보고서). 숫자는 도구 계산값 그대로. prev는 지난주 회의 스냅샷(지난주 대비 증감).
    회의 자료는 참석자 전원(PM·PL·개발자)에게 공유되므로 업무 분류 단위(전체·파트·대메뉴·중메뉴·단계)만 싣고
    개인별 진척률·담당자 열은 싣지 않는다."""
    p, sch = report["progress"], report["schedule"]
    prev = prev or {}
    pt = prev.get("team")

    def plan_row(label, r, bold=False):
        actual = f"**{_pct(r['progress'])}**" if bold else _pct(r["progress"])
        return (f"| {label} | {_pct(r['planned_progress'])} | {actual} | {r['gap_pp']:+.2f}%p | "
                f"{SCHEDULE_ICON[r['schedule_status']]} | `{progress_bar(r['progress'])}` | "
                f"{r['screens_done']}/{r['screens_total']} | {r['remaining_mm']:.2f}MM | {r['delayed_count']}개 |")

    ms = " · ".join(f"{m['date']} {m['name']}(D-{m['d_day']})" for m in sch["milestones"])
    L = [f"{report['project']} · 기준 주: {report['reference_week']} · {report['month_label']}", "",
         "### 일정 대비 진척", "",
         "| 구분 | 계획 진척률 | 실적 진척률 | 차이 | 상태 | 진행 | 화면(완료/전체) | 남은 공수 | 지연 화면 |",
         "|---|---:|---:|---:|---|---|---:|---:|---:|",
         plan_row("**전체**", p, bold=True), *[plan_row(part_label(r["code"]), r) for r in report.get("parts", [])], "",
         f"일정 경과율 **{_pct(sch['elapsed'])}** (프로젝트 기간 중 {sch['as_of']}까지) · 계획 진척률 = 화면별 계획 시작~종료일 "
         f"선형 배분의 계획 MM 가중 · 상태: 차이 {STATUS_OK_PP:+.0f}%p 이상 정상, {STATUS_WARN_PP:+.0f}%p 이상 주의, 그 미만 지연"
         + (f" · 다음 마일스톤: {ms}" if ms else ""), "",
         "#### 전체 기간 대비 현재 위치", "", "```", *timeline_lines(sch["elapsed"], p["planned_progress"], p["progress"]),
         "```", "", *large_menu_schedule(report), ""]
    if pt:
        pgap = round((pt["progress"] - pt["planned_progress"]) * 100, 2)
        parts = [r for r in report.get("parts", []) if r["code"] in prev.get("parts", {})]
        L += [f"### 지난주 대비 변화 (지난주 회의 {prev['week']['start']} ~ {prev['week']['end']} 스냅샷 기준)", "",
              "| 항목 | 지난주 | 이번 주 | 증감 |", "|---|---:|---:|---|",
              f"| 전체 실적 진척률 | {_pct(pt['progress'])} | **{_pct(p['progress'])}** | "
              f"{delta_pp(p['progress'], pt['progress'])} |",
              *[f"| {part_label(r['code'])} 실적 진척률 | {_pct(_old(prev['parts'], r['code']))} | {_pct(r['progress'])} | "
                f"{delta_pp(r['progress'], _old(prev['parts'], r['code']))} |" for r in parts],
              f"| 계획 진척률 | {_pct(pt['planned_progress'])} | {_pct(p['planned_progress'])} | "
              f"{(p['planned_progress'] - pt['planned_progress']) * 100:+.2f}%p |",
              f"| 계획 대비 차이 | {pgap:+.2f}%p | {p['gap_pp']:+.2f}%p | {delta_mark(round(p['gap_pp'] - pgap, 2))} |",
              f"| 완료 화면 | {pt['screens_done']}/{pt['screens_total']} | {p['screens_done']}/{p['screens_total']} | "
              f"{delta_count(p['screens_done'], pt['screens_done'])} |",
              f"| 남은 공수 | {pt['remaining_mm']:.2f}MM | {p['remaining_mm']:.2f}MM | "
              f"{p['remaining_mm'] - pt['remaining_mm']:+.2f}MM |",
              f"| 지연 화면 | {pt['delayed_count']}개 | {p['delayed_count']}개 | {p['delayed_count'] - pt['delayed_count']:+d}개 |",
              ""]
    pl, pm = prev.get("large_menus", {}), prev.get("medium_menus", {})
    L += ["### 대메뉴별 (계획 대비·지난주 대비)", "",
          "| 대메뉴 | 파트 | 계획 | 실적 | 차이 | 상태 | 진행 | 지난주 | 증감 | 화면(완료/전체) | 계획 공수 | 진행 공수 | "
          "남은 공수 | 지연 화면 |",
          "|---|---|---:|---:|---:|---|---|---:|---|---:|---:|---:|---:|---:|"]
    L += [f"| {m['code']} {m['name']} | {m['part']} | {_pct(m['planned_progress'])} | {_pct(m['progress'])} | "
          f"{m['gap_pp']:+.2f}%p | {SCHEDULE_ICON[m['schedule_status']]} | `{progress_bar(m['progress'])}` | "
          f"{_opt_pct(_old(pl, m['code']))} | {delta_pp(m['progress'], _old(pl, m['code']))} | "
          f"{m['screens_done']}/{m['screens_total']} | {m['plan_mm']:.2f}MM | {m['earned_mm']:.2f}MM | "
          f"{m['remaining_mm']:.2f}MM | {m['delayed_count']} |"
          for m in report["large_menus"]]
    L += ["", "### 중메뉴별", "",
          "CRUD = 생성(C)·조회(R)·수정(U)·삭제(D) 기능이 있는 화면 수(조회 전용 화면은 R만), 복잡도 = 상·중·하 화면 수, "
          "공수(MM) = 계획 / 진행(계획 x 진척률, 완료분) / 남은", "",
          "| 중메뉴 | 계획 | 실적 | 차이 | 지난주 | 증감 | 화면 | CRUD | 복잡도 | 계획MM | 진행MM | 남은MM | 지연 |",
          "|---|---:|---:|---:|---:|---|---:|---|---|---:|---:|---:|---:|"]
    L += [f"| {m['code']} {m['name']} | {_pct(m['planned_progress'])} | {_pct(m['progress'])} | {m['gap_pp']:+.2f}%p | "
          f"{_opt_pct(_old(pm, m['code']))} | {delta_pp(m['progress'], _old(pm, m['code']))} | "
          f"{m['screens_done']}/{m['screens_total']} | {crud_label(m['crud'])} | {complexity_label(m['complexity'])} | "
          f"{m['plan_mm']:.2f} | {m['earned_mm']:.2f} | {m['remaining_mm']:.2f} | {m['delayed_count']} |"
          for m in report["medium_menus"]]
    pp = prev.get("phases", {})
    L += ["", "### 단계별 완료 현황", "", "| 단계 | 완료 화면 | 완료율 | 진행 | 지난주 | 증감 |", "|---|---:|---:|---|---:|---|"]
    for ph in report["phases"]:
        r = ph["done"] / ph["total"] if ph["total"] else 0
        before = pp.get(str(ph["no"]))
        was = f"{before}/{ph['total']}" if before is not None else "-"
        L.append(f"| {ph['no']} {ph['name']} | {ph['done']}/{ph['total']} | {_pct(r)} | `{progress_bar(r)}` | "
                 f"{was} | {delta_count(ph['done'], before)} |")
    return "\n".join(L)


def crud_label(c: dict) -> str:
    """CRUD 기능별 화면 수: 'C2 R5 U2 D1'."""
    return " ".join(f"{op}{c[op]}" for op in "CRUD")


def complexity_label(c: dict) -> str:
    """복잡도별 화면 수: '상1 중1 하3'."""
    return " ".join(f"{k}{c[k]}" for k in COMPLEXITY_LEVELS)


TIMELINE_WIDTH = 48  # 12개월 x 4칸 (고정폭)


def _months() -> list[date]:
    p = load_project()
    d, end, out = date.fromisoformat(p["start"]).replace(day=1), date.fromisoformat(p["end"]), []
    while d <= end:
        out.append(d)
        d = (d + timedelta(days=32)).replace(day=1)
    return out


def _pos(day: str) -> int:
    """프로젝트 기간(양 끝 포함) 안에서 날짜의 막대 위치(0~TIMELINE_WIDTH-1)."""
    p = load_project()
    st, en = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
    frac = Fraction((date.fromisoformat(day) - st).days + 1, (en - st).days + 1)
    return min(TIMELINE_WIDTH - 1, max(0, round(frac * TIMELINE_WIDTH) - 1))


def timeline_lines(elapsed: float, planned: float, actual: float, as_of: str | None = None) -> list[str]:
    """전체 기간 대비 현재 위치 텍스트 도식(고정폭): 월 눈금, 오늘(▼), 경과·계획·실적 막대, 마일스톤(◆). 값은 도구 계산값."""
    p, w = load_project(), TIMELINE_WIDTH
    bar = lambda x: "█" * round(x * w) + "░" * (w - round(x * w))  # noqa: E731
    today = " " * _pos(as_of or p["reference_week"]["end"]) + "▼ 기준 주"
    ms = ["·"] * w
    for m in p["milestones"]:
        ms[_pos(m["date"])] = "◆"
    gap = round((actual - planned) * 100, 2)
    return [f"기간  {p['start']} ~ {p['end']} (1칸 = 약 1주)",
            ("월    " + "".join(f"{m.month:<4}" for m in _months())).rstrip(),
            "      " + today,
            f"경과  {bar(elapsed)} {_pct(elapsed)}",
            f"계획  {bar(planned)} {_pct(planned)}",
            f"실적  {bar(actual)} {_pct(actual)} (계획 대비 {gap:+.2f}%p)",
            "      " + "".join(ms) + " ◆ " + " · ".join(f"{m['date'][5:]} {m['name']}" for m in p["milestones"])]


def large_menu_schedule(report: dict) -> list[str]:
    """대메뉴 일정표: 계획 기간(화면 계획 시작~종료)이 걸친 달은 ■, 기준 주가 속한 달은 ▼ 열, 마일스톤 달은 ◆ 행."""
    months, p = _months(), load_project()
    now = date.fromisoformat(p["reference_week"]["end"]).replace(day=1)
    span: dict[str, tuple[str, str]] = {}
    for s in load_screens():
        a, b = span.get(s["large_code"], (s["planned_start"], s["planned_end"]))
        span[s["large_code"]] = (min(a, s["planned_start"]), max(b, s["planned_end"]))
    head = [f"**{m.month}▼**" if m == now else str(m.month) for m in months]
    L = ["#### 대메뉴 일정표 (■ 계획 기간, ▼ 기준 주, ◆ 마일스톤)", "",
         "| 대메뉴 | " + " | ".join(head) + " | 실적 / 계획 |", "|---|" + ":---:|" * len(months) + "---:|"]
    for m in report["large_menus"]:
        a, b = (date.fromisoformat(x).replace(day=1) for x in span[m["code"]])
        cells = ["■" if a <= mo <= b else "" for mo in months]
        L.append(f"| {m['name']}({m['code']}) | " + " | ".join(cells)
                 + f" | {_pct(m['progress'])} / {_pct(m['planned_progress'])} |")
    mss = {date.fromisoformat(x["date"]).replace(day=1) for x in p["milestones"]}
    L.append("| ◆ 마일스톤 | " + " | ".join("◆" if mo in mss else "" for mo in months) + " | |")
    return L


def format_findings_markdown(report: dict) -> str:
    """회의 자료의 계획 대비 지연 화면(상위 N개, 담당자 열 없음)·상태-커밋 불일치·미연결 커밋(작성자 열 없음) 표."""
    warns, unlinked, delayed = report["mismatch_warnings"], report["unlinked_commits"], report["delayed"]
    L = [f"### ⏰ 계획 대비 지연 화면 (전체 {len(delayed)}개 중 계획 종료일이 이른 {min(len(delayed), TOP_N)}개)", ""]
    L += (["| 화면 | 계획 종료 | 상태 | 진척률 | 진행 | 남은 단계 | 남은 공수 |", "|---|---|---|---:|---|---|---:|"]
          + [f"| {s['screen_id']} {s['title']} | {s['planned_end']} | "
             f"{STATUS_ICON.get(s['status'], s['status'])} | {_pct(s['progress'])} | `{progress_bar(s['progress'])}` | "
             f"{', '.join(PHASES[n] for n in s['phases_remaining']) or '-'} | {s['remaining_mm']:.2f}MM |"
             for s in delayed[:TOP_N]]) if delayed else ["없음"]
    L += ["", "### ⚠️ 상태-커밋 불일치", ""]
    L += (["| 화면 | 내용 |", "|---|---|"] + [f"| {w['screen_id']} | {w['message']} |" for w in warns]) if warns else ["없음"]
    L += ["", "### 🔗 미연결 커밋", ""]
    L += (["| 커밋 | 일자 | 메시지 | 사유 |", "|---|---|---|---|"]
          + [f"| {c['hash']} | {c['date']} | `{c['message']}` | "
             f"{UNLINKED_REASON.get(c['reason'], c['reason'])} |" for c in unlinked]) if unlinked else ["없음"]
    return "\n".join(L)
