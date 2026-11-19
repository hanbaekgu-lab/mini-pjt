"""가짜 Confluence 저장소 (로컬 폴더에 마크다운 쓰기). CONFLUENCE_DIR로 경로 설정.

같은 제목으로 다시 create_page 하면 같은 page_id의 파일을 교체한다(중복 없음).
CONFLUENCE_MODE=real 이면 실제 API 어댑터(별도 모듈)를 써야 하며 여기서는 지원하지 않는다.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path


def confluence_dir() -> Path:
    return Path(os.environ.get("CONFLUENCE_DIR", Path(__file__).resolve().parent.parent / "data" / "confluence_pages"))


def page_id_for(title: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", title).strip("_") or "page"


def create_page(title: str, body: str, page_id: str | None = None) -> dict:
    if os.environ.get("CONFLUENCE_MODE", "fake") == "real":
        raise NotImplementedError("real 모드 어댑터는 별도 구성 필요")
    d = confluence_dir()
    d.mkdir(parents=True, exist_ok=True)
    page_id = page_id or page_id_for(title)
    (d / f"{page_id}.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    return {"page_id": page_id, "title": title, "published_at": datetime.now().isoformat(timespec="seconds")}


def get_page(page_id: str) -> dict | None:
    p = confluence_dir() / f"{page_id_for(page_id)}.md"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    title = text.splitlines()[0].lstrip("# ").strip() if text else page_id
    return {"page_id": p.stem, "title": title, "body": text}


def search_pages(query: str) -> list[dict]:
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
