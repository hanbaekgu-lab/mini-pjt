"""상태 저장소: STATE_DB(SQLite 파일 경로)를 지정하면 영구 저장, 비우면 메모리(기본, 테스트·평가).

영구 저장 대상 (서버를 재시작해도 남는다):
- 회의 자료 그래프의 checkpointer(SqliteSaver): interrupt()로 멈춘 승인 대기 상태
- 장기 메모리 Store(SqliteStore): 주차별 회의 스냅샷 (다음 주 회의의 '지난주')
- API thread 기록(threads 테이블): 소유자·라우팅·승인 상태·초안
진척 에이전트의 대화 기억도 같은 checkpointer 방식을 따른다. 로그인 세션 토큰은 데모라 메모리에만 둔다(재시작 후 다시 로그인).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading

ENV = "STATE_DB"
_lock = threading.Lock()


def db_path() -> str | None:
    return os.environ.get(ENV) or None


def persistent() -> bool:
    return db_path() is not None


def _connect() -> sqlite3.Connection:
    path = db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    # API 작업 스레드에서도 쓴다(check_same_thread=False). 연결(saver·store·threads)마다 트랜잭션을 오래 쥐지 않게
    # 자동 커밋(isolation_level=None)으로 열고, 다른 연결이 쓰는 중이면 최대 30초 기다린다.
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def checkpointer():
    """LangGraph checkpointer: 영구(SqliteSaver) 또는 메모리(InMemorySaver)."""
    if persistent():
        from langgraph.checkpoint.sqlite import SqliteSaver
        saver = SqliteSaver(_connect())
        saver.setup()
        return saver
    from langgraph.checkpoint.memory import InMemorySaver
    return InMemorySaver()


def store():
    """LangGraph Store: 영구(SqliteStore) 또는 메모리(InMemoryStore)."""
    if persistent():
        from langgraph.store.sqlite import SqliteStore
        st = SqliteStore(_connect())
        st.setup()
        return st
    from langgraph.store.memory import InMemoryStore
    return InMemoryStore()


_threads_conn: sqlite3.Connection | None = None


def _threads_db() -> sqlite3.Connection:
    global _threads_conn
    if _threads_conn is None:
        _threads_conn = _connect()
        _threads_conn.execute("CREATE TABLE IF NOT EXISTS threads (tid TEXT PRIMARY KEY, rec TEXT NOT NULL)")
        _threads_conn.commit()
    return _threads_conn


def load_threads() -> dict:
    """저장된 thread 기록 전부 (메모리 모드면 빈 dict)."""
    if not persistent():
        return {}
    with _lock:
        return {tid: json.loads(rec) for tid, rec in _threads_db().execute("SELECT tid, rec FROM threads")}


def save_thread(tid: str, rec: dict) -> None:
    """thread 기록 저장(덮어쓰기). 메모리 모드면 아무것도 하지 않는다."""
    if not persistent():
        return
    with _lock:
        db = _threads_db()
        db.execute("INSERT INTO threads (tid, rec) VALUES (?, ?) ON CONFLICT(tid) DO UPDATE SET rec = excluded.rec",
                   (tid, json.dumps(rec, ensure_ascii=False)))
        db.commit()


def close() -> None:
    """thread 테이블 연결을 닫는다 (테스트에서 DB 파일을 바꿀 때)."""
    global _threads_conn
    if _threads_conn is not None:
        _threads_conn.close()
        _threads_conn = None
