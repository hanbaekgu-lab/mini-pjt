"""미들웨어: PII 마스킹 (이메일·전화번호). 마스킹 로직은 이 모듈 한 곳에서만 처리한다."""
from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE_RE = re.compile(r"(?<![\d-])(?:\+82[- ]?)?0\d{1,2}[-. ]?\d{3,4}[-. ]?\d{4}(?![\d-])")


def _mask_email(m: re.Match) -> str:
    local, domain = m.group(0).split("@", 1)
    return f"{local[0]}***@{domain}"


def _mask_phone(m: re.Match) -> str:
    s = m.group(0)
    return re.sub(r"\d(?=(?:\D*\d){4})", "*", s)  # 마지막 4자리만 남김


def mask_pii(text: str) -> str:
    return PHONE_RE.sub(_mask_phone, EMAIL_RE.sub(_mask_email, text))


def mask_obj(obj):
    """문자열/리스트/딕셔너리를 재귀적으로 마스킹한다 (answer·contexts·trace 공용)."""
    if isinstance(obj, str):
        return mask_pii(obj)
    if isinstance(obj, list):
        return [mask_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: mask_obj(v) for k, v in obj.items()}
    return obj


# --- 재시도/타임아웃 미들웨어: MCP 호출은 10초, 에이전트(LLM) 처리 전체는 180초 타임아웃, 최대 2회 재시도 ---
import threading

TIMEOUT = 10.0
LLM_TIMEOUT = 180.0
MAX_RETRIES = 2


class CallFailed(Exception):
    """재시도 후에도 실패한 호출."""


def call_with_retry(fn, *args, timeout: float | None = None, retries: int | None = None, **kwargs):
    timeout = TIMEOUT if timeout is None else timeout
    retries = MAX_RETRIES if retries is None else retries
    last: Exception | None = None
    for _ in range(retries + 1):
        box: dict = {}

        def run():
            try:
                box["v"] = fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                box["e"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            last = TimeoutError(f"{timeout}초 타임아웃")
        elif "e" in box:
            last = box["e"]
        else:
            return box["v"]
    raise CallFailed(f"{getattr(fn, '__name__', 'call')} 실패(재시도 {retries}회 후): {last}")
