# 미니 PJT: 주간 진척 보고 에이전트

AX 교육 Day 8~10 개인 미니 프로젝트.

## 무엇을 푸나
개발자가 진척률을 손으로 입력하고 PM/PL이 매주 회의 자료를 정리해 Confluence에 등록하던 일을 자동화한다.
가짜 커밋 로그(fixture)로 진척률·남은 MM을 계산하고, 회의록·규칙 문서를 검색해 지연 이유·결정사항에 답하며,
주간 회의 자료 초안을 PM/PL이 승인하면 (가짜) Confluence에 게시한다. 모든 데이터는 가짜이며 인증 정보는 포함하지 않는다.

## 실행 환경
- Python 3.14.8, 프로젝트 가상환경 `.venv`
- LLM: AWS Bedrock `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (temperature=0), 리전 `us-west-2`
- 임베딩: Bedrock `amazon.titan-embed-text-v2:0`
- 평가 지표: RAGAS 0.4.4 (Faithfulness, LLMContextRecall), judge는 에이전트와 같은 모델
- 모델·리전 설정은 `src/config.py` 한 곳에서 관리한다. AWS 자격 증명은 `.env`(git 제외)에서 읽는다.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# .env 에 AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION=us-west-2

# API 서버 (POST /query)
.venv\Scripts\python.exe -m uvicorn src.server:app --port 8000

# 테스트 (가짜 LLM 주입, AWS 불필요)
.venv\Scripts\python.exe -m pytest -q

# 평가 (실제 Bedrock 호출, 비용 발생)
.venv\Scripts\python.exe evaluation/run_eval.py --round 1
.venv\Scripts\python.exe evaluation/run_eval.py --round 2
```

## API
`POST /query` — 요청 `{question, user_id, role(developer|pm_pl), thread_id?, approval?(approve|reject)}`,
응답 `{answer, contexts, trace, thread_id, pending_approval}`. 회의 자료는 승인 대기 응답의 `thread_id`에
`approval`을 보내 재개한다. 오류: role/approval 값 422, 없는 thread_id 404, 미등록 user_id 403.
사용자 식별은 인증 없는 데모 수준이다.

## 활용한 패턴 (Day 1~7, 12개)
| # | 패턴 | 적용 위치 |
|---|---|---|
| 1 | LCEL + Pydantic 구조화 출력 | `src/supervisor.py` 라우터(`prompt \| llm.with_structured_output(RouteDecision)`), `src/meeting.py` `Plan`·`MeetingDraft`, `src/agent.py` 문서 Q&A 체인 |
| 2 | ReAct (도구 자율 선택) | `src/agent.py` `answer_progress` — `create_agent` + 진척 도구 |
| 3 | RAG | `src/retriever.py` (1차 Titan 벡터 dense-only, 2차 BM25+벡터 RRF·리랭킹·쿼리 확장·메타데이터 필터) + `src/agent.py` `answer_docs_qa` (근거 인용 답변) |
| 4 | 다중 도구 | `src/tools.py` 결정적 계산 + `src/agent.py` `calc_progress`·`calc_remaining_mm`·`check_mismatch`·`get_commits` |
| 5 | MCP | `src/mcp_confluence_server.py` (stdio, `get_page`·`create_page`·`search_pages`), `src/mcp_client.py` |
| 6 | 가드레일 | `src/guardrails.py` — 역할 범위·권한 상승·직접/간접 인젝션·순위 금지를 도구 실행 전 코드로 강제, 개발자 도구는 본인 범위로 고정 |
| 7 | HITL | `src/meeting.py` `interrupt()` + checkpointer, `src/api.py` thread_id·approval 재개 (PM/PL만 승인) |
| 8 | 미들웨어 | `src/middleware.py` PII 마스킹(한 곳)·타임아웃·재시도, `src/agent.py` `SummarizationMiddleware`(회의 중 긴 대화 요약)·`ModelRetryMiddleware` |
| 9 | Multi-Agent Supervisor | `src/supervisor.py` LLM 라우팅 → 진척 / 문서 Q&A / 회의 자료 에이전트, 라우팅 결과는 trace에 기록 |
| 10 | Plan-Execute + 장기 메모리 | `src/meeting.py` `MeetingFlow` (계획 → 단계 실행 → 승인 → 게시), LangGraph `InMemoryStore`에 주차별 액션아이템 상태 저장 |
| 11 | Trace | `src/tracing.py` (LANGSMITH_API_KEY가 있으면 LangSmith, 없으면 `logs/trace/` JSON) + 응답 `trace` 필드 |
| 12 | RAGAS + LLM-as-Judge | `evaluation/run_eval.py`, `evaluation/judge_prompt.md` |

## 평가 설정
- **1차 baseline(사전 고정): dense-only 검색** (`RAG_LEVERS=dense`, Titan 벡터 코사인, 문서 단위 top-3, 기본 프롬프트). 평가 전에 고정했고 2차에서 바꾸지 않는다.
- 2차: 동일 테스트 세트(`test_queries.csv` 20건)·동일 judge·케이스당 3회, 2회 이상 통과 시 pass.
  레버: 하이브리드(BM25+벡터 RRF+리랭킹), 쿼리 확장, 주차·업무 ID 메타데이터 필터, 인용 규칙+'문서에 없으면 모른다' 프롬프트
  (`RAG_LEVERS=hybrid,expand,filter,grounded`).
- 판정: LLM-as-Judge가 expected_traits·forbidden을 판정하고, expected_tools 호출·수치 exact-match·PII 원문 노출은 코드로 검사한다.
- 평가 실행 전 대화 기억·장기 메모리·런타임 인덱스를 초기화하고, 게시된 회의 자료(`data/generated/`)는 평가 코퍼스에서 제외한다.
- 결과: `evaluation/round1_report.md`, `evaluation/round2_report.md` (원본 `round{N}_results.json`).

## 구조
- `src/agent.py` 에이전트 본체, `src/server.py` 운영 진입점(LLM 연결), `src/api.py` POST /query
- `src/tools.py` 진척·MM 결정적 계산, `src/retriever.py` 검색·런타임 인덱스, `src/meeting.py` 회의 자료 그래프
- `data/fixtures/` 업무·커밋·개발자·정답, `data/corpus/` 평가용 고정 코퍼스(16건)
- `tests/` 오프라인 테스트, `evaluation/` 평가

## 축소 내역
현재 없음 (축소 트리거 미발동).

## 트라이앤에러 회고
- 1차 구현은 LLM 없이 규칙 기반으로 동작해 패턴이 흉내에 그쳤다. 결정적 도구·데이터·가드레일은 유지하고 에이전트 계층을 LangChain/LangGraph + Bedrock으로 교체했다.
- 진척 숫자는 LLM이 바꾸지 않도록 도구 결과를 답변 끝에 그대로 붙인다.
- 에이전트 처리 전체에 MCP와 같은 10초 타임아웃을 걸면 LLM 응답이 끊겨, MCP(10초)와 에이전트 처리(180초) 타임아웃을 분리했다.
