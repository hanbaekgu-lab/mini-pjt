"""POST /login(데모 로그인) + POST /query 질의 엔드포인트 (FastAPI).

/login 요청: {"user_id"} → {"session_token", "name", "role", "part"}. SSO(OIDC 등) 자리에 들어갈 데모 로그인이다
(비밀번호 없음, developers.json에 등록된 user_id만, 미등록은 401). 토큰은 서버 메모리에만 있어 재시작하면 다시 로그인한다.
/query 요청: 표준 본문 {"question"} + 선택 확장 필드 thread_id(대화·승인 재개), approval(approve|reject).
신원은 `Authorization: Bearer <session_token>` 헤더의 로그인 세션에서만 정하고, 역할은 서버가 등록 정보로 정한다.
본문에 user_id·role 등 정의되지 않은 필드가 오면 무시하지 않고 422로 거절한다(로그인 세션을 쓰라는 안내, 혼동 방지).
응답: answer, contexts, trace (+ thread_id, pending_approval)
처리 순서: 세션 확인(401) → thread 소유자 확인(404) → 승인 재개 또는 가드레일 → Supervisor 라우팅(요청당 1회) → 담당 에이전트
→ 출력 단계 PII 마스킹. 가드레일에 막힌 요청은 라우팅(LLM)하지 않는다. trace 순서: guardrail → supervisor_route → ...
오류: 토큰 없음·무효 401, approval 값 오류·정의되지 않은 본문 필드 422, 없는 thread_id·다른 사용자의 thread_id 404.
thread는 만든 사용자(소유자)만 이어 쓴다. 예외로 승인 대기 회의 자료는 PM·PL 누구나 승인·반려할 수 있고, 승인 응답에는
게시 결과와 최소 trace만 담는다(초안·contexts 없음). 개발자의 승인 시도도 contexts 없이 거절한다.
같은 thread_id에 approval 없이 새 질문을 보내면 새 질의로 답하고, 진척 에이전트의 대화 기억은 그 thread를 이어 쓴다.
이미 처리된 thread_id에 approval 재전송 시 상태를 바꾸지 않고 기존 결과를 반환(멱등).
에이전트 본체는 ``set_handler``로 주입한다(agent.setup). 기본 핸들러 ``_default_handler``는 LLM 없이 도구 결과로 답하는
오프라인·테스트용 결정적 핸들러다.
"""
from __future__ import annotations

import re
import uuid
from typing import Callable, Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from src import guardrails, mcp_client, meeting, middleware, retriever, supervisor, tools, tracing

app = FastAPI(title="weekly-progress-agent")

# thread_id -> {"user_id"(소유자), "role", "route", "response", "epoch"(대화 기억 세대),
#               승인 대기 회의 자료면 "draft", "status"(pending|approved|rejected), "decision"(승인 결과), "graph_thread"}
THREADS: dict[str, dict] = {}
SESSIONS: dict[str, str] = {}  # session_token -> user_id (데모: 서버 메모리)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str


class LoginResponse(BaseModel):
    session_token: str
    name: str
    role: str
    part: Optional[str] = None


class QueryBody(BaseModel):
    """/query 본문: 표준 question + 선택 확장 필드. 신원(user_id·role)은 본문이 아니라 로그인 세션에서 온다."""
    model_config = ConfigDict(extra="forbid")
    question: str
    thread_id: Optional[str] = None
    approval: Optional[Literal["approve", "reject"]] = None


class QueryRequest(BaseModel):
    """내부 처리용 요청(세션에서 정한 신원 포함). 에이전트 핸들러가 받는다."""
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
        "_draft": {"title": meeting.meeting_title(), "body": text},
    }


def _publish_draft(draft: dict, approval: str) -> dict:
    """기본 핸들러용 승인 처리(LangGraph 없음). 응답에는 게시 결과와 최소 trace만 담는다."""
    if approval != "approve":
        return {"answer": "반려되어 게시하지 않았습니다.", "contexts": [],
                "trace": [{"step": "hitl_reject", "input": draft["title"], "output": "not_published"}],
                "pending_approval": False}
    try:
        title, body = middleware.mask_pii(draft["title"]), middleware.mask_pii(draft["body"])
        page = middleware.call_with_retry(mcp_client.call_tool, "create_page",  # 게시는 한 번만 시도(중복 게시 방지)
                                          {"title": title, "body": body, "page_id": meeting.meeting_page_id()}, retries=0)
        retriever.index_generated(page["page_id"], title, body)
    except Exception as e:  # 게시 실패: 게시하지 않음, 승인 대기 유지
        return {"answer": f"게시 실패(게시하지 않음): {e}", "contexts": [],
                "trace": [{"step": "confluence_publish", "input": draft["title"], "output": f"error: {e}"}],
                "pending_approval": True}
    return {"answer": f"승인되어 Confluence에 게시했습니다: {page['page_id']} (이메일·전화번호 마스킹 적용 후 게시)", "contexts": [],
            "trace": [{"step": "confluence_publish", "input": draft["title"], "output": page["page_id"]}],
            "pending_approval": False}


def _resume(req: QueryRequest, saved: dict) -> dict:
    """승인 대기 스레드 재개. 코드 계층에서 PM·PL만 승인 가능. 승인·거절 응답에는 contexts를 싣지 않는다."""
    if req.role not in guardrails.MEETING_ROLES:
        return {"answer": DENY, "contexts": [], "trace": [{"step": "guardrail", "input": "approval", "output": "denied"}],
                "pending_approval": saved.get("status") == "pending"}
    if saved.get("status") != "pending":  # 이미 처리됨: 기존 결과 그대로 (멱등)
        return saved["decision"]
    if saved.get("graph_thread") and _graph_resume is not None:  # LangGraph interrupt() 재개
        out = _graph_resume(saved, req.approval)
    else:
        out = _publish_draft(saved["draft"], req.approval)
    out = {**out, "contexts": [], "trace": [{"step": "guardrail", "input": "approval", "output": "allowed"},
                                            {"step": "supervisor_route", "input": "approval", "output": saved["route"]}]
           + out["trace"]}
    if out["pending_approval"] is False:
        saved["status"] = "approved" if req.approval == "approve" else "rejected"
        saved["decision"] = out
    return out


def _tool_trace(names: list[str], **kw) -> list[dict]:
    return [{"step": "tool_call", "input": {"tool": n, **kw}, "output": "ok"} for n in names]


def contact_answer(screen_id: str) -> dict | None:
    """담당자 연락처(이름 + 출력 단계에서 마스킹될 이메일). 요청자 범위 검사는 가드레일이 이미 했다."""
    text = tools.screen_contact(screen_id)
    if text is None:
        return None
    return {"answer": text, "contexts": [{"doc_id": "tool:get_developer", "text": text}],
            "trace": _tool_trace(["get_developer"], screen_id=screen_id), "pending_approval": False}


def _default_handler(req: QueryRequest, route: str) -> dict:
    """오프라인·테스트용 결정적 핸들러 (LLM 없음). 운영·평가는 agent.handle을 쓴다."""
    if route == "meeting_material":
        return _meeting_draft(req)
    q = req.question
    m = tools.SCREEN_ID_RE.search(q)
    screen_id = m.group(0) if m else None
    if re.search(r"이메일|전화|연락처", q) and screen_id and (c := contact_answer(screen_id)):
        return c
    if guardrails.out_of_scope_screens(q, req.user_id, req.role):  # 범위 밖 화면은 공용 문서 질문만 통과 → 문서로만 답한다
        return _docs_answer(q, screen_id)
    scope = tools.user_scope(req.role, req.user_id)  # 역할별 범위: PM 전체, PL 본인 파트, 개발자 본인 화면
    people = [p for p in guardrails.mentioned_people(q) if p in _developer_ids()]
    parts = guardrails.mentioned_parts(q)
    if not screen_id and len(people) == 1:  # 개인 진척은 요청 시에만 (범위 밖 사람은 가드레일이 이미 거절)
        scope = {"developer_id": people[0]}
    elif not screen_id and len(parts) == 1 and req.role != "developer":  # 파트 진척 (PL 타 파트는 가드레일이 거절)
        scope = {"part": parts[0]}
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
    """결정적 문서 답변(LLM 없음): 정의서 질문이면 그 화면의 정의서를, 아니면(지연 이유·결정 등) 첫 검색 결과를 인용한다."""
    hits = retriever.search(q, k=3)
    want = f"SCR-DEF-{screen_id[4:]}" if screen_id and re.search(r"완료\s*조건|정의서|기능|요구|요건|입력|구성", q) else None
    doc = next((h for h in hits if h["doc_id"] == want), hits[0] if hits else None)
    answer = f"{doc['text'].strip()}\n[{doc['doc_id']}]" if doc else "문서에서 근거를 찾지 못했습니다."
    return {"answer": answer, "contexts": hits,
            "trace": [{"step": "tool_call", "input": {"tool": "retrieve_docs", "query": q},
                       "output": [h["doc_id"] for h in hits]}], "pending_approval": False}


_handler: Callable[[QueryRequest, str], dict] = _default_handler


def set_handler(fn: Callable[[QueryRequest, str], dict] | None) -> None:
    """핸들러는 (요청, 라우팅 결과)를 받는다. 요청의 thread_id는 진척 에이전트 대화 기억 키로 쓴다."""
    global _handler
    _handler = fn or _default_handler


_graph_resume: Callable[[dict, str], dict] | None = None


def set_graph_resume(fn: Callable[[dict, str], dict] | None) -> None:
    global _graph_resume
    _graph_resume = fn


def registered_user(user_id: str) -> dict | None:
    return next((d for d in tools.load_developers() if d["developer_id"] == user_id), None)


@app.exception_handler(RequestValidationError)
async def _validation_error(request, exc: RequestValidationError):
    """본문에 user_id·role을 보내면 로그인 세션을 쓰라고 안내한다 (422)."""
    errors = exc.errors()
    if any(e.get("type") == "extra_forbidden" and e.get("loc", [None])[-1] in ("user_id", "role") for e in errors) \
            and request.url.path == "/query":
        return JSONResponse(status_code=422, content={"detail": "신원은 본문이 아니라 로그인 세션으로 보냅니다: "
                                                                "POST /login 후 Authorization: Bearer <session_token>"})
    return JSONResponse(status_code=422, content={"detail": jsonable(errors)})


def jsonable(errors: list) -> list:
    return [{k: (str(v) if k == "ctx" else v) for k, v in e.items() if k != "input"} for e in errors]


@app.post("/login", response_model=LoginResponse)
def login(req: LoginRequest) -> LoginResponse:
    """데모 로그인 (SSO 자리). 등록된 user_id면 세션 토큰을 발급한다."""
    user = registered_user(req.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="등록되지 않은 사용자입니다")
    token = uuid.uuid4().hex
    SESSIONS[token] = user["developer_id"]
    return LoginResponse(session_token=token, name=user["name"], role=user["role"], part=user.get("part"))


def session_user(authorization: str | None) -> dict:
    """Authorization: Bearer <session_token> → 등록 사용자. 없거나 무효면 401."""
    m = re.fullmatch(r"Bearer\s+(\S+)", (authorization or "").strip())
    user = registered_user(SESSIONS.get(m.group(1), "")) if m else None
    if user is None:
        raise HTTPException(status_code=401, detail="로그인 세션이 없거나 만료되었습니다. POST /login으로 로그인하세요",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


def _developer_ids() -> set[str]:
    return {d["developer_id"] for d in tools.load_developers() if d["role"] == "developer"}


@app.post("/query", response_model=QueryResponse)
def query(body: QueryBody, authorization: Optional[str] = Header(default=None)) -> QueryResponse:
    user = session_user(authorization)
    req = QueryRequest(question=body.question, user_id=user["developer_id"], role=user["role"],
                       thread_id=body.thread_id, approval=body.approval)
    out, tid, route = _query(req)
    # 미들웨어: 모든 역할의 answer·contexts·trace에서 PII 마스킹 (출력 단계). thread_id는 무작위 hex 안의 숫자열이
    # 전화번호로 오인돼 가려지지 않게 마스킹 대상에서 뺀다.
    out = middleware.mask_obj(out)
    tracing.record(tid, middleware.mask_pii(req.question), route, out["trace"])
    return QueryResponse(thread_id=tid, **out)


def _query(req: QueryRequest) -> tuple[dict, str, str]:
    """(응답, thread_id, 라우팅 결과). req의 신원은 로그인 세션에서 정한 값이다."""
    saved = THREADS.get(req.thread_id) if req.thread_id is not None else None
    if req.thread_id is not None:
        approving = req.approval is not None and saved is not None and "draft" in saved
        # 다른 사용자의 thread는 존재 여부도 드러내지 않는다 (승인 대기 회의 자료의 승인·반려만 예외)
        if saved is None or (saved["user_id"] != req.user_id and not approving):
            raise HTTPException(status_code=404, detail="thread_id를 찾을 수 없습니다")
        if approving:
            return _resume(req, saved), req.thread_id, saved["route"]
        if req.approval is not None:  # 승인 대상이 없는 본인 thread: 기존 결과 (멱등)
            return saved["response"], req.thread_id, saved["route"]
    tid = req.thread_id or uuid.uuid4().hex
    blocked = guardrails.check_question(req.question, req.user_id, req.role)
    if blocked:  # 도구·핸들러·라우팅 전에 코드 계층에서 차단
        out = {"answer": blocked["answer"], "contexts": [], "trace": blocked["trace"], "pending_approval": False}
        _save(tid, req, "guardrail", out)
        return out, tid, "guardrail"
    route = supervisor.route(req.question)
    head = [{"step": "guardrail", "input": "question", "output": "passed"},
            {"step": "supervisor_route", "input": req.question, "output": route}]
    epoch = (saved or {}).get("epoch", 0)
    memory_key = tid if epoch == 0 else f"{tid}#{epoch}"
    try:
        out = middleware.call_with_retry(_handler, req.model_copy(update={"thread_id": memory_key}), route,
                                         timeout=middleware.LLM_TIMEOUT)
    except middleware.CallFailed as e:  # LLM/도구 호출 실패: 오류 명시, 게시 없음
        if e.timed_out:  # 타임아웃 뒤에도 돌고 있을 수 있는 작업과 대화 기억이 섞이지 않게 다음 요청은 새 기억 키를 쓴다
            epoch += 1
        out = {"answer": f"오류: {e}", "contexts": [],
               "trace": [{"step": "llm_call", "input": req.question, "output": f"error: {e}"}],
               "pending_approval": False}
    ctx, extra = guardrails.sanitize_contexts(req.role, req.user_id, out.get("contexts", []))
    draft, graph_thread = out.get("_draft"), out.get("_graph_thread")
    out = {"answer": out["answer"], "contexts": ctx, "trace": head + list(out.get("trace", [])) + extra,
           "pending_approval": bool(out.get("pending_approval", False))}
    rec = _save(tid, req, route, out)
    rec["epoch"] = epoch
    if draft:
        rec.update(draft=draft, status="pending", graph_thread=graph_thread)
    return out, tid, route


def _save(tid: str, req: QueryRequest, route: str, out: dict) -> dict:
    """thread 기록. 같은 thread의 새 질문은 응답만 바꾸고, 승인 대기 중인 회의 자료(draft)는 유지한다."""
    rec = THREADS.setdefault(tid, {"user_id": req.user_id, "role": req.role})
    rec.update(route=route, response=out)
    return rec
