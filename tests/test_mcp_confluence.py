"""가짜 Confluence MCP 서버(fastmcp, stdio): tool 스키마, 게시/조회/검색, 재게시 시 교체, page_id 지정."""
import asyncio
from pathlib import Path

import pytest

from src import mcp_client

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_DIR", str(tmp_path / "pages"))


def test_stdio_server_lists_three_tools():
    # 실제 stdio 서버(fastmcp)를 띄우고 langchain-mcp-adapters로 도구 목록을 받는다
    tools = asyncio.run(mcp_client.list_tools())
    assert {t.name for t in tools} == {"get_page", "create_page", "search_pages"}
    create = next(t for t in tools if t.name == "create_page")
    assert {"title", "body", "page_id"} <= set(create.args)


def test_create_get_search_roundtrip():
    page = mcp_client.call_tool("create_page", {"title": "테스트 페이지", "body": "## 결정사항\n- 알파벳키워드"})
    got = mcp_client.call_tool("get_page", {"page_id": page["page_id"]})
    assert "알파벳키워드" in got["body"] and got["title"] == "테스트 페이지"
    hits = mcp_client.call_tool("search_pages", {"query": "알파벳키워드"})
    assert [h["page_id"] for h in hits] == [page["page_id"]]
    assert mcp_client.call_tool("search_pages", {"query": "없는단어"}) == []


def test_republish_replaces_not_duplicates():
    a = mcp_client.call_tool("create_page", {"title": "같은 제목", "body": "v1"})
    b = mcp_client.call_tool("create_page", {"title": "같은 제목", "body": "v2"})
    assert a["page_id"] == b["page_id"]
    assert mcp_client.call_tool("get_page", {"page_id": a["page_id"]})["body"].count("v2") == 1
    assert len(mcp_client.call_tool("search_pages", {"query": "같은"})) == 1


def test_get_missing_page_errors():
    with pytest.raises(mcp_client.MCPError):
        mcp_client.call_tool("get_page", {"page_id": "nope"})


def test_page_id_arg_and_storage_conversion(tmp_path):
    page = mcp_client.call_tool("create_page", {"title": "주간회의 자료", "body": "| a | b |\n|---|---|\n| 1 | 2 |",
                                                "page_id": "weekly_meeting_test"})
    assert page["page_id"] == "weekly_meeting_test"
    xml = (tmp_path / "pages" / "weekly_meeting_test.storage.xml").read_text(encoding="utf-8")
    assert "<table" in xml  # 서버 쪽 Markdown → Confluence 저장 형식 변환
