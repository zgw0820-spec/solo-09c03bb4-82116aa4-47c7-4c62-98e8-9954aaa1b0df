"""清理状态机与核对逻辑（核心）。

阶段流转：

    confirmed ──► service_deleting ──► deployment_deleting ──► completed
                       │                     │
                       └── 超时/终止中 ─► pending_review（可重试续跑）
                       └── UID/标签不符 ─► conflict（停止，指出变更项）

reconcile 在一次调用内循环推进，直到出现真正的阻塞（对象终止中、
API 超时、身份冲突）或全部清理完成；每一轮都会重新核对原 Service，
确保删除 Deployment 之前每一次都重新确认原 Service 已消失。
"""
from __future__ import annotations

from dataclasses import dataclass

from . import k8s_client, store

# 终态
PHASE_CONFIRMED = "confirmed"
PHASE_SERVICE_DELETING = "service_deleting"
PHASE_DEPLOYMENT_DELETING = "deployment_deleting"
PHASE_PENDING = "pending_review"
PHASE_CONFLICT = "conflict"
PHASE_COMPLETED = "completed"

# 单次 reconcile 的最大推进轮数，防止意外死循环
MAX_TICKS = 10


@dataclass
class Mismatch:
    kind: str          # Service / Deployment
    name: str
    field: str         # uid / branch
    expected: str | None
    actual: str | None
    detail: str


def _verify(job: dict, spec: dict, live: dict) -> Mismatch | None:
    """核对存活对象的 UID 与分支标签是否仍与确认时的清单一致。"""
    # 基线已记录该对象消失（冲突处理时确认），同名对象再次出现必为重建
    if spec.get("gone"):
        live_uid = k8s_client.uid_of(live)
        return Mismatch(
            kind=spec["kind"], name=spec["name"], field="uid",
            expected=None, actual=live_uid,
            detail=(f"{spec['kind']}/{spec['name']} 在基线中已消失，"
                    f"现又出现同名对象（UID {live_uid}），判定为重建，停止清理"),
        )
    live_uid = k8s_client.uid_of(live)
    live_branch = k8s_client.branch_of(live)
    if live_uid != spec["uid"]:
        return Mismatch(
            kind=spec["kind"], name=spec["name"], field="uid",
            expected=spec["uid"], actual=live_uid,
            detail=(f"{spec['kind']}/{spec['name']} 同名重建："
                    f"UID 已由 {spec['uid']} 变为 {live_uid}，停止清理"),
        )
    if live_branch != spec["branch"]:
        return Mismatch(
            kind=spec["kind"], name=spec["name"], field="branch",
            expected=spec["branch"], actual=live_branch,
            detail=(f"{spec['kind']}/{spec['name']} 已改属：分支标签 "
                    f"{k8s_client.BRANCH_LABEL} 由 {spec['branch']} 变为 {live_branch}，"
                    "UID 未变但归属已变，停止清理"),
        )
    return None


def _mark_conflict(job: dict, mismatch: Mismatch) -> None:
    job["phase"] = PHASE_CONFLICT
    job["conflict"] = mismatch.__dict__
    job["pendingReason"] = None
    store.add_event(job, "error", mismatch.detail)


def _mark_pending(job: dict, reason: str) -> None:
    job["phase"] = PHASE_PENDING
    job["pendingReason"] = reason
    store.add_event(job, "warn", f"待核查：{reason}")


def _get_verified(job: dict, spec: dict, getter) -> tuple[str, dict | None, Mismatch | None]:
    """返回 (状态, 存活对象或None, 不匹配项)。

    状态取值：
      ok      存活且 UID/分支标签核对通过（或返回了不匹配项）
      gone    GET 返回 404，对象已消失
      pending API 超时/网络错误，已登记待核查原因
    注意：pending 是显式状态，调用方不得仅凭 phase 判断是否退出，
    否则会与“对象终止中”的正常等待混淆。
    """
    try:
        live = getter(job["namespace"], spec["name"])
    except k8s_client.K8sNotFound:
        return "gone", None, None
    except k8s_client.K8sOperationError as exc:
        _mark_pending(job, f"读取 {spec['kind']}/{spec['name']} 失败：{exc}")
        return "pending", None, None
    mismatch = _verify(job, spec, live)
    if mismatch:
        return "ok", live, mismatch
    return "ok", live, None


def reconcile(job_id: str) -> dict | None:
    """沿清单推进清理。可安全重复调用（重启续跑、人工重试都走这里）。"""

    def mutate(job: dict) -> None:
        # 冲突态与完成态不允许自动继续
        if job["phase"] == PHASE_CONFLICT:
            store.add_event(job, "warn", "存在未解决的同名重建/改属冲突，已跳过本次执行")
            return
        if job["phase"] == PHASE_COMPLETED:
            return

        svc_spec = job["service"]
        dep_spec = job["deployment"]

        for _ in range(MAX_TICKS):
            # ===== 每轮先核对原 Service（删除 Deployment 的前置条件）=====
            status, live_svc, mismatch = _get_verified(
                job, svc_spec, k8s_client.get_service)
            if status == "pending":
                return  # API 超时/错误，待人工复查
            if mismatch:
                _mark_conflict(job, mismatch)
                return
            if status == "ok":
                if k8s_client.is_terminating(live_svc):
                    _mark_pending(job,
                                  f"Service/{svc_spec['name']} 仍在终止中"
                                  "（deletionTimestamp 已设置），等待其消失后再处理 Deployment")
                    return
                try:
                    k8s_client.delete_service(job["namespace"], svc_spec["name"])
                except k8s_client.K8sConflict:
                    _mark_pending(job, f"Service/{svc_spec['name']} 已在终止中，等待消失")
                    return
                except k8s_client.K8sOperationError as exc:
                    _mark_pending(job, f"删除 Service/{svc_spec['name']} 失败：{exc}")
                    return
                job["phase"] = PHASE_SERVICE_DELETING
                job["pendingReason"] = None
                store.add_event(job, "info",
                                f"已受理删除 Service/{svc_spec['name']}，复查其是否已消失")
                continue  # 回到循环顶部重新 GET：必须确认 404 后才会动 Deployment

            # ===== Service 已确认 404 消失，核对并清理 Deployment =====
            if job["phase"] != PHASE_DEPLOYMENT_DELETING:
                store.add_event(job, "info",
                                f"确认 Service/{svc_spec['name']} 已消失，开始核对 Deployment")
                job["phase"] = PHASE_DEPLOYMENT_DELETING
                job["pendingReason"] = None

            status, live_dep, mismatch = _get_verified(
                job, dep_spec, k8s_client.get_deployment)
            if status == "pending":
                return
            if mismatch:
                _mark_conflict(job, mismatch)
                return
            if status == "gone":
                job["phase"] = PHASE_COMPLETED
                job["pendingReason"] = None
                job["conflict"] = None
                store.add_event(job, "info",
                                f"Deployment/{dep_spec['name']} 已消失，分支 {job['branch']} "
                                "预览环境清理完成")
                return
            if k8s_client.is_terminating(live_dep):
                _mark_pending(job,
                              f"Deployment/{dep_spec['name']} 仍在终止中，等待其消失")
                return
            try:
                k8s_client.delete_deployment(job["namespace"], dep_spec["name"])
            except k8s_client.K8sConflict:
                _mark_pending(job, f"Deployment/{dep_spec['name']} 已在终止中，等待消失")
                return
            except k8s_client.K8sOperationError as exc:
                _mark_pending(job, f"删除 Deployment/{dep_spec['name']} 失败：{exc}")
                return
            store.add_event(job, "info",
                            f"已受理删除 Deployment/{dep_spec['name']}，复查其是否已消失")
            # 回到循环顶部：再次确认 Service 仍为 404，再复查 Deployment
        _mark_pending(job, f"单次执行超过 {MAX_TICKS} 轮仍未稳定，请人工复查")

    return store.update_job(job_id, mutate)


def resolve_conflict(job_id: str) -> dict | None:
    """冲突处理：重新读取当前对象并以新身份刷新清单基线（人工知情后的再确认）。

    若对象已经消失则直接从基线移除；刷新后任务回到 confirmed，
    下次 reconcile 按新基线继续。
    """

    def mutate(job: dict) -> None:
        if job["phase"] != PHASE_CONFLICT:
            return
        conflict = job.get("conflict") or {}
        target_kind = conflict.get("kind")
        specs = [job["service"], job["deployment"]]
        getter = (k8s_client.get_service if target_kind == "Service"
                  else k8s_client.get_deployment)
        for spec in specs:
            if spec["kind"] != target_kind or spec["name"] != conflict.get("name"):
                continue
            try:
                live = getter(job["namespace"], spec["name"])
            except k8s_client.K8sNotFound:
                store.add_event(job, "warn",
                                f"冲突对象 {spec['kind']}/{spec['name']} 现已不存在，基线标记为已消失")
                spec["uid"] = None
                spec["branch"] = None
                spec["gone"] = True
            else:
                new_uid = k8s_client.uid_of(live)
                new_branch = k8s_client.branch_of(live)
                store.add_event(job, "warn",
                                f"人工确认接受 {spec['kind']}/{spec['name']} 的新身份："
                                f"UID={new_uid}，分支={new_branch}，清单基线已刷新")
                spec["uid"] = new_uid
                spec["branch"] = new_branch
                spec.pop("gone", None)
        job["phase"] = PHASE_CONFIRMED
        job["conflict"] = None
        job["pendingReason"] = None

    return store.update_job(job_id, mutate)
