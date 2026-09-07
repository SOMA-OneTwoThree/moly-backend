#!/usr/bin/env bash
# 제한된 수동 등록 → memory 큐 처리. 큐 전체가 대상이며 LLM 비용이 발생한다.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_NAME=''
SIZE=10
BATCHES=1
SECONDS_LIMIT=300
APPLY=false
while (($#)); do
  case "$1" in
    --env) ENV_NAME="${2:?--env value required}"; shift 2 ;;
    --size) SIZE="${2:?--size value required}"; shift 2 ;;
    --batches) BATCHES="${2:?--batches value required}"; shift 2 ;;
    --seconds) SECONDS_LIMIT="${2:?--seconds value required}"; shift 2 ;;
    --yes) APPLY=true; shift ;;
    --help) echo 'Usage: backfill_memory_batches.sh --env dev|prod|env-file [--size 10] [--batches 1] [--seconds 300] [--yes]'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$ENV_NAME" ]] || { echo '--env is required' >&2; exit 2; }
for number in "$SIZE" "$BATCHES" "$SECONDS_LIMIT"; do
  [[ "$number" =~ ^[1-9][0-9]*$ ]] || { echo 'bounds must be positive integers' >&2; exit 2; }
done
export PYTHONPATH=.
for ((batch=1; batch<=BATCHES; batch++)); do
  echo "batch=$batch/$BATCHES env=$ENV_NAME size=$SIZE"
  if "$APPLY"; then
    uv run python scripts/enter_shadow_cohort.py --env "$ENV_NAME" --limit "$SIZE" --yes
  else
    uv run python scripts/enter_shadow_cohort.py --env "$ENV_NAME" --limit "$SIZE"
  fi
  if ! "$APPLY"; then
    echo '미리보기 완료. 등록·큐 처리 실반영은 --yes.'
    break
  fi
  uv run python scripts/run_memory_consumer.py --env "$ENV_NAME" --until-empty --seconds "$SECONDS_LIMIT"
done
