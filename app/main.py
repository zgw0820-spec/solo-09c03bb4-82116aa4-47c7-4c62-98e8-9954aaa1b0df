"""预览环境清理控制台（FastAPI）。

页面：
  GET /                按分支列出同一命名空间内成对的 Service / Deployment 及清理任务
  POST /confirm        确认清理：现场核对身份后保存清单快照
  POST /jobs/{id}/run  沿清单执行/继续一次清理
  POST /jobs/{id}/resolve 冲突后人工确认，刷新清单基线

设计约束见 cleanup.py；本文件只做 HTTP 编排与展示。
"""
from __future__ import annotations

import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import cleanup, k8s_client, store

NAMESPACE = os.environ.get("NAMESPACE", "previews")

app = FastAPI(title="Preview Environment Cleaner")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

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


def _redirect_error(message: str) -> RedirectResponse:
    # 简单地把错误经查询串带回首页展示（演示用途，避免引入消息中间件）
    from urllib.parse import quote
    return RedirectResponse(f"/?error={quote(message)}#top", status_code=303)
