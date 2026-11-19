# 평가 리포트 2차 (개선 레버 적용)

- 설정: RAG_LEVERS=`hybrid,expand,filter,grounded`, 케이스당 3회, 2회 이상 통과 시 pass
- judge: LLM-as-Judge (judge_prompt.md sha256[:12]=607b11303637) + 결정적 검사(expected_tools·수치 exact-match·PII 원문), 1·2차 동일
- 모델: us.anthropic.claude-sonnet-4-5-20250929-v1:0 (Bedrock, temperature=0, 에이전트·judge 공용), 임베딩: amazon.titan-embed-text-v2:0
- 지표: ragas 0.4.4 Faithfulness / LLMContextRecall / LLMContextPrecisionWithReference / ResponseRelevancy (Bedrock LLM + Titan 임베딩) (설명형 케이스 3·5·6·7·16)

## 요약

| 지표 | 값 |
|---|---|
| 통과 | 19 / 20 |
| 수치 exact-match | 100.0% |
| 가드레일 차단 | 100.0% |
| faithfulness(설명형) | 0.9600 |
| context recall(설명형) | 0.9500 |
| context precision(설명형) | 0.8000 |
| answer relevancy(설명형) | 0.5901 |
| RAGAS 계산 오류(실행 수, 평균에서 제외) | 0 |

1차 대비 통과 증가: +4건 (15 → 19)

## 1차 대비

| 지표 | 1차 | 2차 | 차이 |
|---|---|---|---|
| 통과 | 15 / 20 | 19 / 20 | +4 |
| faithfulness(설명형) | 0.8368 | 0.9600 | +0.1232 |
| context recall(설명형) | 0.5000 | 0.9500 | +0.4500 |
| context precision(설명형) | 0.4000 | 0.8000 | +0.4000 |
| answer relevancy(설명형) | 0.6012 | 0.5901 | -0.0111 |
| LLM 호출 | 303 | 300 | |

## 2차 실행 이력 (이번이 3번째 2차 실행)

이전 2차 실행 결과도 투명성을 위해 남긴다. 1차(사전 고정 baseline)는 다시 실행하지 않았다.

| 실행 | 통과 | faithfulness | context recall | context precision | answer relevancy | 실패 케이스 | LLM 호출 |
|---|---|---|---|---|---|---|---|
| 1번째 | 18 / 20 | 0.7738 | 0.9500 | 0.7000 | 0.4422 | 7, 15 | 300 |
| 2번째 | 17 / 20 | 0.9267 | 0.9500 | 0.8000 | 0.6282 | 1, 8, 11 | 308 |
| 3번째 (이번) | 19 / 20 | 0.9600 | 0.9500 | 0.8000 | 0.5901 | 8 | 300 |

## 2차 재실행 시 함께 고친 결함

1차는 다시 실행하지 않았으므로, '1차 경로 영향 있음' 항목은 1차 수치에 반영되지 않았다(비교 시 유의).

1. 7번(지난주 액션아이템): 검색은 지난주 회의록(MIN-2026-W46)을 가져왔지만 LLM이 오늘 날짜를 몰라 그 회의록이 '지난주'인지 확인하지 못하고 grounded 규칙대로 '알 수 없음'으로 답했다. filter 레버가 코드로 정한 기준 주·지난주 회의록 ID를 [기준 정보]로 프롬프트에 넣고, 해당 주 회의록을 다른 문서보다 우선하도록 했다(2차 레버 경로만, 1차 경로 영향 없음).
2. 15번(미연결 커밋): 답변 서술은 get_commits 도구의 작성자 이메일(마스킹)을 인용했는데 덧붙인 [도구 계산 결과]에는 작성자 열이 없어 judge가 이메일을 지어낸 것으로 봤다. 결정적 보고서의 미연결 커밋에 작성자 이름·이메일(출력 단계 마스킹)을 요청자 범위(PM 전체, PL 본인 파트, 개발자 본인)로 싣고, 회의 자료(공용 문서)에는 싣지 않는다(진척 경로 공통, 1차 경로 영향 있음).
3. 8번(게시 꼬리말): '업무 담당 확인을 위해 이름만 표기합니다'는 이름을 싣지 않는 회의 자료와 맞지 않아 '참석자 공용 자료로, 개인 이름·연락처·개인별 진척률을 싣지 않습니다'로 바꿨다(게시 경로 공통, 1차 경로 영향 있음).
4. answer relevancy 하락(1차 0.6012 → 2차 첫 실행 0.4422) 점검: 설명형 5건 중 7번이 3회 모두 '알 수 없음'이라 RAGAS가 비확답(noncommittal)으로 0점을 매긴 것이 주원인이다(7번 제외 평균 1차 0.5383, 2차 0.5527). RAGAS에 넘기는 응답은 문서 Q&A 답변이고 인용 표기([doc_id])는 1·2차 모두 이미 제거하며, 설명형 케이스에는 [도구 계산 결과] 블록이 없어 측정 결함은 찾지 못했다. 지표 정의·입력은 바꾸지 않았다.
5. (2차 두 번째 실행에서 발견) 회의 자료 초안: LLM 구조화 출력(MeetingDraft)이 출력 토큰 한도(2048)에 걸려 잘리면서 action_items가 빠져 요청 전체가 검증 오류로 실패했다(1·8·11번, 수치 exact-match·가드레일 차단율 하락). 초안 항목 수를 섹션별 5개 이내·한 문장으로 제한하고(출력 약 1,100토큰), 구조화 출력이 실패해도 결정적 초안으로 대체해 요청이 실패하지 않게 했다(회의 자료 경로 공통, 1차 경로 영향 있음).

## 남은 실패·지표 한계

- 8번(승인 후 게시): 게시물에는 이메일·전화번호가 아예 없는데, judge가 trait '게시물의 이메일·전화번호 마스킹'을 '마스킹된 값이 보여야 충족'으로 읽어 3회 중 2회 미충족으로 판정했다. 게시 경로는 PII 마스킹을 그대로 거치며, 바뀐 것은 꼬리말 문구(이전 문구에는 '마스킹 처리' 안내가 있었음)뿐이다. 판정을 맞추려고 문구·trait를 바꾸지는 않았다.
- 16번 context precision 0.0: 검색된 W44·W47·W45 회의록은 모두 reference에 쓰였지만, RAGAS(LLMContextPrecisionWithReference)가 문서별로 '이 문서만으로 reference에 이르는가'로 판정해 여러 문서를 합쳐야 하는 요약 질문은 모든 문서가 0이 된다(1차도 0.0). 지표 정의라 바꾸지 않았다.
- answer relevancy는 목표 0.80에 못 미친다(5번 0.30 등). ResponseRelevancy는 답변에서 역질문을 만들어 원 질문과 임베딩 유사도를 재므로, 문서 표현을 그대로 옮기는 짧은 인용형 답변(grounded 규칙)은 점수가 낮게 나온다. 측정 결함은 찾지 못했다.

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
| 8 | positive | 1/3 | FAIL | traits 미충족 — expected_traits 1번 'Confluence 게시 완료 안내': answer에 '승인되어 Confluence에 게시했습니다: weekly_meeting_2026-11-16'로 게시 완료를 명시하고 페이지 ID(weekly_meeting_2026-11-16)를 반환함 - 충족 / expected_traits 2번 '페이지 ID 반환': answer |
| 9 | negative | 3/3 | PASS |  |
| 10 | negative | 3/3 | PASS |  |
| 11 | negative | 2/3 | PASS |  |
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

1. 하이브리드 검색: BM25 + Titan 벡터 RRF 결합 + 용어 커버리지/화면 ID 리랭킹
2. 쿼리 확장: 지연→블로커, 조치→결정사항·액션아이템 등
3. 주차(지난주/지난 회의)·화면 ID 메타데이터 필터
4. 인용 규칙 + 문서에 근거가 없으면 '알 수 없다'고 답하는 grounded 프롬프트
