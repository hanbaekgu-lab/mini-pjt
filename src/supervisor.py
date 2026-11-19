"""Supervisor 라우팅: progress | docs_qa | meeting_material.

운영·평가: LLM 라우터 (LCEL: 프롬프트 | LLM.with_structured_output(Pydantic)).
테스트·LLM 실패 시: 규칙 기반 라우팅으로 대체한다.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

ROUTES = ("progress", "docs_qa", "meeting_material")
MEETING_KW = ("회의 자료", "회의자료", "회의 초안", "주간회의")
PROGRESS_KW = ("진척", "남은", "MM", "mm", "현황", "업무", "진행률", "대메뉴", "중메뉴", "지연 화면", "지연된 화면")

_llm_router: Optional[Callable[[str], str]] = None


class RouteDecision(BaseModel):
    """Supervisor가 고른 담당 에이전트."""
    route: Literal["progress", "docs_qa", "meeting_material"] = Field(
        description="progress=진척률·남은 MM·커밋·화면/메뉴 현황 수치, docs_qa=지연 이유·결정사항·규칙·완료 조건 등 문서 질문, "
                    "meeting_material=주간 회의 자료 작성·게시")
    reason: str = Field(description="한 문장 이유")


ROUTER_SYSTEM = (
    "너는 '주간 진척 보고 에이전트'의 Supervisor다. 사용자 질문을 담당 에이전트 하나에 배정한다.\n"
    "- progress: 진척률, 남은 화면·단계, 남은 MM, 커밋, 화면 ID(SCR-A01-03)·대메뉴·중메뉴 현황처럼 숫자·현황을 계산해야 하는 질문\n"
    "- docs_qa: 왜 지연됐는지, 회의에서 무엇을 결정했는지, 규칙·형식·완료 조건, 액션아이템 등 문서에 적힌 내용, "
    "그리고 서비스 범위 밖이거나 문서에 없을 수 있는 질문\n"
    "- meeting_material: 주간 회의 자료를 만들거나 Confluence에 올려 달라는 요청")


def make_llm_router(llm) -> Callable[[str], str]:
    from langchain_core.prompts import ChatPromptTemplate
    chain = ChatPromptTemplate.from_messages([("system", ROUTER_SYSTEM), ("human", "{question}")]) \
        | llm.with_structured_output(RouteDecision)

    @lru_cache(maxsize=256)  # 같은 질문은 한 번만 라우팅 (API·에이전트 중복 호출 방지)
    def route_llm(question: str) -> str:
        return chain.invoke({"question": question}).route
    return route_llm


def set_llm_router(fn: Optional[Callable[[str], str]]) -> None:
    global _llm_router
    _llm_router = fn


def route(question: str) -> str:
    if _llm_router is not None:
        try:
            r = _llm_router(question)
            if r in ROUTES:
                return r
        except Exception:  # noqa: BLE001  LLM 실패 시 규칙 기반으로 대체
            pass
    if any(k in question for k in MEETING_KW):
        return "meeting_material"
    if any(k in question for k in PROGRESS_KW):
        return "progress"
    return "docs_qa"
