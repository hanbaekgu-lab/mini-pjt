"""가짜 fixture 생성기 (결정적, 고정 시드). 사용: .venv\\Scripts\\python.exe data/fixtures/make_fixtures.py

플랫폼 전환 프로젝트(2026-03-02 ~ 2027-02-26)의 9개월차 기준 주(2026-11-16 ~ 11-22) 데이터를 만든다.
- project.json: 프로젝트 기간·기준 주·파트별 대메뉴
- developers.json: PM 1, PL 2, 개발자 7 (가짜 이름·example.com 이메일)
- screens.json: 대메뉴 8 → 중메뉴 34 → 소메뉴 화면 170개 (화면 ID = SCR-<대><중>-<소>)
- commits.json: 기준 주까지의 커밋 + 기준 주 이후 커밋 일부(제외 대상), 미연결·불일치 사례 포함
- meeting_history.json: 지난주(2026-11-09 ~ 11-15) 회의 스냅샷 (장기 메모리 초기값: 진척·이슈·액션아이템)
- expected_progress.json: 생성기가 의도한 단계 완료 상태로 따로 계산한 정답 (src/tools.py의 파서를 쓰지 않음)
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path

OUT = Path(__file__).resolve().parent
SEED = 20261116
PHASES = {1: "분석", 2: "개발", 3: "단위테스트", 4: "통합테스트"}

PROJECT = {
    "name": "플랫폼 전환 프로젝트",
    "start": "2026-03-02",
    "end": "2027-02-26",
    "reference_week": {"start": "2026-11-16", "end": "2026-11-22", "label": "2026-11-16(월) ~ 2026-11-22(일)"},
    "month_label": "12개월 중 9개월차",
    "milestones": [{"date": "2027-02-12", "name": "화면 170개 전환 완료"},
                   {"date": "2027-02-26", "name": "전환 리허설·오픈"}],
    "parts": {"PL1": {"lead": "pl01", "large_menus": ["A", "B", "C", "D"]},
              "PL2": {"lead": "pl02", "large_menus": ["E", "F", "G", "H"]}},
}

DEVELOPERS = [
    {"developer_id": "pm01", "name": "오루니", "email": "rooney.oh@example.com", "role": "pm", "part": None, "lead": None},
    {"developer_id": "pl01", "name": "강앙리", "email": "henry.kang@example.com", "role": "pl", "part": "PL1", "lead": "pm01"},
    {"developer_id": "pl02", "name": "서살라", "email": "salah.seo@example.com", "role": "pl", "part": "PL2", "lead": "pm01"},
    {"developer_id": "dev01", "name": "김메시", "email": "messi.kim@example.com", "role": "developer", "part": "PL1", "lead": "pl01"},
    {"developer_id": "dev02", "name": "박지단", "email": "zidane.park@example.com", "role": "developer", "part": "PL1", "lead": "pl01"},
    {"developer_id": "dev03", "name": "이케인", "email": "kane.lee@example.com", "role": "developer", "part": "PL1", "lead": "pl01"},
    {"developer_id": "dev04", "name": "최펠레", "email": "pele.choi@example.com", "role": "developer", "part": "PL1", "lead": "pl01"},
    {"developer_id": "dev05", "name": "정카카", "email": "kaka.jung@example.com", "role": "developer", "part": "PL2", "lead": "pl02"},
    {"developer_id": "dev06", "name": "한피구", "email": "figo.han@example.com", "role": "developer", "part": "PL2", "lead": "pl02"},
    {"developer_id": "dev07", "name": "윤베일", "email": "bale.yoon@example.com", "role": "developer", "part": "PL2", "lead": "pl02"},
]

# 대메뉴 코드, 이름, 중메뉴 (이름, 소메뉴 화면 수)
MENUS = [
    ("A", "회원", [("회원가입", 6), ("로그인·인증", 5), ("회원정보 관리", 6), ("등급·포인트", 5), ("탈퇴·휴면", 4)]),
    ("B", "주문", [("장바구니", 5), ("주문서 작성", 6), ("주문 조회", 6), ("취소·반품", 5), ("배송 조회", 5)]),
    ("C", "결제", [("결제수단 관리", 6), ("결제 승인", 5), ("환불 처리", 5), ("결제 이력", 5)]),
    ("D", "상품", [("상품 등록", 6), ("카테고리 관리", 5), ("재고 관리", 5), ("가격·프로모션", 5), ("상품 리뷰", 4)]),
    ("E", "정산", [("판매 정산", 5), ("수수료 관리", 5), ("세금계산서", 4), ("정산 지급", 5)]),
    ("F", "고객지원", [("1:1 문의", 5), ("FAQ 관리", 4), ("공지사항", 4), ("상담 이력", 4)]),
    ("G", "통계", [("매출 통계", 5), ("회원 통계", 4), ("상품 통계", 4), ("대시보드", 6)]),
    ("H", "시스템관리", [("사용자·권한", 6), ("코드 관리", 5), ("시스템 로그", 5)]),
]
SCREEN_SUFFIX = ["메인", "목록 조회", "상세 조회", "등록", "수정", "이력 조회"]
PLAN_MM = [0.3, 0.4, 0.5, 0.6]
# 개발자별 실제 일정 지연(일). 음수는 계획보다 빠름. PL2 파트가 계획보다 더 늦다.
LAG_DAYS = {"dev01": -5, "dev02": 5, "dev03": 15, "dev04": 0, "dev05": 30, "dev06": 45, "dev07": 25}
DEV_START, DEV_END = date(2026, 4, 1), date(2027, 2, 12)  # 3월은 분석·설계, 2월 말은 전환 리허설
PREV_WEEK = {"start": "2026-11-09", "end": "2026-11-15"}   # 지난주 회의(스냅샷) 기준 주
# 일정 상태: 실적 - 계획 차이(%p)가 -3 이상이면 정상, -10 이상이면 주의, 그보다 낮으면 지연 (src/tools.py와 같은 기준)
STATUS_OK_PP, STATUS_WARN_PP = -3.0, -10.0


def workday(d: date) -> date:
    """주말이면 직전 금요일로 당긴다."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def build():
    rng = random.Random(SEED)
    ref_start = date.fromisoformat(PROJECT["reference_week"]["start"])
    ref_end = date.fromisoformat(PROJECT["reference_week"]["end"])
    part_of = {code: p for p, v in PROJECT["parts"].items() for code in v["large_menus"]}
    part_devs = {p: [d["developer_id"] for d in DEVELOPERS if d["role"] == "developer" and d["part"] == p]
                 for p in PROJECT["parts"]}

    # 1) 화면 목록 + 중메뉴 단위 담당자 배정(파트 안에서 화면 수가 가장 적은 개발자에게)
    screens, load = [], {d: 0 for ds in part_devs.values() for d in ds}
    for lcode, lname, mediums in MENUS:
        part = part_of[lcode]
        for mi, (mname, n) in enumerate(mediums, start=1):
            owner = min(part_devs[part], key=lambda d: (load[d], d))
            load[owner] += n
            mcode = f"{lcode}{mi:02d}"
            for si in range(1, n + 1):
                screens.append({
                    "screen_id": f"SCR-{mcode}-{si:02d}", "title": f"{mname} {SCREEN_SUFFIX[si - 1]}",
                    "large_code": lcode, "large_name": lname, "medium_code": mcode, "medium_name": mname,
                    "part": part, "owner": owner, "plan_mm": rng.choice(PLAN_MM),
                })

    # 2) 개발자별 순차 일정: 계획 종료일과 실제(지연 반영) 단계 완료일
    #    중메뉴를 차례로 시작하되 겹쳐서 진행한다(k번째 중메뉴는 화면 3개만큼 늦게 시작).
    plan = {}  # screen_id -> (계획 시작, 계획 종료)
    for dev in load:
        mine = [s for s in screens if s["owner"] == dev]
        order = list(dict.fromkeys(s["medium_code"] for s in mine))
        mine.sort(key=lambda s: (int(s["screen_id"][-2:]) + 3 * order.index(s["medium_code"]), s["screen_id"]))
        total = sum(Fraction(str(s["plan_mm"])) for s in mine)
        span = (DEV_END - DEV_START).days
        acc = Fraction(0)
        for s in mine:
            st = DEV_START + timedelta(days=int(acc / total * span))
            acc += Fraction(str(s["plan_mm"]))
            en = DEV_START + timedelta(days=int(acc / total * span))
            plan[s["screen_id"]] = (st, en)
            s["planned_start"] = workday(st).isoformat()
            s["planned_end"] = workday(en).isoformat()

    commits = []  # (date, email, message, info) info: 생성기가 아는 의미 (정답 계산용)
    email = {d["developer_id"]: d["email"] for d in DEVELOPERS}
    done = {}  # screen_id -> 기준 주 안에 유효한 완료 표시가 있는 단계 집합
    done_on = {}  # screen_id -> {단계: 완료 표시 일자} (지난주 스냅샷 계산용)

    def add(d, dev, msg, sid=None, phase=None, is_done=False, valid=True):
        commits.append({"date": d, "email": email[dev], "message": msg,
                        "info": {"screen_id": sid, "phase": phase, "done": is_done, "valid": valid}})
        if valid and is_done and sid and 1 <= phase <= 4 and d <= ref_end:
            done.setdefault(sid, set()).add(phase)
            if phase <= 4:
                done_on.setdefault(sid, {})[phase] = d

    for s in screens:
        sid, dev = s["screen_id"], s["owner"]
        st, en = (x + timedelta(days=LAG_DAYS[dev]) for x in plan[sid])
        dur = max((en - st).days, 4)
        phase_end = {p: workday(st + timedelta(days=dur * p // 4)) for p in PHASES}
        if en <= ref_end:
            n_done, s["status"] = 4, "완료"
            # 계획상 끝났어야 할 최근 화면 일부는 통합테스트(또는 단위테스트) 대기로 멈춰 있다
            if plan[sid][1] >= date(2026, 8, 1) and rng.random() < 0.12:
                n_done, s["status"] = rng.choice([2, 3]), "진행중"
        elif st <= ref_end:
            n_done = min(3, sum(1 for p in PHASES if phase_end[p] <= ref_end))
            s["status"] = "진행중"
        else:
            n_done, s["status"] = 0, "미착수"
        for p in range(1, n_done + 1):
            add(phase_end[p], dev, f"[{sid}-{p}][done] {s['title']} {PHASES[p]} 완료", sid, p, True)
        if s["status"] == "진행중":
            nxt = PHASES[n_done + 1]
            add(workday(min(max(ref_start, phase_end.get(n_done, st)) + timedelta(days=1), ref_end)), dev,
                f"[{sid}] {s['title']} {nxt} 진행", sid)
        elif s["status"] == "완료" and rng.random() < 0.15:
            add(phase_end[2] - timedelta(days=1), dev, f"[{sid}] {s['title']} 리뷰 반영", sid)

    by_status = lambda stt: sorted(s["screen_id"] for s in screens if s["status"] == stt)  # noqa: E731
    sc = {s["screen_id"]: s for s in screens}
    mismatch = []

    # 3) 의도된 불일치 사례
    # (a) 상태는 '완료'인데 통합테스트 완료 표시가 없음 → 상태 우선 100% + 경고
    for sid in rng.sample(by_status("완료"), 2):
        for c in commits:
            if c["info"]["screen_id"] == sid and c["info"]["phase"] == 4 and c["info"]["done"]:
                c["message"] = f"[{sid}] {sc[sid]['title']} 통합테스트 진행"
                c["info"].update(phase=None, done=False)
        done[sid].discard(4)
        done_on[sid].pop(4, None)
        mismatch.append([sid, "STATUS_DONE_SUBTASKS_INCOMPLETE"])
    # (b) 4단계가 모두 완료됐는데 상태를 '진행중'으로 두고 갱신하지 않음 → 커밋 기준 100% + 경고
    recent_done = [x for x in by_status("완료") if x not in {m[0] for m in mismatch}
                   and sc[x]["planned_end"] >= "2026-10-01"]
    for sid in rng.sample(recent_done, 2):
        sc[sid]["status"] = "진행중"
        mismatch.append([sid, "SUBTASKS_DONE_STATUS_NOT_DONE"])
    # (c) 상태는 '미착수'인데 이번 주 커밋이 있음 → 상태 우선 0% + 경고
    sid = rng.choice(by_status("미착수"))
    add(date(2026, 11, 19), sc[sid]["owner"], f"[{sid}] {sc[sid]['title']} 분석 선행 착수", sid)
    mismatch.append([sid, "NOT_STARTED_BUT_HAS_COMMITS"])
    # (d) 없는 단계(5)에 완료 표시 → 무시 + 경고
    in_prog = [x for x in by_status("진행중") if x not in {m[0] for m in mismatch}]
    sid = rng.choice(in_prog)
    add(date(2026, 11, 18), sc[sid]["owner"], f"[{sid}-5][done] {sc[sid]['title']} 배포 완료", sid, 5, True)
    mismatch.append([sid, "DONE_MARK_UNKNOWN_SUBTASK"])

    # 4) 기준 주 이후 커밋(진척에 반영되지 않음)
    after = [x for x in in_prog if x != sid and len(done.get(x, ())) < 4][:2]
    for i, a in enumerate(after):
        p = len(done.get(a, ())) + 1
        add(date(2026, 11, 24 + i), sc[a]["owner"], f"[{a}-{p}][done] {sc[a]['title']} {PHASES[p]} 완료",
            a, p, True)

    # 5) 미연결 커밋: 화면 ID 없음·형식 오류·없는 화면 ID
    unlinked = [
        (date(2026, 11, 17), "dev02", "로그인 오류 긴급 수정", "no_or_malformed_screen_id"),
        (date(2026, 11, 20), "dev06", "빌드 스크립트 정리", "no_or_malformed_screen_id"),
        (date(2026, 11, 18), "dev03", "[SCR-B2-03] 주문서 버튼 정렬 수정", "no_or_malformed_screen_id"),
        (date(2026, 11, 19), "dev05", "[SCR-E01-1][done] 판매 정산 분석 완료", "no_or_malformed_screen_id"),
        (date(2026, 11, 6), "dev07", "[SCRG02-01] 회원 통계 차트 색상 변경", "no_or_malformed_screen_id"),
        (date(2026, 11, 16), "dev01", "[SCR-A09-01] 신규 화면 시안", "unknown_screen_id"),
        (date(2026, 10, 28), "dev05", "[SCR-F02-07] FAQ 화면 추가 시안", "unknown_screen_id"),
    ]
    for d, dev, msg, reason in unlinked:
        add(d, dev, msg, valid=False)
        commits[-1]["info"]["reason"] = reason

    # 6) 해시 부여 (날짜·작성자·메시지 순으로 정렬)
    commits.sort(key=lambda c: (c["date"], c["email"], c["message"]))
    for i, c in enumerate(commits, start=1):
        c["hash"] = f"c{i:04d}"
    return screens, commits, done, mismatch, done_on


def r4(x: Fraction) -> float:
    return round(float(x), 4)


def r2(x: Fraction) -> float:
    return round(float(x), 2)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def plan_ratio(s: dict, as_of: date) -> Fraction:
    """계획 진척률: 계획 시작일~종료일 사이를 선형으로 보고 기준일 시점의 계획 완료 비율(0~1)."""
    st, en = date.fromisoformat(s["planned_start"]), date.fromisoformat(s["planned_end"])
    if as_of >= en:
        return Fraction(1)
    if as_of <= st:
        return Fraction(0)
    return Fraction((as_of - st).days, (en - st).days)


def elapsed(as_of: date) -> Fraction:
    """일정 경과율: 프로젝트 시작일부터 기준일까지 지난 일수 / 전체 일수 (양 끝 포함)."""
    st, en = date.fromisoformat(PROJECT["start"]), date.fromisoformat(PROJECT["end"])
    return Fraction((as_of - st).days + 1, (en - st).days + 1)


def schedule_status(gap_pp: float) -> str:
    return "정상" if gap_pp >= STATUS_OK_PP else "주의" if gap_pp >= STATUS_WARN_PP else "지연"


def summarize(screens, status, done, as_of: date) -> dict:
    """한 주의 상태(화면 상태·완료 단계)로 실적·계획 진척을 묶음별로 계산한다."""
    mm = {s["screen_id"]: Fraction(str(s["plan_mm"])) for s in screens}
    ratio = {sid: Fraction(1) if status[sid] == "완료" else Fraction(0) if status[sid] == "미착수"
             else Fraction(len(done.get(sid, ())), 4) for sid in mm}
    planned = {s["screen_id"]: plan_ratio(s, as_of) for s in screens}

    def agg(ids):
        plan = sum((mm[i] for i in ids), Fraction(0))
        dn = sum((mm[i] * ratio[i] for i in ids), Fraction(0))
        actual, pl = r4(dn / plan), r4(sum((mm[i] * planned[i] for i in ids), Fraction(0)) / plan)
        gap = round((actual - pl) * 100, 2)
        return {"progress": actual, "planned_progress": pl, "gap_pp": gap, "schedule_status": schedule_status(gap),
                "remaining_mm": r2(plan - dn), "screens_done": sum(1 for i in ids if status[i] == "완료"),
                "screens_total": len(ids)}

    def group(key):
        out = {}
        for s in screens:
            out.setdefault(s[key], []).append(s["screen_id"])
        return {k: agg(v) for k, v in sorted(out.items())}

    eff = {sid: {1, 2, 3, 4} if status[sid] == "완료" else set() if status[sid] == "미착수" else set(done.get(sid, ()))
           for sid in mm}
    return {"team": agg(list(mm)), "parts": group("part"), "large_menus": group("large_code"),
            "medium_menus": group("medium_code"), "phases": {str(p): sum(1 for v in eff.values() if p in v) for p in PHASES},
            "eff": eff}


UNLINKED_KO = {"no_or_malformed_screen_id": "화면 ID 없음·형식 오류", "unknown_screen_id": "없는 화면 ID"}
MISMATCH_KO = {"STATUS_DONE_SUBTASKS_INCOMPLETE": "상태는 '완료'이나 완료 표시 없는 단계 있음",
               "SUBTASKS_DONE_STATUS_NOT_DONE": "4단계가 모두 완료됐으나 상태는 '진행중'"}


def previous_week(screens, commits, done_on):
    """지난주(PREV_WEEK) 종료일 시점의 상태를 커밋 일자로 되돌려 만든다. 진척은 이번 주보다 작거나 같다.
    - 지난주까지 완료 표시가 모두 있던 '완료' 화면은 지난주에도 '완료', 이번 주에 마친 화면은 '진행중'
    - 지난주까지 커밋이 없던 화면은 '미착수'
    - 지난주에 4단계를 마쳤지만 상태 갱신이 이번 주로 늦은 화면 1개: 지난주 불일치 이슈 → 이번 주 해결"""
    p_start, p_end = date.fromisoformat(PREV_WEEK["start"]), date.fromisoformat(PREV_WEEK["end"])
    touched = {}
    for c in commits:
        sid = c["info"]["screen_id"]
        if c["info"]["valid"] and sid and c["date"] <= p_end:
            touched[sid] = max(touched.get(sid, c["date"]), c["date"])
    late = sorted(s["screen_id"] for s in screens if s["status"] == "완료"
                  and p_start <= done_on.get(s["screen_id"], {}).get(4, date.min) <= p_end)[:1]
    status, done = {}, {}
    for s in screens:
        sid, on = s["screen_id"], done_on.get(s["screen_id"], {})
        done[sid] = {p for p, d in on.items() if d <= p_end}
        if s["status"] == "완료" and all(d <= p_end for d in on.values()) and sid not in late:
            status[sid] = "완료"
        elif s["status"] != "미착수" and sid in touched:
            status[sid] = "진행중"
        else:
            status[sid] = "미착수"
    summ = summarize(screens, status, done, p_end)
    issues = []
    for s in sorted(screens, key=lambda s: (s["planned_end"], s["screen_id"])):
        sid = s["screen_id"]
        if s["planned_end"] < PREV_WEEK["end"] and status[sid] != "완료":
            issues.append({"id": f"DLY-{sid}", "kind": "지연", "target": sid,
                           "title": f"{sid} {s['title']}: 계획 종료 {s['planned_end']} 경과, 미완료",
                           "since": monday(date.fromisoformat(s["planned_end"])).isoformat()})
    for sid in sorted(summ["eff"]):
        code = ("STATUS_DONE_SUBTASKS_INCOMPLETE" if status[sid] == "완료" and done[sid] != {1, 2, 3, 4}
                else "SUBTASKS_DONE_STATUS_NOT_DONE" if status[sid] == "진행중" and done[sid] == {1, 2, 3, 4} else None)
        if code:
            issues.append({"id": f"MIS-{sid}", "kind": "불일치", "target": sid, "title": f"{sid} {MISMATCH_KO[code]}",
                           "since": monday(touched[sid]).isoformat()})
    for c in commits:
        if not c["info"]["valid"] and c["date"] <= p_end:
            issues.append({"id": f"UNL-{c['hash']}", "kind": "미연결", "target": c["hash"],
                           "title": f"미연결 커밋 {c['hash']}: {UNLINKED_KO[c['info']['reason']]}",
                           "since": monday(c["date"]).isoformat()})
    # 지난주 액션아이템: 계획 대비 지연 화면 상위 10개 + 미연결 커밋 정정 (회의 자료 기본 초안과 같은 형식)
    sc = {s["screen_id"]: s for s in screens}
    delayed = [i for i in issues if i["kind"] == "지연"]
    actions = []
    for it in delayed[:10]:
        sid = it["target"]
        rest = ",".join(f"{sid}-{n}" for n in sorted({1, 2, 3, 4} - summ["eff"][sid]))
        todo = f"{rest} 완료" if rest else f"{sid} 상태를 '완료'로 갱신"
        actions.append(f"{sid} {sc[sid]['title']}: {todo} (계획 종료 {sc[sid]['planned_end']})")
    unl = [i["target"] for i in issues if i["kind"] == "미연결"]
    if unl:
        actions.append(f"미연결 커밋 {', '.join(unl)}: 올바른 화면 ID로 커밋 메시지 정정")
    snap = {"week": PREV_WEEK, "page_id": f"weekly_meeting_{PREV_WEEK['start']}", "schedule_elapsed": r4(elapsed(p_end)),
            "team": {**{k: summ["team"][k] for k in ("progress", "planned_progress", "remaining_mm", "screens_done",
                                                     "screens_total")}, "delayed_count": len(delayed)},
            "parts": {k: {f: v[f] for f in ("progress", "planned_progress")} for k, v in summ["parts"].items()},
            "large_menus": {k: {f: v[f] for f in ("progress", "planned_progress", "screens_done", "screens_total")}
                            for k, v in summ["large_menus"].items()},
            "medium_menus": {k: {f: v[f] for f in ("progress", "screens_done", "screens_total")}
                             for k, v in summ["medium_menus"].items()},
            "phases": summ["phases"], "issues": issues, "action_items": actions}
    return snap, status


def expected(screens, commits, done, mismatch, done_on) -> tuple[dict, dict]:
    """생성기가 의도한 단계 완료 집합으로 정답을 따로 계산한다 (상태 '완료'=100%, '미착수'=0%).
    두 번째 반환값은 지난주 회의 스냅샷 기록(meeting_history.json)."""
    ref_end = PROJECT["reference_week"]["end"]
    ratio, mm = {}, {}
    for s in screens:
        sid = s["screen_id"]
        mm[sid] = Fraction(str(s["plan_mm"]))
        if s["status"] == "완료":
            ratio[sid] = Fraction(1)
        elif s["status"] == "미착수":
            ratio[sid] = Fraction(0)
        else:
            ratio[sid] = Fraction(len(done.get(sid, ())), 4)

    def agg(ids):
        plan = sum((mm[i] for i in ids), Fraction(0))
        dn = sum((mm[i] * ratio[i] for i in ids), Fraction(0))
        return {"progress": r4(dn / plan) if plan else 0.0, "remaining_mm": r2(plan - dn),
                "screens_done": sum(1 for i in ids if next(s for s in screens if s["screen_id"] == i)["status"] == "완료"),
                "screens_total": len(ids)}

    def group(key):
        out = {}
        for s in screens:
            out.setdefault(s[key], []).append(s["screen_id"])
        return {k: agg(v) for k, v in sorted(out.items())}

    eff = {}
    for s in screens:
        sid = s["screen_id"]
        eff[sid] = {1, 2, 3, 4} if s["status"] == "완료" else set() if s["status"] == "미착수" else done.get(sid, set())
    delayed = sorted(s["screen_id"] for s in screens if s["planned_end"] < ref_end and s["status"] != "완료")
    unlinked = {c["hash"]: c["info"]["reason"] for c in commits
                if not c["info"]["valid"] and c["date"].isoformat() <= ref_end}

    # 일정 대비 진척 (기준 주 종료일 시점)
    as_of = date.fromisoformat(ref_end)
    status = {s["screen_id"]: s["status"] for s in screens}
    cur = summarize(screens, status, done, as_of)
    keep = ("progress", "planned_progress", "gap_pp", "schedule_status")
    schedule = {"as_of": ref_end, "elapsed": r4(elapsed(as_of)), "team": {k: cur["team"][k] for k in keep},
                **{g: {c: {k: v[k] for k in keep} for c, v in cur[g].items()}
                   for g in ("parts", "large_menus", "medium_menus")}}

    # 지난주 스냅샷과 이슈 추적 (이슈 ID: DLY-화면, MIS-화면, UNL-커밋)
    snap, prev_status = previous_week(screens, commits, done_on)
    cur_ids = {f"DLY-{x}" for x in delayed} | {f"MIS-{x}" for x, _ in mismatch} | {f"UNL-{h}" for h in unlinked}
    prev = {i["id"]: i for i in snap["issues"]}
    week0 = date.fromisoformat(PROJECT["reference_week"]["start"])
    tracking = {"resolved": sorted(set(prev) - cur_ids), "new": sorted(cur_ids - set(prev)),
                "persistent": {i: (week0 - date.fromisoformat(prev[i]["since"])).days // 7 + 1
                               for i in sorted(cur_ids & set(prev))}}
    action_done = {}
    for a in snap["action_items"]:
        sid = a.split(" ", 1)[0]
        if a.startswith("미연결"):
            action_done[a] = False  # 커밋 메시지는 정정되지 않았다
        elif "상태를 '완료'로 갱신" in a:
            action_done[a] = status[sid] == "완료"
        else:
            need = {int(x.rsplit("-", 1)[1]) for x in a.split(": ", 1)[1].split(" 완료")[0].split(",")}
            action_done[a] = need <= eff[sid]
    exp = {
        "_note": "생성기(make_fixtures.py)가 의도한 단계 완료 상태로 따로 계산한 정답. 진척률=완료 단계/4(상태 '완료'는 100%, "
                 "'미착수'는 0%), 묶음=계획 MM 가중. 진척률 소수 4자리, MM 소수 2자리. 계획 진척률=화면별 계획 시작~종료 "
                 "선형 비율의 계획 MM 가중, 차이(%p)=실적-계획, 상태: -3%p 이상 정상·-10%p 이상 주의·그 미만 지연.",
        "screens": {s["screen_id"]: {"progress": r4(ratio[s["screen_id"]]),
                                     "remaining_mm": r2(mm[s["screen_id"]] * (1 - ratio[s["screen_id"]]))}
                    for s in screens},
        "developers": group("owner"),
        "large_menus": group("large_code"),
        "medium_menus": group("medium_code"),
        "parts": group("part"),
        "team": agg([s["screen_id"] for s in screens]),
        "phases": {str(p): sum(1 for v in eff.values() if p in v) for p in PHASES},
        "delayed_screens": delayed,
        "unlinked": unlinked,
        "after_week_hashes": [c["hash"] for c in commits if c["date"].isoformat() > ref_end],
        "mismatch": sorted(mismatch),
        "schedule": schedule,
        "previous_week": {"week": PREV_WEEK, "team": snap["team"],
                          "large_menus": {k: v["progress"] for k, v in snap["large_menus"].items()},
                          "medium_menus": {k: v["progress"] for k, v in snap["medium_menus"].items()},
                          "phases": snap["phases"], "issue_ids": sorted(prev),
                          "completed_this_week": sorted(k for k, v in status.items()
                                                        if v == "완료" and prev_status[k] != "완료")},
        "issue_tracking": tracking,
        "previous_action_items": action_done,
    }
    return exp, {PREV_WEEK["start"]: snap}


def main():
    screens, commits, done, mismatch, done_on = build()
    exp, history = expected(screens, commits, done, mismatch, done_on)
    dump = lambda name, obj: (OUT / name).write_text(  # noqa: E731
        json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    dump("project.json", PROJECT)
    dump("developers.json", DEVELOPERS)
    dump("screens.json", screens)
    dump("commits.json", [{"hash": c["hash"], "author_email": c["email"], "date": c["date"].isoformat(),
                           "message": c["message"]} for c in commits])
    dump("meeting_history.json", history)
    dump("expected_progress.json", exp)
    sch, prev, tr = exp["schedule"], exp["previous_week"]["team"], exp["issue_tracking"]
    print(f"화면 {len(screens)}개, 커밋 {len(commits)}건, 팀 진척률 {exp['team']['progress']*100:.2f}% "
          f"(지난주 {prev['progress']*100:.2f}%, 계획 {sch['team']['planned_progress']*100:.2f}%, "
          f"일정 경과 {sch['elapsed']*100:.2f}%), 지연 화면 {len(exp['delayed_screens'])}개, "
          f"이슈 해결 {len(tr['resolved'])}·지속 {len(tr['persistent'])}·신규 {len(tr['new'])}")


if __name__ == "__main__":
    main()
