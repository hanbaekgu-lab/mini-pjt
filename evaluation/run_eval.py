"""평가 실행기 (실제 Bedrock LLM). 사용: .venv\\Scripts\\python.exe evaluation/run_eval.py --round 1|2

- 1차: RAG_LEVERS=dense (dense-only baseline, README에 사전 고정)
- 2차: RAG_LEVERS=hybrid,expand,filter,grounded (동일 테스트 세트·동일 judge)
- 케이스당 3회 실행, 2회 이상 통과하면 pass.
- 판정: LLM-as-Judge(judge_prompt.md, 에이전트와 같은 모델, temperature=0)가 traits·forbidden을 판정하고,
  expected_tools 호출·수치 exact-match·PII 원문 노출은 코드로 결정적으로 검사한다.
- RAGAS 0.4.4: faithfulness, context_recall (설명형 케이스만, Bedrock LLM 사용)
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
GUARDRAIL_IDS = {11, 13, 18, 19, 20}   # 가드레일 차단 대상 케이스 (11 승인 없는 게시, 13 세션 범위 밖 요청, 18 PL 타 파트, 19 인젝션, 20 순위)
NUMERIC_IDS = {1, 2, 4, 14}            # 수치 exact-match 대상 케이스 (1 회의 자료, 2 개발자 본인, 4 PL2 파트, 14 화면 단건)
APPROVAL_ID, DRAFT_ID = 8, 1           # 8번은 1번 응답의 thread_id로 승인(approve) 재개
NUMERIC_PART, NUMERIC_DEV, NUMERIC_SCREEN = "PL2", "dev01", "SCR-A04-05"
EXPECTED = json.loads((ROOT / "data/fixtures/expected_progress.json").read_text(encoding="utf-8"))
DEVS = {d["developer_id"]: d for d in json.loads((ROOT / "data/fixtures/developers.json").read_text(encoding="utf-8"))}
PARTS = json.loads((ROOT / "data/fixtures/project.json").read_text(encoding="utf-8"))["parts"]
JUDGE_PROMPT = (ROOT / "evaluation/judge_prompt.md").read_text(encoding="utf-8")


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


def llm_judge(case: dict, res: dict, tools: set[str]) -> JudgeVerdict:
    judge = config.get_llm().with_structured_output(JudgeVerdict)
    msg = (f"{JUDGE_PROMPT}\n\n---\n[케이스 {case['id']} / {case['category']}]\n질문: {case['input']}\n"
           f"expected_traits: {case['expected_traits']}\nforbidden: {case['forbidden']}\n메모: {case['note']}\n"
           f"정답 참고(reference): {case['reference'] or '없음'}\n\n"
           f"[에이전트 answer]\n{res['answer'][:4000]}\n\n[contexts doc_id] {[c['doc_id'] for c in res['contexts']]}\n"
           f"[호출된 도구] {sorted(tools)}\n[응답 상태] pending_approval={res.get('pending_approval')}, "
           f"thread_id={'있음' if res.get('thread_id') else '없음'}\n"
           + (f"\n[게시된 Confluence 페이지]\n{res['extra'][:3000]}\n" if res.get("extra") else ""))
    return judge.invoke(msg)


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
    rows = {int(r["id"]): r for r in csv.DictReader(open(ROOT / "test_queries.csv", encoding="utf-8-sig"))}
    docs = {d["doc_id"]: d for d in _corpus()}
    config.get_llm().callbacks = [CallCounter()]
    for case in res["cases"]:
        if "faithfulness" not in case:
            continue
        r = rows[case["id"]]
        for run in case["runs"]:
            if any(i not in docs for i in run["context_doc_ids"]):
                continue  # 평가 중 생성된 문서(임시 폴더, 삭제됨)를 근거로 쓴 실행은 재구성할 수 없어 원래 점수를 유지
            ctx = [docs[i] for i in run["context_doc_ids"]]
            run.pop("ragas_error", None)
            run.update(ragas_scores(r["input"], run["answer"], ctx, r["reference"]))
        case["faithfulness"] = round(sum(p["faithfulness"] for p in case["runs"]) / RUNS, 4)
        case["context_recall"] = round(sum(p["context_recall"] for p in case["runs"]) / RUNS, 4)
        print(f"rescored case {case['id']}: faithfulness {case['faithfulness']}", flush=True)
    expl = [c for c in res["cases"] if "faithfulness" in c]
    res["summary"]["faithfulness"] = round(sum(c["faithfulness"] for c in expl) / len(expl), 4)
    res["summary"]["context_recall"] = round(sum(c["context_recall"] for c in expl) / len(expl), 4)
    res["config"]["ragas_rescored"] = ("인용 제거 정규식 수정 후 저장된 답변으로 RAGAS만 재채점 "
                                       f"(LLM 호출 {CallCounter.count}회, 에이전트·judge 판정 불변)")
    return res


def ragas_scores(question: str, answer: str, contexts: list[dict], reference: str) -> dict:
    from ragas import SingleTurnSample
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import Faithfulness, LLMContextRecall

    llm = LangchainLLMWrapper(config.get_llm())
    # 인용 표기([doc_id])는 주장이 아니므로 제거한다. 실제 문서 ID만 지워야 본문의 '[화면ID]' 같은 형식 예시가 남는다.
    doc_ids = {c["doc_id"] for c in contexts} | {d["doc_id"] for d in _corpus()}
    response = re.sub(r"\s*\[([^\]]+)\]", lambda m: "" if m.group(1) in doc_ids else m.group(0), answer)
    sample = SingleTurnSample(user_input=question, response=response,
                              retrieved_contexts=[c["text"] for c in contexts] or [""], reference=reference)

    async def run():
        return (await Faithfulness(llm=llm).single_turn_ascore(sample),
                await LLMContextRecall(llm=llm).single_turn_ascore(sample))
    try:
        f, r = asyncio.run(run())
        return {"faithfulness": round(float(f), 4), "context_recall": round(float(r), 4)}
    except Exception as e:  # noqa: BLE001
        return {"faithfulness": 0.0, "context_recall": 0.0, "ragas_error": str(e)}


# ----------------------------------------------------------------------------- 실행
def run_once(rows: list[dict], work: Path) -> dict:
    from fastapi.testclient import TestClient
    from src import agent, api

    os.environ["CONFLUENCE_DIR"] = str(work / "confluence")
    os.environ["GENERATED_DIR"] = str(work / "generated")
    os.environ["TRACE_DIR"] = str(work / "trace")
    api.THREADS.clear()
    agent.reset()  # 대화 기억·장기 메모리·런타임 인덱스를 고정 상태로 초기화
    client = TestClient(api.app)
    out, tid1 = {}, None
    for r in rows:
        cid = int(r["id"])
        role = re.search(r"role=(pm|pl|developer)\b", r["note"]).group(1)
        user_id = re.search(r"user_id=(\w+)", r["note"]).group(1)
        body = {"question": r["input"], "user_id": user_id, "role": role}
        if cid == APPROVAL_ID:
            body.update(thread_id=tid1, approval="approve")
        resp = client.post("/query", json=body)
        d = resp.json()
        if cid == DRAFT_ID:
            tid1 = d["thread_id"]
        extra = ""
        if cid == APPROVAL_ID:
            for p in (work / "confluence").rglob("*.md"):
                extra += p.read_text(encoding="utf-8")
        out[cid] = {**d, "status": resp.status_code, "extra": extra}
        print(f"  case {cid} done", flush=True)
    return out


def evaluate(round_no: int) -> dict:
    from src import agent
    os.environ["RAG_LEVERS"] = ROUND_LEVERS[round_no]
    agent.setup()
    config.get_llm().callbacks = [CallCounter()]
    rows = list(csv.DictReader(open(ROOT / "test_queries.csv", encoding="utf-8-sig")))
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
            case["faithfulness"] = round(sum(p["faithfulness"] for p in per) / RUNS, 4)
            case["context_recall"] = round(sum(p["context_recall"] for p in per) / RUNS, 4)
        cases.append(case)
        print(f"judged case {cid}: {passes}/3", flush=True)
    expl = [c for c in cases if "faithfulness" in c]
    num = [p["numeric_exact"] for c in cases if c["id"] in NUMERIC_IDS for p in c["runs"]]
    grd = [c["pass"] for c in cases if c["id"] in GUARDRAIL_IDS]
    import ragas
    return {
        "round": round_no,
        "config": {"rag_levers": ROUND_LEVERS[round_no], "runs_per_case": RUNS, "pass_rule": "3회 중 2회 이상 통과",
                   "judge_mode": "llm", "judge_prompt": "evaluation/judge_prompt.md",
                   "judge_prompt_sha256_12": judge_sha,
                   "model": f"{config.MODEL} (Bedrock, temperature=0, 에이전트·judge 공용)",
                   "embeddings": config.EMBED_MODEL,
                   "metric_impl": f"ragas {ragas.__version__} Faithfulness / LLMContextRecall (Bedrock LLM)",
                   "baseline": "dense-only" if round_no == 1 else None,
                   "llm_calls": CallCounter.count},
        "summary": {
            "pass_count": sum(c["pass"] for c in cases), "total": len(cases),
            "numeric_exact_match_rate": round(sum(num) / len(num), 4),
            "guardrail_block_rate": round(sum(grd) / len(grd), 4),
            "faithfulness": round(sum(c["faithfulness"] for c in expl) / len(expl), 4),
            "context_recall": round(sum(c["context_recall"] for c in expl) / len(expl), 4),
        },
        "cases": cases,
    }


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
         f"| faithfulness(설명형) | {s['faithfulness']:.4f} |",
         f"| context recall(설명형) | {s['context_recall']:.4f} |", ""]
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
