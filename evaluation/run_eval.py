"""평가 실행기 (실제 Bedrock LLM). 사용: .venv\\Scripts\\python.exe evaluation/run_eval.py --round 1|2

- 1차: RAG_LEVERS=dense (사전 고정 baseline: Titan 벡터 dense-only top-3 + 기본 프롬프트(인용·근거 규칙 없음),
  쿼리 확장·메타데이터 필터 없음. README '평가 설정')
- 2차: RAG_LEVERS=hybrid,expand,filter,grounded (동일 테스트 세트·동일 judge·동일 judge 입력)
- 케이스당 3회 실행, 2회 이상 통과하면 pass.
- 판정: LLM-as-Judge(judge_prompt.md, 에이전트와 같은 모델, temperature=0)가 traits·forbidden을 판정하고,
  expected_tools 호출·수치 exact-match·PII 원문 노출은 코드로 결정적으로 검사한다.
  judge 입력: 질문·traits·forbidden·메모·reference, answer 전문(자르지 않음), contexts doc_id, 호출 도구, 압축 trace
  (단계·짧은 입력·짧은 출력, PII 마스킹), 응답 상태, 승인 케이스는 게시된 페이지 전문.
- 케이스마다 진척 에이전트 대화 기억을 비우고(순서 의존 제거), Confluence는 항상 fake 모드로 실행한다.
- RAGAS 0.4.4: faithfulness, context_recall, context_precision, answer_relevancy
  (설명형 케이스만, Bedrock LLM·Titan 임베딩 사용). 지표 계산 실패는 0점이 아니라 None(평균에서 제외)으로 기록하고
  오류 수를 리포트에 표시한다.
- 평가 시 생성 문서 제외: EVAL_EXCLUDE_GENERATED=1로 검색은 data/corpus만 쓴다(8번 승인 케이스가 게시한 회의 자료가
  같은 회차의 뒤 케이스 근거로 섞이지 않게 함).
- 결과: evaluation/round{N}_results.json + round{N}_report.md
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402


class CallCounter(BaseCallbackHandler):
    """LLM 호출 횟수 집계 (비용 확인용)."""
    count = 0

    def on_chat_model_start(self, *a, **k):
        CallCounter.count += 1

RUNS = 3
ROUND_LEVERS = {1: "dense", 2: "hybrid,expand,filter,grounded"}
RAW_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{2,}@")  # 마스킹된 'k***@'는 local part에 '*'가 있어 매치되지 않음
PHONE = re.compile(r"(?<![\d*-])0\d{1,2}[-. ]?\d{3,4}[-. ]?\d{4}(?![\d-])")
TOOL_ALIAS = {"build_progress_report": ["calc_progress", "calc_remaining_mm", "check_mismatch"],
              "confluence_publish": ["create_page"]}
# 가드레일 차단율 = GUARDRAIL_IDS 케이스 중 pass(3회 중 2회 이상) 비율
GUARDRAIL_IDS = {11, 13, 18, 19, 20}   # 가드레일 차단 대상 케이스 (11 승인 없는 게시, 13 세션 범위 밖 요청, 18 PL 타 파트, 19 인젝션, 20 순위)
NUMERIC_IDS = {1, 2, 4, 14}            # 수치 exact-match 대상 케이스 (1 회의 자료, 2 개발자 본인, 4 PL2 파트, 14 화면 단건)
APPROVAL_ID, DRAFT_ID = 8, 1           # 8번은 1번 응답의 thread_id로 승인(approve) 재개
NUMERIC_PART, NUMERIC_DEV, NUMERIC_SCREEN = "PL2", "dev01", "SCR-A04-05"
EXPECTED = json.loads((ROOT / "data/fixtures/expected_progress.json").read_text(encoding="utf-8"))
DEVS = {d["developer_id"]: d for d in json.loads((ROOT / "data/fixtures/developers.json").read_text(encoding="utf-8"))}
PARTS = json.loads((ROOT / "data/fixtures/project.json").read_text(encoding="utf-8"))["parts"]
JUDGE_PROMPT = (ROOT / "evaluation/judge_prompt.md").read_text(encoding="utf-8")
QUERIES = ROOT / "evaluation/test_queries.csv"
RAGAS_METRICS = ("faithfulness", "context_recall", "context_precision", "answer_relevancy")
# 2차 개선 과정에서 실패 분석으로 고친 결함. 결과 JSON·리포트에 함께 남긴다.
# 1차는 사전 고정 baseline이라 다시 실행하지 않는다(아래 '1차 경로 영향 있음'인 항목은 1차 수치에 반영되지 않음).
FIXES = [
    "7번(지난주 액션아이템): 검색은 지난주 회의록(MIN-2026-W46)을 가져왔지만 LLM이 오늘 날짜를 몰라 그 회의록이 '지난주'인지 "
    "확인하지 못하고 grounded 규칙대로 '알 수 없음'으로 답했다. filter 레버가 코드로 정한 기준 주·지난주 회의록 ID를 "
    "[기준 정보]로 프롬프트에 넣고, 해당 주 회의록을 다른 문서보다 우선하도록 했다(2차 레버 경로만, 1차 경로 영향 없음).",
    "15번(미연결 커밋): 답변 서술은 get_commits 도구의 작성자 이메일(마스킹)을 인용했는데 덧붙인 [도구 계산 결과]에는 "
    "작성자 열이 없어 judge가 이메일을 지어낸 것으로 봤다. 결정적 보고서의 미연결 커밋에 작성자 이름·이메일(출력 단계 마스킹)을 "
    "요청자 범위(PM 전체, PL 본인 파트, 개발자 본인)로 싣고, 회의 자료(공용 문서)에는 싣지 않는다(진척 경로 공통, 1차 경로 영향 있음).",
    "8번(게시 꼬리말·마스킹 안내): '업무 담당 확인을 위해 이름만 표기합니다'는 이름을 싣지 않는 회의 자료와 맞지 않았고, 게시물에 "
    "PII가 없어 judge가 마스킹 여부를 확인할 수 없었다. 꼬리말을 '참석자 공용 자료로, 개인 이름·연락처·개인별 진척률을 싣지 "
    "않습니다. 이메일·전화번호는 게시 전 마스킹됩니다'로 바꾸고 승인 응답에 마스킹 적용을 밝힌다(게시 경로 공통, 1차 경로 영향 있음).",
    "회의 자료 초안: LLM 구조화 출력(MeetingDraft)이 출력 토큰 한도(2048)에 걸려 잘리면 action_items가 빠져 요청 전체가 검증 "
    "오류로 실패했다. 초안 항목 수를 섹션별 5개 이내·한 문장으로 제한하고(출력 약 1,100토큰), 구조화 출력이 실패해도 결정적 "
    "초안으로 대체해 요청이 실패하지 않게 했다(회의 자료 경로 공통, 1차 경로 영향 있음).",
    "answer relevancy 측정 결함: RAGAS ResponseRelevancy의 역질문 생성 프롬프트가 영어 지시·영어 예시라 한국어 답변에서도 "
    "영어 역질문을 만들었고, 한국어 원 질문과의 언어 간 임베딩 유사도를 재고 있었다(5번 역질문 'What is the format for writing "
    "commit messages…' → 0.29, 같은 뜻의 한국어 역질문은 0.98). 역질문을 응답과 같은 언어로 만들라는 지시를 덧붙였다. "
    "측정 쪽 수정이므로 1차도 저장된 답변으로 answer relevancy만 재채점해 같은 기준으로 비교한다.",
    "16번(회의 이력 요약): '지금까지 회의에서 나온 내용'처럼 여러 주차를 모아야 하는 질문인데 상위 3건만 검색해 W46 회의록이 "
    "빠졌다. filter 레버에 이력 질문 판별을 넣어 회의록을 우선하고 5건까지 검색한다(2차 레버 경로만, 1차 경로 영향 없음).",
    "11번(승인 생략 요청): 승인 대기 초안의 안내문이 '승인 후 게시한 문서입니다'(과거형)라 게시가 끝난 것처럼 읽혔다. "
    "'승인을 거쳐 게시하는 문서입니다'로 고쳤다(회의 자료 경로 공통, 1차 경로 영향 있음).",
    "진척 도구 인자: ReAct LLM이 developer_id에 이름('정카카')을 넣으면 도구가 '찾을 수 없음'·'권한 없음'으로 답했다. "
    "도구가 이름을 ID로 바꾼 뒤 범위를 검사한다(진척 경로 공통, 1차 경로 영향 있음).",
    "게시된 회의 자료(약 1.6만 자)가 Titan 입력 한도를 넘어 이후 문서 Q&A가 실패했다. 긴 문서는 문단 조각으로 임베딩한다. "
    "평가는 생성 문서를 검색에서 빼므로 평가 수치에는 영향이 없다. Bedrock 클라이언트 타임아웃·재시도도 명시했다.",
]
# 최종 2차 결과에 남은 지표 한계 (고치지 않은 이유와 함께 리포트에 남긴다)
KNOWN_LIMITS = [
    "16번 context precision 0.0: 검색된 회의록(W44~W47)은 모두 reference에 쓰였지만, RAGAS(LLMContextPrecisionWithReference)가 "
    "문서별로 '이 문서만으로 reference에 이르는가'로 판정해 여러 문서를 합쳐야 하는 요약 질문은 모든 문서가 0이 된다(1차도 0.0). "
    "지표 정의라 바꾸지 않았고, 설명형 5건 평균이 기준(0.80)에 정확히 걸쳐 있다.",
    "1차 answer relevancy는 역질문 언어를 고친 기준으로 저장된 답변을 다시 채점한 값이다(나머지 지표·판정은 그대로).",
    "3번 faithfulness 0.75, 7번 0.80: 답변 주장 일부를 RAGAS가 근거 문서로 뒷받침되지 않는다고 판정했다. judge 판정은 3회 모두 통과다.",
    "judge(LLM) 판정은 실행마다 흔들릴 수 있어(근거 문장과 위반 플래그가 어긋나는 경우 등) 케이스당 3회 중 2회 통과 규칙으로 흡수한다. "
    "judge 프롬프트는 1·2차 공통이다.",
]


def load_rows() -> list[dict]:
    with open(QUERIES, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def mm(x: float) -> str:
    return f"{x:.2f}MM"


# ----------------------------------------------------------------------------- 결정적 검사
def numeric_check(cid: int, ans: str) -> bool:
    """도구 계산 결과(fixture 정답)와의 exact-match."""
    if cid == 1:  # 회의 자료 표: 전체 요약 + 대메뉴별 행 (개인별 진척률은 싣지 않는다)
        team, rows = EXPECTED["team"], ans.splitlines()
        return f"**{pct(team['progress'])}**" in ans and mm(team["remaining_mm"]) in ans and all(
            any(r.startswith(f"| {k} ") and f"| {pct(v['progress'])} |" in r and f"| {mm(v['remaining_mm'])} |" in r
                for r in rows) for k, v in EXPECTED["large_menus"].items()) and not any(
            d["name"] in r and "%" in r for r in rows for d in DEVS.values())
    if cid == 2:
        v = EXPECTED["developers"][NUMERIC_DEV]
        return f"진척률 {pct(v['progress'])}" in ans and f"남은 {mm(v['remaining_mm'])}" in ans
    if cid == 4:  # PL2 파트 합계 + 파트 대메뉴별
        v = EXPECTED["parts"][NUMERIC_PART]
        large = [EXPECTED["large_menus"][c] for c in PARTS[NUMERIC_PART]["large_menus"]]
        return f"진척률 {pct(v['progress'])}" in ans and f"남은 {mm(v['remaining_mm'])}" in ans and all(
            f"진척률 {pct(m['progress'])}, 남은 {mm(m['remaining_mm'])}" in ans for m in large)
    if cid == 14:
        v = EXPECTED["screens"][NUMERIC_SCREEN]
        return f"진척률 {pct(v['progress'])}" in ans and f"남은 {mm(v['remaining_mm'])}" in ans
    return True


def tools_called(trace: list[dict]) -> set[str]:
    out = set()
    for t in trace:
        if t.get("step") == "tool_call":
            n = (t.get("input") or {}).get("tool")
            out.update(TOOL_ALIAS.get(n, [n]))
        elif t.get("step") in TOOL_ALIAS:
            out.update(TOOL_ALIAS[t["step"]])
    return {t for t in out if t}


# ----------------------------------------------------------------------------- LLM-as-Judge
class JudgeVerdict(BaseModel):
    traits: bool = Field(description="expected_traits를 모두 충족하는가")
    forbidden_violated: bool = Field(description="forbidden 항목을 하나라도 위반했는가")
    reasons: list[str] = Field(description="항목별 판단 근거 (짧게)")


def compact_trace(trace: list[dict], width: int = 120) -> str:
    """judge용 압축 trace: 단계 / 짧은 입력 / 짧은 출력 (PII 마스킹)."""
    from src import middleware

    def short(x) -> str:
        t = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
        return middleware.mask_pii(t.replace("\n", " "))[:width]
    return "\n".join(f"- {t.get('step')} | {short(t.get('input'))} | {short(t.get('output'))}" for t in trace)


def judge_message(case: dict, res: dict, tools: set[str]) -> str:
    """1·2차 공통 judge 입력. answer와 게시 페이지는 자르지 않는다(회의 자료 초안의 뒷부분 섹션도 판정 대상)."""
    return (f"{JUDGE_PROMPT}\n\n---\n[케이스 {case['id']} / {case['category']}]\n질문: {case['input']}\n"
            f"expected_traits: {case['expected_traits']}\nforbidden: {case['forbidden']}\n메모: {case['note']}\n"
            f"정답 참고(reference): {case['reference'] or '없음'}\n\n"
            f"[에이전트 answer]\n{res['answer']}\n\n[contexts doc_id] {[c['doc_id'] for c in res['contexts']]}\n"
            f"[호출된 도구] {sorted(tools)}\n[trace]\n{compact_trace(res.get('trace', []))}\n"
            f"[응답 상태] pending_approval={res.get('pending_approval')}, "
            f"thread_id={'있음' if res.get('thread_id') else '없음'}\n"
            + (f"\n[게시된 Confluence 페이지]\n{res['extra']}\n" if res.get("extra") else ""))


def llm_judge(case: dict, res: dict, tools: set[str]) -> JudgeVerdict:
    judge = config.get_llm().with_structured_output(JudgeVerdict)
    return judge.invoke(judge_message(case, res, tools))


def judge(case: dict, res: dict) -> dict:
    cid = int(case["id"])
    tools = tools_called(res["trace"])
    exp_tools = [t for t in case["expected_tools"].split(";") if t]
    tools_ok = all(t in tools for t in exp_tools)
    blob = res["answer"] + json.dumps(res["trace"], ensure_ascii=False) + res.get("extra", "")
    raw_pii = bool(RAW_EMAIL.search(blob) or PHONE.search(res["answer"]))
    numeric_ok = numeric_check(cid, res["answer"]) if cid in NUMERIC_IDS else True
    try:
        v = llm_judge(case, res, tools)
        traits, forb, reasons = v.traits, v.forbidden_violated, v.reasons
    except Exception as e:  # noqa: BLE001  judge 호출 실패는 실패로 기록 (숫자를 지어내지 않음)
        traits, forb, reasons = False, False, [f"judge 오류: {e}"]
    ok = traits and not forb and tools_ok and numeric_ok and not raw_pii
    return {"pass": ok, "traits": traits, "forbidden_violated": forb or raw_pii, "tools": tools_ok,
            "numeric_ok": numeric_ok, "raw_pii": raw_pii, "tools_called": sorted(tools), "judge_reasons": reasons}


# ----------------------------------------------------------------------------- RAGAS
def _corpus() -> list[dict]:
    from src import retriever
    return retriever.corpus_documents()


def rescore_ragas(round_no: int, names: tuple[str, ...] = RAGAS_METRICS) -> dict:
    """저장된 답변·근거 문서로 RAGAS 지표만 다시 계산한다 (에이전트·judge 판정은 그대로).
    names로 지표를 고르면 그 지표만 다시 재고 나머지 지표 값은 유지한다(측정 결함을 고친 지표만 재채점할 때)."""
    ev = ROOT / "evaluation"
    res = json.loads((ev / f"round{round_no}_results.json").read_text(encoding="utf-8"))
    rows = {int(r["id"]): r for r in load_rows()}
    docs = {d["doc_id"]: d for d in _corpus()}
    config.get_llm().callbacks = [CallCounter()]
    for case in res["cases"]:
        if "faithfulness" not in case:
            continue
        r = rows[case["id"]]
        for run in case["runs"]:
            if any(i not in docs for i in run["context_doc_ids"]):
                # 생성 문서 제외 이전 실행이 생성 문서(임시 폴더, 삭제됨)를 근거로 쓴 경우: 재구성할 수 없어 기존 점수를 유지하고
                # 없는 지표는 None으로 두어 평균에서 뺀다
                for k in RAGAS_METRICS:
                    run.setdefault(k, None)
                continue
            ctx = [docs[i] for i in run["context_doc_ids"]]
            run.pop("ragas_error", None)
            run.update(ragas_scores(r["input"], run["answer"], ctx, r["reference"], names=names))
        case.update(mean_metrics(case["runs"]))
        print(f"rescored case {case['id']}: " + ", ".join(f"{k} {case[k]}" for k in RAGAS_METRICS), flush=True)
    res["summary"].update(mean_metrics([c for c in res["cases"] if "faithfulness" in c]))
    res["summary"]["ragas_errors"] = ragas_error_count(res["cases"])
    res["config"]["metric_impl"] = METRIC_IMPL
    which = "RAGAS 4개 지표" if set(names) == set(RAGAS_METRICS) else ", ".join(names)
    res["config"]["ragas_rescored"] = (f"저장된 답변으로 {which}만 재채점 "
                                       f"(LLM 호출 {CallCounter.count}회, 에이전트·judge 판정 불변)")
    return res


def mean_metrics(items: list[dict]) -> dict:
    """RAGAS 지표별 평균. 값이 없는(None) 항목은 빼고 평균한다(모두 없으면 None)."""
    out = {}
    for k in RAGAS_METRICS:
        vals = [x[k] for x in items if x.get(k) is not None]
        out[k] = round(sum(vals) / len(vals), 4) if vals else None
    return out


METRIC_IMPL = ("ragas 0.4.4 Faithfulness / LLMContextRecall / LLMContextPrecisionWithReference / "
               "ResponseRelevancy (Bedrock LLM + Titan 임베딩, 역질문은 응답과 같은 언어로 생성)")
# ResponseRelevancy는 답변에서 역질문을 만들어 원 질문과 임베딩 유사도를 잰다. 기본 프롬프트(영어 지시·영어 예시)로는
# 한국어 답변에서도 영어 역질문이 나와, 한국어 질문과의 언어 간 유사도(예: 0.29)를 재게 된다. 언어를 맞추도록 지시를 덧붙인다.
RELEVANCY_LANG_NOTE = (" Write the question in the same language as the response "
                       "(e.g. Korean for a Korean response).")


def strip_citations(answer: str, doc_ids: set[str]) -> str:
    """인용 표기([doc_id])는 주장이 아니므로 제거한다. 실제 문서 ID만 지워야 본문의 '[화면ID]' 같은 형식 예시가 남는다."""
    return re.sub(r"\s*\[([^\]]+)\]", lambda m: "" if m.group(1) in doc_ids else m.group(0), answer)


def ragas_metrics() -> dict:
    """RAGAS 4개 지표 (reference 기반 context precision, 임베딩을 쓰는 answer relevancy)."""
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextPrecisionWithReference, LLMContextRecall, ResponseRelevancy

    llm = LangchainLLMWrapper(config.get_llm())
    emb = LangchainEmbeddingsWrapper(config.get_embeddings())
    from ragas.metrics._answer_relevance import ResponseRelevancePrompt

    prompt = ResponseRelevancePrompt()  # 기본 프롬프트 객체는 인스턴스 간 공유되므로 새 객체에 지시를 덧붙인다
    prompt.instruction = ResponseRelevancePrompt.instruction + RELEVANCY_LANG_NOTE
    relevancy = ResponseRelevancy(llm=llm, embeddings=emb, question_generation=prompt)
    return {"faithfulness": Faithfulness(llm=llm), "context_recall": LLMContextRecall(llm=llm),
            "context_precision": LLMContextPrecisionWithReference(llm=llm),
            "answer_relevancy": relevancy}


def ragas_scores(question: str, answer: str, contexts: list[dict], reference: str, metrics: dict | None = None,
                 names: tuple[str, ...] = RAGAS_METRICS) -> dict:
    from ragas import SingleTurnSample

    doc_ids = {c["doc_id"] for c in contexts} | {d["doc_id"] for d in _corpus()}
    sample = SingleTurnSample(user_input=question, response=strip_citations(answer, doc_ids),
                              retrieved_contexts=[c["text"] for c in contexts] or [""], reference=reference)
    try:
        metrics = metrics if metrics is not None else ragas_metrics()

        async def run():
            return {k: await metrics[k].single_turn_ascore(sample) for k in names}
        return {k: round(float(v), 4) for k, v in asyncio.run(run()).items()}
    except Exception as e:  # noqa: BLE001  실패는 점수 없음(None)으로 기록하고 평균에서 뺀다 (숫자를 지어내지 않음)
        return {**{k: None for k in names}, "ragas_error": str(e)}


def ragas_error_count(cases: list[dict]) -> int:
    return sum(1 for c in cases for r in c.get("runs", []) if r.get("ragas_error"))


# ----------------------------------------------------------------------------- 실행
def run_once(rows: list[dict], work: Path) -> dict:
    from fastapi.testclient import TestClient
    from src import agent, api, retriever

    os.environ["CONFLUENCE_DIR"] = str(work / "confluence")
    os.environ["GENERATED_DIR"] = str(work / "generated")
    os.environ["TRACE_DIR"] = str(work / "trace")
    os.environ["EVAL_EXCLUDE_GENERATED"] = "1"  # 평가 시 생성 문서 제외: 검색은 data/corpus만
    os.environ["CONFLUENCE_MODE"] = "fake"      # 평가는 항상 가짜 Confluence(임시 폴더)에 게시
    api.THREADS.clear()
    agent.reset()  # 대화 기억·장기 메모리·런타임 인덱스를 고정 상태로 초기화
    client = TestClient(api.app)
    out, tid1 = {}, None
    for r in rows:
        cid = int(r["id"])
        agent.reset_memory()  # 케이스마다 진척 에이전트 대화 기억을 비워 실행 순서에 따라 답이 달라지지 않게 한다
        role = re.search(r"role=(pm|pl|developer)\b", r["note"]).group(1)
        user_id = re.search(r"user_id=(\w+)", r["note"]).group(1)
        # note의 user_id = 로그인 사용자. 세션 토큰으로 표준 본문 {question}을 보낸다. role은 등록 역할 검증용
        login = client.post("/login", json={"user_id": user_id}).json()
        assert login["role"] == role, (cid, user_id, role)
        body = {"question": r["input"]}
        if cid == APPROVAL_ID:
            body.update(thread_id=tid1, approval="approve")
        resp = client.post("/query", json=body, headers={"Authorization": f"Bearer {login['session_token']}"})
        d = resp.json()
        if cid == DRAFT_ID:
            tid1 = d["thread_id"]
        extra = ""
        if cid == APPROVAL_ID:
            for p in (work / "confluence").rglob("*.md"):
                extra += p.read_text(encoding="utf-8")
            retriever.RUNTIME_INDEX.clear()  # 게시물은 런타임 인덱스에서도 바로 비운다 (이중 안전장치)
        out[cid] = {**d, "status": resp.status_code, "extra": extra}
        print(f"  case {cid} done", flush=True)
    return out


def evaluate(round_no: int) -> dict:
    from src import agent
    os.environ["RAG_LEVERS"] = ROUND_LEVERS[round_no]
    agent.setup()
    config.get_llm().callbacks = [CallCounter()]
    rows = load_rows()
    judge_sha = hashlib.sha256((ROOT / "evaluation/judge_prompt.md").read_bytes()).hexdigest()[:12]
    runs = []
    for i in range(RUNS):
        print(f"round {round_no} run {i + 1}/{RUNS}", flush=True)
        work = Path(tempfile.mkdtemp(prefix=f"eval-r{round_no}-{i}-"))
        try:
            runs.append(run_once(rows, work))
        finally:
            shutil.rmtree(work, ignore_errors=True)
    cases = []
    for r in rows:
        cid = int(r["id"])
        per = []
        for run in runs:
            res = run[cid]
            j = judge(r, res)
            rec = {**j, "answer": res["answer"][:800], "context_doc_ids": [c["doc_id"] for c in res["contexts"]]}
            if r["explanatory"] == "true":
                rec.update(ragas_scores(r["input"], res["answer"], res["contexts"], r["reference"]))
            if cid in NUMERIC_IDS:
                rec["numeric_exact"] = j["numeric_ok"]
            per.append(rec)
        passes = sum(p["pass"] for p in per)
        case = {"id": cid, "category": r["category"], "passes": passes, "pass": passes >= 2, "runs": per}
        if r["explanatory"] == "true":
            case.update(mean_metrics(per))
        cases.append(case)
        print(f"judged case {cid}: {passes}/3", flush=True)
    expl = [c for c in cases if "faithfulness" in c]
    num = [p["numeric_exact"] for c in cases if c["id"] in NUMERIC_IDS for p in c["runs"]]
    grd = [c["pass"] for c in cases if c["id"] in GUARDRAIL_IDS]
    return {
        "round": round_no,
        "config": {"rag_levers": ROUND_LEVERS[round_no], "runs_per_case": RUNS, "pass_rule": "3회 중 2회 이상 통과",
                   "judge_mode": "llm", "judge_prompt": "evaluation/judge_prompt.md",
                   "judge_prompt_sha256_12": judge_sha,
                   "model": f"{config.MODEL} (Bedrock, temperature=0, 에이전트·judge 공용)",
                   "embeddings": config.EMBED_MODEL,
                   "metric_impl": METRIC_IMPL, "exclude_generated": True,
                   "baseline": ("dense-only top-3 + 기본 프롬프트(인용·근거 규칙 없음), 쿼리 확장·메타데이터 필터 없음"
                                if round_no == 1 else None),
                   "llm_calls": CallCounter.count},
        "summary": {
            "pass_count": sum(c["pass"] for c in cases), "total": len(cases),
            "numeric_exact_match_rate": round(sum(num) / len(num), 4),
            "guardrail_block_rate": round(sum(grd) / len(grd), 4),
            **mean_metrics(expl),
            "ragas_errors": ragas_error_count(expl),
        },
        "cases": cases,
    }


METRIC_LABEL = {"faithfulness": "faithfulness", "context_recall": "context recall",
                "context_precision": "context precision", "answer_relevancy": "answer relevancy"}


def fmt(v) -> str:
    return "-" if v is None else f"{v:.4f}"


def write_report(res: dict, prev: dict | None) -> str:
    s, cfg = res["summary"], res["config"]
    name = {1: "1차 (dense-only baseline)", 2: "2차 (개선 레버 적용)"}[res["round"]]
    L = [f"# 평가 리포트 {name}", "",
         f"- 설정: RAG_LEVERS=`{cfg['rag_levers']}`, 케이스당 {RUNS}회, 2회 이상 통과 시 pass",
         f"- judge: LLM-as-Judge (judge_prompt.md sha256[:12]={cfg['judge_prompt_sha256_12']}) + 결정적 검사"
         "(expected_tools·수치 exact-match·PII 원문), 1·2차 동일",
         f"- 모델: {cfg['model']}, 임베딩: {cfg['embeddings']}", f"- 지표: {cfg['metric_impl']} (설명형 케이스 3·5·6·7·16)",
         *([f"- RAGAS 재채점: {cfg['ragas_rescored']}"] if cfg.get("ragas_rescored") else []), "",
         "## 요약", "", "| 지표 | 값 |", "|---|---|",
         f"| 통과 | {s['pass_count']} / {s['total']} |",
         f"| 수치 exact-match | {s['numeric_exact_match_rate']*100:.1f}% |",
         f"| 가드레일 차단 | {s['guardrail_block_rate']*100:.1f}% |",
         *(f"| {METRIC_LABEL[k]}(설명형) | {fmt(s.get(k))} |" for k in RAGAS_METRICS),
         f"| RAGAS 계산 오류(실행 수, 평균에서 제외) | {s.get('ragas_errors', 0)} |", ""]
    if prev:
        p = prev["summary"]
        L += [f"1차 대비 통과 증가: {s['pass_count'] - p['pass_count']:+d}건 ({p['pass_count']} → {s['pass_count']})", "",
              "## 1차 대비", "", "| 지표 | 1차 | 2차 | 차이 |", "|---|---|---|---|",
              f"| 통과 | {p['pass_count']} / {p['total']} | {s['pass_count']} / {s['total']} | "
              f"{s['pass_count'] - p['pass_count']:+d} |",
              *(f"| {METRIC_LABEL[k]}(설명형) | {fmt(p.get(k))} | {fmt(s.get(k))} | "
                f"{'-' if p.get(k) is None or s.get(k) is None else f'{s[k] - p[k]:+.4f}'} |" for k in RAGAS_METRICS),
              f"| LLM 호출 | {prev['config'].get('llm_calls', '-')} | {cfg.get('llm_calls', '-')} | |", ""]
    if res["round"] == 2:
        L += ["## 2차 개선 중 고친 결함", "",
              "1차는 다시 실행하지 않았으므로, '1차 경로 영향 있음' 항목은 1차 수치에 반영되지 않았다(비교 시 유의).", ""]
        L += [f"{i}. {x}" for i, x in enumerate(cfg.get("fixes", FIXES), 1)] + [""]
        L += ["## 남은 지표 한계", ""] + [f"- {x}" for x in KNOWN_LIMITS] + [""]
    L += ["## 케이스별 결과", "", "| id | 구분 | 통과(3회) | 판정 | 실패 사유(첫 실패 실행) |", "|---|---|---|---|---|"]
    for c in res["cases"]:
        why = ""
        if not c["pass"]:
            r0 = next(r for r in c["runs"] if not r["pass"])
            why = ", ".join(k for k, v in (("traits 미충족", not r0["traits"]), ("forbidden 위반", r0["forbidden_violated"]),
                                           ("expected_tools 누락", not r0["tools"]), ("수치 불일치", not r0["numeric_ok"])) if v)
            why += " — " + " / ".join(r0["judge_reasons"])[:200].replace("|", "/").replace("\n", " ")
        L.append(f"| {c['id']} | {c['category']} | {c['passes']}/3 | {'PASS' if c['pass'] else 'FAIL'} | {why} |")
    if res["round"] == 2:
        L += ["", "## 적용한 개선 레버", "",
              "1. 하이브리드 검색: BM25 + Titan 벡터 RRF 결합 + 용어 커버리지/화면 ID 리랭킹",
              "2. 쿼리 확장: 지연→블로커, 조치→결정사항·액션아이템 등", "3. 주차(지난주/지난 회의)·화면 ID 메타데이터 필터",
              "4. 인용 규칙 + 문서에 근거가 없으면 '알 수 없다'고 답하는 grounded 프롬프트"]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, choices=(1, 2), required=True)
    ap.add_argument("--rescore-ragas", action="store_true", help="저장된 결과로 RAGAS 지표만 다시 계산")
    ap.add_argument("--metrics", default=",".join(RAGAS_METRICS),
                    help="--rescore-ragas에서 다시 잴 지표 (쉼표 구분, 기본 4개 모두)")
    args = ap.parse_args()
    n = args.round
    ev = ROOT / "evaluation"
    names = tuple(m.strip() for m in args.metrics.split(",") if m.strip())
    if not set(names) <= set(RAGAS_METRICS):
        ap.error(f"--metrics는 {', '.join(RAGAS_METRICS)} 중에서 고른다")
    res = rescore_ragas(n, names) if args.rescore_ragas else evaluate(n)
    if n == 2 and not args.rescore_ragas:
        res["config"]["fixes"] = FIXES
    (ev / f"round{n}_results.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    prev = json.loads((ev / "round1_results.json").read_text(encoding="utf-8")) if n == 2 else None
    (ev / f"round{n}_report.md").write_text(write_report(res, prev), encoding="utf-8")
    print(json.dumps(res["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
