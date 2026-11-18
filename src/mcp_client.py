"""가짜 Confluence MCP 서버(stdio) 호출 클라이언트. 호출당 10초 타임아웃."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 10.0


class MCPError(Exception):
    pass


def call_tool(name: str, arguments: dict, timeout: float = TIMEOUT):
    proc = subprocess.Popen([sys.executable, "-m", "src.mcp_confluence_server"], cwd=ROOT, env=os.environ.copy(),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    result: dict = {}

    def run():
        try:
            reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}}]
            for r in reqs:
                proc.stdin.write((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
            proc.stdin.flush()
            for _ in range(2):
                result["last"] = json.loads(proc.stdout.readline().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            result["exc"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    if t.is_alive() or "last" not in result:
        raise MCPError(f"MCP 호출 실패/타임아웃: {name}")
    resp = result["last"]
    if "error" in resp:
        raise MCPError(resp["error"]["message"])
    res = resp["result"]
    text = res["content"][0]["text"]
    if res.get("isError"):
        raise MCPError(text)
    return json.loads(text)
