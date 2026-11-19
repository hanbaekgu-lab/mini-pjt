"""검색 대상 관리. 평가용 코퍼스(data/corpus)와 런타임 인덱스(게시된 회의 자료)를 분리한다.

- corpus_documents(): data/corpus 고정 스냅샷만 (평가용, generated 제외)
- 게시된 회의 자료는 data/generated/ 에 저장하고 런타임 인덱스에만 추가. 같은 page_id는 교체.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_INDEX: dict[str, dict] = {}


def corpus_dir() -> Path:
    return Path(os.environ.get("CORPUS_DIR", ROOT / "data" / "corpus"))


def generated_dir() -> Path:
    if os.environ.get("GENERATED_DIR"):
        return Path(os.environ["GENERATED_DIR"])
    if os.environ.get("CONFLUENCE_DIR"):
        return Path(os.environ["CONFLUENCE_DIR"]).parent / "generated"
    return ROOT / "data" / "generated"


def _read_dir(d: Path, source: str) -> list[dict]:
    if not d.exists():
        return []
    return [{"doc_id": p.stem, "text": p.read_text(encoding="utf-8"), "source": source}
            for p in sorted(d.glob("*.md"))]


def corpus_documents() -> list[dict]:
    """평가용 코퍼스. 게시된 회의 자료(generated)는 포함하지 않는다."""
    return _read_dir(corpus_dir(), "corpus")


def index_generated(page_id: str, title: str, body: str) -> dict:
    """게시된 회의 자료를 data/generated에 저장하고 런타임 인덱스에 추가(같은 page_id는 교체)."""
    d = generated_dir()
    d.mkdir(parents=True, exist_ok=True)
    text = f"# {title}\n\n{body}\n"
    (d / f"{page_id}.md").write_text(text, encoding="utf-8")
    doc = {"doc_id": page_id, "text": text, "source": "generated"}
    RUNTIME_INDEX[page_id] = doc
    return doc


def runtime_documents() -> list[dict]:
    return corpus_documents() + list(RUNTIME_INDEX.values())


# ---------------------------------------------------------------------------
# 검색: dense-only baseline(1차) vs 하이브리드(BM25+dense+리랭킹)+쿼리 확장+메타데이터 필터(2차)
# RAG_LEVERS 환경변수(쉼표 구분)로 켜는 레버를 정한다.
#   dense | hybrid | expand | filter | grounded.  기본(운영)=전체, 1차 baseline="dense"
# ---------------------------------------------------------------------------
import math
from collections import Counter

ALL_LEVERS = ("hybrid", "expand", "filter", "grounded")
STOP = {"어떤", "어떻게", "뭐야", "알려줘", "알려", "있어", "해줘", "하기로", "했어", "어느", "정도", "이번", "지난",
        "우리", "회사", "관련", "해서", "에서", "내용", "돼", "되나", "얼마", "무엇", "누가", "나", "내가", "것", "게",
        "어디", "다음", "왜", "뭐", "지난주", "중에", "아직", "지금", "나온", "관련해서", "요약해줘", "요약"}
PARTICLES = ("에서", "으로", "이랑", "까지", "하고", "에게", "가", "이", "는", "은", "를", "을", "에", "의", "도", "로", "랑", "과", "와", "야", "고")
EXPAND = {  # 질문 모호 → 쿼리 확장 (동의어/문서 섹션 용어)
    "지연": ["블로커", "지연"], "왜": ["블로커"], "조치": ["결정사항", "액션아이템"],
    "형식": ["커밋", "규칙"], "규칙": ["규칙"], "액션아이템": ["액션아이템"],
    "완료조건": ["완료 조건"], "조건": ["완료 조건"], "결정": ["결정사항"],
}


def levers() -> set[str]:
    raw = os.environ.get("RAG_LEVERS")
    if raw is None:
        return set(ALL_LEVERS)
    return {x.strip() for x in raw.split(",") if x.strip()}


def _words(s: str) -> list[str]:
    return re.findall(r"SCR-[A-Z]\d{2}-\d{2}|[0-9A-Za-z가-힣_]+", s)


def _stem(w: str) -> str:
    if w.startswith("SCR-"):
        return w
    for p in PARTICLES:
        if w.endswith(p) and len(w) > len(p) + 1:
            return w[: -len(p)]
    return w


def query_terms(query: str, expand: bool = False) -> list[str]:
    terms = []
    for w in _words(query):
        w = _stem(w)
        if w in STOP or len(w) < 2 and not w.isdigit():
            continue
        terms.append(w)
    if expand:
        for t in list(terms):
            for key, extra in EXPAND.items():
                if key in t:
                    terms += extra
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _bigrams(s: str) -> Counter:
    s = re.sub(r"\s+", "", s.lower())
    return Counter(s[i:i + 2] for i in range(len(s) - 1))


def _cos(a: Counter, b: Counter) -> float:
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


# 벡터 임베더: 운영·평가는 Bedrock Titan(set_embedder), 테스트는 기본 문자 bigram(오프라인)
_embedder = None
_VEC_CACHE: dict[str, list[float]] = {}


def set_embedder(embeddings) -> None:
    global _embedder
    _embedder = embeddings


def _vec(text: str) -> list[float]:
    if text not in _VEC_CACHE:  # 같은 문서·질의는 한 번만 임베딩(비용 절감)
        _VEC_CACHE[text] = _embedder.embed_query(text)
    return _VEC_CACHE[text]


def dense_sim(query: str, doc: dict) -> float:
    if _embedder is None:
        return _cos(_bigrams(query), _bigrams(doc["text"]))
    a, b = _vec(query), _vec(doc["text"])
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def dense_search(query: str, docs: list[dict], k: int = 3) -> list[tuple[float, dict]]:
    """dense-only baseline: 문서 전체를 벡터로 임베딩해 코사인 유사도 상위 k개."""
    scored = [(dense_sim(query, d), d) for d in docs]
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def bm25_scores(terms: list[str], docs: list[dict], k1: float = 1.5, b: float = 0.75) -> list[float]:
    texts = [d["text"].lower() for d in docs]
    avg = sum(len(t) for t in texts) / max(len(texts), 1)
    n = len(docs)
    scores = [0.0] * n
    for term in terms:
        tl = term.lower()
        df = sum(1 for t in texts if tl in t)
        if not df:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for i, t in enumerate(texts):
            tf = t.count(tl)
            if tf:
                scores[i] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(t) / avg))
    return scores


def doc_meta(doc: dict) -> dict:
    """주차·화면 ID·유형 메타데이터."""
    m = re.search(r"\((\d{4}-\d{2}-\d{2})\)", doc["text"].splitlines()[0]) if doc["text"] else None
    kind = ("회의록" if doc["doc_id"].startswith("MIN-") else "화면정의서" if doc["doc_id"].startswith("SCR-DEF")
            else "팀규칙" if doc["doc_id"].startswith("RULE-") else "일정" if doc["doc_id"].startswith("PLAN-")
            else "Confluence회의자료")
    return {"date": m.group(1) if m else None, "kind": kind, "screens": set(_screen_ids(doc["text"]))}


def _screen_ids(text: str) -> list[str]:
    from src import tools
    return tools.SCREEN_ID_RE.findall(text)


def _week_start(date: str):
    from datetime import date as D, timedelta
    d = D.fromisoformat(date)
    return d - timedelta(days=d.weekday())


def _target_week(query: str):
    """'지난주/지난 회의' → 기준 주(fixture) 직전 주의 월요일, 그 외 None."""
    from datetime import date as D, timedelta
    if re.search(r"지난\s*(주|회의)|저번", query):
        ref = json_ref_start()
        return D.fromisoformat(ref) - timedelta(days=7)
    return None


def json_ref_start() -> str:
    from src import tools
    return tools.load_reference_week()["start"]


def search_scored(query: str, k: int = 3, docs: list[dict] | None = None) -> list[tuple[float, dict]]:
    docs = docs if docs is not None else runtime_documents()
    lv = levers()
    if "hybrid" not in lv:
        return dense_search(query, docs, k)
    terms = query_terms(query, expand="expand" in lv)
    bm = bm25_scores(terms, docs)
    dn = [dense_sim(query, d) for d in docs]
    def rank(vals):
        order = sorted(range(len(vals)), key=lambda i: -vals[i])
        return {i: r for r, i in enumerate(order)}
    rb, rd = rank(bm), rank(dn)
    fused = [1 / (60 + rb[i]) + 1 / (60 + rd[i]) for i in range(len(docs))]
    # 리랭킹: 질의 용어 커버리지 + 화면 ID 일치
    screen_ids = set(_screen_ids(query))
    scored = []
    for i, d in enumerate(docs):
        meta = doc_meta(d)
        cov = sum(1 for t in terms if t.lower() in d["text"].lower()) / max(len(terms), 1)
        sc = fused[i] + 0.05 * cov + (0.05 if screen_ids & meta["screens"] else 0)
        if "filter" in lv:
            tw = _target_week(query)
            if tw is not None and meta["kind"] == "회의록" and meta["date"]:
                if _week_start(meta["date"]) != tw:
                    sc *= 0.2  # 주차 혼입 방지
            if screen_ids and meta["kind"] != "팀규칙" and not (screen_ids & meta["screens"]):
                sc *= 0.3
            if re.search(r"완료\s*조건|정의서", query) and meta["kind"] == "화면정의서":
                sc *= 1.5
            if re.search(r"규칙|형식|양식|템플릿|가이드", query) and meta["kind"] == "팀규칙":
                sc *= 1.5
        scored.append((sc, d, bm[i], cov))
    scored.sort(key=lambda x: -x[0])
    return [(s, d) for s, d, _, _ in scored[:k]]


def coverage(query: str, doc: dict) -> float:
    terms = query_terms(query, expand="expand" in levers())
    if not terms:
        return 0.0
    return sum(1 for t in terms if t.lower() in doc["text"].lower()) / len(terms)


def search(query: str, k: int = 5) -> list[dict]:
    if "hybrid" not in levers():
        return [d for _, d in dense_search(query, runtime_documents(), k)]
    q = _tokens(query)
    hits = [d for s, d in search_scored(query, k) if coverage(query, d) > 0 or q & _tokens(d["text"])]
    return hits


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[0-9A-Za-z가-힣_-]+", s.lower()))
