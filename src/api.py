"""POST /query 단일 엔드포인트 (FastAPI).

요청: question, user_id, role(developer|pm_pl), 재개용 thread_id, approval(approve|reject)
응답: answer, contexts, trace (+ thread_id, pending_approval)
오류: role/approval 값 오류 422, 없는 thread_id 404, 미등록 user_id 403.
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
    role: Literal["developer", "pm_pl"]
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
    if req.role != "pm_pl":
        return {"answer": DENY, "contexts": [], "trace": [{"step": "guardrail", "input": "meeting_material",
                                                          "output": "denied"}], "pending_approval": False}
    text = meeting.build_meeting_material()
    prev = retriever.search("지난주 액션아이템 미완료", k=1)  # Plan-Execute: 지난 액션아이템 확인 단계
    return {
        "answer": f"[회의 자료 초안 - 승인 대기]\n{text}\n\nPM/PL의 approve 후 Confluence에 게시됩니다.",
        "contexts": [{"doc_id": "tool:build_progress_report", "text": text}],
        "trace": _tool_trace(["calc_progress", "calc_remaining_mm", "check_mismatch"], plan_step="collect_progress")
        + [{"step": "tool_call", "input": {"tool": "retrieve_docs", "plan_step": "check_previous_action_items"},
            "output": [d["doc_id"] for d in prev]}]
        + [{"step": "hitl_interrupt", "input": "meeting_material", "output": "pending"}],
        "pending_approval": True,
        "_draft": {"title": _meeting_title(), "body": text},
    }


def _resume(req: QueryRequest, saved: dict) -> dict:
    """승인 대기 스레드 재개. 코드 계층에서 PM/PL만 승인 가능."""
    resp = saved["response"]
    if req.role != "pm_pl":
        return {**resp, "answer": DENY, "trace": resp["trace"] + [
            {"step": "guardrail", "input": "approval", "output": "denied"}]}
    if saved.get("graph_thread") and _graph_resume is not None:  # LangGraph interrupt() 재개
        return _graph_resume(saved, req.approval)
    draft = saved["draft"]
    if req.approval == "approve":
        try:
            title, body = middleware.mask_pii(draft["title"]), middleware.mask_pii(draft["body"])
            page = middleware.call_with_retry(mcp_client.call_tool, "create_page", {"title": title, "body": body})
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
    m = re.search(r"TASK-\d{3}", q)
    task_id = m.group(0) if m else None
    if re.search(r"이메일|전화", q) and task_id:  # 담당자 연락처: 이름 + (출력 단계에서 마스킹될) 이메일
        t = next((t for t in tools.load_tasks() if t["task_id"] == task_id), None)
        dev = next((d for d in tools.load_developers() if t and d["developer_id"] == t["owner"]), None)
        if dev:
            text = f"{task_id} 담당자: {dev['name']} / 이메일: {dev['email']} / 전화번호: 등록되어 있지 않습니다"
            return {"answer": text, "contexts": [{"doc_id": "tool:get_developer", "text": text}],
                    "trace": _tool_trace(["get_developer"], task_id=task_id), "pending_approval": False}
    dev_id = req.user_id if req.role == "developer" else None
    report = tools.build_progress_report(developer_id=dev_id, task_id=task_id)
    text = tools.format_progress_report(report)
    names = ["calc_progress", "calc_remaining_mm", "check_mismatch"]
    extra = ""
    if re.search(r"커밋", q):
        commits = tools.get_commits()
        names.append("get_commits")
        if "developers" in report or task_id is None:
            hs = ", ".join(c["hash"] for c in commits["unlinked"])
            extra = f"\n미연결 커밋 {len(commits['unlinked'])}건: {hs}"
    return {
        "answer": text + extra,
        "contexts": [{"doc_id": "tool:build_progress_report", "text": text}],
        "trace": _tool_trace(names, developer_id=dev_id, task_id=task_id),
        "pending_approval": False,
    }


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
    tracing.record(res.thread_id, middleware.mask_pii(req.question), route, out["trace"])
    return QueryResponse(**out)


def _query(req: QueryRequest) -> QueryResponse:
    if req.user_id not in registered_user_ids():
        raise HTTPException(status_code=403, detail="등록되지 않은 사용자입니다")
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
