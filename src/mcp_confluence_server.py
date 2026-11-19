"""Confluence MCP 서버 (stdio, 줄 단위 JSON-RPC 2.0).

tool 스키마: get_page, create_page, search_pages.
create_page는 Markdown 본문을 받아 서버에서 Confluence 저장 형식(표·작업 목록·info 패널)으로 변환해 게시한다.
CONFLUENCE_MODE=fake(기본)는 로컬 폴더(CONFLUENCE_DIR), real은 .env의 주소·인증 정보로 실제 Confluence에 게시한다.
실행: python -m src.mcp_confluence_server
"""
from __future__ import annotations

import json
import sys

from src import confluence

TOOLS = [
    {"name": "get_page", "description": "page_id로 페이지 조회",
     "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string"}}, "required": ["page_id"]}},
    {"name": "create_page", "description": "Markdown 본문으로 페이지 생성(서버에서 Confluence 형식으로 변환, 같은 제목이면 교체)",
     "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"},
                                                    "page_id": {"type": "string"}},
                     "required": ["title", "body"]}},
    {"name": "search_pages", "description": "본문/제목 키워드 검색",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
]


def call_tool(name: str, args: dict):
    if name == "get_page":
        page = confluence.get_page(args["page_id"])
        if page is None:
            raise KeyError("page not found")
        return page
    if name == "create_page":
        return confluence.create_page(args["title"], args["body"], args.get("page_id"))
    if name == "search_pages":
        return confluence.search_pages(args["query"])
    raise KeyError(f"unknown tool {name}")


def handle(msg: dict) -> dict | None:
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-confluence", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params", {})
            data = call_tool(p["name"], p.get("arguments", {}))
            result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": False}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    except Exception as e:
        if method == "tools/call":
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        resp = handle(json.loads(line))
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
