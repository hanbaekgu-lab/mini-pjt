"""Trace 기록: LangSmith 키가 있으면 시도하고, 없거나 실패하면 로컬 JSON(logs/trace/)로 남긴다."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


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
