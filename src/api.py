"""POST /query 단일 엔드포인트 (FastAPI).

요청: question, user_id, role(pm|pl|developer), 재개용 thread_id, approval(approve|reject)
응답: answer, contexts, trace (+ thread_id, pending_approval)
오류: role/approval 값 오류 422, 없는 thread_id 404, 미등록 user_id 403, 등록 역할과 다른 role 403.
신원은 세션(user_id·role)에서만 정한다 (데모 수준, 인증 없음).
이미 처리된 thread_id에 approval 재전송 시 상태를 바꾸지 않고 기존 결과를 반환(멱등).
에이전트 본체는 ``set_handler``로 주입한다(기본: 진척 도구 기반 결정적 응답).
"""
from __future__ import annotations

import re
import uuid
from typing import Callable, Literal, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src import guardrails, mcp_client, meeting, middleware, retriever, supervisor, tools, tracing

app = FastAPI(title="weekly-progress-agent")

# thread_id -> {"user_id", "role", "response"}
THREADS: dict[str, dict] = {}


class QueryRequest(BaseModel):
    question: str
    user_id: str
    role: Literal["pm", "pl", "developer"]
    thread_id: Optional[str] = None
    approval: Optional[Literal["approve", "reject"]] = None


class QueryResponse(BaseModel):
    answer: str
    contexts: list[dict]
    trace: list[dict]
    thread_id: str
    pending_approval: bool = False


DENY = guardrails.DENY
MEETING_KW = ("회의 자료", "회의자료", "회의 초안", "주간회의")


def _meeting_title() -> str:
    return meeting.meeting_title()


def _meeting_draft(req: QueryRequest) -> dict:
    if req.role not in guardrails.MEETING_ROLES:
        return {"answer": DENY, "contexts": [], "trace": [{"step": "guardrail", "input": "meeting_material",
                                                          "output": "denied"}], "pending_approval": False}
    text = meeting.build_meeting_material()  # 지난주 스냅샷은 지난 회의 기록(장기 메모리 초기값)에서 읽는다
    prev = retriever.search("지난주 회의 블로커 액션아이템", k=1)  # Plan-Execute: 지난주 회의 확인 단계
    return {
        "answer": f"[회의 자료 초안 - 승인 대기]\n{text}\n\nPM 또는 PL의 approve 후 Confluence에 게시됩니다.",
        "contexts": [{"doc_id": "tool:build_progress_report", "text": text}],
        "trace": _tool_trace(["calc_progress", "calc_remaining_mm", "check_mismatch"], plan_step="collect_progress")
        + [{"step": "tool_call", "input": {"tool": "memory_store", "plan_step": "check_previous_meeting"},
            "output": "history:meeting_history.json"},
           {"step": "tool_call", "input": {"tool": "retrieve_docs", "plan_step": "check_previous_meeting"},
            "output": [d["doc_id"] for d in prev]}]
        + [{"step": "hitl_interrupt", "input": "meeting_material", "output": "pending"}],
        "pending_approval": True,
        "_draft": {"title": _meeting_title(), "body": text},
    }


def _resume(req: QueryRequest, saved: dict) -> dict:
    """승인 대기 스레드 재개. 코드 계층에서 PM·PL만 승인 가능."""
    resp = saved["response"]
    if req.role not in guardrails.MEETING_ROLES:
        return {**resp, "answer": DENY, "trace": resp["trace"] + [
            {"step": "guardrail", "input": "approval", "output": "denied"}]}
    if saved.get("graph_thread") and _graph_resume is not None:  # LangGraph interrupt() 재개
        return _graph_resume(saved, req.approval)
    draft = saved["draft"]
    if req.approval == "approve":
        try:
            title, body = middleware.mask_pii(draft["title"]), middleware.mask_pii(draft["body"])
            page = middleware.call_with_retry(mcp_client.call_tool, "create_page",
                                              {"title": title, "body": body, "page_id": meeting.meeting_page_id()})
            retriever.index_generated(page["page_id"], title, body)
        except Exception as e:  # 게시 실패: 게시하지 않음
            return {**resp, "answer": f"게시 실패(게시하지 않음): {e}",
                    "trace": resp["trace"] + [{"step": "confluence_publish", "input": draft["title"],
                                               "output": f"error: {e}"}]}
        saved["status"] = "approved"
        return {"answer": f"승인되어 Confluence에 게시했습니다: {page['page_id']}",
                "contexts": resp["contexts"],
                "trace": resp["trace"] + [{"step": "confluence_publish", "input": draft["title"],
                                           "output": page["page_id"]}],
                "pending_approval": False}
    saved["status"] = "rejected"
    return {"answer": "반려되어 게시하지 않았습니다.", "contexts": resp["contexts"],
            "trace": resp["trace"] + [{"step": "hitl_reject", "input": draft["title"], "output": "not_published"}],
            "pending_approval": False}


def _tool_trace(names: list[str], **kw) -> list[dict]:
    return [{"step": "tool_call", "input": {"tool": n, **kw}, "output": "ok"} for n in names]


def _default_handler(req: QueryRequest) -> dict:
    if supervisor.route(req.question) == "meeting_material":
        return _meeting_draft(req)
    q = req.question
    m = tools.SCREEN_ID_RE.search(q)
    screen_id = m.group(0) if m else None
    if re.search(r"이메일|전화", q) and screen_id:  # 담당자 연락처: 이름 + (출력 단계에서 마스킹될) 이메일
        s = tools.find_screen(screen_id)
        dev = next((d for d in tools.load_developers() if s and d["developer_id"] == s["owner"]), None)
        if dev:
            text = f"{screen_id} 담당자: {dev['name']} / 이메일: {dev['email']} / 전화번호: 등록되어 있지 않습니다"
            return {"answer": text, "contexts": [{"doc_id": "tool:get_developer", "text": text}],
                    "trace": _tool_trace(["get_developer"], screen_id=screen_id), "pending_approval": False}
    if guardrails.out_of_scope_screens(q, req.user_id, req.role):  # 범위 밖 화면은 정의서 질문만 통과 → 문서로만 답한다
        return _docs_answer(q, screen_id)
    scope = tools.user_scope(req.role, req.user_id)  # 역할별 범위: PM 전체, PL 본인 파트, 개발자 본인 화면
    people = [p for p in guardrails.mentioned_people(q) if p in _developer_ids()]
    if not screen_id and len(people) == 1:  # 개인 진척은 요청 시에만 (범위 밖 사람은 가드레일이 이미 거절)
        scope = {"developer_id": people[0]}
    report = tools.build_progress_report(screen_id=screen_id) if screen_id else tools.build_progress_report(**scope)
    text = tools.format_progress_report(report)
    names = ["calc_progress", "calc_remaining_mm", "check_mismatch"]
    extra = ""
    if re.search(r"커밋", q) and screen_id is None:
        names.append("get_commits")
        unlinked = report.get("unlinked_commits", [])
        extra = f"\n미연결 커밋 {len(unlinked)}건: {', '.join(c['hash'] for c in unlinked)}"
    return {
        "answer": text + extra,
        "contexts": [{"doc_id": "tool:build_progress_report", "text": text}],
        "trace": _tool_trace(names, screen_id=screen_id, **scope),
        "pending_approval": False,
    }


def _docs_answer(q: str, screen_id: str | None) -> dict:
    """결정적 문서 답변(LLM 없음): 질문한 화면의 정의서를 우선해 근거 문서를 그대로 인용한다."""
    hits = retriever.search(q, k=3)
    doc = next((h for h in hits if screen_id and h["doc_id"] == f"SCR-DEF-{screen_id[4:]}"), hits[0] if hits else None)
    answer = f"{doc['text'].strip()}\n[{doc['doc_id']}]" if doc else "문서에서 근거를 찾지 못했습니다."
    return {"answer": answer, "contexts": hits,
            "trace": [{"step": "tool_call", "input": {"tool": "retrieve_docs", "query": q},
                       "output": [h["doc_id"] for h in hits]}], "pending_approval": False}


_handler: Callable[[QueryRequest], dict] = _default_handler


def set_handler(fn: Callable[[QueryRequest], dict] | None) -> None:
    global _handler
    _handler = fn or _default_handler


_graph_resume: Callable[[dict, str], dict] | None = None


def set_graph_resume(fn: Callable[[dict, str], dict] | None) -> None:
    global _graph_resume
    _graph_resume = fn


def registered_user_ids() -> set[str]:
    return {d["developer_id"] for d in tools.load_developers()}


def registered_role(user_id: str) -> str | None:
    return next((d["role"] for d in tools.load_developers() if d["developer_id"] == user_id), None)


def _developer_ids() -> set[str]:
    return {d["developer_id"] for d in tools.load_developers() if d["role"] == "developer"}


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest) -> QueryResponse:
    saved = THREADS.get(req.thread_id) if req.thread_id else None
    route = (saved or {}).get("route") or supervisor.route(req.question)
    res = _query(req)
    THREADS.get(res.thread_id, {}).setdefault("route", route)
    # Supervisor 라우팅을 모든 응답 trace 맨 앞에 노출
    if not any(t.get("step") == "supervisor_route" for t in res.trace):
        res.trace = [{"step": "supervisor_route", "input": req.question, "output": route}] + list(res.trace)
    # 미들웨어: 모든 역할의 answer·contexts·trace에서 PII 마스킹 (출력 단계)
    out = middleware.mask_obj(res.model_dump())
    out["thread_id"] = res.thread_id  # 무작위 hex 안의 숫자열이 전화번호로 오인돼 가려지지 않게 한다
    tracing.record(res.thread_id, middleware.mask_pii(req.question), route, out["trace"])
    return QueryResponse(**out)


def _query(req: QueryRequest) -> QueryResponse:
    if req.user_id not in registered_user_ids():
        raise HTTPException(status_code=403, detail="등록되지 않은 사용자입니다")
    if req.role != registered_role(req.user_id):  # 세션 수준에서 다른 역할을 사칭하는 시도
        raise HTTPException(status_code=403, detail="사용자의 등록 역할과 요청 역할이 다릅니다")
    if req.thread_id is not None and req.thread_id not in THREADS:
        raise HTTPException(status_code=404, detail="thread_id를 찾을 수 없습니다")
    if req.thread_id is not None:
        saved = THREADS[req.thread_id]
        if req.approval is None or saved["response"].get("pending_approval") is False:
            return QueryResponse(thread_id=req.thread_id, **saved["response"])
        if "draft" in saved:
            out = _resume(req, saved)
            if out.get("pending_approval") is False:
                saved["response"] = out
            return QueryResponse(thread_id=req.thread_id, **out)
    tid = req.thread_id or uuid.uuid4().hex
    blocked = guardrails.check_question(req.question, req.user_id, req.role)
    if blocked:  # 도구·핸들러 실행 전에 코드 계층에서 차단
        out = {"answer": blocked["answer"], "contexts": [], "trace": blocked["trace"], "pending_approval": False}
        THREADS[tid] = {"user_id": req.user_id, "role": req.role, "response": out}
        return QueryResponse(thread_id=tid, **out)
    try:
        out = middleware.call_with_retry(_handler, req, timeout=middleware.LLM_TIMEOUT)
    except middleware.CallFailed as e:  # LLM/도구 호출 실패: 오류 명시, 게시 없음
        out = {"answer": f"오류: {e}", "contexts": [],
               "trace": [{"step": "llm_call", "input": req.question, "output": f"error: {e}"}],
               "pending_approval": False}
    ctx, extra = guardrails.sanitize_contexts(req.role, req.user_id, out.get("contexts", []))
    out = {**out, "contexts": ctx, "trace": list(out.get("trace", [])) + extra}
    draft, graph_thread = out.get("_draft"), out.get("_graph_thread")
    out = {k: out[k] for k in ("answer", "contexts", "trace", "pending_approval") if k in out}
    THREADS[tid] = {"user_id": req.user_id, "role": req.role, "response": out}
    if draft:
        THREADS[tid]["draft"] = draft
        THREADS[tid]["status"] = "pending"
        if graph_thread:
            THREADS[tid]["graph_thread"] = graph_thread
    return QueryResponse(thread_id=tid, **out)
