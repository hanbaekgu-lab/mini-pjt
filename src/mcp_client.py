"""Confluence MCP 서버(stdio) 호출 클라이언트 (langchain-mcp-adapters MultiServerMCPClient).

나머지 코드는 동기 함수 call_tool(name, arguments)만 쓴다. 안에서 비동기 세션을 열어 도구 하나를 호출하고 닫는다.
호출당 10초 타임아웃이며, 재시도는 middleware.call_with_retry가 맡는다.
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


def call_tool(name: str, arguments: dict, timeout: float = TIMEOUT):
    try:
        return _run(asyncio.wait_for(_acall(name, arguments), timeout))
    except MCPError:
        raise
    except Exception as e:  # noqa: BLE001  타임아웃·서버 기동 실패 등
        raise MCPError(f"MCP 호출 실패/타임아웃: {name}: {type(e).__name__} {e}") from e
