#!/usr/bin/env python3
"""端到端试跑：通过 HTTP 驱动 mock-k8s 与 FastAPI 控制台。

覆盖四个场景：
  1. 正常清理（hotfix-1，无延迟）：先 Service 后 Deployment，逐步 reconcile 至 completed
  2. 入口延迟消失（feature-a，DELETE 后 15s 才移除）：受理后进入待核查，等待后继续
  3. 确认后改属（feature-b 的 Service 分支标签被改）：reconcile 立即 conflict 并指出变更项
  4. 同名重建（重新造一个分支并重建其 Deployment）：UID 变化，reconcile 冲突
另含：重启两个进程后沿 jobs.json / mock-state.json 继续清理的验证。

用法: python tests/e2e.py   （先启动两个服务，或用 scripts/run-local.sh 后另开终端）
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse

import httpx

WEB = "http://127.0.0.1:8000"
MOCK = "http://127.0.0.1:18080"
NS = "previews"

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, cond, detail))
    print(f"[{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def reset() -> None:
    httpx.post(f"{MOCK}/mock/reset", timeout=10).raise_for_status()
    # 清空 web 端任务清单
    import os
    store = os.environ.get("JOB_STORE", "data/jobs.json")
    if os.path.exists(store):
        os.remove(store)


def confirm(branch: str) -> str:
    name = f"preview-{branch}"
    resp = httpx.post(f"{WEB}/confirm", data={
        "branch": branch, "service_name": name, "deployment_name": name,
    }, follow_redirects=False, timeout=10)
    assert resp.status_code == 303, resp.text
    loc = resp.headers["location"]
    return loc.split("job-")[1]


def run(job_id: str) -> dict:
    resp = httpx.post(f"{WEB}/jobs/{job_id}/run", follow_redirects=False, timeout=10)
    assert resp.status_code == 303, resp.text
    return get_job(job_id)


def get_job(job_id: str) -> dict:
    jobs = httpx.get(f"{WEB}/api/jobs", timeout=10).json()
    return next(j for j in jobs if j["id"] == job_id)


def mock_get(kind: str, name: str):
    if kind == "Service":
        url = f"{MOCK}/api/v1/namespaces/{NS}/services/{name}"
    else:
        url = f"{MOCK}/apis/apps/v1/namespaces/{NS}/deployments/{name}"
    resp = httpx.get(url, timeout=10)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def scenario(action: str, kind: str, name: str, **params) -> dict:
    q = urllib.parse.urlencode({"kind": kind, "name": name, **params})
    return httpx.post(f"{MOCK}/mock/scenarios/{action}?{q}", timeout=10).json()


def wait_gone(kind: str, name: str, timeout_s: float = 30) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if mock_get(kind, name) is None:
            return True
        time.sleep(0.5)
    return False


def case_normal_cleanup() -> None:
    print("\n=== 场景1：正常清理（先 Service 后 Deployment）===")
    job_id = confirm("hotfix-1")
    job = run(job_id)
    # 无延迟时一次 reconcile 内完成 Service 删除→确认404→Deployment 删除→确认404
    check("一次执行内按序完成 Service、Deployment 清理",
          job["phase"] == "completed", job["phase"])
    check("两个对象均已不存在",
          mock_get("Service", "preview-hotfix-1") is None
          and mock_get("Deployment", "preview-hotfix-1") is None)
    events = " | ".join(e["message"] for e in job["events"])
    check("事件顺序体现先 Service 消失后才删 Deployment",
          "已受理删除 Service" in events and "已消失，开始核对 Deployment" in events
          and "已受理删除 Deployment" in events, events)
    # 幂等：完成后再执行无副作用
    again = run(job_id)
    check("完成后重复执行保持 completed", again["phase"] == "completed")


def case_delayed_delete() -> None:
    print("\n=== 场景2：入口（Service）延迟消失 ===")
    job_id = confirm("feature-a")
    run(job_id)
    job = get_job(job_id)
    svc = mock_get("Service", "preview-feature-a")
    check("DELETE 已受理：对象带 deletionTimestamp 但仍可 GET",
          svc is not None and bool(svc["metadata"].get("deletionTimestamp")))
    check("此时状态为待核查而非误删 Deployment",
          job["phase"] == "pending_review"
          and mock_get("Deployment", "preview-feature-a") is not None
          and not mock_get("Deployment", "preview-feature-a")["metadata"].get("deletionTimestamp"),
          job.get("pendingReason", ""))
    check("等待期间重复执行保持待核查，不越步",
          run(job_id)["phase"] == "pending_review")
    print("  …等待 15s 延迟到期")
    gone = wait_gone("Service", "preview-feature-a", 25)
    check("延迟到期后 Service 真正消失", gone)
    job = run(job_id)
    # 该分支 Deployment 同样带 15s 删除延迟：受理后应再次进入待核查，不越步
    check("续跑确认 Service 404 后才受理 Deployment 删除并等待其终止",
          job["phase"] == "pending_review"
          and mock_get("Service", "preview-feature-a") is None
          and mock_get("Deployment", "preview-feature-a")["metadata"].get("deletionTimestamp"),
          job.get("pendingReason", ""))
    check("Deployment 延迟期间重复执行保持待核查",
          run(job_id)["phase"] == "pending_review")
    print("  …等待 Deployment 的 15s 延迟到期")
    check("Deployment 延迟到期消失", wait_gone("Deployment", "preview-feature-a", 25))
    job = run(job_id)
    check("再续跑一次任务完成", job["phase"] == "completed", job["phase"])
    check("Deployment 也已删除",
          mock_get("Deployment", "preview-feature-a") is None)


def case_reown() -> None:
    print("\n=== 场景3：确认后改属（分支标签变化，UID 不变）===")
    job_id = confirm("feature-b")
    before = mock_get("Service", "preview-feature-b")
    out = scenario("reown", "Service", "preview-feature-b", branch="stolen-branch")
    check("模拟端改属后 UID 保持不变", out["uid"] == before["metadata"]["uid"], out["uid"])
    job = run(job_id)
    check("清理停止在 conflict", job["phase"] == "conflict", job["phase"])
    c = job["conflict"]
    check("指出变更项为分支标签（而非 UID）",
          c is not None and c["field"] == "branch"
          and c["expected"] == "feature-b" and c["actual"] == "stolen-branch",
          json.dumps(c, ensure_ascii=False))
    check("冲突后未对任何对象发起删除",
          not mock_get("Service", "preview-feature-b")["metadata"].get("deletionTimestamp")
          and not mock_get("Deployment", "preview-feature-b")["metadata"].get("deletionTimestamp"))
    # 人工知情后刷新基线再继续
    httpx.post(f"{WEB}/jobs/{job_id}/resolve", follow_redirects=False, timeout=10)
    job = run(job_id)
    check("刷新基线后一次执行即完成", job["phase"] == "completed", job["phase"])


def case_recreate() -> None:
    print("\n=== 场景4：同名重建（UID 变化）===")
    # feature-b 在上一个场景已清掉，用 mock 重新造一对来演示
    # 直接重建 feature-b 的两个对象（复用 seed 结构手工 POST 不支持，改用 reset 后重做场景3前置）
    # 这里改为对仍存在的 hotfix-1 已删除，故重新 reset 会清任务；为隔离起见新建分支名：
    # 通过 recreate 场景把 feature-a 的 Deployment 立即重建（feature-a 已被清理则先重建整组）
    # —— 简化：重新 seed 一次会清掉全部状态，因此本场景在独立分支上靠“先确认、再 recreate”验证。
    # reset 并重建任务清单（前面的断言结果不受影响）。
    reset()
    job_id = confirm("hotfix-1")
    old_uid = mock_get("Deployment", "preview-hotfix-1")["metadata"]["uid"]
    out = scenario("recreate", "Deployment", "preview-hotfix-1")
    check("模拟端同名重建产生新 UID", out["newUid"] != old_uid,
          f"{old_uid} -> {out['newUid']}")
    # 先让 Service 正常删除消失，使流程走到 Deployment 核对
    job = run(job_id)
    if not wait_gone("Service", "preview-hotfix-1", 10):
        check("Service 应消失", False)
        return
    # Service 消失后再次执行：此时必须在 Deployment 核对处发现 UID 不符
    job = run(job_id)
    check("Deployment 核对发现 UID 不符并停止", job["phase"] == "conflict"
          and job["conflict"]["field"] == "uid"
          and job["conflict"]["actual"] == out["newUid"],
          json.dumps(job.get("conflict"), ensure_ascii=False))
    check("新 Deployment 未被删除",
          not mock_get("Deployment", "preview-hotfix-1")["metadata"].get("deletionTimestamp"))
    check("Service 已按原计划删除（冲突只阻断发生变化的对象）",
          mock_get("Service", "preview-hotfix-1") is None)


def case_recreate_service() -> None:
    print("\n=== 场景4b：Service 同名重建（确认后、受理前 UID 变化）===")
    reset()
    job_id = confirm("feature-b")
    old_uid = mock_get("Service", "preview-feature-b")["metadata"]["uid"]
    out = scenario("recreate", "Service", "preview-feature-b")
    check("Service 同名重建产生新 UID", out["newUid"] != old_uid)
    job = run(job_id)
    check("Service 核对发现 UID 不符并停止，且未发起任何删除",
          job["phase"] == "conflict" and job["conflict"]["field"] == "uid"
          and not mock_get("Service", "preview-feature-b")["metadata"].get("deletionTimestamp")
          and not mock_get("Deployment", "preview-feature-b")["metadata"].get("deletionTimestamp"),
          json.dumps(job.get("conflict"), ensure_ascii=False))


def case_restart_resume() -> None:
    print("\n=== 场景5：重启后沿原清单复查并继续 ===")
    reset()
    # 给 hotfix-1 加延迟，制造“终止中重启”
    scenario("delay", "Service", "preview-hotfix-1", seconds="30")
    job_id = confirm("hotfix-1")
    run(job_id)
    check("重启前为待核查（Service 终止中）", get_job(job_id)["phase"] == "pending_review")
    print("  请在另一个终端重启两个进程（脚本模式下本用例只校验持久化文件存在与内容）；"
          "或由 tests/restart-check.sh 完成真正的进程重启。")
    import os
    store = os.environ.get("JOB_STORE", "data/jobs.json")
    state = os.environ.get("STATE_FILE", "data/mock-state.json")
    on_disk = os.path.exists(store) and os.path.exists(state)
    with open(store, encoding="utf-8") as fh:
        persisted = json.load(fh)
    check("任务清单与模拟端状态均已落盘", on_disk)
    check("落盘清单保留确认时 UID/分支基线",
          persisted["jobs"][job_id]["service"]["uid"].startswith("uid-svc-hotfix-1")
          and persisted["jobs"][job_id]["deployment"]["branch"] == "hotfix-1")
    with open(state, encoding="utf-8") as fh:
        mock_state = json.load(fh)
    terminating = any(
        o["kind"] == "Service" and o["metadata"].get("deletionTimestamp")
        for o in mock_state["objects"].values()
    )
    check("模拟端状态保留 deletionTimestamp，重启后仍是终止中", terminating)


def main() -> int:
    reset()
    case_normal_cleanup()
    case_delayed_delete()
    case_reown()
    case_recreate()
    case_recreate_service()
    case_restart_resume()
    print("\n========== 汇总 ==========")
    failed = [n for n, ok, _ in results if not ok]
    for name, ok, detail in results:
        print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail and not ok else ""))
    print(f"\n共 {len(results)} 项，失败 {len(failed)} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
