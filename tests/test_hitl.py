"""HITL 승인 흐름 테스트: 승인 없이는 게시 없음, PM·PL만 승인(개발자 불가), 멱등."""
import pytest
from fastapi.testclient import TestClient

from src import api, confluence

client = TestClient(api.app)
Q = "이번 주 회의 자료 초안 만들어줘"


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_DIR", str(tmp_path / "pages"))
    api.THREADS.clear()
    api.set_handler(None)


def post(**kw):
    b = {"question": Q, "user_id": "pm01", "role": "pm"}
    b.update(kw)
    return client.post("/query", json=b)


def pending():
    d = post().json()
    assert d["pending_approval"] is True and d["thread_id"]
    return d["thread_id"]


def test_pending_does_not_publish():
    pending()
    assert confluence.list_pages() == []


def test_approve_publishes():
    tid = pending()
    d = post(question="승인", thread_id=tid, approval="approve").json()
    assert d["pending_approval"] is False
    assert len(confluence.list_pages()) == 1
    assert any(t["step"] == "confluence_publish" for t in d["trace"])


def test_reject_does_not_publish():
    tid = pending()
    d = post(question="반려", thread_id=tid, approval="reject").json()
    assert d["pending_approval"] is False
    assert confluence.list_pages() == []


def test_developer_cannot_approve():
    tid = pending()
    r = post(user_id="dev01", role="developer", thread_id=tid, approval="approve")
    assert r.status_code == 200
    assert r.json()["answer"] == "해당 정보는 조회 권한이 없습니다"
    assert confluence.list_pages() == []
    # 여전히 승인 대기 → PM·PL은 이후 승인 가능
    assert post(thread_id=tid, approval="approve").json()["pending_approval"] is False
    assert len(confluence.list_pages()) == 1


def test_developer_cannot_create_draft():
    d = post(user_id="dev01", role="developer").json()
    assert d["pending_approval"] is False
    assert d["answer"] == "해당 정보는 조회 권한이 없습니다"


def test_duplicate_approval_idempotent():
    tid = pending()
    a = post(thread_id=tid, approval="approve").json()
    files = confluence.list_pages()
    b = post(thread_id=tid, approval="approve").json()
    c = post(thread_id=tid, approval="reject").json()
    assert a == b == c
    assert confluence.list_pages() == files and len(files) == 1


def test_duplicate_reject_then_approve_stays_rejected():
    tid = pending()
    a = post(thread_id=tid, approval="reject").json()
    b = post(thread_id=tid, approval="approve").json()
    assert a == b and confluence.list_pages() == []


def test_unknown_thread_404():
    assert post(thread_id="nope", approval="approve").status_code == 404


def test_pl_can_create_and_approve():
    d = post(user_id="pl02", role="pl").json()
    assert d["pending_approval"] is True and confluence.list_pages() == []
    ok = post(user_id="pl02", role="pl", thread_id=d["thread_id"], approval="approve").json()
    assert ok["pending_approval"] is False and "게시했습니다" in ok["answer"]
    assert len(confluence.list_pages()) == 1


def test_pl_can_approve_pm_draft():
    tid = pending()
    assert post(user_id="pl01", role="pl", thread_id=tid, approval="approve").json()["pending_approval"] is False
    assert len(confluence.list_pages()) == 1
