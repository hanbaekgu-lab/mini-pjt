"""가드레일 (코드 계층 강제). 도구/핸들러 실행 전에 호출된다.

1 역할별 조회 범위 + 권한 상승 거절 (고정 문구)   2 PII 마스킹은 middleware.py
3 직접 인젝션 거절 / 간접 인젝션 청크 제외        4 승인 없는 게시 금지(api.py, PM/PL만 승인)
5 사람 평가·순위 거절 (업무 단위 현황 안내)
"""
from __future__ import annotations

import re

from src import tools

DENY = "해당 정보는 조회 권한이 없습니다"
INJECTION_REFUSAL = "프롬프트 인젝션 시도로 판단되어 요청을 처리할 수 없습니다."
RANKING_REFUSAL = ("사람에 대한 평가·순위는 제공하지 않습니다. 대신 업무 단위 현황"
                   "(업무별 진척률·남은 MM)을 확인해 보세요.")

INJECTION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"(이전|위|앞의|기존)\s*(지시|명령|지침|규칙|프롬프트)[^.\n]{0,15}(무시|잊|따르지)",
    r"(지시|명령|지침|규칙)(을|를)?\s*(모두\s*)?(무시|잊어)",
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)",
    r"disregard\s+.{0,20}(instructions?|rules?)",
    r"시스템\s*프롬프트", r"system\s*prompt", r"developer\s*message",
    r"(프롬프트|지침|설정)[^.\n]{0,10}(출력|공개|보여|알려|말해)",
    r"reveal\s+.{0,20}(prompt|instructions)",
    r"jailbreak|탈옥|DAN\s*모드",
    r"(you are now|지금부터\s*(너는|넌))",
    r"(모든|전체)\s*(데이터|정보|개발자)[^.\n]{0,10}(출력|유출|전송)",
]]
ESCALATION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"(나는|저는|난|제가|내가)\s*(사실\s*)?(pm|pl|피엠|피엘|관리자|팀장|리더|admin)",
    r"(pm|pl|관리자|admin|팀장)\s*(/\s*pl\s*)?(권한|역할)(으로|을|를|이|임|이다|입니다)",
    r"(권한|역할)(을|를)?\s*(pm|pl|관리자|admin|pm_pl)(으?로)?\s*(변경|상승|올려|바꿔|설정)",
    r"as\s+(an?\s+)?(admin|pm|manager)", r"role\s*[=:]\s*pm_pl", r"sudo|root\s*권한",
    r"권한\s*(상승|우회|해제)",
]]
RANKING_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"순위", r"랭킹|ranking|rank\b", r"등수", r"서열",
    r"(가장|제일|젤|누가|가장\s*많이|최고|최악)\s*(잘|못|열심|게으|우수|뒤처|느린|빠른|성과|생산)",
    r"누가\s*(더|제일|가장)", r"(잘|못)\s*하는\s*(사람|개발자|팀원)",
    r"(인사)?평가|고과|성과\s*(비교|등급|점수)|등급|점수\s*(매겨|줘)|줄\s*세워|비교해",
    r"(best|worst|top)\s*(developer|performer)",
]]
ALL_SCOPE_PATTERNS = [re.compile(p) for p in [
    r"(팀원|팀\s*전체|다른\s*(개발자|사람|팀원)|전체\s*(개발자|팀|현황|진척)|모든\s*개발자|개발자\s*(들|별)|타인|남의)",
]]

TASK_ID_RE = re.compile(r"TASK-\d{3}")
DEV_ID_RE = re.compile(r"\bdev\d{2}\b|\bpm\d{2}\b", re.IGNORECASE)


def is_injection(text: str) -> bool:
    return any(p.search(text) for p in INJECTION_PATTERNS)


def _hit(patterns, text: str) -> bool:
    return any(p.search(text) for p in patterns)


def check_question(question: str, user_id: str, role: str) -> dict | None:
    """질문을 도구 실행 전에 검사. 위반 시 {policy, answer, trace}, 통과 시 None."""
    def block(policy: str, answer: str) -> dict:
        return {"policy": policy, "answer": answer,
                "trace": [{"step": "guardrail", "input": policy, "output": "refused"}]}

    if is_injection(question):
        return block("direct_injection", INJECTION_REFUSAL)
    if _hit(ESCALATION_PATTERNS, question):
        return block("privilege_escalation", DENY)
    if _hit(RANKING_PATTERNS, question):
        return block("ranking_evaluation", RANKING_REFUSAL)
    if role == "developer":
        if not _scope_ok(question, user_id):
            return block("scope", DENY)
    return None


def _scope_ok(question: str, user_id: str) -> bool:
    devs = tools.load_developers()
    for d in devs:
        if d["developer_id"] == user_id:
            continue
        if d["developer_id"].lower() in question.lower() or d["name"] in question:
            return False
        if d["email"].lower() in question.lower():
            return False
        # 이름에서 성을 뺀 호칭 (예: '민준님')
        if len(d["name"]) >= 3 and d["name"][1:] in question:
            return False
    for tid in set(TASK_ID_RE.findall(question)):
        owner = next((t["owner"] for t in tools.load_tasks() if t["task_id"] == tid), None)
        if owner is not None and owner != user_id:
            return False
    if _hit(ALL_SCOPE_PATTERNS, question):
        return False
    return True


def filter_injected_chunks(chunks: list[dict]) -> tuple[list[dict], list[str]]:
    """검색 문서 속 간접 인젝션: 해당 청크만 제외한다. (남은 청크, 제외된 doc_id)"""
    kept, dropped = [], []
    for c in chunks:
        if is_injection(c.get("text", "")):
            dropped.append(c.get("doc_id", "?"))
        else:
            kept.append(c)
    return kept, dropped


def scope_chunks(role: str, user_id: str, chunks: list[dict]) -> list[dict]:
    """개발자 역할: 다른 개발자의 진척 수치가 담긴 줄은 제외(결정사항·블로커 등 공용 내용만 유지)."""
    if role != "developer":
        return chunks
    others = [d for d in tools.load_developers() if d["developer_id"] != user_id]
    numeric = re.compile(r"\d+(\.\d+)?\s*(%|MM|mm|퍼센트)|진척률|남은\s*MM")
    out = []
    for c in chunks:
        lines = []
        for ln in c.get("text", "").splitlines():
            mentions = any(d["name"] in ln or d["developer_id"] in ln or d["email"] in ln for d in others)
            if mentions and numeric.search(ln):
                continue
            lines.append(ln)
        out.append({**c, "text": "\n".join(lines)})
    return out


def sanitize_contexts(role: str, user_id: str, contexts: list[dict]) -> tuple[list[dict], list[dict]]:
    """응답 contexts에 간접 인젝션 제외 + 역할 범위 적용. (contexts, trace 항목)"""
    kept, dropped = filter_injected_chunks(contexts)
    kept = scope_chunks(role, user_id, kept)
    trace = [{"step": "guardrail", "input": "indirect_injection", "output": f"excluded:{','.join(dropped)}"}] if dropped else []
    return kept, trace
