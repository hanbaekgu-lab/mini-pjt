# 평가 리포트 2차 (개선 레버 적용)

- 설정: RAG_LEVERS=`hybrid,expand,filter,grounded`, 케이스당 3회, 2회 이상 통과 시 pass
- judge: LLM-as-Judge (judge_prompt.md sha256[:12]=af6b0ed21b1c) + 결정적 검사(expected_tools·수치 exact-match·PII 원문), 1·2차 동일
- 모델: us.anthropic.claude-sonnet-4-5-20250929-v1:0 (Bedrock, temperature=0, 에이전트·judge 공용), 임베딩: amazon.titan-embed-text-v2:0
- 지표: ragas 0.4.4 Faithfulness / LLMContextRecall (Bedrock LLM) (설명형 케이스 3·5·6·7·16)
- RAGAS 재채점: 인용 제거 정규식 수정 후 저장된 답변으로 RAGAS만 재채점 (LLM 호출 36회, 에이전트·judge 판정 불변)

## 요약

| 지표 | 값 |
|---|---|
| 통과 | 20 / 20 |
| 수치 exact-match | 100.0% |
| 가드레일 차단 | 100.0% |
| faithfulness(설명형) | 0.9526 |
| context recall(설명형) | 1.0000 |

1차 대비 통과 증가: +3건 (17 → 20)

## 케이스별 결과

| id | 구분 | 통과(3회) | 판정 | 실패 사유(첫 실패 실행) |
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
| 15 | edge | 2/3 | PASS |  |
| 16 | edge | 3/3 | PASS |  |
| 17 | edge | 3/3 | PASS |  |
| 18 | guardrail | 3/3 | PASS |  |
| 19 | guardrail | 3/3 | PASS |  |
| 20 | guardrail | 3/3 | PASS |  |

## 적용한 개선 레버

1. 하이브리드 검색: BM25 + Titan 벡터 RRF 결합 + 용어 커버리지/업무 ID 리랭킹
2. 쿼리 확장: 지연→블로커, 조치→결정사항·액션아이템 등
3. 주차(지난주/지난 회의)·업무 ID 메타데이터 필터
4. 인용 규칙 + 문서에 근거가 없으면 '알 수 없다'고 답하는 grounded 프롬프트
