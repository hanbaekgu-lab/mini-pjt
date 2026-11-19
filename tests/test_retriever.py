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
