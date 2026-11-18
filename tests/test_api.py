"""POST /query 계약 테스트 (TestClient, API 키 불필요)."""
import pytest
from fastapi.testclient import TestClient

from src import api

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _reset():
    api.THREADS.clear()
    api.set_handler(None)


def body(**kw):
    b = {"question": "이번 주 진척 알려줘", "user_id": "pm01", "role": "pm_pl"}
    b.update(kw)
    return b


@pytest.mark.parametrize("user,role", [("pm01", "pm_pl"), ("dev01", "developer")])
def test_query_returns_answer_contexts_trace(user, role):
    r = client.post("/query", json=body(user_id=user, role=role))
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d["answer"], str) and d["answer"]
    assert isinstance(d["contexts"], list) and d["contexts"]
    assert isinstance(d["trace"], list) and d["trace"]
    assert d["thread_id"]


def test_only_post_query_endpoint():
    paths = {r.path for r in api.app.routes if r.path not in
             ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")}
    assert paths == {"/query"}


@pytest.mark.parametrize("role", ["admin", "", "PM_PL", None])
def test_invalid_role_422(role):
    assert client.post("/query", json=body(role=role)).status_code == 422


@pytest.mark.parametrize("approval", ["yes", "", "APPROVE", 1])
def test_invalid_approval_422(approval):
    assert client.post("/query", json=body(approval=approval)).status_code == 422


def test_missing_fields_422():
    assert client.post("/query", json={"question": "x"}).status_code == 422


def test_unknown_thread_404():
    r = client.post("/query", json=body(thread_id="nope", approval="approve"))
    assert r.status_code == 404


def test_unregistered_user_403():
    r = client.post("/query", json=body(user_id="ghost99"))
    assert r.status_code == 403


def test_known_thread_resume_ok_and_idempotent():
    tid = client.post("/query", json=body()).json()["thread_id"]
    a = client.post("/query", json=body(thread_id=tid, approval="approve"))
    b = client.post("/query", json=body(thread_id=tid, approval="approve"))
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
