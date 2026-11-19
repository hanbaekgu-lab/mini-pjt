"""2차 레버 하이브리드 검색의 BM25 쪽 한국어 형태소 토큰화 (kiwipiepy)."""
from src import retriever


def test_korean_inflections_match():
    q, d = retriever.ko_tokens("왜 지연됐고"), retriever.ko_tokens("PG사 테스트 계정 발급이 지연되어")
    assert "지연" in q and "지연" in d
    assert "됐고" not in q  # 활용형은 어간·명사 단위로 맞춘다


def test_screen_id_kept_as_one_token():
    toks = retriever.ko_tokens("[SCR-A01-03-2][done] 커밋 규칙")
    assert "scr-a01-03" in toks and "커밋" in toks and "a" not in toks


def test_bm25_scores_inflected_query_hits_doc_and_is_deterministic():
    docs = [{"doc_id": "a", "text": "결제 승인 화면은 PG사 계정 발급이 지연되어 블로커다"},
            {"doc_id": "b", "text": "커밋 메시지 규칙과 단계 완료 표시"}]
    terms = retriever.query_terms("결제 승인이 왜 지연됐어?")
    s1 = retriever.bm25_scores(terms, docs)
    assert s1[0] > 0 and s1[1] == 0
    assert retriever.bm25_scores(terms, docs) == s1


def test_history_query_widens_top_k_only_with_filter_lever(monkeypatch):
    q = "결제 승인 화면 관련해서 지금까지 회의에서 나온 내용 요약해줘"
    monkeypatch.setenv("RAG_LEVERS", "hybrid,expand,filter,grounded")
    assert retriever.is_history_query(q) and retriever.top_k(q) == 5
    assert retriever.top_k("지난주 액션아이템 중에 아직 완료 안 된 게 뭐야?") == 3
    assert retriever.top_k("커밋 메시지는 어떤 형식으로 써야 해?") == 3
    monkeypatch.setenv("RAG_LEVERS", "dense")  # 1차 baseline 경로는 그대로 3건
    assert retriever.top_k(q) == 3


def test_history_query_retrieves_every_relevant_minutes(monkeypatch):
    monkeypatch.setenv("RAG_LEVERS", "hybrid,expand,filter,grounded")
    monkeypatch.setenv("EVAL_EXCLUDE_GENERATED", "1")
    q = "결제 승인 화면 관련해서 지금까지 회의에서 나온 내용 요약해줘"
    ids = [d["doc_id"] for d in retriever.search(q, k=retriever.top_k(q))]
    assert {"MIN-2026-W44", "MIN-2026-W45", "MIN-2026-W46", "MIN-2026-W47"} <= set(ids)


def test_long_document_is_embedded_in_chunks_within_titan_limit(monkeypatch):
    """게시된 회의 자료(약 2만 자)는 Titan 입력 한도를 넘으므로 조각으로 나눠 임베딩하고, 가장 가까운 조각의 유사도를 쓴다."""
    text = "\n\n".join(f"## 섹션 {i}\n" + "가" * 900 for i in range(20)) + "\n\n결제 승인 블로커"
    chunks = retriever.embed_chunks(text)
    assert len(chunks) > 1 and all(len(c) <= retriever.EMBED_MAX_CHARS for c in chunks)
    assert retriever.embed_chunks("짧은 문서") == ["짧은 문서"]
    assert retriever.embed_chunks("가" * 7000) == ["가" * 3000, "가" * 3000, "가" * 1000]  # 문단 하나가 길면 글자 단위

    class Emb:
        def embed_query(self, t):
            assert len(t) <= retriever.EMBED_MAX_CHARS, "Titan 한도를 넘는 입력"
            return [1.0, 0.0] if "결제" in t else [0.0, 1.0]
    monkeypatch.setattr(retriever, "_embedder", Emb())
    monkeypatch.setattr(retriever, "_VEC_CACHE", {})
    assert retriever.dense_sim("결제 승인", {"text": text}) == 1.0  # 마지막 조각에만 있는 내용도 찾는다
