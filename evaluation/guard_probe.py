"""가드레일 판정 확인: evaluation/guard_probe.json의 정상·공격 문구를 정규식 → LLM 분류기 순서로 판정한다.

실행: python evaluation/guard_probe.py   (실제 Bedrock 호출: 정규식을 통과한 문구 수만큼, 약 30회)
결과: 오탐(정상을 막음)·미탐(공격을 통과) 목록과 건수. 오탐 0·미탐 0이 목표다.
정규식 단계만의 확인은 LLM 없이 tests/test_guardrails.py가 같은 세트로 한다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config, guardrails  # noqa: E402

PROBE = ROOT / "evaluation" / "guard_probe.json"


def judge(question: str, classifier) -> tuple[str, str]:
    """(판정, 단계). 판정은 ok | injection | person_ranking, 단계는 regex | llm."""
    if guardrails.is_injection(question):
        return "injection", "regex"
    if guardrails.is_ranking(question):
        return "person_ranking", "regex"
    return classifier(guardrails.normalize(question)).category, "llm"


def main() -> int:
    items = json.loads(PROBE.read_text(encoding="utf-8"))["items"]
    clf = guardrails.make_llm_classifier(config.get_llm())
    false_pos, missed = [], []
    for it in items:
        got, layer = judge(it["question"], clf)
        ok = got == it["expected"]
        print(f"{'OK ' if ok else 'NG '} {it['id']:9} 기대={it['expected']:14} 판정={got:14} ({layer}) | {it['question']}")
        if not ok:
            (false_pos if it["expected"] == "ok" else missed).append(it["id"])
    print(f"\n정상 {sum(i['expected'] == 'ok' for i in items)}건 중 오탐 {len(false_pos)}건 {false_pos}")
    print(f"공격 {sum(i['expected'] != 'ok' for i in items)}건 중 미탐 {len(missed)}건 {missed}")
    return 1 if false_pos or missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
