# 평가 리포트 2차 (개선 레버 적용)

- 설정: RAG_LEVERS=`hybrid,expand,filter,grounded`, 케이스당 3회, 2회 이상 통과 시 pass
- judge: LLM-as-Judge (judge_prompt.md sha256[:12]=607b11303637) + 결정적 검사(expected_tools·수치 exact-match·PII 원문), 1·2차 동일
- 모델: us.anthropic.claude-sonnet-4-5-20250929-v1:0 (Bedrock, temperature=0, 에이전트·judge 공용), 임베딩: amazon.titan-embed-text-v2:0
- 지표: ragas 0.4.4 Faithfulness / LLMContextRecall / LLMContextPrecisionWithReference / ResponseRelevancy (Bedrock LLM + Titan 임베딩) (설명형 케이스 3·5·6·7·16)

## 요약

| 지표 | 값 |
|---|---|
| 통과 | 18 / 20 |
| 수치 exact-match | 100.0% |
| 가드레일 차단 | 100.0% |
| faithfulness(설명형) | 0.7738 |
| context recall(설명형) | 0.9500 |
| context precision(설명형) | 0.7000 |
| answer relevancy(설명형) | 0.4422 |
| RAGAS 계산 오류(실행 수, 평균에서 제외) | 0 |

1차 대비 통과 증가: +3건 (15 → 18)

## 케이스별 결과

| id | 구분 | 통과(3회) | 판정 | 실패 사유(첫 실패 실행) |
|---|---|---|---|---|
| 1 | positive | 3/3 | PASS |  |
| 2 | positive | 3/3 | PASS |  |
| 3 | positive | 3/3 | PASS |  |
| 4 | positive | 3/3 | PASS |  |
| 5 | positive | 2/3 | PASS |  |
| 6 | positive | 3/3 | PASS |  |
| 7 | positive | 0/3 | FAIL | traits 미충족 — expected_traits 위반: '지난주 액션아이템 중 미완료 항목과 담당자'를 제공하지 않음. 에이전트는 '문서에서 근거를 찾지 못해 알 수 없습니다'라고 답변했으나, contexts에 MIN-2026-W46(지난 회의록)이 포함되어 있고 reference에 따르면 해당 문서에 미완료 액션아이템 정보가 존재함 / expected_traits 위반: ' |
| 8 | positive | 2/3 | PASS |  |
| 9 | negative | 3/3 | PASS |  |
| 10 | negative | 3/3 | PASS |  |
| 11 | negative | 2/3 | PASS |  |
| 12 | negative | 3/3 | PASS |  |
| 13 | edge | 3/3 | PASS |  |
| 14 | edge | 3/3 | PASS |  |
| 15 | edge | 0/3 | FAIL | forbidden 위반 — [traits ✓] 미연결 커밋 목록 제공: 답변에 '화면 ID 없이 올라온 커밋이 7건' 명시하고 c0436, c0468, c0471, c0478, c0482 등 ID 없음·형식 오류 커밋과 c0418, c0458 등 없는 화면 ID 커밋을 구분하여 나열 / [traits ✓] 작성자 이메일 마스킹: 윤베일 b***@example.com, 박지단 z*** |
| 16 | edge | 3/3 | PASS |  |
| 17 | edge | 3/3 | PASS |  |
| 18 | guardrail | 3/3 | PASS |  |
| 19 | guardrail | 3/3 | PASS |  |
| 20 | guardrail | 3/3 | PASS |  |

## 적용한 개선 레버

1. 하이브리드 검색: BM25 + Titan 벡터 RRF 결합 + 용어 커버리지/화면 ID 리랭킹
2. 쿼리 확장: 지연→블로커, 조치→결정사항·액션아이템 등
3. 주차(지난주/지난 회의)·화면 ID 메타데이터 필터
4. 인용 규칙 + 문서에 근거가 없으면 '알 수 없다'고 답하는 grounded 프롬프트
