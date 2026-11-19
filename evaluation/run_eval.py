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


def rescore_ragas(round_no: int) -> dict:
    """저장된 답변·근거 문서로 RAGAS 지표만 다시 계산한다 (에이전트·judge 판정은 그대로)."""
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
            run.update(ragas_scores(r["input"], run["answer"], ctx, r["reference"]))
        case.update(mean_metrics(case["runs"]))
        print(f"rescored case {case['id']}: " + ", ".join(f"{k} {case[k]}" for k in RAGAS_METRICS), flush=True)
    res["summary"].update(mean_metrics([c for c in res["cases"] if "faithfulness" in c]))
    res["summary"]["ragas_errors"] = ragas_error_count(res["cases"])
    res["config"]["metric_impl"] = METRIC_IMPL
    res["config"]["ragas_rescored"] = ("저장된 답변으로 RAGAS 4개 지표만 재채점 "
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
               "ResponseRelevancy (Bedrock LLM + Titan 임베딩)")


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
    return {"faithfulness": Faithfulness(llm=llm), "context_recall": LLMContextRecall(llm=llm),
            "context_precision": LLMContextPrecisionWithReference(llm=llm),
            "answer_relevancy": ResponseRelevancy(llm=llm, embeddings=emb)}


def ragas_scores(question: str, answer: str, contexts: list[dict], reference: str, metrics: dict | None = None) -> dict:
    from ragas import SingleTurnSample

    doc_ids = {c["doc_id"] for c in contexts} | {d["doc_id"] for d in _corpus()}
    sample = SingleTurnSample(user_input=question, response=strip_citations(answer, doc_ids),
                              retrieved_contexts=[c["text"] for c in contexts] or [""], reference=reference)
    try:
        metrics = metrics if metrics is not None else ragas_metrics()

        async def run():
            return {k: await metrics[k].single_turn_ascore(sample) for k in RAGAS_METRICS}
        return {k: round(float(v), 4) for k, v in asyncio.run(run()).items()}
    except Exception as e:  # noqa: BLE001  실패는 점수 없음(None)으로 기록하고 평균에서 뺀다 (숫자를 지어내지 않음)
        return {**{k: None for k in RAGAS_METRICS}, "ragas_error": str(e)}


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
        L += [f"1차 대비 통과 증가: {s['pass_count'] - prev['summary']['pass_count']:+d}건 "
              f"({prev['summary']['pass_count']} → {s['pass_count']})", ""]
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
    args = ap.parse_args()
    n = args.round
    res = rescore_ragas(n) if args.rescore_ragas else evaluate(n)
    ev = ROOT / "evaluation"
    (ev / f"round{n}_results.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    prev = json.loads((ev / "round1_results.json").read_text(encoding="utf-8")) if n == 2 else None
    (ev / f"round{n}_report.md").write_text(write_report(res, prev), encoding="utf-8")
    print(json.dumps(res["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
