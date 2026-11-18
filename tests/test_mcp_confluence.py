"""가짜 Confluence MCP 서버(stdio): tool 스키마, 게시/조회/검색, 재게시 시 교체."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import mcp_client

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_DIR", str(tmp_path / "pages"))


def test_stdio_server_lists_three_tools():
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    out = subprocess.run([sys.executable, "-m", "src.mcp_confluence_server"], cwd=ROOT, env=os.environ.copy(),
                         input="\n".join(json.dumps(r) for r in reqs) + "\n", capture_output=True, text=True,
                         encoding="utf-8", timeout=20).stdout.strip().splitlines()
    tools = json.loads(out[1])["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_page", "create_page", "search_pages"}


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
