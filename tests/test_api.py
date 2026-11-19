"""POST /login + POST /query 계약 테스트 (TestClient, API 키 불필요)."""
import pytest
from fastapi.testclient import TestClient

from helpers import login, post_query
from src import api, confluence

client = TestClient(api.app)
DENY = "해당 정보는 조회 권한이 없습니다"


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_DIR", str(tmp_path / "pages"))
    api.THREADS.clear()
    api.set_handler(None)


def body(**kw):
    b = {"question": "이번 주 진척 알려줘", "user_id": "pm01"}
    b.update(kw)
    return b


def ask(**kw):
    return post_query(client, body(**kw))


@pytest.mark.parametrize("user", ["pm01", "pl01", "dev01"])
def test_query_returns_answer_contexts_trace(user):
    r = ask(user_id=user)
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["answer"], str) and d["answer"]
    assert isinstance(d["contexts"], list) and d["contexts"]
    assert isinstance(d["trace"], list) and d["trace"]
    assert d["thread_id"]


def test_only_login_and_query_endpoints():
    paths = {r.path for r in api.app.routes if "POST" in getattr(r, "methods", set())}
    assert paths == {"/login", "/query"}


# --- 로그인 세션 ---
def test_login_returns_token_and_registered_identity():
    r = client.post("/login", json={"user_id": "pl02"})
    assert r.status_code == 200
    d = r.json()
    assert d["session_token"] and d["name"] == "서살라" and d["role"] == "pl" and d["part"] == "PL2"


def test_login_unregistered_401():
    assert client.post("/login", json={"user_id": "ghost99"}).status_code == 401


def test_query_standard_body_with_bearer_token():
    r = client.post("/query", json={"question": "내 진척률 알려줘"}, headers=login(client, "dev01"))
    assert r.status_code == 200 and "김메시(dev01) 담당 합계" in r.json()["answer"]  # 역할·범위는 서버가 등록 정보로 정한다


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer nope"}, {"Authorization": "token"}])
def test_missing_or_invalid_token_401(headers):
    assert client.post("/query", json={"question": "x"}, headers=headers).status_code == 401


@pytest.mark.parametrize("extra", [{"user_id": "pm01"}, {"role": "pm"}, {"user_id": "pm01", "role": "pm"}])
def test_identity_in_body_rejected_422(extra):
    # 본문의 user_id·role은 무시하지 않고 거절해 '로그인 세션 사용'을 안내한다 (권한 상승 혼동 방지)
    r = client.post("/query", json={"question": "전체 진척 알려줘", **extra}, headers=login(client, "dev01"))
    assert r.status_code == 422 and "로그인 세션" in r.json()["detail"]


@pytest.mark.parametrize("approval", ["yes", "APPROVE", ""])
def test_invalid_approval_422(approval):
    assert client.post("/query", json={"question": "x", "approval": approval},
                       headers=login(client, "pm01")).status_code == 422


def test_missing_question_422():
    assert client.post("/query", json={}, headers=login(client, "pm01")).status_code == 422


def test_unknown_thread_404():
    assert ask(thread_id="nope", approval="approve").status_code == 404


def test_known_thread_resume_ok_and_idempotent():
    tid = ask().json()["thread_id"]
    a = ask(thread_id=tid, approval="approve")
    b = ask(thread_id=tid, approval="approve")
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


def test_thread_id_not_masked_as_phone(monkeypatch):
    # 무작위 thread_id 안의 숫자열('01012345678')이 전화번호 마스킹에 걸리면 재개가 404가 된다 (회귀)
    headers = login(client, "pm01")  # 로그인 토큰 발급 뒤에 uuid를 고정한다

    class U:
        hex = "ab01012345678cd"
    monkeypatch.setattr(api.uuid, "uuid4", lambda: U())
    tid = client.post("/query", json={"question": "이번 주 진척 알려줘"}, headers=headers).json()["thread_id"]
    assert tid == U.hex
    assert client.post("/query", json={"question": "x", "thread_id": tid, "approval": "approve"},
                       headers=headers).status_code == 200


# --- R3: thread 소유권 (세션 사용자 기준) ---
@pytest.mark.parametrize("user", ["dev01", "pl01"])
def test_other_users_thread_is_404(user):
    tid = ask(question="프로젝트 전체 진척률 알려줘").json()["thread_id"]
    assert ask(user_id=user, question="아무거나", thread_id=tid).status_code == 404
    assert ask(user_id=user, question="승인", thread_id=tid, approval="approve").status_code == 404  # 승인 대상 아님


def test_denied_approval_returns_no_contexts():
    d = ask(question="이번 주 회의 자료 만들어줘").json()
    a = ask(user_id="dev01", question="승인", thread_id=d["thread_id"], approval="approve").json()
    assert a["answer"] == DENY and a["contexts"] == [] and confluence.list_pages() == []
    assert "dev0" not in str(a["trace"]) and len(str(a)) < 1000  # 초안·진척 데이터가 실리지 않는다


def test_approval_response_has_only_publish_result():
    d = ask(question="이번 주 회의 자료 만들어줘").json()
    a = ask(user_id="pl01", question="승인", thread_id=d["thread_id"], approval="approve").json()
    assert "게시했습니다" in a["answer"] and a["contexts"] == []
    assert [t["step"] for t in a["trace"]] == ["guardrail", "supervisor_route", "confluence_publish"]


# --- Y15: 같은 thread의 새 질문은 새 답 ---
def test_followup_question_on_thread_gets_new_answer():
    first = ask(question="프로젝트 전체 진척률 알려줘").json()
    second = ask(question="SCR-A04-05 진척률 알려줘", thread_id=first["thread_id"]).json()
    assert second["thread_id"] == first["thread_id"]
    assert "SCR-A04-05" in second["answer"] and second["answer"] != first["answer"]


def test_followup_keeps_pending_draft_approvable():
    d = ask(question="이번 주 회의 자료 만들어줘").json()
    ask(question="SCR-A04-05 진척률 알려줘", thread_id=d["thread_id"])
    a = ask(question="승인", thread_id=d["thread_id"], approval="approve").json()
    assert "게시했습니다" in a["answer"] and len(confluence.list_pages()) == 1


def test_handler_gets_thread_as_memory_key():
    seen = []

    def h(req, route):
        seen.append(req.thread_id)
        return {"answer": "ok", "contexts": [], "trace": [], "pending_approval": False}
    api.set_handler(h)
    tid = ask().json()["thread_id"]
    ask(thread_id=tid)
    assert seen == [tid, tid]  # 같은 thread는 같은 대화 기억 키
    other = ask().json()["thread_id"]
    assert seen[-1] == other != tid
