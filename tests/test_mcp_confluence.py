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


# --- MCP Resources / Prompts ---
def test_server_exposes_resources_and_prompt():
    got = asyncio.run(mcp_client.list_resources_and_prompts())
    assert got["resources"] == ["confluence://pages"]
    assert got["templates"] == ["confluence://page/{page_id}"]
    assert got["prompts"] == ["weekly_meeting_material"]


def test_read_page_resources():
    import json
    mcp_client.call_tool("create_page", {"title": "주간회의 자료 리소스", "body": "## 결정사항\n- 리소스키워드",
                                         "page_id": "weekly_meeting_res"})
    pages = json.loads(mcp_client.read_resource("confluence://pages"))
    assert {"page_id": "weekly_meeting_res", "title": "주간회의 자료 리소스"} in pages
    body = mcp_client.read_resource("confluence://page/weekly_meeting_res")
    assert body.startswith("# 주간회의 자료 리소스") and body.count("# 주간회의 자료 리소스") == 1 and "리소스키워드" in body
    with pytest.raises(mcp_client.MCPError):
        mcp_client.read_resource("confluence://page/nope")


def test_meeting_material_prompt_matches_render_sections():
    from src import meeting
    text = mcp_client.get_prompt("weekly_meeting_material", {"week_start": "2026-11-16"})
    assert "2026-11-16" in text and all(f"## {s}" in text for s in meeting.MATERIAL_SECTIONS)
    assert "사람 이름" in text and "승인" in text
    body = meeting.build_meeting_material()  # 실제 회의 자료도 같은 섹션 순서를 따른다
    pos = [body.index(f"## {s}") for s in meeting.MATERIAL_SECTIONS]
    assert pos == sorted(pos)
