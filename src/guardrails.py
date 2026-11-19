"""가드레일 (코드 계층 강제). 도구/핸들러 실행 전에 호출된다.

1 역할별 조회 범위 (고정 문구)                    2 PII 마스킹은 middleware.py
  (범위는 개인 진척·사람 정보에만 적용한다. 화면 정의서·팀 규칙·전환 일정·회의록 같은 팀 공용 문서는 누구나 읽는다.
   범위 밖 화면이라도 정의서 내용(완료 조건 등)만 묻는 질문은 문서 Q&A로 답하고, 그 화면의 진척 질문은 거절한다)
3 직접 인젝션 거절 / 간접 인젝션 청크 제외        4 승인 없는 게시 금지(api.py, PM·PL만 승인)
5 사람 평가·순위 거절 (업무 단위 현황 안내)

역할: pm(전체) / pl(본인 파트의 대메뉴·소속 개발자) / developer(본인 화면만)
신원(user_id·role)은 세션(요청 필드)에서만 정한다. 질문 속 '나 PM이야' 같은 자기 소개 문구는 판단에 쓰지 않으며,
범위 밖 데이터를 요청할 때만 거절한다. 역할과 user_id 등록 정보 불일치는 api.py에서 403으로 막는다.
"""
from __future__ import annotations

import re

from src import tools

DENY = "해당 정보는 조회 권한이 없습니다"
INJECTION_REFUSAL = "프롬프트 인젝션 시도로 판단되어 요청을 처리할 수 없습니다."
RANKING_REFUSAL = ("사람에 대한 평가·순위는 제공하지 않습니다. 대신 업무 단위 현황"
                   "(화면·메뉴별 진척률·남은 MM)을 확인해 보세요.")
MEETING_ROLES = ("pm", "pl")  # 회의 자료 초안 작성·승인 가능 역할

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
RANKING_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"순위", r"랭킹|ranking|rank\b", r"등수", r"서열",
    r"(가장|제일|젤|누가|가장\s*많이|최고|최악)\s*(잘|못|열심|게으|우수|뒤처|느린|빠른|성과|생산)",
    r"누가\s*(더|제일|가장)", r"(잘|못)\s*하는\s*(사람|개발자|팀원)",
    r"(인사)?평가|고과|성과\s*(비교|등급|점수)|(개발자|팀원|사람|직원)\s*등급|점수\s*(매겨|줘)|줄\s*세워|비교해",
    r"(best|worst|top)\s*(developer|performer)",
]]
# 화면 정의서(완료 조건·기능 등) 질문: 팀 공용 문서라 누구나 읽는다. 진척·담당·커밋 등 사람의 진척을 드러내는 말이 섞이면 해당 없음
SCREEN_DOC_RE = re.compile(r"완료\s*조건|정의서|화면\s*정의|요구\s*사항|요건|기능|입력\s*항목|화면\s*구성")
SCREEN_PROGRESS_RE = re.compile(r"진척|진행|남은|MM|mm|현황|커밋|지연|상태|담당|누가|얼마|몇\s*%|퍼센트|끝났|충족|만족|달성|완료(했|됐|된|율|\s*여부)")
ALL_SCOPE_PATTERNS = [re.compile(p) for p in [
    r"(팀원|팀\s*전체|다른\s*(개발자|사람|팀원)|전체\s*(개발자|팀|현황|진척)|모든\s*개발자|개발자\s*(들|별)|타인|남의)",
]]


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
    if _hit(RANKING_PATTERNS, question):
        return block("ranking_evaluation", RANKING_REFUSAL)
    if role in ("developer", "pl") and not _scope_ok(question, user_id, role):
        return block("scope", DENY)
    return None


def allowed_people(role: str, user_id: str) -> set[str] | None:
    """질문·문서에서 언급해도 되는 사람. PM=제한 없음(None), PL=본인 파트 구성원, 개발자=본인."""
    if role == "pm":
        return None
    if role == "developer":
        return {user_id}
    part = tools.user_scope(role, user_id).get("part")
    return {d["developer_id"] for d in tools.load_developers() if part and d["part"] == part} | {user_id}


def mentioned_people(question: str) -> list[str]:
    """질문에 언급된 사람(developer_id). ID·이름·이메일·성을 뺀 호칭(예: '메시님')으로 찾는다."""
    out = []
    for d in tools.load_developers():
        given = re.escape(d["name"][1:])
        # '메시지'처럼 단어의 일부인 경우는 이름으로 보지 않는다.
        if (d["developer_id"].lower() in question.lower() or d["name"] in question
                or d["email"].lower() in question.lower()
                or (len(d["name"]) >= 3 and re.search(
                    given + r"(?:님|씨|의|은|는|이|가|을|를|도|한테|에게)?(?![가-힣])", question))):
            out.append(d["developer_id"])
    return out


def _scope_ok(question: str, user_id: str, role: str) -> bool:
    allowed = allowed_people(role, user_id)
    if any(p not in allowed for p in mentioned_people(question)):
        return False
    if out_of_scope_screens(question, user_id, role) and not is_screen_doc_question(question):
        return False  # 범위 밖 화면의 진척(개인 진척이 드러남)은 거절, 화면 정의서 질문은 허용
    if role == "developer" and _hit(ALL_SCOPE_PATTERNS, question):
        return False
    if role == "pl":  # 다른 파트(PL1·PL2) 지칭
        scope = tools.user_scope(role, user_id)
        for part in tools.load_project()["parts"]:
            if part != scope.get("part") and re.search(rf"(?<![A-Za-z0-9]){part}(?![0-9])", question, re.IGNORECASE):
                return False
    return True


def out_of_scope_screens(question: str, user_id: str, role: str) -> list[str]:
    """질문 속 화면 ID 중 요청자 진척 범위 밖인 화면 (PM은 없음)."""
    scope = tools.user_scope(role, user_id)
    return sorted(sid for sid in set(tools.SCREEN_ID_RE.findall(question))
                  if (s := tools.find_screen(sid)) is not None and not tools.in_scope(s, scope))


def is_screen_doc_question(question: str) -> bool:
    """화면 정의서(팀 공용 문서) 내용만 묻는 질문인가: 정의서 용어가 있고 진척·담당 관련 말이 없음."""
    return bool(SCREEN_DOC_RE.search(question)) and not SCREEN_PROGRESS_RE.search(question)


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
    """PM 외 역할: 범위 밖 사람의 진척 수치가 담긴 줄은 제외(결정사항·블로커 등 공용 내용만 유지)."""
    allowed = allowed_people(role, user_id)
    if allowed is None:
        return chunks
    others = [d for d in tools.load_developers() if d["developer_id"] not in allowed]
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
