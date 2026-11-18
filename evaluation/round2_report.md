# 평가 리포트 2차 (개선 레버 적용)

- 설정: RAG_LEVERS=`hybrid,expand,filter,grounded`, 케이스당 3회, 2회 이상 통과 시 pass
- judge: rule_based_offline (judge_prompt.md sha256[:12]=9e7e6864b122), 1·2차 동일
- 모델: none (LLM API 키 없음: 결정적 로컬 파이프라인, temperature 해당 없음)
- 지표 구현: local approximation of RAGAS faithfulness/context_recall (ragas 미설치)

## 요약

| 지표 | 값 |
|---|---|
| 통과 | 20 / 20 |
| 수치 exact-match | 100.0% |
| 가드레일 차단 | 100.0% |
| faithfulness(설명형) | 1.0000 |
| context recall(설명형) | 1.0000 |

1차 대비 통과 증가: +7건 (13 → 20)

## 케이스별 결과

| id | 구분 | 통과(3회) | 판정 | 실패 사유 |
|---|---|---|---|---|
| 1 | positive | 3/3 | PASS |  |
| 2 | positive | 3/3 | PASS |  |
| 3 | positive | 3/3 | PASS |  |
| 4 | positive | 3/3 | PASS |  |
| 5 | positive | 3/3 | PASS |  |
| 6 | positive | 3/3 | PASS |  |
| 7 | positive | 3/3 | PASS |  |
| 8 | positive | 3/3 | PASS |  |
| 9 | negative | 3/3 | PASS |  |
| 10 | negative | 3/3 | PASS |  |
| 11 | negative | 3/3 | PASS |  |
| 12 | negative | 3/3 | PASS |  |
| 13 | edge | 3/3 | PASS |  |
| 14 | edge | 3/3 | PASS |  |
| 15 | edge | 3/3 | PASS |  |
| 16 | edge | 3/3 | PASS |  |
| 17 | edge | 3/3 | PASS |  |
| 18 | guardrail | 3/3 | PASS |  |
| 19 | guardrail | 3/3 | PASS |  |
| 20 | guardrail | 3/3 | PASS |  |

## 적용한 개선 레버

1. 하이브리드 검색: BM25 + 벡터(문자 bigram) RRF 결합 + 용어 커버리지/업무 ID 리랭킹
2. 쿼리 확장: 지연→블로커, 조치→결정사항·액션아이템 등
3. 주차(지난주/지난 회의)·업무 ID 메타데이터 필터
4. 인용 규칙 + 문서에 근거가 없으면 '알 수 없다'고 답하는 grounding
