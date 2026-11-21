"""预览环境清理控制台（FastAPI）。

页面：
  GET /                按分支列出同一命名空间内成对的 Service / Deployment 及清理任务
  GET /acceptance      滚动发布后，检查原 Service 能否用于验收新版
  GET /acceptance/check 对指定分支重新判定（303 回 /acceptance）
  POST /confirm        确认清理：现场核对身份后保存清单快照
  POST /jobs/{id}/run  沿清单执行/继续一次清理
  POST /jobs/{id}/resolve 冲突后人工确认，刷新清单基线

设计约束见 cleanup.py / acceptance.py；本文件只做 HTTP 编排与展示。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import acceptance, cleanup, k8s_client, store

NAMESPACE = os.environ.get("NAMESPACE", "previews")

app = FastAPI(title="Preview Environment Cleaner")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# 最近一次验收判定结果（仅内存缓存，供页面展示；判定只由显式检查触发）
_ACCEPTANCE_LOCK = threading.Lock()
_LAST_ACCEPTANCE: dict[str, acceptance.AcceptanceResult] = {}

PHASE_LABELS = {
    "confirmed": "已确认待清理",
    "service_deleting": "Service 删除中",
    "deployment_deleting": "Deployment 删除中",
    "pending_review": "待核查",
    "conflict": "冲突已停止",
    "completed": "清理完成",
}


def _group_inventory() -> tuple[list[dict], str | None]:
    """按分支标签把同一命名空间里的 Service / Deployment 配对。"""
    try:
        services = k8s_client.list_services(NAMESPACE)
        deployments = k8s_client.list_deployments(NAMESPACE)
    except k8s_client.K8sOperationError as exc:
        return [], f"无法连接 Kubernetes API：{exc}"

    by_branch: dict[str, dict] = {}
    for svc in services:
        branch = k8s_client.branch_of(svc) or "(无分支标签)"
        by_branch.setdefault(branch, {"branch": branch, "service": None, "deployment": None})
        by_branch[branch]["service"] = svc
    for dep in deployments:
        branch = k8s_client.branch_of(dep) or "(无分支标签)"
        by_branch.setdefault(branch, {"branch": branch, "service": None, "deployment": None})
        by_branch[branch]["deployment"] = dep

    groups = []
    for branch, pair in sorted(by_branch.items()):
        svc, dep = pair["service"], pair["deployment"]
        groups.append({
            "branch": branch,
            "paired": svc is not None and dep is not None,
            "service": _brief(svc, "Service") if svc else None,
            "deployment": _brief(dep, "Deployment") if dep else None,
        })
    return groups, None


def _brief(obj: dict, kind: str) -> dict:
    meta = obj.get("metadata", {})
    return {
        "kind": kind,
        "name": meta.get("name"),
        "uid": meta.get("uid"),
        "branch": k8s_client.branch_of(obj),
        "terminating": bool(meta.get("deletionTimestamp")),
        "deletionTimestamp": meta.get("deletionTimestamp"),
        "delay": (meta.get("annotations", {}) or {}).get("mock.preview/delete-delay"),
    }


def _job_view(job: dict) -> dict:
    view = dict(job)
    view["phaseLabel"] = PHASE_LABELS.get(job["phase"], job["phase"])
    return view


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    groups, api_error = _group_inventory()
    jobs = [_job_view(j) for j in store.list_jobs()]
    # 任务按分支挂到对应分组上，方便页面同时展示"现状 + 清理进度"
    jobs_by_branch: dict[str, list] = {}
    for job in jobs:
        jobs_by_branch.setdefault(job["branch"], []).append(job)
    for group in groups:
        group["jobs"] = jobs_by_branch.get(group["branch"], [])
    return templates.TemplateResponse(request, "index.html", {
        "namespace": NAMESPACE,
        "branch_label": k8s_client.BRANCH_LABEL,
        "groups": groups,
        "jobs": jobs,
        "jobs_by_branch": jobs_by_branch,
        "api_error": api_error,
        "phase_labels": PHASE_LABELS,
    })


@app.get("/acceptance", response_class=HTMLResponse)
def acceptance_page(request: Request) -> HTMLResponse:
    """分支页：展示最近一次判定结果。

    页面本身不重新判定（避免刷新即改状态）；结果只来自显式“运行验收检查”
    或 /api/acceptance 的现场读取，并带判定期间的一致性快照。
    """
    groups, api_error = _group_inventory()
    with _ACCEPTANCE_LOCK:
        results = {branch: res for branch, res in _LAST_ACCEPTANCE.items()}
    backends = [g for g in groups if g.get("service")]
    return templates.TemplateResponse(request, "acceptance.html", {
        "namespace": NAMESPACE,
        "branch_label": k8s_client.BRANCH_LABEL,
        "backends": backends,
        "results": results,
        "verdict_labels": acceptance.VERDICT_LABELS,
        "api_error": api_error,
    })


def _run_acceptance(branch: str) -> acceptance.AcceptanceResult:
    """现场判定一个分支的 Service。任何读取失败都归入“待核实”，不猜测结论。"""
    def pending(service_name: str, reason: str) -> acceptance.AcceptanceResult:
        res = acceptance.AcceptanceResult(
            verdict=acceptance.VERDICT_PENDING,
            service_name=service_name,
            detail=reason,
            checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        res.problems.append(reason)
        return res

    try:
        services = k8s_client.list_services(NAMESPACE)
        deployments = k8s_client.list_deployments(NAMESPACE)
    except k8s_client.K8sOperationError as exc:
        return pending("—", f"读取命名空间对象失败，无法给出判定：{exc}")

    candidates = [s for s in services
                  if (k8s_client.branch_of(s) or "(无分支标签)") == branch]
    if not candidates:
        return pending("—", f"分支 {branch} 在该命名空间内没有 Service")
    if len(candidates) > 1:
        # 不猜测：多 Service 时并为待核实
        return pending(
            ", ".join(s["metadata"]["name"] for s in candidates),
            f"分支 {branch} 有 {len(candidates)} 个 Service，无法确定验收对象")

    svc = candidates[0]
    # 逐条重读，避免用列表缓存里的旧快照做判定
    try:
        svc = k8s_client.get_service(NAMESPACE, svc["metadata"]["name"])
    except k8s_client.K8sNotFound:
        return pending(svc["metadata"]["name"], "Service 在判定期间消失，状态不一致")
    except k8s_client.K8sOperationError as exc:
        return pending(svc["metadata"]["name"], f"读取 Service 失败：{exc}")

    if not acceptance.default_policy(svc)[0]:
        return acceptance.evaluate(svc, None, NAMESPACE)

    dep = acceptance.find_deployment_for_service(svc, deployments, services)
    if dep is not None:
        try:
            dep = k8s_client.get_deployment(NAMESPACE, dep["metadata"]["name"])
        except k8s_client.K8sOperationError as exc:
            # Deployment 读不到 → 待核实，但仍判定 Service 本身
            res = acceptance.evaluate(svc, None, NAMESPACE)
            res.problems.append(f"读取 Deployment 失败：{exc}")
            res.detail = "；".join(res.problems)
            return res
    return acceptance.evaluate(svc, dep, NAMESPACE)


@app.post("/acceptance/check")
def acceptance_check(branch: str = Form(...)) -> RedirectResponse:
    res = _run_acceptance(branch)
    with _ACCEPTANCE_LOCK:
        _LAST_ACCEPTANCE[branch] = res
    return RedirectResponse(f"/acceptance#branch-{quote(branch)}", status_code=303)


@app.get("/api/acceptance")
def api_acceptance(branch: str | None = None) -> JSONResponse:
    """现场判定并以 JSON 返回（branch 缺省则判定全部有 Service 的分支）。"""
    try:
        services = k8s_client.list_services(NAMESPACE)
        list_error = None
    except k8s_client.K8sOperationError as exc:
        services = []
        list_error = f"无法读取 Service 列表：{exc}"

    if list_error:
        branches = [branch] if branch else ["(未知)"]
    else:
        branches = [branch] if branch else sorted({
            k8s_client.branch_of(s) or "(无分支标签)" for s in services})

    out = []
    for b in branches:
        res = _run_acceptance(b)
        if list_error:
            res.problems.insert(0, list_error)
            res.detail = "；".join(res.problems)
            res.verdict = acceptance.VERDICT_PENDING
        with _ACCEPTANCE_LOCK:
            _LAST_ACCEPTANCE[b] = res
        out.append({
            "branch": b,
            "verdict": res.verdict,
            "verdictLabel": res.verdict_label,
            "detail": res.detail,
            "service": res.service_name,
            "deployment": res.deployment_name,
            "checks": res.checks,
            "problems": res.problems,
            "readEpoch": res.read_epoch,
            "checkedAt": res.checked_at,
            "endpoints": [r.__dict__ | {"readyAndLive": r.ready_and_live}
                          for r in res.endpoints],
            "staleEndpoints": [r.__dict__ for r in res.stale_endpoints],
        })
    return JSONResponse({"namespace": NAMESPACE, "branches": out})


@app.post("/confirm")
def confirm(branch: str = Form(...), service_name: str = Form(...),
            deployment_name: str = Form(...)) -> RedirectResponse:
    """确认清理：重新从 API 拉取两个对象，身份核对通过后才落清单。"""
    try:
        svc = k8s_client.get_service(NAMESPACE, service_name)
        dep = k8s_client.get_deployment(NAMESPACE, deployment_name)
    except k8s_client.K8sNotFound as exc:
        return _redirect_error(f"确认失败：对象已不存在（{exc}）")
    except k8s_client.K8sOperationError as exc:
        return _redirect_error(f"确认失败：{exc}")

    problems = []
    for spec_name, obj in (("Service", svc), ("Deployment", dep)):
        actual_branch = k8s_client.branch_of(obj)
        if actual_branch != branch:
            problems.append(
                f"{spec_name}/{obj['metadata']['name']} 分支标签为 {actual_branch}，与所选 {branch} 不符")
        if k8s_client.is_terminating(obj):
            problems.append(f"{spec_name}/{obj['metadata']['name']} 已在终止中")
    if problems:
        return _redirect_error("；".join(problems))

    job = store.create_job(NAMESPACE, branch, svc, dep)
    return RedirectResponse(f"/#job-{job['id']}", status_code=303)


@app.post("/jobs/{job_id}/run")
def run_job(job_id: str) -> RedirectResponse:
    job = store.get_job(job_id)
    if job is None:
        return _redirect_error("任务不存在（可能重启后清单丢失）")
    if job["phase"] == "conflict":
        return _redirect_error("任务因冲突已停止，请先在页面处理冲突后再继续")
    if job["phase"] == "completed":
        return RedirectResponse(f"/#job-{job_id}", status_code=303)
    cleanup.reconcile(job_id)
    return RedirectResponse(f"/#job-{job_id}", status_code=303)


@app.post("/jobs/{job_id}/resolve")
def resolve_job(job_id: str) -> RedirectResponse:
    job = store.get_job(job_id)
    if job is None:
        return _redirect_error("任务不存在")
    if job["phase"] != "conflict":
        return RedirectResponse(f"/#job-{job_id}", status_code=303)
    cleanup.resolve_conflict(job_id)
    return RedirectResponse(f"/#job-{job_id}", status_code=303)


@app.get("/api/jobs")
def api_jobs() -> JSONResponse:
    return JSONResponse([_job_view(j) for j in store.list_jobs()])


@app.get("/api/inventory")
def api_inventory() -> JSONResponse:
    groups, api_error = _group_inventory()
    return JSONResponse({"namespace": NAMESPACE, "groups": groups, "error": api_error})


def _redirect_error(message: str, path: str = "/") -> RedirectResponse:
    # 简单地把错误经查询串带回页面展示（演示用途，避免引入消息中间件）
    return RedirectResponse(f"{path}?error={quote(message)}#top", status_code=303)
