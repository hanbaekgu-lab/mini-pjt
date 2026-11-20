"""Supervisor 라우팅 정확도 확인: evaluation/routing_test.json의 질문을 LLM 라우터로 분류해 기대 라우트와 비교한다.

실행: python evaluation/route_check.py   (실제 Bedrock 호출: 질문 수만큼, 약 20회)
다중 의도 질문(진척 수치 + 문서 내용)은 progress+docs_qa가 정답이다(두 에이전트를 진척 → 문서 순서로).
LLM 없이 도는 규칙 기반 대체 라우터의 정확도는 tests/test_eval_pipeline.py가 같은 세트로 확인한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, supervisor  # noqa: E402

ROUTING = ROOT / "evaluation" / "routing_test.json"


def main() -> int:
    items = json.loads(ROUTING.read_text(encoding="utf-8"))["items"]
    router = supervisor.make_llm_router(config.get_llm())
    wrong = []
    for it in items:
        got = router(it["question"])
        ok = got in it["expected"]
        print(f"{'OK ' if ok else 'NG '} {it['id']} 기대={'/'.join(it['expected']):26} 판정={got:16} | {it['question']}")
        if not ok:
            wrong.append(it["id"])
    n = len(items)
    print(f"\n정확도 {n - len(wrong)}/{n} ({(n - len(wrong)) / n:.0%}), 오분류 {wrong}")
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
