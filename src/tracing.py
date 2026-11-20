"""Trace 기록: LangSmith 키가 있으면 시도하고, 없거나 실패하면 로컬 JSON(logs/trace/)로 남긴다.

요청 단위 LLM 사용량(호출 수·지연시간·토큰)은 콜백(UsageHandler)이 모아 trace 끝에 llm_usage 단계로 붙인다.
요청마다 contextvar에 집계용 dict를 두므로 동시 요청끼리 섞이지 않는다(작업 스레드는 middleware가 컨텍스트를 복사해 넘긴다).
"""
from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

_usage: contextvars.ContextVar[dict | None] = contextvars.ContextVar("llm_usage", default=None)


_events: contextvars.ContextVar = contextvars.ContextVar("progress_events", default=None)


def start_events(sink) -> contextvars.Token:
    """진행 이벤트 수신자를 등록한다 (SSE 스트림용). sink(kind, data)."""
    return _events.set(sink)


def end_events(token: contextvars.Token) -> None:
    _events.reset(token)


def emit(kind: str, data: dict | None = None) -> None:
    """진행 이벤트를 보낸다. 수신자가 없으면(일반 /query) 아무것도 하지 않는다. 이벤트 실패는 처리에 영향을 주지 않는다."""
    sink = _events.get()
    if sink is not None:
        try:
            sink(kind, data or {})
        except Exception:  # noqa: BLE001
            pass


def start_usage() -> contextvars.Token:
    """요청 시작: 이 컨텍스트(와 복사된 작업 스레드)의 LLM 사용량 집계를 새로 연다."""
    return _usage.set({"calls": 0, "errors": 0, "latency": [], "input_tokens": 0, "output_tokens": 0,
                       "_start": {}, "_lock": threading.Lock()})


def end_usage(token: contextvars.Token) -> dict | None:
    """요청 끝: 집계 요약을 돌려주고 컨텍스트를 되돌린다. LLM 호출이 없었으면 None."""
    acc = _usage.get()
    _usage.reset(token)
    if not acc or not (acc["calls"] or acc["errors"]):
        return None
    lat = acc["latency"]
    return {"llm_calls": acc["calls"], "llm_errors": acc["errors"], "latency_s": round(sum(lat), 2),
            "max_latency_s": round(max(lat), 2) if lat else 0.0,
            "input_tokens": acc["input_tokens"], "output_tokens": acc["output_tokens"]}


class UsageHandler(BaseCallbackHandler):
    """LLM 호출마다 지연시간과 토큰 사용량을 현재 요청의 집계에 더한다 (집계가 없으면 아무것도 하지 않는다)."""

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        emit("llm_start")
        acc = _usage.get()
        if acc is not None:
            acc["_start"][run_id] = time.perf_counter()

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage, latency = {}, None
        try:
            usage = response.generations[0][0].message.usage_metadata or {}
        except (AttributeError, IndexError):
            pass
        acc = _usage.get()
        if acc is not None:
            with acc["_lock"]:
                acc["calls"] += 1
                started = acc["_start"].pop(run_id, None)
                if started is not None:
                    latency = time.perf_counter() - started
                    acc["latency"].append(latency)
                acc["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
                acc["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        emit("llm_end", {"latency_s": round(latency, 2) if latency is not None else None,
                         "output_tokens": int(usage.get("output_tokens", 0) or 0)})

    def on_llm_error(self, error, *, run_id, **kwargs):
        acc = _usage.get()
        if acc is not None:
            with acc["_lock"]:
                acc["errors"] += 1
                acc["_start"].pop(run_id, None)


USAGE_HANDLER = UsageHandler()


def trace_dir() -> Path:
    return Path(os.environ.get("TRACE_DIR", Path(__file__).resolve().parent.parent / "logs" / "trace"))


def record(thread_id: str, question: str, route: str, trace: list[dict]) -> str:
    """기록 위치(backend)를 반환한다. trace는 이미 PII 마스킹된 값이어야 한다."""
    if os.environ.get("LANGSMITH_API_KEY"):
        try:
            from langsmith import Client  # type: ignore
            Client().create_run(name="query", run_type="chain", inputs={"question": question},
                                outputs={"route": route, "trace": trace})
            return "langsmith"
        except Exception:  # noqa: BLE001
            pass
    d = trace_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{thread_id}-{int(time.time() * 1000)}.json"
    p.write_text(json.dumps({"thread_id": thread_id, "question": question, "route": route, "trace": trace},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return "local"
