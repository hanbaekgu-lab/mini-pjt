"""주간 회의 자료 생성.

- build_meeting_material(): LLM 없는 결정적 초안 (테스트·기본 핸들러용)
- MeetingFlow: Plan-Execute LangGraph (계획 → 실행 단계들 → interrupt() 승인 → MCP 게시)
  장기 메모리(LangGraph Store)에는 주차별 액션아이템 진행 상태만 저장한다.
"""
from __future__ import annotations

import uuid
from typing import Optional, TypedDict

from pydantic import BaseModel, Field

from src import tools


def meeting_title() -> str:
    return f"주간회의 자료 {tools.load_reference_week()['start']}"


def build_meeting_material() -> str:
    report = tools.build_progress_report()
    names = {d["developer_id"]: d["name"] for d in tools.load_developers()}
    tasks = report["progress"]["tasks"]
    warns, unlinked = report["mismatch_warnings"], report["unlinked_commits"]
    decisions = ["진척 수치는 도구 계산값(계획 MM 가중, 하위 작업 완료 기준)을 사용한다.",
                 f"업무 상태와 커밋이 불일치하면 업무 상태를 우선하고 경고로 표시한다 (이번 주 {len(warns)}건)."]
    blockers = [f"[{w['task_id']}] {w['message']}" for w in warns]
    blockers += [f"미연결 커밋 {c['hash']}: {c['reason']}" for c in unlinked]
    actions = []
    for t in tasks:
        if t["subtasks_remaining"]:
            rest = ",".join(f"{t['task_id']}-{n}" for n in t["subtasks_remaining"])
            actions.append(f"{names.get(t['owner'], t['owner'])}: {rest} 완료 (남은 {t['remaining_mm']:.2f}MM)")
    return render(decisions, blockers, actions, tools.format_progress_report(report))


def render(decisions, blockers, actions, progress_text: str) -> str:
    lines = ["## 결정사항", *[f"- {x}" for x in decisions or ["없음"]],
             "## 블로커", *[f"- {x}" for x in blockers or ["없음"]],
             "## 액션아이템", *[f"- {x}" for x in actions or ["없음"]],
             "## 진척 현황", progress_text]  # 숫자는 도구 결과 그대로 (LLM이 바꾸지 않음)
    return "\n".join(lines)


# ----------------------------------------------------------------------------- Plan-Execute 그래프
STEPS = ("collect_progress", "check_previous_action_items", "draft_material")


class Plan(BaseModel):
    """회의 자료 작성 계획."""
    steps: list[str] = Field(description=f"실행할 단계 이름 목록(순서대로). 사용 가능: {', '.join(STEPS)}")


class MeetingDraft(BaseModel):
    """회의 자료 본문 (숫자는 쓰지 않는다. 진척 수치는 도구 결과를 그대로 붙인다)."""
    decisions: list[str] = Field(description="이번 회의 결정사항 후보")
    blockers: list[str] = Field(description="블로커 (경고·미연결 커밋·지난주 미완료 포함)")
    action_items: list[str] = Field(description="담당자가 드러나는 다음 액션아이템")


class MeetingState(TypedDict, total=False):
    plan: list[str]
    report_text: str
    warnings: list[str]
    previous_items: list[str]
    previous_source: str
    body: str
    approval: str
    page_id: str
    error: str
    log: list[dict]


def _previous_items_from_corpus() -> list[str]:
    """Store가 비어 있으면 지난주 회의록의 액션아이템 섹션으로 초기화한다 (고정 스냅샷)."""
    from src import retriever
    for d in retriever.corpus_documents():
        if d["doc_id"] == "MIN-2026-W46":
            sec = d["text"].split("## 액션아이템", 1)[-1].split("\n\n", 1)[0]
            return [ln.lstrip("- ").strip() for ln in sec.splitlines() if ln.strip().startswith("-")]
    return []


class MeetingFlow:
    def __init__(self, llm, publish):
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.store.memory import InMemoryStore

        self.llm, self.publish_fn = llm, publish
        self.store = InMemoryStore()  # 장기 메모리: ("action_items",) / 주차 시작일
        g = StateGraph(MeetingState)
        g.add_node("plan", self._plan)
        g.add_node("execute", self._execute)
        g.add_node("approve", self._approve)
        g.add_node("publish", self._publish)
        g.add_edge(START, "plan")
        g.add_edge("plan", "execute")
        g.add_edge("execute", "approve")
        g.add_conditional_edges("approve", lambda s: "publish" if s.get("approval") == "approve" else END)
        g.add_edge("publish", END)
        self.graph = g.compile(checkpointer=InMemorySaver(), store=self.store)

    # --- Plan
    def _plan(self, state: MeetingState) -> MeetingState:
        plan = self.llm.with_structured_output(Plan).invoke(
            "주간 진척 회의 자료를 만들기 위한 실행 계획을 세워라. 진척 수치를 먼저 모으고, 지난주 액션아이템을 확인한 뒤 "
            f"초안을 작성해야 한다. 사용 가능한 단계: {', '.join(STEPS)}")
        steps = [s for s in plan.steps if s in STEPS]
        steps += [s for s in STEPS if s not in steps]  # 필수 단계 누락은 코드가 보완
        steps.sort(key=STEPS.index)                    # 데이터 의존 순서 보장
        return {"plan": steps, "log": [{"step": "plan", "input": plan.steps, "output": steps}]}

    # --- Execute: 계획 순서대로 단계 실행
    def _execute(self, state: MeetingState) -> MeetingState:
        out: MeetingState = {"log": list(state.get("log", []))}
        for step in state["plan"]:
            out.update(getattr(self, "_" + step)({**state, **out}))
        return out

    def _collect_progress(self, state):
        report = tools.build_progress_report()
        warns = [f"[{w['task_id']}] {w['message']}" for w in report["mismatch_warnings"]]
        warns += [f"미연결 커밋 {c['hash']}: {c['reason']}" for c in report["unlinked_commits"]]
        log = state["log"] + [{"step": "tool_call", "input": {"tool": n, "plan_step": "collect_progress"}, "output": "ok"}
                              for n in ("calc_progress", "calc_remaining_mm", "check_mismatch")]
        return {"report_text": tools.format_progress_report(report), "warnings": warns, "log": log}

    def _check_previous_action_items(self, state):
        week = tools.load_reference_week()["start"]
        prev = self.store.search(("action_items",))
        prev = [it for it in prev if it.key < week]
        if prev:
            items, src = prev[-1].value["items"], f"store:{prev[-1].key}"
        else:
            items, src = _previous_items_from_corpus(), "corpus:MIN-2026-W46"
        log = state["log"] + [{"step": "tool_call", "input": {"tool": "retrieve_docs", "plan_step": "check_previous_action_items"},
                               "output": src}]
        return {"previous_items": items, "previous_source": src, "log": log}

    def _draft_material(self, state):
        draft = self.llm.with_structured_output(MeetingDraft).invoke(
            "아래 자료로 주간 진척 회의 자료의 결정사항·블로커·액션아이템을 한국어로 작성하라. "
            "진척률·MM 같은 숫자는 쓰지 마라(도구 결과가 따로 붙는다). 사람을 평가하거나 순위를 매기지 마라.\n\n"
            f"[도구 계산 결과]\n{state['report_text']}\n\n[불일치 경고·미연결 커밋]\n" + "\n".join(state["warnings"])
            + "\n\n[지난주 액션아이템]\n" + "\n".join(state["previous_items"]))
        body = render(draft.decisions, draft.blockers, draft.action_items, state["report_text"])
        log = state["log"] + [{"step": "llm_draft", "input": "meeting_material", "output": "drafted"}]
        return {"body": body, "log": log}

    # --- HITL
    def _approve(self, state: MeetingState) -> MeetingState:
        from langgraph.types import interrupt
        decision = interrupt({"title": meeting_title(), "body": state["body"]})
        return {"approval": decision, "log": state["log"] + [{"step": "hitl_decision", "input": "meeting_material",
                                                             "output": decision}]}

    def _publish(self, state: MeetingState) -> MeetingState:
        try:
            page_id = self.publish_fn(meeting_title(), state["body"])
        except Exception as e:  # noqa: BLE001  게시 실패: 게시하지 않음
            return {"error": str(e), "log": state["log"] + [{"step": "confluence_publish", "input": meeting_title(),
                                                            "output": f"error: {e}"}]}
        week = tools.load_reference_week()["start"]
        items = [ln[2:] for ln in state["body"].split("## 액션아이템", 1)[1].split("## 진척 현황")[0].splitlines()
                 if ln.startswith("- ")]
        self.store.put(("action_items",), week, {"items": items})  # 이번 주 액션아이템 상태 저장
        return {"page_id": page_id, "log": state["log"] + [{"step": "confluence_publish", "input": meeting_title(),
                                                           "output": page_id}]}

    # --- 외부 진입점
    def start(self) -> tuple[str, dict]:
        tid = uuid.uuid4().hex
        cfg = {"configurable": {"thread_id": tid}}
        self.graph.invoke({}, cfg)
        return tid, self.graph.get_state(cfg).values

    def resume(self, tid: str, approval: str) -> dict:
        from langgraph.types import Command
        cfg = {"configurable": {"thread_id": tid}}
        self.graph.invoke(Command(resume=approval), cfg)
        return self.graph.get_state(cfg).values

    def reset_store(self) -> None:
        for it in self.store.search(("action_items",)):
            self.store.delete(("action_items",), it.key)
