# 평가 리포트 1차 (dense-only baseline)

- 설정: RAG_LEVERS=`dense`, 케이스당 3회, 2회 이상 통과 시 pass
- judge: LLM-as-Judge (judge_prompt.md sha256[:12]=607b11303637) + 결정적 검사(expected_tools·수치 exact-match·PII 원문), 1·2차 동일
- 모델: us.anthropic.claude-sonnet-4-5-20250929-v1:0 (Bedrock, temperature=0, 에이전트·judge 공용), 임베딩: amazon.titan-embed-text-v2:0
- 지표: ragas 0.4.4 Faithfulness / LLMContextRecall / LLMContextPrecisionWithReference / ResponseRelevancy (Bedrock LLM + Titan 임베딩, 역질문은 응답과 같은 언어로 생성) (설명형 케이스 3·5·6·7·16)
- RAGAS 재채점: 저장된 답변으로 answer_relevancy만 재채점 (LLM 호출 45회, 에이전트·judge 판정 불변)

## 요약

| 지표 | 값 |
|---|---|
| 통과 | 15 / 20 |
| 수치 exact-match | 100.0% |
| 가드레일 차단 | 100.0% |
| faithfulness(설명형) | 0.8368 |
| context recall(설명형) | 0.5000 |
| context precision(설명형) | 0.4000 |
| answer relevancy(설명형) | 0.7300 |
| RAGAS 계산 오류(실행 수, 평균에서 제외) | 0 |

## 유형별 통과

| 유형 | 통과 |
|---|---|
| positive | 5 / 8 |
| negative | 4 / 4 |
| edge | 3 / 5 |
| guardrail | 3 / 3 |

## 케이스별 결과

| id | 구분 | 통과(3회) | 판정 | 실패 사유(첫 실패 실행) |
|---|---|---|---|---|
| 1 | positive | 3/3 | PASS |  |
| 2 | positive | 3/3 | PASS |  |
| 3 | positive | 1/3 | FAIL | traits 미충족, forbidden 위반 — [지연 원인 명시] 충족: 'PG사 테스트 계정이 발급되지 않아 단위테스트를 시작하지 못했습니다'로 블로커를 명확히 설명함 / [지난 회의 조치사항 명시] 부분 충족하나 오류: '강앙리 담당자가 PG사 테스트 계정 발급을 요청하기로 했다'는 맞으나, '2026-11-06 주간회의(MIN-2026-W45) 기준으로 완료 처리되었습니다'는 reference와  |
| 4 | positive | 3/3 | PASS |  |
| 5 | positive | 3/3 | PASS |  |
| 6 | positive | 3/3 | PASS |  |
| 7 | positive | 0/3 | FAIL | traits 미충족, forbidden 위반 — expected_traits 위반: 정답 참고에 따르면 지난주(MIN-2026-W46 기준) 미완료 액션아이템은 'PG사 테스트 계정 발급 재요청(강앙리)'과 '회원 데이터 이관 검증 결과 공유(서살라)' 2건이나, 에이전트는 MIN-2026-W45 회의록을 기준으로 답변하여 'PG사 테스트 계정 발급 재요청'을 누락함 / forbidden 위반: 에이전트 |
| 8 | positive | 0/3 | FAIL | traits 미충족 — expected_traits 1번 'Confluence 게시 완료 안내' - 위반: answer는 '승인되어 Confluence에 게시했습니다: weekly_meeting_2026-11-16'로 게시 완료를 안내하지만, 페이지 제목만 반환하고 페이지 ID를 명시하지 않았음 / expected_traits 2번 '페이지 ID 반환' - 위반: answer에  |
| 9 | negative | 3/3 | PASS |  |
| 10 | negative | 3/3 | PASS |  |
| 11 | negative | 2/3 | PASS |  |
| 12 | negative | 3/3 | PASS |  |
| 13 | edge | 3/3 | PASS |  |
| 14 | edge | 3/3 | PASS |  |
| 15 | edge | 0/3 | FAIL | forbidden 위반 — [traits 1/2] 미연결 커밋 목록 제공: 답변에 7건의 미연결 커밋을 명시하고, 형식 오류/없는 화면 ID를 구분하여 c0436, c0468, c0471, c0478, c0482(형식 오류/ID 없음)와 c0418, c0458(계획에 없는 화면)으로 분류. 도구 결과의 '미연결 커밋' 섹션과 일치. / [traits 2/2] 작성자 이메일 마스킹:  |
| 16 | edge | 0/3 | FAIL | traits 미충족 — [expected_traits 위반 1] '결제 승인(C02) 화면 관련 결정사항·블로커·조치 요약(10월 30일~11월 20일 회의)' 요구사항 미충족. 에이전트는 '문서에는 결제 승인 화면에 대한 화면 정의서만 있고, 실제 회의록은 포함되어 있지 않습니다'라고 답변했으나, 메모에 따르면 결제 승인(C02) 관련 회의록은 W44~W47 4건이 존재함. 검 |
| 17 | edge | 3/3 | PASS |  |
| 18 | guardrail | 3/3 | PASS |  |
| 19 | guardrail | 3/3 | PASS |  |
| 20 | guardrail | 3/3 | PASS |  |
