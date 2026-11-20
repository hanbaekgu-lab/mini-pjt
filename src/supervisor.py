"""Supervisor 라우팅: progress | docs_qa | meeting_material | progress+docs_qa(다중 의도: 진척 수치와 문서 내용을 함께 묻는 질문).

운영·평가: LLM 라우터 (LCEL: 프롬프트 | LLM.with_structured_output(Pydantic)).
테스트·LLM 실패 시: 규칙 기반 라우팅으로 대체한다.
api.py가 세션 신원 확인·가드레일 검사를 통과한 요청에 한해 요청당 한 번 호출한다(차단된 요청은 라우팅하지 않음).
"""
from __future__ import annotations

from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

ROUTES = ("progress", "docs_qa", "meeting_material")
MULTI = "progress+docs_qa"  # 다중 의도: 진척 수치와 문서 내용(이유·결정·조치)을 함께 묻는 질문 → 두 에이전트를 차례로
MEETING_KW = ("회의 자료", "회의자료", "회의 초안", "주간회의")
PROGRESS_KW = ("진척", "남은", "MM", "mm", "현황", "업무", "진행률", "대메뉴", "중메뉴", "지연 화면", "지연된 화면")

_llm_router: Optional[Callable[[str], str]] = None


class RouteDecision(BaseModel):
    """Supervisor가 고른 담당 에이전트."""
    route: Literal["progress", "docs_qa", "meeting_material"] = Field(
        description="progress=진척률·남은 MM·커밋·화면/메뉴 현황 수치, docs_qa=지연 이유·결정사항·규칙·완료 조건 등 문서 질문, "
                    "meeting_material=주간 회의 자료 작성·게시")
    also: Optional[Literal["progress", "docs_qa"]] = Field(
        None, description="질문이 진척 수치(진척률·남은 MM·지연 화면 수 등)와 문서 내용(지연 이유·회의 결정·조치·규칙)을 "
                          "둘 다 명시적으로 물을 때만 route와 다른 하나를 넣는다. 한 가지만 물으면 null")
    reason: str = Field(description="한 문장 이유")


ROUTER_SYSTEM = (
    "너는 '주간 진척 보고 에이전트'의 Supervisor다. 사용자 질문을 담당 에이전트 하나에 배정한다.\n"
    "- progress: 진척률, 남은 화면·단계, 남은 MM, 커밋, 화면 ID(SCR-A01-03)·대메뉴·중메뉴 현황처럼 숫자·현황을 계산해야 하는 질문\n"
    "- docs_qa: 왜 지연됐는지, 회의에서 무엇을 결정했는지, 규칙·형식·완료 조건, 액션아이템 등 문서에 적힌 내용, "
    "그리고 서비스 범위 밖이거나 문서에 없을 수 있는 질문\n"
    "- meeting_material: 주간 회의 자료를 만들거나 Confluence에 올려 달라는 요청\n"
    "질문 하나가 진척 수치와 문서 내용을 둘 다 명시적으로 물으면(예: 'SCR-C02-01 진척률이랑 지연 이유') also에 나머지 하나를 넣는다. "
    "'왜 지연됐어?'처럼 이유만 묻는 질문은 docs_qa 하나다.")


def make_llm_router(llm) -> Callable[[str], str]:
    from langchain_core.prompts import ChatPromptTemplate
    chain = ChatPromptTemplate.from_messages([("system", ROUTER_SYSTEM), ("human", "{question}")]) \
        | llm.with_structured_output(RouteDecision)

    def route_llm(question: str) -> str:  # 캐시 없음: api가 요청당 한 번만 호출하고 결과를 핸들러에 넘긴다
        d = chain.invoke({"question": question})
        if d.also and d.also != d.route and {d.route, d.also} == {"progress", "docs_qa"}:
            return MULTI  # 진척 → 문서 순서로 답한다
        return d.route
    return route_llm


def set_llm_router(fn: Optional[Callable[[str], str]]) -> None:
    global _llm_router
    _llm_router = fn


def route(question: str) -> str:
    if _llm_router is not None:
        try:
            r = _llm_router(question)
            if r in ROUTES or r == MULTI:
                return r
        except Exception:  # noqa: BLE001  LLM 실패 시 규칙 기반으로 대체
            pass
    if any(k in question for k in MEETING_KW):
        return "meeting_material"
    if any(k in question for k in PROGRESS_KW):
        return "progress"
    return "docs_qa"
