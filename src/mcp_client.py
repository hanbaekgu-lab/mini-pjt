"""Confluence MCP 서버(stdio) 호출 클라이언트 (langchain-mcp-adapters MultiServerMCPClient).

나머지 코드는 동기 함수 call_tool(name, arguments)만 쓴다. 안에서 비동기 세션을 열어 도구 하나를 호출하고 닫는다.
호출당 10초 대기(wait_for)이며, 바깥 타임아웃(15초)·재시도는 middleware.call_with_retry가 맡는다.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 10.0
SERVER = "confluence"


class MCPError(Exception):
    pass


def make_client():
    from langchain_mcp_adapters.client import MultiServerMCPClient
    # stdio 서버는 기본적으로 일부 환경변수만 물려받으므로 CONFLUENCE_* 설정을 전달하려고 env를 명시한다
    return MultiServerMCPClient({SERVER: {"command": sys.executable, "args": ["-m", "src.mcp_confluence_server"],
                                          "transport": "stdio", "cwd": str(ROOT),
                                          "env": {**os.environ, "PYTHONWARNINGS": "ignore"}}})


async def list_tools() -> list:
    return await make_client().get_tools()


async def _acall(name: str, arguments: dict):
    from langchain_core.tools import ToolException
    from langchain_mcp_adapters.tools import load_mcp_tools

    async with make_client().session(SERVER) as session:  # 서버 1회 기동으로 도구 조회와 호출을 함께 처리
        tools = {t.name: t for t in await load_mcp_tools(session, handle_tool_errors=False)}
        if name not in tools:
            raise MCPError(f"unknown tool {name}")
        try:
            out = await tools[name].ainvoke(arguments)
        except ToolException as e:  # 서버 도구 오류(isError=True)
            raise MCPError(str(e)) from e
    text = out if isinstance(out, str) else "".join(b.get("text", "") for b in out if isinstance(b, dict))
    return json.loads(text)


def _run(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(1) as ex:  # 이미 이벤트 루프 안이면 별도 스레드에서 실행
        return ex.submit(asyncio.run, coro).result()


async def _aread_resource(uri: str) -> str:
    async with make_client().session(SERVER) as session:
        res = await session.read_resource(uri)
    return "".join(getattr(c, "text", "") for c in res.contents)


async def _aget_prompt(name: str, arguments: dict) -> str:
    async with make_client().session(SERVER) as session:
        res = await session.get_prompt(name, arguments)
    return "".join(getattr(m.content, "text", "") for m in res.messages)


async def list_resources_and_prompts() -> dict:
    """서버가 노출한 resource(고정 URI·URI 템플릿)와 prompt 이름."""
    async with make_client().session(SERVER) as session:
        res, tpl, prm = (await session.list_resources(), await session.list_resource_templates(),
                         await session.list_prompts())
    return {"resources": [str(r.uri) for r in res.resources], "templates": [t.uriTemplate for t in tpl.resourceTemplates],
            "prompts": [p.name for p in prm.prompts]}


def read_resource(uri: str, timeout: float = TIMEOUT) -> str:
    """MCP resource 읽기 (예: confluence://page/weekly_meeting_2026-11-16). 실패는 MCPError."""
    try:
        return _run(asyncio.wait_for(_aread_resource(uri), timeout))
    except Exception as e:  # noqa: BLE001
        raise MCPError(f"MCP resource 읽기 실패: {uri}: {type(e).__name__} {e}") from e


def get_prompt(name: str, arguments: dict, timeout: float = TIMEOUT) -> str:
    """MCP prompt 텍스트 (예: weekly_meeting_material). 실패는 MCPError."""
    try:
        return _run(asyncio.wait_for(_aget_prompt(name, arguments), timeout))
    except Exception as e:  # noqa: BLE001
        raise MCPError(f"MCP prompt 조회 실패: {name}: {type(e).__name__} {e}") from e


def call_tool(name: str, arguments: dict, timeout: float = TIMEOUT):
    from src import tracing
    tracing.emit("mcp_call", {"tool": name})  # SSE 진행 이벤트 (수신자가 없으면 무시)
    try:
        return _run(asyncio.wait_for(_acall(name, arguments), timeout))
    except MCPError:
        raise
    except Exception as e:  # noqa: BLE001  타임아웃·서버 기동 실패 등
        raise MCPError(f"MCP 호출 실패/타임아웃: {name}: {type(e).__name__} {e}") from e
