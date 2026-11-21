#!/usr/bin/env bash
# 验证“重启后能沿原清单复查并继续清理”：
# 1. 确认清理一个带删除延迟的分支，制造 Service 终止中状态
# 2. 杀掉 mock-k8s 与 web 两个进程并重新启动（模拟整机/容器重启）
# 3. 不重新确认，直接沿落盘清单继续，任务最终完成
# 使用独立端口（18090/8090），可与 scripts/run-local.sh 的服务并存。
# Docker Compose 下等价于 docker compose restart。
set -euo pipefail
cd "$(dirname "$0")/.."

export PYTHONPATH="${PWD}:/tmp/venvtest${PYTHONPATH:+:${PYTHONPATH}}"
export NAMESPACE=previews
DATA_DIR=/tmp/preview-restart
MOCK_PORT=18090
WEB_PORT=8090
export JOB_STORE="${DATA_DIR}/jobs.json"
export STATE_FILE="${DATA_DIR}/mock-state.json"
export KUBE_API_BASE="http://127.0.0.1:${MOCK_PORT}"
export DELAY_FEATURE_A=3
mkdir -p "${DATA_DIR}"

WEB="http://127.0.0.1:${WEB_PORT}"
MOCK="http://127.0.0.1:${MOCK_PORT}"

start_services() {
  PORT="${MOCK_PORT}" nohup python3 -m mock-k8s.server > /tmp/restart-mock.log 2>&1 &
  echo $! > /tmp/restart-mock.pid
  nohup /tmp/venvtest/bin/uvicorn app.main:app --host 127.0.0.1 --port "${WEB_PORT}" \
      > /tmp/restart-web.log 2>&1 &
  echo $! > /tmp/restart-web.pid
  for _ in $(seq 1 20); do
    curl -sf "${MOCK}/mock/state" >/dev/null && curl -sf "${WEB}/" >/dev/null && return 0
    sleep 0.5
  done
  echo "服务启动失败"; tail -5 /tmp/restart-mock.log /tmp/restart-web.log; exit 1
}
stop_services() {
  kill "$(cat /tmp/restart-mock.pid)" "$(cat /tmp/restart-web.pid)" 2>/dev/null || true
  sleep 1
}

echo "--- 重置环境并启动服务"
rm -f "${JOB_STORE}" "${STATE_FILE}"
start_services
curl -sf -X POST "${MOCK}/mock/reset" >/dev/null

echo "--- 确认 feature-a 并执行一次（Service 进入终止中）"
curl -sf -X POST "${WEB}/confirm" \
  -d "branch=feature-a&service_name=preview-feature-a&deployment_name=preview-feature-a" \
  -o /dev/null
JOB=$(curl -sf "${WEB}/api/jobs" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['id'])")
curl -sf -X POST "${WEB}/jobs/${JOB}/run" -o /dev/null
PHASE=$(curl -sf "${WEB}/api/jobs" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['phase'])")
echo "重启前阶段: ${PHASE}"
[ "${PHASE}" = "pending_review" ] || { echo "期望 pending_review"; exit 1; }

echo "--- 重启两个进程"
stop_services
start_services
echo "重启后模拟端仍保留终止状态:"
curl -sf "${MOCK}/api/v1/namespaces/previews/services/preview-feature-a" \
  | python3 -c "import json,sys; print(' deletionTimestamp =', json.load(sys.stdin)['metadata'].get('deletionTimestamp'))"
PHASE=$(curl -sf "${WEB}/api/jobs" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['phase'])")
echo "重启后网页读出的任务阶段: ${PHASE}（清单来自 jobs.json，未重新确认）"
[ "${PHASE}" = "pending_review" ] || { echo "重启后期望仍是 pending_review"; exit 1; }

echo "--- 等待延迟到期后沿原清单继续（不重新确认）"
sleep 4
curl -sf -X POST "${WEB}/jobs/${JOB}/run" -o /dev/null
sleep 4
curl -sf -X POST "${WEB}/jobs/${JOB}/run" -o /dev/null
FINAL=$(curl -sf "${WEB}/api/jobs" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['phase'])")
echo "最终阶段: ${FINAL}"
[ "${FINAL}" = "completed" ] || { echo "重启续跑失败"; cat /tmp/restart-web.log; stop_services; exit 1; }
echo "OK: 重启续跑验证通过（任务 ${JOB} 沿原清单完成）"
stop_services
