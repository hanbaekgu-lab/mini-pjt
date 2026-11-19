# mini PJT: 주간 진척 보고 에이전트

## 무엇을 푸나
개발자가 진척률을 손으로 입력하고 PM·PL이 매주 회의 자료를 직접 정리해 Confluence에 올리느라 시간이 들고, 진척률이 근거 없는 자가 보고 수치에 머문다.

이 에이전트는 커밋 로그로 화면별 진척률·남은 MM을 결정적으로 계산하고, 회의록·화면 정의서·팀 규칙을 검색해 지연 이유·결정사항에 근거를 인용해 답하며,
주간 회의 자료 초안을 PM 또는 PL이 승인하면 Confluence에 게시한다. AX 교육 개인 mini PJT이며, 모든 데이터는 가짜이고 인증 정보는 포함하지 않는다.
서비스 정의(사용자·가치·가드레일·성공 기준)는 [SERVICE.md](SERVICE.md)에 있다.

## 활용한 패턴
| # | 패턴 | 적용 위치 |
|---|---|---|
| 1 | LCEL + Pydantic 구조화 출력 | `src/supervisor.py` 라우터(`prompt \| llm.with_structured_output(RouteDecision)`), `src/meeting.py` `Plan`·`MeetingDraft`, `src/agent.py` 문서 Q&A 체인 |
| 2 | ReAct (도구 자율 선택) | `src/agent.py` `answer_progress` — `create_agent` + 진척 도구. 질문마다 도구를 새로 부르고, 도구를 건너뛰면 한 번 더 지시한다 |
| 3 | RAG | `src/retriever.py` (1차 Titan 벡터 dense-only, 2차 kiwipiepy 형태소 BM25 + 벡터 RRF·리랭킹·쿼리 확장·메타데이터 필터) + `src/agent.py` `answer_docs_qa` (근거 인용 답변) |
| 4 | 다중 도구 | `src/tools.py` 결정적 계산 + `src/agent.py` `calc_progress`·`calc_remaining_mm`·`check_mismatch`·`get_commits` |
| 5 | MCP | `src/mcp_confluence_server.py` (fastmcp, stdio, `get_page`·`create_page`·`search_pages`), `src/mcp_client.py` (langchain-mcp-adapters `MultiServerMCPClient`), `src/confluence.py`(fake/real), `src/confluence_convert.py`(Markdown→Confluence) |
| 6 | 가드레일 | `src/guardrails.py` — 역할 범위(PM 전체·PL 본인 파트·개발자 본인, 신원은 세션 기준·역할 불일치 403)·직접/간접 인젝션·순위 금지를 도구 실행 전 코드로 강제, 진척 도구도 요청자 범위로 고정 |
| 7 | HITL | `src/meeting.py` `interrupt()` + checkpointer, `src/api.py` thread_id·approval 재개 (PM·PL만 승인) |
| 8 | 미들웨어 | `src/middleware.py` PII 마스킹(한 곳)·타임아웃·재시도, `src/agent.py` `SummarizationMiddleware`(회의 중 긴 대화 요약)·`ModelRetryMiddleware` |
| 9 | Multi-Agent Supervisor | `src/supervisor.py` 구조화 출력 LLM 라우터 → 진척 / 문서 Q&A / 회의 자료 에이전트, 라우팅 결과는 trace 맨 앞에 기록 |
| 10 | Plan-Execute + 장기 메모리 | `src/meeting.py` `MeetingFlow` (계획 → 진척 수집 → 지난주 회의 확인 → 초안 → 승인 → 게시), LangGraph `InMemoryStore`에 주차별 회의 스냅샷 저장 |
| 11 | Trace | `src/tracing.py` (LANGSMITH_API_KEY가 있으면 LangSmith, 없으면 `logs/trace/` JSON) + 응답 `trace` 필드 |
| 12 | RAGAS + LLM-as-Judge | `evaluation/run_eval.py` (RAGAS 4개 지표), `evaluation/judge_prompt.md` |

**Supervisor를 `langgraph_supervisor` 대신 구조화 출력 라우터로 둔 이유**: 라우팅이 한 번의 결정적 분기(`RouteDecision`)라서
(1) 가드레일 차단과 HITL 재개(`thread_id`·`approval`)를 라우팅 전에 코드로 처리할 수 있고, (2) 승인 대기 중인 회의 자료 그래프가
다른 에이전트로 넘어가는 `transfer_to_*` 도구 호출에 휘말리지 않으며, (3) 라우팅 결과 하나가 trace에 그대로 남아 평가 판정과 디버깅이 쉽다.
LLM 호출이 실패하면 규칙 기반 라우팅으로 대체한다.

## 아키텍처
![주간 진척 보고 에이전트 아키텍처](docs/images/architecture.svg)

1. `POST /query` → 세션 신원 확인(미등록 403, 역할 불일치 403) → 가드레일(범위·인젝션·순위)을 도구 실행 전에 검사
2. Supervisor가 진척 / 문서 Q&A / 회의 자료 중 하나로 라우팅
   - **진척**: ReAct 에이전트가 요청자 범위로 고정된 도구로 계산하고, 도구 결과를 답변 끝에 그대로 붙인다
   - **문서 Q&A**: 검색 → 간접 인젝션 청크 제외·범위 밖 진척 수치 가림 → 근거 인용 답변
   - **회의 자료**: Plan-Execute 그래프가 진척 수집 → 지난주 회의 확인(장기 메모리·회의록) → 초안 → `interrupt()` 승인 대기 → 승인 시 MCP `create_page`로 게시
3. 출력 단계에서 PII 마스킹(answer·contexts·trace), trace 기록

## 실행 방법
Python 3.14, 가상환경 `.venv`. 모든 명령은 저장소 루트에서 실행한다. `run.sh`는 Linux/macOS bash와 Windows Git Bash에서 같이 쓴다(`.venv/Scripts`와 `.venv/bin` 자동 판별).

```bash
./run.sh setup          # .venv 생성, requirements 설치, .env가 없으면 .env.example 복사
# .env의 mock 값을 실제 값으로 교체 (AWS 자격 증명·AWS_DEFAULT_REGION, Confluence 설정)
./run.sh server         # API 서버 (POST /query, 포트 8000, PORT로 변경)
./run.sh test           # 오프라인 테스트 (가짜 LLM 주입, AWS 불필요)
./run.sh fixtures       # fixture 재생성 (고정 시드, 같은 결과)
./run.sh eval 1         # 1차 평가 (실제 Bedrock 호출, 비용 발생) / ./run.sh eval 2
./run.sh rescore 2      # 저장된 답변으로 RAGAS 4개 지표만 재채점
./run.sh docker-build   # 이미지 빌드 (docker 필요)
./run.sh docker-run     # docker run --env-file .env -p 8000:8000 weekly-progress-agent
```

`run.sh` 없이 실행할 때(Windows PowerShell 예):
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python.exe -m uvicorn src.server:app --port 8000
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe evaluation/run_eval.py --round 1
```

**Docker**: `Dockerfile`(python:3.14-slim)은 비밀 정보를 넣지 않는다. `.dockerignore`가 `.env`·`.venv`·`logs`·`.git`·`data/generated`·`docs/course`를 제외하고,
실행할 때 `docker run --env-file .env -p 8000:8000 weekly-progress-agent`로 주입한다.

**실행 환경**
- LLM: AWS Bedrock `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (temperature=0), 리전은 `.env`의 `AWS_DEFAULT_REGION` 값을 따름
- 임베딩: Bedrock `amazon.titan-embed-text-v2:0`
- 평가 지표: RAGAS 0.4.4 (Faithfulness, LLMContextRecall, LLMContextPrecisionWithReference, ResponseRelevancy), judge는 에이전트와 같은 모델
- MCP: mcp 1.30.0, fastmcp 2.13.0, langchain-mcp-adapters 0.3.2. 한국어 형태소: kiwipiepy 0.24.1
- 모델·리전 설정은 `src/config.py` 한 곳에서 관리한다. 설정 키 목록은 `.env.example`(mock 값)을 본다.

## RAGAS 평가 결과
> 도메인 재설계(플랫폼 전환·화면 170개·역할 pm/pl/developer) 후 **재평가 예정**이다. 아래 숫자는 **이전 도메인 기준** 결과이며,
> context precision·answer relevancy는 이번에 추가한 지표라 이전 값이 없다. 재평가 전까지 `tests/test_eval_reports.py`의 결과 판정은 건너뛴다.

| 지표 (설명형 케이스) | 1차 (dense-only) | 2차 (개선) | 성공 기준 |
|---|---|---|---|
| faithfulness | 0.8109 (이전 도메인 기준) | 0.9526 (이전 도메인 기준) | 0.85 이상 |
| context recall | 0.80 (이전 도메인 기준) | 1.00 (이전 도메인 기준) | 0.80 이상 |
| context precision | 재평가 예정 | 재평가 예정 | 0.80 이상 |
| answer relevancy | 재평가 예정 | 재평가 예정 | 0.80 이상 |

- **1차 baseline(사전 고정): dense-only 검색** (`RAG_LEVERS=dense`, Titan 벡터 코사인, 문서 단위 top-3, 기본 프롬프트). 평가 전에 고정했고 2차에서 바꾸지 않는다.
- 2차 레버: 하이브리드(kiwipiepy 형태소 BM25 + 벡터 RRF + 리랭킹), 쿼리 확장, 주차·화면 ID 메타데이터 필터, 인용 규칙 + '문서에 없으면 모른다' 프롬프트 (`RAG_LEVERS=hybrid,expand,filter,grounded`).
- context precision은 정답(reference) 기반(`LLMContextPrecisionWithReference`), answer relevancy는 Titan 임베딩을 ragas 래퍼로 감싸 쓴다(`ResponseRelevancy`).
- RAGAS에 넘기기 전 답변의 인용 표기 중 실제 문서 ID(`[MIN-2026-W46]`)만 지우고, 본문의 형식 예시(`[화면ID]`)는 남긴다.
- **평가 시 생성 문서 제외**: 평가 실행기가 `EVAL_EXCLUDE_GENERATED=1`을 설정해 검색은 `data/corpus`만 쓰고, 승인 케이스 직후 런타임 인덱스도 비운다.
- 결과: `evaluation/round1_report.md`, `evaluation/round2_report.md` (원본 `round{N}_results.json`). RAGAS만 다시 계산: `./run.sh rescore N`.

## 인-아웃 세트 통과율 (자체 평가)
테스트 세트 `evaluation/test_queries.csv` 20건(positive 8, negative 4, edge 5, guardrail 3), 케이스당 3회 실행해 2회 이상 통과하면 pass.
판정은 LLM-as-Judge(expected_traits·forbidden)와 코드 검사(expected_tools 호출·수치 exact-match·PII 원문 노출)를 함께 쓴다.

> 새 도메인 세트로는 **재평가 예정**이다. 아래는 **이전 도메인 기준** 결과다.

| 항목 | 1차 (dense-only) | 2차 (개선) | 개선폭 | 성공 기준 |
|---|---|---|---|---|
| 통과 | 17 / 20 (이전 도메인 기준) | 20 / 20 (이전 도메인 기준) | +3건 | 16 이상, +3건 이상 |
| 수치 exact-match | 100% | 100% | - | 100% |
| 가드레일 차단 | 100% | 100% | - | 100% |

**주요 개선 사항 (2차)**
- 검색 레버: 하이브리드 검색·쿼리 확장·주차/화면 ID 필터·grounded 프롬프트
- 특정 업무(화면) 진척 질문에도 `check_mismatch`를 호출해 상태-커밋 불일치를 함께 알림
- 미연결 커밋에 작성자 이메일을 붙여 출력 단계 마스킹이 드러나게 함
- 게시물에 개인정보 처리 안내 문구 추가, 근거 인용 프롬프트를 '문서 표현 그대로·부연 금지'로 강화

## 트라이앤에러 회고
### 시도했지만 실패한 접근과 이유
- **LLM이 숫자를 직접 쓰게 함**: 진척률·MM을 LLM 설명에만 맡기면 반올림·누락이 생겼다. 도구 결과를 답변 끝에 그대로 붙이는 방식으로 바꿨다.
- **에이전트 처리 전체에 10초 타임아웃**: MCP와 같은 10초를 LLM 처리에도 걸자 응답이 끊겼다. MCP(10초)와 에이전트 처리(180초) 타임아웃을 분리했다.
- **인용 제거 정규식**: RAGAS에 넘기기 전 인용 표기를 지우는 정규식이 본문의 커밋 형식 예시(`[업무ID]`)까지 지워 faithfulness 0점이 나왔다. 실제 문서 ID만 지우도록 고치고 저장된 답변으로 재채점했다.
- **평가 중 생성 문서 혼입**: 한 회차 안에서 8번 케이스(승인 후 게시)가 만든 회의 자료가 런타임 인덱스에 들어가 16번 케이스의 근거로 쓰였다. '평가 시 생성 문서 제외' 원칙과 어긋나 평가 모드에서는 코퍼스만 검색하도록 고쳤다(테스트로 고정).
- **같은 대화의 후속 질문**: PL이 파트 진척을 물은 뒤 '중메뉴별·단계별 현황'을 묻자 ReAct 에이전트가 도구를 다시 부르지 않고 이전 답을 재사용해 중메뉴 개수를 틀리게 말했다. 프롬프트에 '질문마다 도구 새로 호출·최신 도구 결과의 숫자만'을 넣고, 도구를 건너뛰면 한 번 더 지시하도록 코드로 막았다.
- **손으로 쓴 JSON-RPC MCP 서버**: 동작은 했지만 프로토콜 세부(초기화·오류 형식)를 직접 관리해야 했다. 공식 SDK 계열 fastmcp와 langchain-mcp-adapters로 바꿨다(서버 기동이 무거워져 호출당 약 2초 늘었다).
- **문자열 포함 기반 BM25**: '지연됐고'와 '지연되어'처럼 활용형이 다르면 맞지 않았다. kiwipiepy 형태소 토큰화로 바꿨다.

### 최종 채택한 접근과 이유
- 숫자는 `src/tools.py`가 결정적으로 계산하고 LLM은 설명만 한다 → exact-match 100%를 버그 기준으로 관리할 수 있다.
- 권한은 프롬프트가 아니라 도구 실행 전 코드와 범위가 고정된 도구로 강제한다 → 인젝션·신원 주장 문구로 우회되지 않는다.
- Supervisor는 구조화 출력 라우터 하나로 둔다 → HITL 재개와 가드레일을 결정적으로 처리하고 trace로 확인한다.
- 회의 자료는 Plan-Execute + `interrupt()` → 승인 전 게시가 구조적으로 불가능하다.

### 남은 한계와 향후 방향
- HITL 대기 상태(`THREADS`)와 LangGraph checkpointer·Store가 메모리에 있어 서버를 재시작하면 사라진다 → SQLite/Postgres checkpointer로 옮길 예정.
- 사용자 식별은 인증 없는 데모 수준(`user_id`·`role`을 요청 본문으로 받음) → 사내 SSO 연동 필요.
- 실제 Confluence(`CONFLUENCE_MODE=real`)는 REST 어댑터와 오프라인 테스트만 있고 실제 서버로는 검증하지 않았다.
- '이번 주'는 fixture의 고정 기준 주(2026-11-16~22)다 → 실제 운영 시 날짜 기반으로 바꿔야 한다.
- MCP 호출마다 stdio 서버를 새로 띄워 호출당 2초 정도 걸린다 → 운영에서는 세션 재사용 필요.
- 새 도메인 기준 평가(4개 RAGAS 지표 포함)는 재실행 전이다.

## 핵심 코드 위치
| 기능 | 위치 |
|---|---|
| API 진입점·세션 신원·HITL 재개 | `src/api.py:180` `query`, `src/api.py:195` `_query` |
| 가드레일 검사(도구 실행 전) | `src/api.py:212`, `src/guardrails.py:60` `check_question` |
| 출력 단계 PII 마스킹 | `src/api.py:189`, `src/middleware.py:20` `mask_pii` |
| Supervisor 구조화 출력 라우터 | `src/supervisor.py:20` `RouteDecision`, `src/supervisor.py:36` `make_llm_router` |
| 진척 ReAct 에이전트 | `src/agent.py:55` `PROGRESS_SYSTEM`, `src/agent.py:70` `_progress_tools`, `src/agent.py:129` `answer_progress` |
| 문서 Q&A (RAG) | `src/agent.py:23` `GROUNDED_PROMPT`, `src/agent.py:33` `answer_docs_qa` |
| 하이브리드 검색·형태소 BM25 | `src/retriever.py:173` `ko_tokens`, `src/retriever.py:187` `bm25_scores`, `src/retriever.py:240` `search_scored` |
| 평가 시 생성 문서 제외 | `src/retriever.py:56` `runtime_documents`, `evaluation/run_eval.py:238` |
| 진척 결정적 계산 | `src/tools.py:255` `calc_progress`, `src/tools.py:300` `build_progress_report` |
| 회의 자료 Plan-Execute·HITL | `src/meeting.py:284` `MeetingFlow`, `src/meeting.py:368` `interrupt()` |
| MCP 서버·클라이언트 | `src/mcp_confluence_server.py:16` `FastMCP`, `src/mcp_client.py:24` `make_client`, `src/mcp_client.py:61` `call_tool` |
| Markdown → Confluence 변환 | `src/confluence_convert.py:45` `md_to_storage` |
| 재시도·타임아웃 | `src/middleware.py:47` `call_with_retry` |
| RAGAS 4개 지표 | `evaluation/run_eval.py:201` `ragas_metrics`, `evaluation/run_eval.py:214` `ragas_scores` |

## 산출물 구성
강의 산출물 구성(3-1)과 이 저장소의 실제 구성:

```
강의 3-1 산출물                     이 저장소
mini-pjt/                           mini-pjt/
├── README.md                       ├── README.md · SERVICE.md
├── SERVICE.md                      ├── requirements.txt · pytest.ini · .env.example · .gitignore
├── src/                            ├── Dockerfile · .dockerignore · run.sh
│   ├── agent.py                    ├── src/        agent.py · tools.py · retriever.py (+ api, server, supervisor, meeting,
│   ├── tools.py                    │               guardrails, middleware, tracing, mcp_*, confluence*, config)
│   └── retriever.py                ├── data/       corpus/ (평가용 고정 16건) · fixtures/ (가짜 데이터·생성기·정답)
├── data/                           │               confluence_pages/ (게시 예시)
└── evaluation/                     ├── evaluation/ test_queries.csv · run_eval.py · judge_prompt.md
    ├── test_queries.csv            │               round1_report.md · round2_report.md · round{N}_results.json
    ├── round1_report.md            ├── tests/      오프라인 테스트 (가짜 LLM, AWS 불필요)
    └── round2_report.md            └── docs/       images/ (아키텍처·사용 예시) · spec/ (seed.yaml, 인터뷰 요약)
                                                    course/ (강의 자료, git 제외)
```

## API 스펙 (표준 + 확장)
`POST /query` 하나로 질의하고 승인 흐름도 이어 간다.

| 구분 | 필드 | 설명 |
|---|---|---|
| 표준 요청 | `question` | 사용자 질문 |
| 확장 요청 (필수) | `user_id`, `role` | 세션 신원. `role`은 `pm`·`pl`·`developer` |
| 확장 요청 (선택) | `thread_id`, `approval` | 승인 대기 응답의 `thread_id`와 `approve`·`reject` |
| 표준 응답 | `answer`, `contexts[{doc_id, text}]`, `trace[{step, input, output}]` | 답변, 근거, 실행 기록 |
| 확장 응답 | `thread_id`, `pending_approval` | 대화·승인 스레드 ID, 승인 대기 여부 |

```json
// 요청
{"question": "이번 주 회의 자료 만들어서 Confluence에 올려줘", "user_id": "pm01", "role": "pm"}
// 응답 (승인 대기)
{"answer": "[회의 자료 초안 - 승인 대기] ...", "contexts": [{"doc_id": "tool:build_progress_report", "text": "..."}],
 "trace": [{"step": "supervisor_route", "input": "...", "output": "meeting_material"}, ...],
 "thread_id": "3f2a...", "pending_approval": true}
// 승인 (같은 thread_id, PM·PL만)
{"question": "승인", "user_id": "pm01", "role": "pm", "thread_id": "3f2a...", "approval": "approve"}
// 응답
{"answer": "승인되어 Confluence에 게시했습니다: weekly_meeting_2026-11-16", ..., "pending_approval": false}
```

| 오류 | 조건 |
|---|---|
| 422 | `role`이 pm·pl·developer가 아니거나 `approval`이 approve·reject가 아님 |
| 403 | 미등록 `user_id`, 또는 등록 역할과 다른 `role` |
| 404 | 없는 `thread_id`로 재개 |

이미 처리된 `thread_id`에 approval을 다시 보내면 상태를 바꾸지 않고 기존 결과를 돌려준다(멱등). 개발자의 승인 시도는 '해당 정보는 조회 권한이 없습니다'로 거절한다.
사용자 식별은 인증 없는 데모 수준이다.

## 도메인 (가짜 데이터)
- **플랫폼 전환 프로젝트**: 2026-03-02 ~ 2027-02-26 (12개월). 기준 주는 9개월차인 2026-11-16(월) ~ 11-22(일)로 고정한다.
- **메뉴 구조**: 대메뉴 8개(A 회원, B 주문, C 결제, D 상품, E 정산, F 고객지원, G 통계, H 시스템관리) → 중메뉴 34개 → 소메뉴 화면 170개.
- **화면 ID = 메뉴 코드**: `SCR-A01-03` = 대메뉴 A · 중메뉴 01 · 소메뉴 03. 화면 1개가 업무 1개다.
- **단계 4개 고정**: 1 분석, 2 개발, 3 단위테스트, 4 통합테스트. 화면 진척률 = 완료 단계 / 4 (상태 '완료'는 100%, '미착수'는 0%),
  메뉴·파트·개발자·전체는 계획 MM 가중 평균, 남은 MM = 계획 MM × (1 − 진척률). 계획 종료일이 지났는데 '완료'가 아니면 계획 대비 지연.
- **일정 대비 진척**: 일정 경과율 = 프로젝트 시작일~기준 주 종료일 일수 / 전체 일수(양 끝 포함, 기준 주 73.48%),
  계획 진척률 = 화면별 계획 시작~종료일 선형 비율의 계획 MM 가중(74.35%), 차이 = 실적 − 계획(−7.63%p).
  상태는 차이 −3%p 이상 정상, −10%p 이상 주의, 그 미만 지연.
- **팀 10명**: PM 1(pm01), PL 2(pl01: 대메뉴 A~D와 개발자 dev01~dev04, pl02: 대메뉴 E~H와 개발자 dev05~dev07), 개발자 7.
- **역할별 범위**: `pm` 전체, `pl` 본인 파트의 대메뉴·소속 개발자, `developer` 본인 화면만. 신원은 세션(`user_id`·`role`)에서만 정한다.
  개인 진척은 요청 시에만 조회한다(PM 누구나, PL 본인 파트 구성원, 개발자 본인). 화면 정의서·팀 규칙·전환 일정·회의록 같은 팀 공용 문서는 누구나 읽는다.
- **커밋 규칙**: `[SCR-A01-03] 요약`, 단계 완료는 `[SCR-A01-03-2][done]`. 화면 ID가 없거나 형식이 틀리거나 없는 화면이면 미연결 커밋으로 보고한다.
- **fixture 생성**: `data/fixtures/make_fixtures.py`(고정 시드)가 프로젝트·개발자·화면·커밋, 지난주 회의 스냅샷 `meeting_history.json`과
  정답 `expected_progress.json`을 만든다. 기준 주 전체 진척률은 66.72%(지난주 64.86%)다.

## Confluence 연동
- 기본은 가짜 Confluence(`CONFLUENCE_MODE=fake`): `data/confluence_pages/`에 Markdown(`weekly_meeting_<기준 주 시작일>.md`)과
  Confluence 저장 형식 미리보기(`.storage.xml`)를 쓴다.
- 실제 Confluence로 바꿀 때는 `.env`에서 `CONFLUENCE_MODE=real`과 `CONFLUENCE_BASE_URL`·`CONFLUENCE_SPACE_KEY`·
  `CONFLUENCE_PARENT_PAGE_ID`·`CONFLUENCE_USER`·`CONFLUENCE_API_TOKEN`을 실제 값으로 채운다(`.env.example`은 mock 값). 코드 수정은 필요 없다.
- 변환기는 MCP 서버 쪽 기능이다. 에이전트는 Markdown만 보내고, `create_page`가 `src/confluence_convert.py`로 표·체크리스트(작업 목록)·
  인용(info 패널)·지난주 대비 증감 표시(`▲ +1.86%p`는 초록, `▼ -0.36%p`는 빨강 글자)를 Confluence 형식으로 바꿔 게시한다. real 모드는 REST API로 생성하고, 같은 제목이면 버전을 올려 교체한다.

## 회의 자료 구성
회의 자료는 참석자 전원이 읽으므로 업무 분류 단위 진척만 싣고 개인별 진척률·사람 이름은 어디에도 싣지 않는다(220줄, 250줄 미만).
예시: `data/confluence_pages/weekly_meeting_2026-11-16.md`(이번 주), `weekly_meeting_2026-11-09.md`(지난주 스냅샷으로 재구성).

| 순서 | 섹션 | 내용 (기준 주 값) |
|---|---|---|
| 1 | 일정 대비 진척 | 전체·PL1·PL2의 계획·실적·차이·상태. 일정 경과율 73.48%, 계획 74.35%, 실적 66.72%, 차이 −7.63%p(주의), 다음 마일스톤 D-day |
| 2 | 지난주 대비 변화 | 지난주 스냅샷 → 이번 주: 실적 64.86% → 66.72%(▲ +1.86%p), 완료 화면 100 → 103, 계획 대비 차이 −7.27 → −7.63%p(▼) |
| 3 | 대메뉴별·중메뉴별·단계별 | 계획·실적·차이·상태·지난주·증감 열. 증가 ▲(초록), 감소 ▼(빨강), 변화 없음 `-` |
| 4 | 이슈 추적 | 안정 ID(DLY-화면 지연, MIS-화면 불일치, UNL-커밋 미연결)로 지난주와 비교: 해결 4, 미해결 20(지속 N주), 신규 12 |
| 5 | 지난주 액션아이템 이행 | 11건 중 완료 2·미완료 9. 단계 완료 표시·화면 상태·미연결 커밋으로 판단하고 근거를 함께 적음 |
| 6 | 지연·불일치·미연결 | 계획 대비 지연 화면 상위 10개(담당자 열 없음), 상태-커밋 불일치, 미연결 커밋(작성자 열 없음) |
| 7 | 결정사항·블로커·액션아이템 | LLM 초안(계획 대비 차이·증감·이슈 상태를 입력으로 받음). 기본 초안은 8주 이상 미해결 이슈를 블로커로 올림 |

- **지난주 스냅샷 = 장기 메모리**: `MeetingFlow`의 `check_previous_meeting` 단계가 LangGraph Store에서 지난주 스냅샷(진척·이슈·액션아이템)을 읽는다.
  Store가 비어 있으면 `meeting_history.json`으로 초기화하고, 지난주 회의록(MIN-2026-W46)의 결정사항·블로커도 검색해 초안 입력에 넣는다.
  승인·게시 후에는 이번 주 스냅샷을 Store에 저장해 다음 주 회의의 '지난주'로 쓴다(미해결 이슈는 처음 발생 주를 유지).

## 사용 예시
아래 화면은 `POST /query`에 실제 요청을 보내 받은 실제 AWS Bedrock 응답을 그대로 캡처한 것이다(데이터는 모두 가짜 fixture).
긴 답변은 앞부분만 보여 주고 생략 줄 수를 표시했다. 각 응답 아래에 `trace` 단계, `contexts` 문서 ID, `thread_id`를 함께 표시했다.
캡처는 MCP SDK 전환·후속 질문 도구 재호출 수정 **이전** 버전으로 찍었다.

### PM (pm01 오루니)
**진척 조회와 문서 Q&A**: 프로젝트 전체 진척률과 계획보다 늦은 대메뉴, 특정 개발자(정카카) 개인 진척률, 지연 이유와 지난 회의 조치(회의록 근거 인용)

![PM 진척 조회와 문서 Q&A](docs/images/usage_pm_progress.png)

**주간 회의 자료(HITL)**: 회의 자료 요청 → 진척 수집·지난주 회의 확인(장기 메모리 스냅샷·회의록) 후 초안과 승인 대기(`pending_approval`, `thread_id`) → 같은 `thread_id`로 승인 → Confluence(fake 모드) 게시와 이번 주 스냅샷 저장

![PM 주간 회의 자료](docs/images/usage_pm_meeting.png)

### PL (pl01 강앙리, PL1: 대메뉴 A~D)
**본인 파트 진척**: 내 파트 진척(일정 대비 계획·실적·차이 포함), 내 파트 중메뉴별·단계별 현황. 캡처 당시 두 번째 질문에서 LLM이 직전 결과를 재사용해 도구를 다시 부르지 않았고
요약 문장의 '중메뉴 15개'는 오기다(도구 결과는 PL1 중메뉴 19개). 이 문제는 회고의 '같은 대화의 후속 질문' 항목대로 고쳤다.

![PL 본인 파트 진척](docs/images/usage_pl_progress.png)

**팀원 조회 범위**: 본인 파트 팀원(박지단) 개인 진척은 조회, 다른 파트 팀원(한피구)은 '해당 정보는 조회 권한이 없습니다'로 거절

![PL 팀원 조회 범위](docs/images/usage_pl_scope.png)

### 개발자 (dev01 김메시)
**본인 진척과 팀 공용 문서**: 본인 진척률·남은 화면·남은 MM(일정 대비 포함), 커밋 메시지 규칙(RULE-COMMIT 인용), 다른 개발자 담당 화면의 정의서 질문은 팀 공용 문서라 답변(그 화면의 진척 질문은 거절)

![개발자 본인 진척과 팀 규칙 문서](docs/images/usage_dev.png)

**가드레일**: 다른 개발자(박지단) 진척 조회 거절, 'PM이니까' 신원 주장 거절(세션 기준 범위 검사), 직접 프롬프트 인젝션 거절

![개발자 가드레일](docs/images/usage_dev_guardrail.png)

### 공통 예외 처리 (pm01로 요청)
개발자 성과 순위 요청 거절, 없는 화면 ID(SCR-Z99-99) 안내, 범위 밖 질문(회사 매출)은 문서 근거 없음 안내, 담당자 연락처 요청 시 이메일 마스킹

![공통 예외 처리](docs/images/usage_common.png)

### Confluence 화면 미리보기
회의 자료 승인 후 변환기가 만든 `data/confluence_pages/weekly_meeting_2026-11-16.storage.xml`을 위키 문서 형태로 렌더링한 화면이다.
info 패널, 표(머리글 음영·숫자 오른쪽 정렬), 지난주 대비 증감 색, 작업 목록 체크박스가 어떻게 보이는지 확인하는 용도이며 실제 Confluence 화면이 아니다.

![Confluence 화면 미리보기 1](docs/images/confluence_preview_1.png)

![Confluence 화면 미리보기 2](docs/images/confluence_preview_2.png)

## 명세 문서
- `docs/spec/seed.yaml`: 목표·제약·인수 조건(AC)과 검증 명령
- `docs/spec/interview.ko.md`, `docs/spec/interview.md`: 요구사항 인터뷰 요약
