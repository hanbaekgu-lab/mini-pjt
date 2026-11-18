# Interview summary (session interview_20261118_060344, MCP ambiguity 0.14, seed_ready)

## Context / constraints (course)
- AX course, Day 8~10 individual mini project, ~1.5 days real dev. Deliverables: src/(agent.py, tools.py, retriever.py), data/, POST /query API returning {answer, contexts:[{doc_id,text}], trace:[{step,input,output}]}, SERVICE.md (user/problem/value, expansion, tools/data, 3~5 guardrails, success criteria), test_queries.csv (>=10, recommended 15~20; cols id,category,input,expected_traits,forbidden,expected_tools,note; categories positive 40/negative 20/edge 25/guardrail 15), evaluation/round1_report.md & round2_report.md (RAGAS), README (patterns used, trial-and-error retrospective). Public GitHub repo. Required patterns 1,3,11,12; >=6 recommended; user wants ALL 12.
- Grading: SERVICE.md quality, sharpness of problem/value, agent completeness, in-out pass rate & round1->round2 improvement, guardrail adherence.

## Product (user-confirmed)
"주간 진척 보고 에이전트" (working title during interview: "팀 프로젝트 진척 회의 도우미 에이전트"): manages per-developer task allocation & progress during a project; generates summarized weekly meeting doc into Confluence; in-meeting Q&A over meeting materials.
- Today: developers type progress % manually into Confluence; PM/PL composes weekly doc manually.
- Users: PM/PL (full view) + developers (own data only).
- Representative questions: PM "이번 주 회의 자료 만들어서 Confluence에 올려줘"; Dev "이번주 내가 맡은 업무 진척율이 어느정도되고, 어떤 업무가 남았어? 예상 소요 MM는 어느정도야?"; RAG "TASK-012가 왜 지연됐고, 지난 회의에서 어떤 조치를 하기로 했어?"
- Fake data only; no real git integration in this project.

## Progress computation
- Commit message carries task ID "[TASK-012] ..." ; ID-less commits flagged "미연결". Commit author email ↔ developer mapping (also used for permission).
- Progress = subtask completion ratio weighted by task plan MM; subtask done judged by commit marker "[TASK-012-3][done]". Commit count only as evidence.
- Remaining MM = plan MM × (1 − progress). Numbers computed deterministically by tools; LLM only narrates.
- Week = Mon–Sun. Task status wins over commits; mismatch shown as warning.
- Fixtures: tasks, subtasks, plan MM, commit log.

## RAG corpus & eval ground truth
- RAG answers reasons/decisions/rules, not numbers.
- Fake Korean docs 15~20: weekly minutes 4~6 weeks (fixed sections 결정사항/블로커/액션아이템), task definitions (numbered done criteria), team rules (commit convention, MM estimation guide, minutes template). Past Confluence meeting docs (generated) are re-indexed into corpus.
- Ground truth: blocker/action-item sentences of specific week's minutes + source doc IDs fixed in advance. Tool outputs included in contexts; numeric accuracy by exact-match separately.

## Success criteria
- In-out set: >=16/20 pass (80%), count-based.
- Numbers exact-match 100% (miss = bug, not improvement metric).
- Guardrail block 100%.
- RAGAS faithfulness >=0.85, context recall >=0.80 on explanatory cases only.
- Meeting doc checklist all pass: sections present, numbers match tool output, not published before HITL approval.
- Round1->Round2: +3 passing cases (+15%p).
- Round1 baseline = dense-only, pre-declared in README; same set & same judge; 3-run average.
- Round2 levers by failure type: hybrid BM25+dense + rerank; query expansion (task ID, synonyms); week/task metadata filter; citation + "문서에 없으면 모른다" prompt.

## Guardrails (5)
1. Role scope: PM/PL all, dev own only (incl. "누가 제일 느려?", "나 PM이니까"). Refuse with fixed text "해당 정보는 조회 권한이 없습니다".
2. PII masking (email, phone) on all final outputs and before Confluence publish — implemented once in middleware.
3. Prompt injection: direct → refuse; indirect in retrieved docs (re-indexed Confluence, commit msgs) → drop chunk, answer original question; never reveal system prompt.
4. No Confluence publish without HITL; only PM/PL can approve.
5. No person evaluation/ranking for any role; offer task-level status instead.
- Enforcement in tool layer before execution. POST /query body extended with user_id, role (demo-level, unauthenticated, stated in SERVICE.md). Real email used internally for mapping; masking only on output.

## Pattern mapping (12)
1 LCEL+Pydantic structured outputs; 2 ReAct bind_tools in progress agent; 3 RAG (round1 dense, round2 hybrid+rerank+query expansion); 4 multi tools (progress calc, remaining MM, commit lookup, mismatch warning); 5 MCP fake Confluence server (stdio, local markdown; tools get_page/create_page/search_pages; real adapter only if CONFLUENCE_MODE=real); 6 guardrails above; 7 HITL interrupt() before publish; 8 middleware PII mask + conversation summarization + retry; 9 Supervisor LLM-routing to progress / docs-QA / meeting-doc agents; 10 Plan-Execute for meeting doc flow (collect progress → check last action items → draft → approve → publish) + LangGraph Store keeping weekly action-item status only; 11 Trace LangSmith default, local JSON log fallback, also in API trace field; 12 RAGAS + LLM-as-Judge.
- HITL resume: pending response returns thread_id; next POST /query with thread_id + approval: approve|reject.
- Downgrade order: plan-execute→fixed chain; memory→JSON; supervisor 3→2; drop summarization. Never downgrade 1,3,11,12, guardrails, HITL.

## Guard follow-up decisions (user-approved)
1. Corpus split: eval corpus fixed snapshot in data/corpus/. Generated meeting docs (fake Confluence) saved to data/generated/, added only to runtime index, excluded from evaluation. One minutes doc with hidden injected instruction pre-included in fixed corpus for indirect-injection test.
2. Case pass = expected_traits satisfied (LLM-as-Judge) AND no forbidden AND expected_tools called. 3 runs, pass if >=2. 20 cases = positive 8 / negative 4 / edge 5 / guardrail 3.
3. "This week" fixed to fixture reference date (e.g., 2026-11-16 ~ 2026-11-22, Mon–Sun), not real now.
4. Developer role on RAG path: other people's progress numbers masked/withheld; team-shared decisions/blockers visible (guardrail 1 applies to RAG too).
5. Agent and judge use same model, temperature=0; concrete model named in README per course-provided API.
- Out of scope / demo-level: body role spoofing itself (unauthenticated); only natural-language role claims are tested.

## SERVICE.md value & expansion
- Team 6 devs (example; any team size); PM/PL weekly doc 3h → 5min; dev manual entry 30min/week → 0; ≈5h55m/week saved (estimate; general: 2h55m + devs × 30min). Non-time value: commit-evidenced freshness/objectivity; meeting time on blockers.
- Expansion A (internal contribution): team pilot with real Git/Jira/Confluence → company-wide standardization → template to other teams. No monetization.
- State: "보고 부담을 줄이는 도구이며 개인 평가에는 사용하지 않음".
