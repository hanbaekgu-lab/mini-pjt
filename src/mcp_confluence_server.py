"""Confluence MCP 서버 (공식 SDK 계열 fastmcp, stdio).

tool: get_page, create_page, search_pages.
create_page는 Markdown 본문을 받아 서버에서 Confluence 저장 형식(표·작업 목록·info 패널)으로 변환해 게시한다.
CONFLUENCE_MODE=fake(기본)는 로컬 폴더(CONFLUENCE_DIR), real은 .env의 주소·인증 정보로 실제 Confluence에 게시한다.
실행: python -m src.mcp_confluence_server (클라이언트가 자식 프로세스로 띄워 stdio로 접속한다)
"""
from __future__ import annotations

import json

from fastmcp import FastMCP

from src import confluence

mcp = FastMCP("fake-confluence")


@mcp.tool()
def get_page(page_id: str) -> str:
    """page_id로 Confluence 페이지(제목·본문)를 조회한다. 없으면 오류를 낸다.

    Args:
        page_id: 페이지 ID (예: weekly_meeting_2026-11-16)
    """
    page = confluence.get_page(page_id)
    if page is None:
        raise ValueError("page not found")
    return json.dumps(page, ensure_ascii=False)


@mcp.tool()
def create_page(title: str, body: str, page_id: str = "") -> str:
    """Markdown 본문으로 페이지를 만든다. 서버에서 Confluence 저장 형식으로 변환하고, 같은 제목·page_id면 교체한다.

    Args:
        title: 페이지 제목
        body: Markdown 본문
        page_id: 파일명으로 쓸 영문 페이지 ID (비우면 제목에서 만든다)
    """
    return json.dumps(confluence.create_page(title, body, page_id or None), ensure_ascii=False)


@mcp.tool()
def search_pages(query: str) -> str:
    """본문·제목에 키워드가 모두 들어 있는 페이지 목록(page_id, title)을 찾는다.

    Args:
        query: 공백으로 구분한 검색 키워드
    """
    return json.dumps(confluence.search_pages(query), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(show_banner=False)  # 기본 stdio
