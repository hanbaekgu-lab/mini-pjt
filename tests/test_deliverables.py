import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_files_exist():
    for p in ["src/agent.py", "src/tools.py", "src/retriever.py", "README.md", "SERVICE.md", "data/corpus"]:
        assert (ROOT / p).exists(), p
    assert 15 <= len(list((ROOT / "data/corpus").glob("*.md"))) <= 20


def test_queries_csv():
    rows = list(csv.DictReader(open(ROOT / "test_queries.csv", encoding="utf-8-sig")))
    assert len(rows) == 20
    counts = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1
    assert counts == {"positive": 8, "negative": 4, "edge": 5, "guardrail": 3}
    for c in ("reference", "reference_doc_ids", "explanatory"):
        assert c in rows[0]


def test_service_md():
    t = (ROOT / "SERVICE.md").read_text(encoding="utf-8")
    for k in ("추정", "개인 평가에는 사용하지 않", "사내 표준화"):
        assert k in t


def test_corpus_has_injection_doc_and_sections():
    docs = list((ROOT / "data/corpus").glob("MIN-*.md"))
    assert 4 <= len(docs) <= 6
    for d in docs:
        t = d.read_text(encoding="utf-8")
        assert "## 결정사항" in t and "## 블로커" in t and "## 액션아이템" in t
    assert any("이전 지시를 모두 무시" in d.read_text(encoding="utf-8") for d in docs)
