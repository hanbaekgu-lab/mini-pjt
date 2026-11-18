"""게시된 회의 자료: 섹션, 도구 수치 일치, MCP 재조회, 런타임 검색 대상, 재게시 교체, 코퍼스 제외."""
import pytest
from fastapi.testclient import TestClient

from src import api, mcp_client, meeting, retriever, tools

client = TestClient(api.app)
Q = "이번 주 회의 자료 초안 만들어줘"


@pytest.fixture(autouse=True)
def _reset(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_DIR", str(tmp_path / "pages"))
    monkeypatch.setenv("GENERATED_DIR", str(tmp_path / "generated"))
    api.THREADS.clear()
    api.set_handler(None)
    retriever.RUNTIME_INDEX.clear()
    yield
    retriever.RUNTIME_INDEX.clear()


def post(**kw):
    b = {"question": Q, "user_id": "pm01", "role": "pm_pl"}
    b.update(kw)
    return client.post("/query", json=b).json()


def publish():
    tid = post()["thread_id"]
    d = post(thread_id=tid, approval="approve")
    assert d["pending_approval"] is False
    page_id = next(t["output"] for t in d["trace"] if t["step"] == "confluence_publish")
    return page_id


def test_material_has_sections_and_matching_numbers():
    body = meeting.build_meeting_material()
    for sec in ("## 결정사항", "## 블로커", "## 액션아이템"):
        assert sec in body
    rep = tools.build_progress_report()
    team = rep["progress"]
    assert f"진척률 {team['progress']*100:.2f}%" in body
    assert f"남은 {team['remaining_mm']:.2f}MM" in body
    for t in team["tasks"]:
        assert f"{t['task_id']} {t['title']}" in body
        assert f"진척률 {t['progress']*100:.2f}%, 남은 {t['remaining_mm']:.2f}MM" in body


def test_published_page_retrievable_via_mcp():
    pid = publish()
    page = mcp_client.call_tool("get_page", {"page_id": pid})
    assert "## 결정사항" in page["body"] and "## 액션아이템" in page["body"]
    team = tools.build_progress_report()["progress"]
    assert f"진척률 {team['progress']*100:.2f}%" in page["body"]
    assert pid in [h["page_id"] for h in mcp_client.call_tool("search_pages", {"query": "블로커"})]


def test_published_page_in_runtime_index_not_in_corpus():
    pid = publish()
    assert pid in [d["doc_id"] for d in retriever.runtime_documents()]
    assert any(d["doc_id"] == pid for d in retriever.search("주간회의 자료 액션아이템"))
    assert pid not in [d["doc_id"] for d in retriever.corpus_documents()]
    assert all(d["source"] == "corpus" for d in retriever.corpus_documents())


def test_republish_replaces_without_duplicates():
    p1 = publish()
    api.THREADS.clear()
    p2 = publish()
    assert p1 == p2
    assert [d["doc_id"] for d in retriever.runtime_documents()].count(p1) == 1
    assert len(mcp_client.call_tool("search_pages", {"query": "결정사항"})) == 1
    assert len(list(retriever.generated_dir().glob("*.md"))) == 1
