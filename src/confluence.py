"""Confluence 저장소 (MCP 서버가 사용하는 백엔드).

- CONFLUENCE_MODE=fake (기본): 로컬 폴더(CONFLUENCE_DIR)에 Markdown을 쓴다. 변환 결과 미리보기(.storage.xml)도 함께 저장한다.
- CONFLUENCE_MODE=real: 실제 Confluence REST API(v1)에 저장 형식(storage)으로 게시한다.
  주소·스페이스·인증 정보는 .env에서만 읽는다(.env.example의 mock 값을 실제 값으로 교체).
같은 제목/page_id로 다시 create_page 하면 기존 페이지를 교체한다(중복 없음).
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from src.confluence_convert import md_to_storage

REAL_SETTINGS = ("CONFLUENCE_BASE_URL", "CONFLUENCE_SPACE_KEY", "CONFLUENCE_USER", "CONFLUENCE_API_TOKEN")


def mode() -> str:
    return os.environ.get("CONFLUENCE_MODE", "fake")


def confluence_dir() -> Path:
    return Path(os.environ.get("CONFLUENCE_DIR", Path(__file__).resolve().parent.parent / "data" / "confluence_pages"))


def page_id_for(title: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", title).strip("_") or "page"


# ----------------------------------------------------------------------------- 공개 함수 (MCP tool)
def create_page(title: str, body: str, page_id: str | None = None) -> dict:
    if mode() == "real":
        return _real_create(title, body)
    d = confluence_dir()
    d.mkdir(parents=True, exist_ok=True)
    page_id = page_id or page_id_for(title)
    (d / f"{page_id}.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    (d / f"{page_id}.storage.xml").write_text(md_to_storage(body), encoding="utf-8")  # 실제 게시 시 보낼 형태 미리보기
    return {"page_id": page_id, "title": title, "published_at": datetime.now().isoformat(timespec="seconds")}


def get_page(page_id: str) -> dict | None:
    if mode() == "real":
        return _real_get(page_id)
    p = confluence_dir() / f"{page_id_for(page_id)}.md"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    title = text.splitlines()[0].lstrip("# ").strip() if text else page_id
    return {"page_id": p.stem, "title": title, "body": text}


def search_pages(query: str) -> list[dict]:
    if mode() == "real":
        return _real_search(query)
    terms = [t for t in query.split() if t]
    out = []
    for pid in list_pages():
        page = get_page(pid)
        if page and all(t.lower() in page["body"].lower() for t in terms):
            out.append({"page_id": pid, "title": page["title"]})
    return out


def list_pages() -> list[str]:
    d = confluence_dir()
    return sorted(p.stem for p in d.glob("*.md")) if d.exists() else []


# ----------------------------------------------------------------------------- 실제 Confluence (REST API v1)
def real_settings() -> dict:
    missing = [k for k in REAL_SETTINGS if not os.environ.get(k)]
    if missing:  # 값은 출력하지 않고 빠진 변수 이름만 알린다
        raise RuntimeError(f"CONFLUENCE_MODE=real 설정 누락: {', '.join(missing)} (.env.example 참고)")
    s = {k: os.environ[k] for k in REAL_SETTINGS}
    s["CONFLUENCE_PARENT_PAGE_ID"] = os.environ.get("CONFLUENCE_PARENT_PAGE_ID", "")
    return s


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    s = real_settings()
    token = base64.b64encode(f"{s['CONFLUENCE_USER']}:{s['CONFLUENCE_API_TOKEN']}".encode()).decode()
    req = urllib.request.Request(
        s["CONFLUENCE_BASE_URL"].rstrip("/") + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode() or "{}")


def _real_create(title: str, body: str) -> dict:
    s = real_settings()
    storage = {"storage": {"value": md_to_storage(body), "representation": "storage"}}
    q = urllib.parse.urlencode({"spaceKey": s["CONFLUENCE_SPACE_KEY"], "title": title, "expand": "version"})
    found = _request("GET", f"/rest/api/content?{q}").get("results", [])
    if found:  # 같은 제목이 있으면 버전을 올려 교체
        page = found[0]
        res = _request("PUT", f"/rest/api/content/{page['id']}",
                       {"type": "page", "title": title, "version": {"number": page["version"]["number"] + 1},
                        "body": storage})
    else:
        payload = {"type": "page", "title": title, "space": {"key": s["CONFLUENCE_SPACE_KEY"]}, "body": storage}
        if s["CONFLUENCE_PARENT_PAGE_ID"]:
            payload["ancestors"] = [{"id": s["CONFLUENCE_PARENT_PAGE_ID"]}]
        res = _request("POST", "/rest/api/content", payload)
    url = s["CONFLUENCE_BASE_URL"].rstrip("/") + res.get("_links", {}).get("webui", "")
    return {"page_id": str(res["id"]), "title": title, "url": url,
            "published_at": datetime.now().isoformat(timespec="seconds")}


def _real_get(page_id: str) -> dict | None:
    res = _request("GET", f"/rest/api/content/{urllib.parse.quote(page_id)}?expand=body.storage")
    if not res.get("id"):
        return None
    return {"page_id": str(res["id"]), "title": res["title"], "body": res["body"]["storage"]["value"]}


def _real_search(query: str) -> list[dict]:
    s = real_settings()
    cql = f'space = "{s["CONFLUENCE_SPACE_KEY"]}" and type = page and text ~ "{query}"'
    res = _request("GET", "/rest/api/content/search?" + urllib.parse.urlencode({"cql": cql}))
    return [{"page_id": str(r["id"]), "title": r["title"]} for r in res.get("results", [])]
