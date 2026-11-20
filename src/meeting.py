"""주간 회의 자료 생성.

- build_meeting_material(): LLM 없는 결정적 초안 (테스트·기본 핸들러용)
- MeetingFlow: Plan-Execute LangGraph (계획 → 실행 단계들 → interrupt() 승인 → MCP 게시)
  단계가 실패하면 Replan(retry·skip·abort, 재시도 1회·필수 단계 건너뛰기 금지)으로 처리하고, 중단되면 승인 대기 없이 끝낸다.
  승인은 approve·reject 외에 고친 제목·본문으로 승인(edit)할 수 있다(구성원 이름은 지운다).
  장기 메모리(LangGraph Store)에는 주차별 회의 스냅샷(진척·이슈·액션아이템)을 저장한다. Store가 비어 있으면
  지난 회의 기록(data/fixtures/meeting_history.json)으로 초기화하고, 게시 후에는 이번 주 스냅샷을 저장해 다음 주에 쓴다.
- 회의 자료 구성: 안내 → 일정 대비 진척 → 지난주 대비 변화 → 대메뉴별 → 중메뉴별 → 단계별 → 이슈 추적 →
  지난주 액션아이템 이행 → 계획 대비 지연 화면 → 불일치·미연결 → 결정사항 → 블로커 → 이번 주 액션아이템
"""
from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Literal, Optional, TypedDict

from pydantic import BaseModel, Field

from src import tools

NOTICE = ("> 주간 진척 보고 에이전트가 커밋·화면 데이터로 자동 작성하고, PM 또는 PL 승인을 거쳐 게시하는 문서입니다. "
          "참석자 전원에게 공유되므로 업무 단위 진척만 싣습니다(개인별 진척률·이름 없음).")
PUBLISH_FOOTER = ("※ 이 문서는 참석자 공용 자료로, 개인 이름·연락처·개인별 진척률을 싣지 않습니다. "
                  "이메일·전화번호는 게시 전 마스킹됩니다.")
DRAFT_MAX_ITEMS = 5    # LLM 초안 섹션별 항목 수 상한(출력 토큰 한도 안에서 구조화 출력이 잘리지 않게)
LONG_ISSUE_WEEKS = 8  # 이 주수 이상 지속된 미해결 이슈는 기본 초안의 블로커로 올린다
SNAPSHOT_NS = ("meeting_snapshot",)  # 장기 메모리 네임스페이스 (키: 주 시작일)


MATERIAL_SECTIONS = ("진척 현황", "이슈 추적", "지난주 액션아이템 이행", "계획 대비 지연·불일치·미연결",
                     "결정사항", "블로커", "액션아이템")


def material_guide(week_start: str) -> str:
    """주간 회의 자료 작성 지침 (MCP prompt로도 노출). render()의 섹션 구성·공개 원칙과 같은 내용이다."""
    return (f"기준 주 {week_start} 주간 회의 자료를 아래 순서의 섹션으로 작성한다: "
            + " → ".join(f"## {x}" for x in MATERIAL_SECTIONS) + ".\n"
            "- 참석자 전원에게 공유되는 자료이므로 진척은 대메뉴·중메뉴·단계·화면 같은 업무 단위로만 쓰고, 사람 이름·연락처·"
            "개인별 진척률은 싣지 않는다.\n"
            "- 진척률·MM 같은 숫자는 진척 도구 결과를 그대로 쓰고, 지난주 대비 증가는 ▲, 감소는 ▼로 표시한다.\n"
            "- 이슈는 해결·미해결(지속 N주)·신규로 나누고, 지난주 액션아이템은 커밋·화면 상태로 이행 여부를 판단한다.\n"
            "- 액션아이템은 체크리스트(- [ ])로 쓰고, 게시는 PM 또는 PL 승인 뒤에만 한다.")


def with_footer(body: str) -> str:
    """게시 본문 끝에 공용 자료·마스킹 안내 꼬리말을 붙인다 (게시 경로와 저장소 샘플 페이지 공통)."""
    return f"{body}\n\n---\n{PUBLISH_FOOTER}"


def meeting_page_id(week: str | None = None) -> str:
    """게시 파일명(page_id)은 영문으로 둔다."""
    return f"weekly_meeting_{week or tools.load_reference_week()['start']}"


def meeting_title(week: str | None = None) -> str:
    return f"주간회의 자료 {week or tools.load_reference_week()['start']}"


# ----------------------------------------------------------------------------- 지난주 대비·이슈 추적 (결정적)
def _weeks(since: str, week: str) -> int:
    return (date.fromisoformat(week) - date.fromisoformat(since)).days // 7 + 1


def track_issues(current: list[dict], previous: dict | None, week: str) -> dict:
    """지난주 스냅샷 이슈와 이번 주 이슈를 ID로 비교: 해결(지난주에만) / 미해결(둘 다, 지속 N주) / 신규(이번 주에만)."""
    prev = {i["id"]: i for i in (previous or {}).get("issues", [])}
    now = {i["id"] for i in current}
    persistent = [{**i, "since": prev[i["id"]]["since"], "weeks": _weeks(prev[i["id"]]["since"], week)}
                  for i in current if i["id"] in prev]
    return {"resolved": [{**i, "weeks": _weeks(i["since"], week) - 1} for i in prev.values() if i["id"] not in now],
            "persistent": sorted(persistent, key=lambda i: (-i["weeks"], i["id"])),
            "new": [{**i, "since": week, "weeks": 1} for i in current if i["id"] not in prev]}


def action_status(text: str, screens: dict, unlinked: set[str]) -> tuple[Optional[bool], str]:
    """지난주 액션아이템 이행 여부를 데이터로 판단한다. (완료 여부, 근거). 판단할 수 없으면 None.
    - 단계 ID(SCR-A01-03-4): 그 단계 완료 표시가 있으면 완료
    - 화면 ID만: 화면 상태가 '완료'면 완료
    - 커밋 해시(c0418): 더 이상 미연결이 아니면 완료"""
    refs = [m for m in tools.SCREEN_REF_RE.finditer(text) if f"SCR-{m.group(1)}{m.group(2)}-{m.group(3)}" in screens]
    phases = [(f"SCR-{m.group(1)}{m.group(2)}-{m.group(3)}", int(m.group(4))) for m in refs if m.group(4)]
    if phases:
        left = [f"{sid}-{n}({tools.PHASES.get(n, n)})" for sid, n in phases if n not in screens[sid]["phases_done"]]
        return (not left, "단계 완료 표시 확인" if not left else f"남은 단계 {', '.join(left)}")
    if refs:
        sids = sorted({f"SCR-{m.group(1)}{m.group(2)}-{m.group(3)}" for m in refs})
        left = [s for s in sids if screens[s]["status"] != tools.STATUS_DONE]
        return (not left, "상태 '완료' 확인" if not left else f"상태 '{screens[left[0]]['status']}'")
    hashes = re.findall(r"(?<![A-Za-z0-9])c\d{4}(?!\d)", text)
    if hashes:
        left = [h for h in hashes if h in unlinked]
        return (not left, "미연결 커밋 없음" if not left else f"미연결 그대로 {', '.join(left)}")
    return None, "데이터로 확인할 수 없음(회의에서 확인)"


def meeting_context(previous: dict | None = None, load_history: bool = True) -> dict:
    """회의 자료에 필요한 결정적 계산 묶음. previous가 없고 load_history면 지난 회의 기록에서 직전 스냅샷을 읽는다."""
    report = tools.build_progress_report()
    week = tools.load_reference_week()["start"]
    if previous is None and load_history:
        previous = tools.previous_snapshot(week)
    screens = {s["screen_id"]: s for s in report["progress"]["screens"]}
    unlinked = {c["hash"] for c in report["unlinked_commits"]}
    actions = [(t, *action_status(t, screens, unlinked)) for t in (previous or {}).get("action_items", [])]
    return {"week": week, "report": report, "previous": previous,
            "issues": track_issues(tools.current_issues(report), previous, week), "prev_actions": actions}


def week_snapshot(ctx: dict, action_items: list[str]) -> dict:
    """이번 주 회의 스냅샷 (다음 주 회의의 '지난주'). meeting_history.json과 같은 형식."""
    r, week = ctx["report"], tools.load_reference_week()
    p = r["progress"]
    return {"week": {"start": week["start"], "end": week["end"]}, "page_id": meeting_page_id(),
            "schedule_elapsed": r["schedule"]["elapsed"],
            "team": {k: p[k] for k in ("progress", "planned_progress", "remaining_mm", "screens_done", "screens_total",
                                       "delayed_count")},
            "parts": {m["code"]: {"progress": m["progress"], "planned_progress": m["planned_progress"]} for m in r["parts"]},
            "large_menus": {m["code"]: {k: m[k] for k in ("progress", "planned_progress", "screens_done", "screens_total")}
                            for m in r["large_menus"]},
            "medium_menus": {m["code"]: {k: m[k] for k in ("progress", "screens_done", "screens_total")}
                             for m in r["medium_menus"]},
            "phases": {str(ph["no"]): ph["done"] for ph in r["phases"]},
            "issues": [{k: i[k] for k in ("id", "kind", "target", "title", "since")}
                       for i in ctx["issues"]["persistent"] + ctx["issues"]["new"]],
            "action_items": action_items}


def _issue_markdown(ctx: dict) -> str:
    iss, prev = ctx["issues"], ctx["previous"]
    if prev is None:
        return "지난 회의 기록이 없어 이번 주 이슈를 모두 신규로 등록합니다."
    kinds = ("지연", "불일치", "미연결")
    row = lambda label, xs: (f"| {label} | " + " | ".join(str(sum(1 for i in xs if i["kind"] == k)) for k in kinds)  # noqa: E731
                             + f" | **{len(xs)}** |")
    L = [f"지난주 회의({prev['week']['start']} 주) 이슈와 ID로 비교합니다. ID: DLY-화면(계획 대비 지연), "
         "MIS-화면(상태-커밋 불일치), UNL-커밋(미연결 커밋). 지속 주수는 처음 발생한 주부터 셉니다.", "",
         "| 구분 | 지연 | 불일치 | 미연결 | 합계 |", "|---|---:|---:|---:|---:|",
         row("✅ 해결", iss["resolved"]), row("🔁 미해결(지속)", iss["persistent"]), row("🆕 신규", iss["new"]), "",
         "| 이슈 | 내용 | 상태 | 지속 |", "|---|---|---|---:|"]
    L += [f"| {i['id']} | {i['title']} | ✅ 해결 | {i['weeks']}주 |" for i in iss["resolved"]]
    L += [f"| {i['id']} | {i['title']} | 🆕 신규 | 1주 |" for i in iss["new"]]
    L += [f"| {i['id']} | {i['title']} | 🔁 미해결 | {i['weeks']}주 |" for i in iss["persistent"]]
    return "\n".join(L)


def _prev_actions_markdown(ctx: dict) -> str:
    acts = ctx["prev_actions"]
    if not acts:
        return "지난주 액션아이템 기록이 없습니다."
    mark = {True: "✅ 완료", False: "⬜ 미완료", None: "❔ 확인 필요"}
    done = sum(1 for _, ok, _ in acts if ok)
    return "\n".join([f"지난주 액션아이템 {len(acts)}건 중 완료 {done}건 · 미완료 {len(acts) - done}건 "
                      "(커밋의 단계 완료 표시·화면 상태·미연결 커밋으로 판단)", "",
                      "| 지난주 액션아이템 | 이행 | 확인 근거 |", "|---|---|---|",
                      *[f"| {strip_names(t)} | {mark[ok]} | {why} |" for t, ok, why in acts]])


def default_draft(ctx: dict) -> tuple[list[str], list[str], list[str]]:
    """LLM 없는 결정적 결정사항·블로커·액션아이템 (사람 이름 없이 화면·메뉴 단위)."""
    report, iss = ctx["report"], ctx["issues"]
    p, warns, unlinked, delayed = report["progress"], report["mismatch_warnings"], report["unlinked_commits"], report["delayed"]
    behind = [f"{m['code']} {m['name']}" for m in report["large_menus"] if m["schedule_status"] == "지연"]
    new_kinds = {k: sum(1 for i in iss["new"] if i["kind"] == k) for k in ("지연", "불일치", "미연결")}
    decisions = ["진척 수치는 도구 계산값(화면별 4단계 완료 비율, 계획 MM 가중)을 사용한다.",
                 f"전체 실적이 계획 대비 {p['gap_pp']:+.2f}%p({p['schedule_status']})이므로, 차이가 "
                 f"{tools.STATUS_WARN_PP:+.0f}%p보다 큰 대메뉴({', '.join(behind) or '없음'})는 담당 PL이 다음 회의까지 만회 계획을 공유한다.",
                 f"화면 상태와 커밋이 불일치하면 상태를 우선하고 경고로 표시한다 (이번 주 {len(warns)}건, 신규 {new_kinds['불일치']}건).",
                 "지난주 액션아이템 중 미완료 항목은 이번 주 액션아이템으로 이어서 관리한다."]
    blockers = [f"{i['title']} (지속 {i['weeks']}주)" for i in iss["persistent"] if i["weeks"] >= LONG_ISSUE_WEEKS]
    if unlinked:
        blockers.append(f"미연결 커밋 {len(unlinked)}건(신규 {new_kinds['미연결']}건)이 화면 진척에 반영되지 않고 있다.")
    actions = []
    for s in delayed[:tools.TOP_N]:
        rest = ",".join(f"{s['screen_id']}-{n}" for n in s["phases_remaining"])
        todo = f"{rest} 완료" if rest else f"{s['screen_id']} 상태를 '완료'로 갱신"
        actions.append(f"{s['screen_id']} {s['title']}: {todo} (계획 종료 {s['planned_end']})")
    if unlinked:
        actions.append(f"미연결 커밋 {', '.join(c['hash'] for c in unlinked)}: 올바른 화면 ID로 커밋 메시지 정정")
    return decisions, blockers, actions


def build_meeting_material(previous: dict | None = None, load_history: bool = True) -> str:
    """화면 170개를 모두 싣지 않는다: 집계 표와 계획 대비 지연 화면 상위 N개, 이슈 추적만.
    회의 자료는 참석자 전원에게 공유되므로 진척은 업무 분류 단위로만 싣는다(개인별 진척률 없음).
    결정사항·블로커·액션아이템에도 사람 이름을 싣지 않는다(화면·메뉴 단위로만 표기)."""
    ctx = meeting_context(previous, load_history)
    return render(*default_draft(ctx), ctx)


def strip_names(text: str) -> str:
    """회의 자료 문장에서 팀원 이름을 지운다 (LLM 초안이 이름을 쓰더라도 공유 문서에는 남기지 않음)."""
    for d in tools.load_developers():
        text = re.sub(re.escape(d["name"]) + r"(님|씨)?", "", text)
    text = re.sub(r"\(\s*(담당자?\s*:?)?\s*[,/·]?\s*\)", "", text)  # 이름만 있던 괄호 제거
    return re.sub(r"^\s*[:,/·-]\s*", "", re.sub(r"\s{2,}", " ", text)).strip()


def render(decisions, blockers, actions, ctx: dict) -> str:
    decisions, blockers, actions = ([strip_names(x) for x in xs] for xs in (decisions, blockers, actions))
    report = ctx["report"]
    lines = [NOTICE, "",
             "## 진척 현황", tools.format_progress_markdown(report, ctx["previous"]), "",  # 숫자는 도구 결과 그대로
             "## 이슈 추적", _issue_markdown(ctx), "",
             "## 지난주 액션아이템 이행", _prev_actions_markdown(ctx), "",
             "## 계획 대비 지연·불일치·미연결", tools.format_findings_markdown(report), "",
             "## 결정사항", *[f"- {x}" for x in decisions or ["없음"]], "",
             "## 블로커", *[f"- {x}" for x in blockers or ["없음"]], "",
             "## 액션아이템", *[f"- [ ] {x}" for x in actions or ["없음"]]]
    return "\n".join(lines)


def context_summary(ctx: dict) -> str:
    """LLM 초안 입력용 요약: 계획 대비 차이·지난주 대비 증감·이슈 상태·지난주 액션아이템 이행 (개인별 수치 없음)."""
    r, prev, iss = ctx["report"], ctx["previous"] or {}, ctx["issues"]
    p, pct = r["progress"], tools._pct
    L = [f"일정 경과율 {pct(r['schedule']['elapsed'])}, 계획 진척률 {pct(p['planned_progress'])}, 실적 진척률 "
         f"{pct(p['progress'])}, 차이 {p['gap_pp']:+.2f}%p ({p['schedule_status']})"]
    L += [f"{tools.part_label(m['code'])}: 계획 {pct(m['planned_progress'])}, 실적 {pct(m['progress'])}, "
          f"차이 {m['gap_pp']:+.2f}%p ({m['schedule_status']})" for m in r["parts"]]
    if prev:
        L.append(f"지난주 대비 전체 실적 {tools.delta_pp(p['progress'], prev['team']['progress'])}, 완료 화면 "
                 f"{prev['team']['screens_done']} → {p['screens_done']}, 지연 화면 {prev['team']['delayed_count']} → "
                 f"{p['delayed_count']}")
    L += [f"대메뉴 {m['code']} {m['name']}: 계획 대비 {m['gap_pp']:+.2f}%p ({m['schedule_status']}), 지난주 대비 "
          f"{tools.delta_pp(m['progress'], prev.get('large_menus', {}).get(m['code'], {}).get('progress'))}"
          for m in r["large_menus"]]
    L.append(f"이슈: 해결 {len(iss['resolved'])}건({', '.join(i['id'] for i in iss['resolved'])}), "
             f"미해결 {len(iss['persistent'])}건(장기: {', '.join(f'{i['id']} {i['weeks']}주' for i in iss['persistent'][:5])}), "
             f"신규 {len(iss['new'])}건({', '.join(i['id'] for i in iss['new'])})")
    L += [f"지난주 액션아이템 '{t}': {'완료' if ok else '미완료' if ok is False else '확인 필요'} ({why})"
          for t, ok, why in ctx["prev_actions"]]
    return "\n".join(L)


def render_snapshot_page(snap: dict) -> str:
    """지난 회의 스냅샷으로 그 주 회의 자료 페이지를 결정적으로 재구성한다 (LLM 없음, 사람 이름 없음)."""
    names = {}
    for s in tools.load_screens():
        names[s["large_code"]], names[s["medium_code"]] = s["large_name"], s["medium_name"]
    t, pct = snap["team"], tools._pct
    gap = lambda x: round((x["progress"] - x["planned_progress"]) * 100, 2)  # noqa: E731
    L = ["> 지난주 회의 스냅샷(장기 메모리)으로 재구성한 회의 자료입니다. 업무 단위 진척만 싣습니다(개인별 진척률·이름 없음).", "",
         "## 진척 현황", f"{tools.load_project()['name']} · 기준 주: {snap['week']['start']} ~ {snap['week']['end']}", "",
         "### 일정 대비 진척", "", "| 구분 | 계획 진척률 | 실적 진척률 | 차이 | 상태 | 화면(완료/전체) | 남은 공수 | 지연 화면 |",
         "|---|---:|---:|---:|---|---:|---:|---:|",
         f"| **전체** | {pct(t['planned_progress'])} | **{pct(t['progress'])}** | {gap(t):+.2f}%p | "
         f"{tools.SCHEDULE_ICON[tools.schedule_status(gap(t))]} | {t['screens_done']}/{t['screens_total']} | "
         f"{t['remaining_mm']:.2f}MM | {t['delayed_count']}개 |",
         *[f"| {tools.part_label(k)} | {pct(v['planned_progress'])} | {pct(v['progress'])} | {gap(v):+.2f}%p | "
           f"{tools.SCHEDULE_ICON[tools.schedule_status(gap(v))]} | - | - | - |" for k, v in snap["parts"].items()], "",
         f"일정 경과율 **{pct(snap['schedule_elapsed'])}** (프로젝트 기간 중 {snap['week']['end']}까지)", "",
         "#### 전체 기간 대비 현재 위치", "", "```",
         *tools.timeline_lines(snap["schedule_elapsed"], t["planned_progress"], t["progress"], snap["week"]["end"]),
         "```", "",
         "### 대메뉴별", "", "| 대메뉴 | 계획 | 실적 | 차이 | 화면(완료/전체) |", "|---|---:|---:|---:|---:|",
         *[f"| {k} {names[k]} | {pct(v['planned_progress'])} | {pct(v['progress'])} | {gap(v):+.2f}%p | "
           f"{v['screens_done']}/{v['screens_total']} |" for k, v in snap["large_menus"].items()], "",
         "### 중메뉴별", "", "| 중메뉴 | 실적 | 화면(완료/전체) |", "|---|---:|---:|",
         *[f"| {k} {names[k]} | {pct(v['progress'])} | {v['screens_done']}/{v['screens_total']} |"
           for k, v in snap["medium_menus"].items()], "",
         "### 단계별 완료 현황", "", "| 단계 | 완료 화면 |", "|---|---:|",
         *[f"| {k} {tools.PHASES[int(k)]} | {v}/{t['screens_total']} |" for k, v in snap["phases"].items()], "",
         "## 이슈 목록", "", "| 이슈 | 내용 | 발생 주 |", "|---|---|---|",
         *[f"| {i['id']} | {i['title']} | {i['since']} |" for i in snap["issues"]], "",
         "## 액션아이템", *[f"- [ ] {strip_names(x)}" for x in snap["action_items"] or ["없음"]]]
    return "\n".join(L)


# ----------------------------------------------------------------------------- Plan-Execute 그래프
STEPS = ("collect_progress", "check_previous_meeting", "draft_material")


class Plan(BaseModel):
    """회의 자료 작성 계획."""
    steps: list[str] = Field(description=f"실행할 단계 이름 목록(순서대로). 사용 가능: {', '.join(STEPS)}")


REQUIRED_STEPS = ("collect_progress", "draft_material")  # 이 단계 없이는 회의 자료를 만들 수 없다 (건너뛰기 불가)
MAX_STEP_RETRIES = 1


class Replan(BaseModel):
    """단계 실패 뒤 계획 수정 결정."""
    action: Literal["retry", "skip", "abort"] = Field(
        description="retry=같은 단계를 한 번 더 시도, skip=이 단계 없이 다음 단계로, abort=회의 자료 작성을 중단")
    reason: str = Field(description="한 문장 이유")


class MeetingDraft(BaseModel):
    """회의 자료 본문 (숫자는 쓰지 않는다. 진척 수치는 도구 결과를 그대로 붙인다)."""
    decisions: list[str] = Field(description="이번 회의 결정사항 후보")
    blockers: list[str] = Field(description="블로커 (장기 미해결 이슈·경고·미연결 커밋·지난주 미완료 포함)")
    action_items: list[str] = Field(description="화면·메뉴 단위의 다음 액션아이템 (사람 이름 없이)")


class MeetingState(TypedDict, total=False):
    plan: list[str]
    report_text: str
    warnings: list[str]
    previous: Optional[dict]
    previous_source: str
    previous_minutes: str
    previous_page: str
    body: str
    title: str          # PM·PL이 고쳐서 승인한 제목 (없으면 기본 제목)
    approval: str
    page_id: str
    error: str
    log: list[dict]


def _previous_minutes(week_start: str) -> tuple[str, str]:
    """지난주 회의록(MIN-YYYY-Www)의 결정사항·블로커 섹션 (문서 검색 대상 코퍼스)."""
    from src import retriever
    prev = date.fromordinal(date.fromisoformat(week_start).toordinal() - 7).isocalendar()
    doc_id = f"MIN-{prev.year}-W{prev.week:02d}"
    for d in retriever.corpus_documents():
        if d["doc_id"] == doc_id:
            return doc_id, d["text"].split("## 액션아이템", 1)[0].strip()
    return doc_id, ""


def page_actions(body: str) -> str:
    """게시된 회의 자료 페이지 본문에서 액션아이템 섹션만 꺼낸다 (없으면 빈 문자열)."""
    return body.split("## 액션아이템", 1)[1].strip() if "## 액션아이템" in body else ""


class MeetingFlow:
    """Plan-Execute 그래프. Plan 단계는 LLM이 계획을 내고, 코드가 허용된 고정 단계(STEPS)만 남겨 누락 보완·순서 정렬한다.
    read_page가 있으면 지난주 게시 페이지를 MCP get_page로 읽고, 없거나 실패하면 장기 메모리 스냅샷으로 대신한다."""

    def __init__(self, llm, publish, read_page=None):
        from langgraph.graph import END, START, StateGraph

        from src import state

        self.llm, self.publish_fn, self.read_page = llm, publish, read_page
        self.store = state.store()  # 장기 메모리: SNAPSHOT_NS / 주차 시작일 → 회의 스냅샷 (STATE_DB면 영구 저장)
        g = StateGraph(MeetingState)
        g.add_node("plan", self._plan)
        g.add_node("execute", self._execute)
        g.add_node("approve", self._approve)
        g.add_node("publish", self._publish)
        g.add_edge(START, "plan")
        g.add_edge("plan", "execute")
        g.add_conditional_edges("execute", lambda s: END if s.get("error") else "approve")  # 중단되면 승인 없이 끝
        g.add_conditional_edges("approve", lambda s: "publish" if s.get("approval") == "approve" else END)
        g.add_edge("publish", END)
        self.graph = g.compile(checkpointer=state.checkpointer(), store=self.store)  # 승인 대기 상태 (STATE_DB면 영구)

    # --- Plan
    def _plan(self, state: MeetingState) -> MeetingState:
        plan = self.llm.with_structured_output(Plan).invoke(
            "주간 진척 회의 자료를 만들기 위한 실행 계획을 세워라. 진척 수치를 먼저 모으고, 지난주 회의(진척 스냅샷·이슈·"
            f"액션아이템)를 확인한 뒤 초안을 작성해야 한다. 사용 가능한 단계: {', '.join(STEPS)}")
        steps = [s for s in plan.steps if s in STEPS]
        steps += [s for s in STEPS if s not in steps]  # 필수 단계 누락은 코드가 보완
        steps.sort(key=STEPS.index)                    # 데이터 의존 순서 보장
        return {"plan": steps, "log": [{"step": "plan", "input": plan.steps, "output": steps}]}

    # --- Execute: 계획 순서대로 단계 실행. 단계가 실패하면 Replan으로 다시 시도·건너뛰기·중단을 정한다
    def _execute(self, state: MeetingState) -> MeetingState:
        out: MeetingState = {"log": list(state.get("log", []))}
        for step in state["plan"]:
            tries = 0
            while True:
                try:
                    out.update(getattr(self, "_" + step)({**state, **out}))
                    break
                except Exception as e:  # noqa: BLE001
                    action, reason = self._replan(step, e, tries)
                    out["log"] = out["log"] + [{"step": "replan", "input": {"failed_step": step, "error": type(e).__name__},
                                                "output": {"action": action, "reason": reason}}]
                    if action == "retry":
                        tries += 1
                        continue
                    if action == "abort":
                        return {**out, "error": f"{step} 단계 실패로 회의 자료 작성을 중단했습니다: {e}"}
                    break  # skip
        return out

    def _replan(self, step: str, error: Exception, tries: int) -> tuple[str, str]:
        """실패한 단계의 처리 방법. LLM이 판단하되 코드가 제약을 건다: 재시도는 1회까지, 필수 단계는 건너뛸 수 없다."""
        try:
            d = self.llm.with_structured_output(Replan).invoke(
                f"주간 회의 자료 작성 중 '{step}' 단계가 실패했다(오류: {type(error).__name__}: {str(error)[:200]}). "
                f"재시도 횟수 {tries}/{MAX_STEP_RETRIES}. 필수 단계: {', '.join(REQUIRED_STEPS)}. "
                "일시적 오류(타임아웃·연결)면 retry, 선택 단계면 skip, 계속할 수 없으면 abort를 골라라.")
            action, reason = d.action, d.reason
        except Exception:  # noqa: BLE001  판단 실패 시 규칙으로
            action, reason = ("retry" if tries < MAX_STEP_RETRIES else "abort"), "규칙 기반 판단(LLM 판단 실패)"
        if action == "retry" and tries >= MAX_STEP_RETRIES:
            action, reason = ("abort" if step in REQUIRED_STEPS else "skip"), f"재시도 한도 초과 ({reason})"
        if action == "skip" and step in REQUIRED_STEPS:
            action, reason = ("retry" if tries < MAX_STEP_RETRIES else "abort"), f"필수 단계는 건너뛸 수 없음 ({reason})"
        return action, reason

    def _collect_progress(self, state):
        report = tools.build_progress_report()
        team_only = {k: v for k, v in report.items() if k != "developers"}  # 개인별 진척률은 초안 입력에서 제외
        warns = [f"[{w['screen_id']}] {w['message']}" for w in report["mismatch_warnings"]]
        warns += [f"미연결 커밋 {c['hash']}: {c['reason']}" for c in report["unlinked_commits"]]
        # 지연 화면 담당자·커밋 작성자 이름도 초안 입력에서 뺀다 (공유 문서라 사람 이름을 쓰지 않음)
        log = state["log"] + [{"step": "tool_call", "input": {"tool": n, "plan_step": "collect_progress"}, "output": "ok"}
                              for n in ("calc_progress", "calc_remaining_mm", "check_mismatch")]
        return {"report_text": strip_names(tools.format_progress_report(team_only, authors=False)), "warnings": warns, "log": log}

    def snapshots(self) -> list:
        return sorted(self.store.search(SNAPSHOT_NS, limit=1000), key=lambda it: it.key)

    def _check_previous_meeting(self, state):
        """장기 메모리에서 지난주 회의 스냅샷(진척·이슈·액션아이템)을 읽고, 지난주 게시 페이지(MCP get_page)와
        지난주 회의록을 확인한다. 게시 페이지를 읽지 못하면 스냅샷으로 재구성한 페이지를 쓴다."""
        week = tools.load_reference_week()["start"]
        seeded = ""
        if not [it for it in self.snapshots() if it.key < week]:  # Store가 비면 지난 회의 기록으로 초기화
            for k, v in tools.load_meeting_history().items():
                self.store.put(SNAPSHOT_NS, k, v)
            seeded = " (meeting_history.json으로 초기화)"
        prev = [it for it in self.snapshots() if it.key < week]
        previous, src = (prev[-1].value, f"store:{prev[-1].key}{seeded}") if prev else (None, "store:없음")
        doc_id, minutes = _previous_minutes(week)
        page, page_src = "", "없음"
        if previous is not None:
            pid = previous.get("page_id") or meeting_page_id(previous["week"]["start"])
            try:
                if self.read_page is None:
                    raise RuntimeError("MCP 미연결")
                page, page_src = self.read_page(pid)["body"], f"mcp:{pid}"
            except Exception as e:  # noqa: BLE001  게시 페이지가 없거나 MCP 실패: 스냅샷으로 대신
                page, page_src = render_snapshot_page(previous), f"snapshot:{previous['week']['start']} ({type(e).__name__})"
        log = state["log"] + [
            {"step": "tool_call", "input": {"tool": "memory_store", "plan_step": "check_previous_meeting"}, "output": src},
            {"step": "tool_call", "input": {"tool": "get_page", "plan_step": "check_previous_meeting"},
             "output": page_src},
            {"step": "tool_call", "input": {"tool": "retrieve_docs", "plan_step": "check_previous_meeting"},
             "output": doc_id if minutes else "없음"}]
        return {"previous": previous, "previous_source": src, "previous_minutes": minutes,
                "previous_page": strip_names(page_actions(page)), "log": log}

    def _draft_material(self, state):
        ctx = meeting_context(state.get("previous"), load_history=False)
        prompt = (
            "아래 자료로 주간 진척 회의 자료의 결정사항·블로커·액션아이템을 한국어로 작성하라. "
            "진척률·MM 같은 숫자는 쓰지 마라(도구 결과가 따로 붙는다). 사람을 평가하거나 순위를 매기지 마라.\n"
            f"각 섹션은 {DRAFT_MAX_ITEMS}개 이내, 항목은 한 문장으로 짧게 쓴다. 지연 화면·미연결 커밋은 하나씩 나열하지 말고 묶어서 쓴다"
            "(전체 목록은 표로 따로 붙는다).\n"
            "이 자료는 회의 참석자 전원(PM·PL·개발자)에게 공유된다. 개인별 진척률이나 개인 간 진척 비교는 언급하지 말고, "
            "진척은 대메뉴·중메뉴·단계·화면 같은 업무 단위로만 말하라. 결정사항·블로커·액션아이템 어디에도 사람 이름을 쓰지 마라.\n"
            "계획 대비 차이가 큰 대메뉴, 오래 지속된 미해결 이슈, 지난주 미완료 액션아이템을 결정사항·블로커·액션아이템에 반영하라.\n\n"
            f"[도구 계산 결과]\n{state['report_text']}\n\n[일정 대비·지난주 대비·이슈 추적]\n{context_summary(ctx)}\n\n"
            "[불일치 경고·미연결 커밋]\n" + "\n".join(state["warnings"])
            + "\n\n[지난주 회의록 결정사항·블로커]\n" + state.get("previous_minutes", "")
            + "\n\n[지난주 게시 회의 자료의 액션아이템]\n" + state.get("previous_page", ""))
        try:
            d = self.llm.with_structured_output(MeetingDraft).invoke(prompt)
            parts, out = (d.decisions, d.blockers, d.action_items), "drafted"
        except Exception as e:  # noqa: BLE001  출력 한도 초과로 잘린 구조화 출력 등: 요청 전체를 실패시키지 않고 결정적 초안으로 대체
            parts, out = default_draft(ctx), f"fallback:default_draft({type(e).__name__})"
        body = render(*parts, ctx)
        log = state["log"] + [{"step": "llm_draft", "input": "meeting_material", "output": out}]
        return {"body": body, "log": log}

    # --- HITL
    def _approve(self, state: MeetingState) -> MeetingState:
        from langgraph.types import interrupt
        decision = interrupt({"title": meeting_title(), "body": state["body"]})
        if isinstance(decision, dict):  # 고쳐서 승인: {"action": "edit", "title"?, "body"?}
            out: MeetingState = {"approval": "approve"}
            changed = [k for k in ("title", "body") if decision.get(k)]
            if decision.get("title"):
                out["title"] = decision["title"].strip()
            if decision.get("body"):
                out["body"] = strip_names(decision["body"].strip())  # 공용 자료 원칙: 구성원 이름 제거
            return {**out, "log": state["log"] + [
                {"step": "hitl_decision", "input": "meeting_material", "output": "approve"},
                {"step": "hitl_edit", "input": "meeting_material", "output": ",".join(changed)}]}
        return {"approval": decision, "log": state["log"] + [{"step": "hitl_decision", "input": "meeting_material",
                                                             "output": decision}]}

    def _publish(self, state: MeetingState) -> MeetingState:
        title = state.get("title") or meeting_title()
        try:
            page_id = self.publish_fn(title, state["body"])
        except Exception as e:  # noqa: BLE001  게시 실패: 게시하지 않음
            return {"error": str(e), "log": state["log"] + [{"step": "confluence_publish", "input": meeting_title(),
                                                            "output": f"error: {e}"}]}
        week = tools.load_reference_week()["start"]
        actions = state["body"].split("## 액션아이템", 1)[1] if "## 액션아이템" in state["body"] else ""  # 수정본에 없을 수 있음
        items = [ln.removeprefix("- [ ] ") for ln in actions.splitlines() if ln.startswith("- [ ] ")]
        snap = week_snapshot(meeting_context(state.get("previous"), load_history=False), items)
        self.store.put(SNAPSHOT_NS, week, snap)  # 이번 주 스냅샷 저장 → 다음 주 회의의 '지난주'
        return {"page_id": page_id, "log": state["log"] + [
            {"step": "confluence_publish", "input": title, "output": page_id},
            {"step": "memory_store", "input": f"snapshot:{week}", "output": "saved"}]}

    # --- 외부 진입점
    def start(self) -> tuple[str, dict]:
        tid = uuid.uuid4().hex
        cfg = {"configurable": {"thread_id": tid}}
        self.graph.invoke({}, cfg)
        return tid, self.graph.get_state(cfg).values

    def resume(self, tid: str, approval: str | dict) -> dict:
        """approval: 'approve' | 'reject' | {'action': 'edit', 'title'?, 'body'?} (고쳐서 승인)."""
        from langgraph.types import Command
        cfg = {"configurable": {"thread_id": tid}}
        self.graph.invoke(Command(resume=approval), cfg)
        return self.graph.get_state(cfg).values

    def reset_store(self) -> None:
        for it in self.snapshots():
            self.store.delete(SNAPSHOT_NS, it.key)
