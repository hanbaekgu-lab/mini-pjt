#!/usr/bin/env bash
# 주간 진척 보고 에이전트 실행 스크립트 (Linux/macOS bash, Windows Git Bash 공용)
# 사용: ./run.sh <setup|server|test|eval N|rescore N|fixtures|docker-build|docker-run>
set -euo pipefail
cd "$(dirname "$0")"

IMAGE=weekly-progress-agent

# 가상환경 실행 파일 위치: Windows는 .venv/Scripts, 그 외는 .venv/bin
if [ -d .venv/Scripts ]; then VENV_BIN=.venv/Scripts; else VENV_BIN=.venv/bin; fi
PY="$VENV_BIN/python"

need_venv() {
  [ -x "$PY" ] || [ -x "$PY.exe" ] || { echo "가상환경이 없습니다. 먼저 ./run.sh setup 을 실행하세요." >&2; exit 1; }
}

round_arg() {
  case "${1:-}" in 1|2) ;; *) echo "회차는 1 또는 2입니다. 예: ./run.sh $2 1" >&2; exit 1 ;; esac
}

cmd="${1:-}"
case "$cmd" in
  setup)
    if [ ! -d .venv ]; then
      if command -v python3 >/dev/null 2>&1; then python3 -m venv .venv; else python -m venv .venv; fi
      if [ -d .venv/Scripts ]; then VENV_BIN=.venv/Scripts; else VENV_BIN=.venv/bin; fi
      PY="$VENV_BIN/python"
    fi
    "$PY" -m pip install -r requirements.txt
    [ -f .env ] || { cp .env.example .env; echo ".env를 만들었습니다. mock 값을 실제 값으로 바꾸세요."; }
    ;;
  server)
    need_venv
    "$PY" -m uvicorn src.server:app --host 0.0.0.0 --port "${PORT:-8000}"
    ;;
  test)
    need_venv
    shift
    "$PY" -m pytest -q "$@"
    ;;
  eval)
    need_venv
    round_arg "${2:-}" eval
    echo "실제 Bedrock을 호출합니다(비용 발생)."
    "$PY" evaluation/run_eval.py --round "$2"
    ;;
  rescore)
    need_venv
    round_arg "${2:-}" rescore
    echo "저장된 답변으로 RAGAS 4개 지표만 다시 계산합니다(Bedrock 호출)."
    "$PY" evaluation/run_eval.py --round "$2" --rescore-ragas
    ;;
  fixtures)
    need_venv
    "$PY" data/fixtures/make_fixtures.py
    ;;
  docker-build)
    command -v docker >/dev/null 2>&1 || { echo "docker가 설치되어 있지 않습니다." >&2; exit 1; }
    docker build -t "$IMAGE" .
    ;;
  docker-run)
    command -v docker >/dev/null 2>&1 || { echo "docker가 설치되어 있지 않습니다." >&2; exit 1; }
    [ -f .env ] || { echo ".env가 없습니다. .env.example을 복사해 값을 채우세요." >&2; exit 1; }
    docker run --rm --env-file .env -p "${PORT:-8000}:8000" "$IMAGE"
    ;;
  *)
    echo "사용: ./run.sh <setup|server|test|eval 1|2|rescore 1|2|fixtures|docker-build|docker-run>"
    exit 1
    ;;
esac
