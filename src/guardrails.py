"""가드레일 (코드 계층 강제). 도구/핸들러 실행 전에 호출된다.

1 역할별 조회 범위 (고정 문구)                    2 PII 마스킹은 middleware.py
  (범위는 개인 진척·사람 정보에만 적용한다. 화면 정의서·팀 규칙·전환 일정·회의록 같은 팀 공용 문서는 누구나 읽는다.
   범위 밖 화면이라도 정의서·회의록 내용(완료 조건, 지연 이유, 결정·조치)만 묻는 질문은 문서 Q&A로 답하고,
   그 화면의 진척 수치·담당자·연락처 질문은 거절한다. 문서 답변에서도 범위 밖 사람의 진척 수치 줄은 가린다)
3 직접 인젝션 거절 / 간접 인젝션은 숨은 지시 줄만 제외        4 승인 없는 게시 금지(api.py, PM·PL만 승인)
5 사람 평가·순위 거절 (업무 단위 현황 안내)

역할: pm(전체) / pl(본인 파트의 대메뉴·소속 개발자) / developer(본인 화면만)
신원(user_id·role)은 로그인 세션(api.py, Bearer 토큰)에서만 정한다. 질문 속 '나 PM이야' 같은 자기 소개 문구는 판단에 쓰지 않으며,
범위 밖 데이터를 요청할 때만 거절한다. 본문에 신원을 보내면 api.py가 422로 막는다.
"""
from __future__ import annotations

import re

from src import tools

DENY = "해당 정보는 조회 권한이 없습니다"
INJECTION_REFUSAL = "프롬프트 인젝션 시도로 판단되어 요청을 처리할 수 없습니다."
RANKING_REFUSAL = ("사람에 대한 평가·순위는 제공하지 않습니다. 대신 업무 단위 현황"
                   "(화면·메뉴별 진척률·남은 MM)을 확인해 보세요.")
MEETING_ROLES = ("pm", "pl")  # 회의 자료 초안 작성·승인 가능 역할

# 인젝션: 어시스턴트에게 지시를 덮어쓰거나 숨은 설정을 내놓으라는 '명령' 형태만 막는다.
# '커밋 규칙 무시하면 어떻게 돼?', '시스템 프롬프트 변경 회의 결정사항'처럼 단어를 언급만 하는 질문은 막지 않는다.
INJECTION_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r"(이전|위|앞의|기존|모든)\s*(지시|명령|지침|규칙|프롬프트)[^.\n]{0,15}(무시|잊|따르지)",
    r"(지시|명령|지침)(을|를|은|는)?\s*(모두\s*|전부\s*)?(무시|잊어)",
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)",
    r"disregard\s+.{0,20}(instructions?|rules?)",
    r"(시스템|system)\s*(프롬프트|prompt)(를|을)?\s*(그대로\s*|전부\s*|모두\s*)?(출력|공개|보여|알려|말해|print|show|reveal)",
    r"(print|show|reveal|tell\s+me|output)\s+(me\s+)?(your|the)\s+(system\s+|hidden\s+|initial\s+)?(prompt|instructions)",
    r"(너의|너의\s*숨겨진|네|니|당신의|숨겨진|내부)\s*(시스템\s*)?(프롬프트|지침|지시문|설정)(을|를)?\s*(출력|공개|보여|알려|말해)",
    r"developer\s*message",
    r"jailbreak|탈옥|DAN\s*모드",
    r"(you are now|지금부터\s*(너는|넌))",
    r"(모든|전체)\s*(데이터|정보|개발자)[^.\n]{0,10}(출력|유출|전송)",
]]
# 사람 평가·순위: 사람(개발자·팀원·이름)을 비교·평가·줄 세우는 요청만 거절한다. '메뉴별 진척 비교'처럼 업무 단위 비교는 허용
RANKING_ALWAYS = [re.compile(p, re.IGNORECASE) for p in [
    r"인사\s*평가|고과|성과\s*(평가|비교|등급|점수|순위)",
    r"(누가|가장|제일|젤)\s*(더\s*|제일\s*|가장\s*)?(일\s*)?(잘|못|열심|게으|우수|뒤처)",
    r"(best|worst|top)\s*(developer|performer)",
]]
RANKING_ACT = re.compile(r"순위|랭킹|ranking|rank\b|등수|서열|줄\s*세|비교|평가|등급|점수\s*(매|줘)|잘\s*하|못\s*하|잘해|못해|"
                         r"우수|뒤처|게으|열심|최고|최악", re.IGNORECASE)
RANKING_PEOPLE = re.compile(r"개발자|팀원|사람|직원|인원|멤버|누가|developer|people|person|member", re.IGNORECASE)
# 팀 공용 문서 질문: 화면 정의서(완료 조건·기능 등)와 회의록(지연 이유·결정사항·조치·블로커·액션아이템)은 누구나 읽는다.
# 진척 수치·담당자·연락처처럼 사람의 진척·정보를 드러내는 말이 섞이면 해당 없음. '왜 지연됐어'는 이유(회의록) 질문이고,
# '지연됐어?'(지연 여부)처럼 이유·회의 용어 없이 상태만 묻는 질문은 진척 질문으로 본다.
SCREEN_DOC_RE = re.compile(r"완료\s*조건|정의서|화면\s*정의|요구\s*사항|요건|기능|입력\s*항목|화면\s*구성|"
                           r"왜|이유|원인|회의|결정|조치|블로커|액션\s*아이템")
SCREEN_PROGRESS_RE = re.compile(r"진척|진행률|남은|MM|mm|현황|커밋|상태|담당|누가|누구|얼마|몇\s*%|퍼센트|끝났|충족|만족|달성|"
                                r"완료(했|됐|된|율|\s*여부)|이메일|메일|전화|연락처")
ALL_SCOPE_PATTERNS = [re.compile(p) for p in [
    r"(팀원|팀\s*전체|다른\s*(개발자|사람|팀원)|전체\s*(개발자|팀|현황|진척)|모든\s*개발자|개발자\s*(들|별)|타인|남의)",
]]


def is_injection(text: str) -> bool:
    return any(p.search(text) for p in INJECTION_PATTERNS)


def is_ranking(text: str) -> bool:
    """사람을 평가·비교·줄 세우는 요청인가 (사람 지칭 또는 이름 + 평가·비교 표현)."""
    if _hit(RANKING_ALWAYS, text):
        return True
    return bool(RANKING_ACT.search(text)) and bool(RANKING_PEOPLE.search(text) or mentioned_people(text))


def mentioned_parts(question: str) -> list[str]:
    """질문에 언급된 파트 코드(PL1·PL2). 'PL 2'처럼 띄어 써도 찾는다."""
    return [p for p in tools.load_project()["parts"]
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(p[:-1])}\s*{re.escape(p[-1])}(?![0-9])", question, re.IGNORECASE)]


def _hit(patterns, text: str) -> bool:
    return any(p.search(text) for p in patterns)


def check_question(question: str, user_id: str, role: str) -> dict | None:
    """질문을 도구 실행 전에 검사. 위반 시 {policy, answer, trace}, 통과 시 None."""
    def block(policy: str, answer: str) -> dict:
        return {"policy": policy, "answer": answer,
                "trace": [{"step": "guardrail", "input": policy, "output": "refused"}]}

    if is_injection(question):
        return block("direct_injection", INJECTION_REFUSAL)
    if is_ranking(question):
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
    parts = mentioned_parts(question)
    if role == "developer" and parts and not is_screen_doc_question(question):
        return False  # 파트 단위 진척은 PM·PL만 (개발자는 본인 화면만)
    if role == "pl" and any(p != tools.user_scope(role, user_id).get("part") for p in parts):
        return False  # 다른 파트(PL1·PL2) 지칭
    return True


def out_of_scope_screens(question: str, user_id: str, role: str) -> list[str]:
    """질문 속 화면 ID 중 요청자 진척 범위 밖인 화면 (PM은 없음)."""
    scope = tools.user_scope(role, user_id)
    return sorted(sid for sid in set(tools.SCREEN_ID_RE.findall(question))
                  if (s := tools.find_screen(sid)) is not None and not tools.in_scope(s, scope))


def is_screen_doc_question(question: str) -> bool:
    """팀 공용 문서(화면 정의서·회의록) 내용만 묻는 질문인가: 문서 용어가 있고 진척 수치·담당·연락처 관련 말이 없음."""
    return bool(SCREEN_DOC_RE.search(question)) and not SCREEN_PROGRESS_RE.search(question)


def filter_injected_chunks(chunks: list[dict]) -> tuple[list[dict], list[str]]:
    """검색 문서 속 간접 인젝션: 숨은 지시가 든 줄만 빼고 나머지(결정사항 등)는 근거로 남긴다.
    줄을 빼고 남는 내용이 없으면 그 문서를 제외한다. (남은 청크, 'doc_id(뺀 줄 수)' 목록)"""
    kept, dropped = [], []
    for c in chunks:
        text = c.get("text", "")
        if not is_injection(text):
            kept.append(c)
            continue
        lines = [ln for ln in text.splitlines() if not is_injection(ln)]
        n = len(text.splitlines()) - len(lines)
        dropped.append(f"{c.get('doc_id', '?')}({n}줄)")
        rest = "\n".join(lines).strip()
        if rest and not is_injection(rest):  # 줄을 넘나드는 지시는 문서 전체를 뺀다
            kept.append({**c, "text": rest})
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
