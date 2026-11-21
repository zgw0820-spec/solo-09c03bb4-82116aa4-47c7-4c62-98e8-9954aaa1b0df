#!/usr/bin/env bash
# 本地无 Docker 环境时的启动方式（Docker Compose 是正式交付入口，见 docker-compose.yml）。
# 用法: ./scripts/run-local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH="${PWD}:${PYTHONPATH:-}"
export NAMESPACE=previews
export JOB_STORE="${PWD}/data/jobs.json"
export STATE_FILE="${PWD}/data/mock-state.json"
export KUBE_API_BASE=http://127.0.0.1:18080
export DELAY_FEATURE_A="${DELAY_FEATURE_A:-15}"
mkdir -p data

python -m mock-k8s.server > /tmp/mock-k8s.log 2>&1 &
MOCK_PID=$!
echo "mock-k8s pid=$MOCK_PID (log /tmp/mock-k8s.log)"
trap 'kill $MOCK_PID 2>/dev/null || true' EXIT

python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
