"""滚动发布后的验收就绪判定：原 Service 能否用于验收新版。

只判定同时满足以下条件的 Service：
  - 同一命名空间；
  - type=ClusterIP；
  - 使用默认流量策略（externalTrafficPolicy / internalTrafficPolicy 未设置或为 Cluster）；
  - publishNotReadyAddresses 未开启。

判定规则（全部满足才“可验收”）：
  1. Service 未处于终止中；
  2. 对应 Deployment 的 status.observedGeneration 已追上 metadata.generation
     （控制器已观察到当前 spec）；
  3. 该 Service 的 EndpointSlice（按 kubernetes.io/service-name 关联）汇总后，
     至少有一个 ready=true 且未终止的端点；
  4. 每一个 ready=true 且未终止的端点，都能按
     targetRef.uid → Pod → ownerReference(ReplicaSet, UID 核对)
     → ownerReference(Deployment, UID 核对) 追到目标 Deployment；
  5. 端点所属 ReplicaSet 的 spec.template 与 Deployment 当前 spec.template
     一致（即属于当前 Pod 模板）。这比复算 pod-template-hash 更可靠：
     真实集群的哈希由控制器生成，客户端无法稳定复现。

显示状态：
  - acceptable  可验收：第 1–5 条全部成立；
  - pending     待核实：读取失败、判定期间状态变化、就绪状态或归属缺少证据；
  - blocked     不可验收：证据完整但结论为否（无就绪端点、存在旧版/其他分支端点等）；
  - unsupported 不适用：Service 不满足默认流量策略等前置条件。

一致性：所有读取都带 read_epoch；判定前后各重读一次 Deployment / Service /
EndpointSlice 的 resourceVersion，任一变化即判 pending，保证判定对应同一个集群状态。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import k8s_client

VERDICT_ACCEPTABLE = "acceptable"
VERDICT_PENDING = "pending"
VERDICT_BLOCKED = "blocked"
VERDICT_UNSUPPORTED = "unsupported"

DEFAULT_TRAFFIC_POLICIES = {"", "Cluster", None}

VERDICT_LABELS = {
    VERDICT_ACCEPTABLE: "可验收",
    VERDICT_PENDING: "待核实",
    VERDICT_BLOCKED: "不可验收",
    VERDICT_UNSUPPORTED: "不适用",
}


@dataclass
class EndpointRecord:
    """一个 EndpointSlice 端点及其归属证据。"""
    slice_name: str
    address: str
    ready: bool | None
    terminating: bool | None
    target_uid: str | None
    target_name: str | None
    port_name: str | None
    kind: str = "unknown"          # current / stale / other / unknown
    pod_name: str | None = None
    pod_branch: str | None = None
    pod_hash: str | None = None
    replicaset: str | None = None
    evidence: str = ""

    @property
    def ready_and_live(self) -> bool:
        return self.ready is True and not self.terminating

    @property
    def is_old_or_other(self) -> bool:
        return self.kind in ("stale", "other")


@dataclass
class AcceptanceResult:
    verdict: str
    service_name: str
    deployment_name: str | None = None
    deployment_uid: str | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)
    endpoints: list[EndpointRecord] = field(default_factory=list)
    stale_endpoints: list[EndpointRecord] = field(default_factory=list)
    read_epoch: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    detail: str = ""
    checked_at: str = ""

    def add_check(self, name: str, ok: bool | None, detail: str = "") -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail})

    @property
    def verdict_label(self) -> str:
        return VERDICT_LABELS.get(self.verdict, self.verdict)


def default_policy(service: dict[str, Any]) -> tuple[bool, str]:
    """判断 Service 是否属于本次检查范围；返回 (是否适用, 原因)。"""
    spec = service.get("spec") or {}
    svc_type = spec.get("type", "ClusterIP")
    if svc_type != "ClusterIP":
        return False, f"Service type 为 {svc_type}，不是 ClusterIP"
    etp = spec.get("externalTrafficPolicy")
    if etp not in DEFAULT_TRAFFIC_POLICIES:
        return False, f"externalTrafficPolicy={etp}，不是默认流量策略"
    itp = spec.get("internalTrafficPolicy")
    if itp not in DEFAULT_TRAFFIC_POLICIES:
        return False, f"internalTrafficPolicy={itp}，不是默认流量策略"
    if spec.get("publishNotReadyAddresses"):
        return False, "publishNotReadyAddresses=true，端点列表不反映就绪状态"
    return True, ""


# 控制器写入 Pod 模板、但不会出现在 Deployment spec 里的标签
_CONTROLLER_TEMPLATE_LABELS = {"pod-template-hash"}


def _normalize_template(template: dict[str, Any]) -> str:
    """规范化 Pod 模板用于比较。

    真实集群里 Deployment 控制器会把 pod-template-hash 标签写进 ReplicaSet 的
    spec.template，而 Deployment 自身的 spec.template 不含该标签；不剔除会导致
    所有端点被误判为旧版。这里只忽略控制器注入的字段，其余保持严格比较。
    """
    template = template or {}
    meta = dict(template.get("metadata") or {})
    labels = meta.get("labels")
    if isinstance(labels, dict):
        meta["labels"] = {k: v for k, v in labels.items()
                          if k not in _CONTROLLER_TEMPLATE_LABELS}
    normalized = dict(template)
    normalized["metadata"] = meta
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _endpoint_ready(ep: dict[str, Any]) -> bool | None:
    """读取 ready 条件；缺失或不是布尔值返回 None（无证据，不猜测）。"""
    ready = (ep.get("conditions") or {}).get("ready")
    return ready if isinstance(ready, bool) else None


def _endpoint_terminating(ep: dict[str, Any]) -> bool | None:
    term = (ep.get("conditions") or {}).get("terminating")
    return term if isinstance(term, bool) else False


def _slice_matches(slice_meta: dict[str, Any], service: dict[str, Any]) -> bool:
    labels = slice_meta.get("labels") or {}
    svc_name = service["metadata"]["name"]
    if labels.get("kubernetes.io/service-name"):
        return labels["kubernetes.io/service-name"] == svc_name
    svc_uid = k8s_client.uid_of(service)
    return any(ref.get("kind") == "Service" and ref.get("uid") == svc_uid
               for ref in slice_meta.get("ownerReferences") or [])


def _rvs(slices: list[dict[str, Any]]) -> dict[str, str]:
    return {s["metadata"]["name"]: str(s["metadata"].get("resourceVersion"))
            for s in slices}


def evaluate(service: dict[str, Any], deployment: dict[str, Any] | None,
             namespace: str) -> AcceptanceResult:
    """对单个 Service（及同分支 Deployment）做一次验收就绪判定。"""
    svc_name = service["metadata"]["name"]
    res = AcceptanceResult(
        verdict=VERDICT_PENDING,
        service_name=svc_name,
        deployment_name=(deployment or {}).get("metadata", {}).get("name"),
        deployment_uid=k8s_client.uid_of(deployment) if deployment else None,
        checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    ok, why = default_policy(service)
    if not ok:
        res.verdict = VERDICT_UNSUPPORTED
        res.problems.append(why)
        res.detail = f"不适用验收检查：{why}"
        return res
    res.add_check(
        "默认流量策略的 ClusterIP",
        True,
        "externalTrafficPolicy / internalTrafficPolicy 未设置；publishNotReadyAddresses 未开启")

    if k8s_client.is_terminating(service):
        res.problems.append("Service 正在终止中，不能作为验收入口")

    if deployment is None:
        res.verdict = VERDICT_PENDING
        res.detail = "未找到唯一对应的 Deployment（无法核对当前 Pod 模板）"
        res.problems.append(
            "未找到与该 Service 同分支的唯一 Deployment，归属与模板证据不足")
        res.add_check("控制器已观察当前 generation", None, "缺少 Deployment")
        return res

    dep_meta = deployment["metadata"]
    dep_status = deployment.get("status") or {}
    rv_start: dict[str, Any] = {
        "deployment": str(dep_meta.get("resourceVersion")),
        "service": str(service["metadata"].get("resourceVersion")),
    }

    # ===== 控制器是否已观察当前 generation =====
    generation = dep_meta.get("generation")
    observed = dep_status.get("observedGeneration")
    if not isinstance(generation, int):
        res.problems.append("Deployment 缺少 metadata.generation，无法证明控制器已观察当前版本")
        res.add_check("控制器已观察当前 generation", None, "缺少 generation")
    elif not isinstance(observed, int):
        res.problems.append(
            "Deployment 缺少 status.observedGeneration，无法证明控制器已观察当前版本")
        res.add_check("控制器已观察当前 generation", None, "缺少 observedGeneration")
    elif observed < generation:
        res.problems.append(
            f"Deployment 控制器尚未观察到当前 generation"
            f"（observedGeneration={observed} < generation={generation}）")
        res.add_check("控制器已观察当前 generation", False,
                      f"observedGeneration={observed} < generation={generation}")
    else:
        res.add_check("控制器已观察当前 generation", True,
                      f"observedGeneration={observed} >= generation={generation}")

    current_template = _normalize_template(
        (deployment.get("spec") or {}).get("template") or {})

    # ===== 读取 EndpointSlice =====
    try:
        listed = k8s_client.list_endpointslices(namespace)
    except k8s_client.K8sOperationError as exc:
        res.problems.append(f"读取 EndpointSlice 失败：{exc}")
        res.detail = "无法读取 EndpointSlice"
        return res

    matched = [s for s in listed if _slice_matches(s["metadata"], service)]
    slices: list[dict[str, Any]] = []
    for s in matched:
        try:
            # 逐条重读，判定使用同一份内容
            slices.append(k8s_client.get_endpointslice(namespace, s["metadata"]["name"]))
        except k8s_client.K8sNotFound:
            res.problems.append(
                f"EndpointSlice/{s['metadata']['name']} 在读取过程中消失，判定期间状态不一致")
        except k8s_client.K8sOperationError as exc:
            res.problems.append(f"读取 EndpointSlice/{s['metadata']['name']} 失败：{exc}")

    if not slices:
        if not res.problems:
            res.problems.append(f"Service/{svc_name} 没有关联的 EndpointSlice")
        res.add_check("存在可读的 EndpointSlice", False, "未找到 service-name 关联的切片")
        res.verdict = VERDICT_PENDING
        res.detail = "没有可用的 EndpointSlice：" + "；".join(res.problems)
        return res
    rv_start["slices"] = _rvs(slices)
    res.add_check("存在可读的 EndpointSlice", True, f"{len(slices)} 个切片")

    port_names = {(p or {}).get("name")
                  for p in (service.get("spec") or {}).get("ports") or []}

    records: list[EndpointRecord] = []
    skipped_ports = 0
    for s in slices:
        for ep in s.get("endpoints") or []:
            port_name = ep.get("port")
            if port_name is not None and port_names and port_name not in port_names:
                skipped_ports += 1
                continue
            target = ep.get("targetRef") or {}
            records.append(EndpointRecord(
                slice_name=s["metadata"]["name"],
                address=", ".join(ep.get("addresses") or []) or "(无地址)",
                ready=_endpoint_ready(ep),
                terminating=_endpoint_terminating(ep),
                target_uid=target.get("uid"),
                target_name=target.get("name"),
                port_name=port_name,
            ))

    if not records:
        res.problems.append(
            f"EndpointSlice 中有 {skipped_ports} 个端点，但没有端点匹配该 Service 的端口名 "
            f"{sorted(n for n in port_names if n)}")
        res.add_check("至少有一个就绪且未终止的端点", False, "无匹配端口的端点")
        res.detail = "没有可用的端点"
        return res

    # ===== 逐个端点取证 =====
    pod_cache: dict[str, dict | None] = {}
    rs_cache: dict[str, dict | None] = {}
    pod_rv: dict[str, str] = {}
    rs_rv: dict[str, str] = {}
    for rec in records:
        _resolve_endpoint(rec, deployment, current_template, namespace,
                          res, pod_cache, rs_cache)
    for name, pod in pod_cache.items():
        if pod is not None:
            pod_rv[name] = str(pod["metadata"].get("resourceVersion"))
    for name, rs in rs_cache.items():
        if rs is not None:
            rs_rv[name] = str(rs["metadata"].get("resourceVersion"))

    res.endpoints = records
    ready_live = [r for r in records if r.ready_and_live]
    stale = [r for r in ready_live if r.is_old_or_other]
    current_eps = [r for r in ready_live if r.kind == "current"]
    unknown = [r for r in ready_live if r.kind == "unknown"]
    res.stale_endpoints = stale

    # 端点就绪状态缺失：即使不计入就绪端点，也属于“证据不足”
    missing_ready = [r for r in records if r.ready is None]
    if missing_ready:
        res.problems.append(
            f"{len(missing_ready)} 个端点缺少布尔类型的 conditions.ready，无法确认就绪状态")

    if not ready_live:
        res.add_check("至少有一个就绪且未终止的端点", False, "0 个")
    else:
        res.add_check("至少有一个就绪且未终止的端点", True,
                      f"{len(ready_live)} 个 ready 且未终止")

    if unknown:
        # 存在 ready 端点但无法追到 Deployment：证据不足，不能给出“可验收”
        res.problems.append(
            f"{len(unknown)} 个就绪端点无法经 Pod → ReplicaSet 追到目标 Deployment，"
            "归属证据不足")

    if current_eps:
        res.add_check("端点追到 Deployment 当前 Pod 模板", True,
                      f"{len(current_eps)}/{len(ready_live)} 个就绪端点属于当前模板")
    else:
        res.add_check("端点追到 Deployment 当前 Pod 模板", False,
                      f"0/{len(ready_live)} 个就绪端点属于当前模板")
    if unknown:
        res.add_check("端点归属证据完整", False,
                      f"{len(unknown)} 个就绪端点无法确认归属")
    if stale:
        res.add_check("旧版/其他分支就绪端点", None,
                      f"{len(stale)} 个仍需列出：" + "；".join(
                          f"{r.address}（Pod/{r.pod_name}，{r.evidence}）" for r in stale))

    # ===== 一致性复核：重读起点对象 =====
    res.problems.extend(
        _verify_consistency(res, service, deployment, namespace, rv_start,
                            pod_rv, rs_rv))

    # ===== 归并判定 =====
    if res.problems:
        res.verdict = VERDICT_PENDING
        res.detail = "；".join(res.problems)
    elif stale:
        res.verdict = VERDICT_BLOCKED
        res.detail = (f"仍有 {len(stale)} 个旧版或其他分支端点就绪且未终止，"
                      "Service 会把流量分给旧版，不能用于验收新版")
    elif not current_eps:
        res.verdict = VERDICT_BLOCKED
        if not ready_live:
            res.detail = (f"没有 ready=true 且未终止的端点（{len(records)} 个端点均不就绪），"
                          "Service 当前没有可用于验收的后端")
        else:
            res.detail = "没有就绪端点属于 Deployment 当前 Pod 模板"
    else:
        res.verdict = VERDICT_ACCEPTABLE
        res.detail = (f"{len(current_eps)} 个就绪且未终止的端点均按 UID 追到 "
                      f"Deployment/{res.deployment_name} 的当前 Pod 模板，可用于验收新版")
    return res


def _resolve_endpoint(rec: EndpointRecord, deployment: dict, current_template: str,
                      namespace: str, res: AcceptanceResult,
                      pod_cache: dict[str, dict | None],
                      rs_cache: dict[str, dict | None]) -> None:
    target = rec.target_name
    if not target:
        rec.evidence = "targetRef 缺少 Pod 名称，无法归属"
        return
    if not rec.target_uid:
        rec.evidence = "targetRef 缺少 UID，无法确认端点指向的对象"
        return

    pod = pod_cache.get(target)
    if target not in pod_cache:
        try:
            pod = k8s_client.get_pod(namespace, target)
        except k8s_client.K8sNotFound:
            res.problems.append(f"端点 {rec.address} 的 targetRef Pod/{target} 已不存在")
            pod = None
        except k8s_client.K8sOperationError as exc:
            res.problems.append(f"读取端点 {rec.address} 的 Pod/{target} 失败：{exc}")
            pod = None
        pod_cache[target] = pod
    if pod is None:
        rec.evidence = f"无法读取 Pod/{target}"
        return

    rec.pod_name = target
    if k8s_client.uid_of(pod) != rec.target_uid:
        rec.evidence = (f"targetRef UID {rec.target_uid} 与 Pod/{target} "
                        f"UID {k8s_client.uid_of(pod)} 不符，证据不一致")
        return

    pod_meta = pod.get("metadata") or {}
    pod_labels = pod_meta.get("labels") or {}
    rec.pod_hash = pod_labels.get("pod-template-hash")
    rec.pod_branch = k8s_client.branch_of(pod)

    dep_branch = k8s_client.branch_of(deployment)
    if rec.pod_branch is not None and dep_branch is not None \
            and rec.pod_branch != dep_branch:
        # 端点所在 Pod 的分支标签与目标 Deployment 不一致：明确的其他分支证据
        rec.kind = "other"
        rec.evidence = (f"Pod/{target} 分支标签为 {rec.pod_branch}，"
                        f"目标 Deployment 为 {dep_branch}")
        return

    controllers = [o for o in pod_meta.get("ownerReferences") or []
                   if o.get("controller") and o.get("kind") == "ReplicaSet"]
    if len(controllers) != 1:
        rec.evidence = f"Pod/{target} 的 controller ownerReference 不是唯一的 ReplicaSet"
        return
    rs_ref = controllers[0]
    rs_name = rs_ref.get("name")
    if not rs_name:
        rec.evidence = f"Pod/{target} 的 ReplicaSet ownerReference 缺少名称"
        return

    if rs_name not in rs_cache:
        try:
            rs_cache[rs_name] = k8s_client.get_replicaset(namespace, rs_name)
        except k8s_client.K8sNotFound:
            res.problems.append(f"Pod/{target} 的 ReplicaSet/{rs_name} 已不存在")
            rs_cache[rs_name] = None
        except k8s_client.K8sOperationError as exc:
            res.problems.append(f"读取 ReplicaSet/{rs_name} 失败：{exc}")
            rs_cache[rs_name] = None
    rs = rs_cache[rs_name]
    if rs is None:
        rec.evidence = f"无法读取 ReplicaSet/{rs_name}"
        return
    if k8s_client.uid_of(rs) != rs_ref.get("uid"):
        rec.evidence = (f"Pod ownerReference 的 ReplicaSet UID {rs_ref.get('uid')} "
                        f"与实时对象 {k8s_client.uid_of(rs)} 不符（疑似同名重建）")
        return
    rec.replicaset = rs_name

    dep_refs = [o for o in (rs.get("metadata") or {}).get("ownerReferences") or []
                if o.get("controller") and o.get("kind") == "Deployment"]
    if len(dep_refs) != 1:
        rec.evidence = f"ReplicaSet/{rs_name} 的 controller owner 不是唯一的 Deployment"
        return
    dep_ref = dep_refs[0]
    if dep_ref.get("uid") != k8s_client.uid_of(deployment):
        # 完整追到了“另一个” Deployment：这是明确的旧版/其他分支证据，不是缺证据
        rec.kind = "other"
        rec.evidence = (f"属于 Deployment/{dep_ref.get('name')}"
                        f"（UID {dep_ref.get('uid')}），不是目标 Deployment")
        return

    rs_template = _normalize_template((rs.get("spec") or {}).get("template") or {})
    rs_hash = ((rs.get("metadata") or {}).get("labels") or {}).get("pod-template-hash")
    if rec.pod_hash and rs_hash and rec.pod_hash != rs_hash:
        rec.evidence = (f"Pod 模板哈希 {rec.pod_hash} 与 "
                        f"ReplicaSet/{rs_name} 的 {rs_hash} 不一致")
        return
    if not rs_template or rs_template == "{}":
        rec.evidence = f"ReplicaSet/{rs_name} 缺少 spec.template，无法核对是否当前模板"
        return

    if rs_template == current_template:
        rec.kind = "current"
        rec.evidence = (f"Pod/{target} → ReplicaSet/{rs_name} → "
                        f"Deployment/{deployment['metadata']['name']}，模板与当前 spec 一致")
    else:
        rec.kind = "stale"
        rec.evidence = (f"Pod/{target} → ReplicaSet/{rs_name} 属于同一 Deployment，"
                        "但 Pod 模板不是当前 spec（旧版）")


def _verify_consistency(res: AcceptanceResult, service: dict, deployment: dict,
                        namespace: str, rv_start: dict[str, Any],
                        pod_rv: dict[str, str], rs_rv: dict[str, str]) -> list[str]:
    """重读判定所依赖的对象；读取失败或 resourceVersion 变化说明判定跨了集群状态。

    除了 Service / Deployment / EndpointSlice，端点归属用到的 Pod 与 ReplicaSet
    也一并复核：否则“追到当前模板”的结论可能建立在已被替换的对象上。
    """
    problems: list[str] = []
    dep_name = deployment["metadata"]["name"]

    try:
        dep_now = k8s_client.get_deployment(namespace, dep_name)
    except k8s_client.K8sNotFound:
        return ["Deployment 在判定期间被删除，状态不一致"]
    except k8s_client.K8sOperationError as exc:
        return [f"复核 Deployment 失败：{exc}"]

    try:
        svc_now = k8s_client.get_service(namespace, service["metadata"]["name"])
    except k8s_client.K8sNotFound:
        return ["Service 在判定期间被删除，状态不一致"]
    except k8s_client.K8sOperationError as exc:
        return [f"复核 Service 失败：{exc}"]

    if str(dep_now["metadata"].get("resourceVersion")) != rv_start["deployment"]:
        problems.append(
            f"Deployment resourceVersion 由 {rv_start['deployment']} 变为 "
            f"{dep_now['metadata'].get('resourceVersion')}，判定期间集群状态变化，请重试")
    if str(svc_now["metadata"].get("resourceVersion")) != rv_start["service"]:
        problems.append(
            f"Service resourceVersion 由 {rv_start['service']} 变为 "
            f"{svc_now['metadata'].get('resourceVersion')}，判定期间集群状态变化，请重试")

    try:
        slices_now = [s for s in k8s_client.list_endpointslices(namespace)
                      if _slice_matches(s["metadata"], service)]
    except k8s_client.K8sOperationError as exc:
        return problems + [f"复核 EndpointSlice 失败：{exc}"]

    if _rvs(slices_now) != rv_start.get("slices", {}):
        problems.append("EndpointSlice 集合或 resourceVersion 在判定期间变化，请重试")

    for name, expected in pod_rv.items():
        try:
            now = k8s_client.get_pod(namespace, name)
        except k8s_client.K8sNotFound:
            problems.append(f"Pod/{name} 在判定期间被删除，状态不一致")
            continue
        except k8s_client.K8sOperationError as exc:
            problems.append(f"复核 Pod/{name} 失败：{exc}")
            continue
        if str(now["metadata"].get("resourceVersion")) != expected:
            problems.append(f"Pod/{name} 在判定期间发生变化，请重试")

    for name, expected in rs_rv.items():
        try:
            now = k8s_client.get_replicaset(namespace, name)
        except k8s_client.K8sNotFound:
            problems.append(f"ReplicaSet/{name} 在判定期间被删除，状态不一致")
            continue
        except k8s_client.K8sOperationError as exc:
            problems.append(f"复核 ReplicaSet/{name} 失败：{exc}")
            continue
        if str(now["metadata"].get("resourceVersion")) != expected:
            problems.append(f"ReplicaSet/{name} 在判定期间发生变化，请重试")

    res.read_epoch = {
        "deploymentResourceVersion": rv_start["deployment"],
        "serviceResourceVersion": rv_start["service"],
        "slices": rv_start.get("slices", {}),
        "pods": pod_rv,
        "replicaSets": rs_rv,
        "consistent": not problems,
    }
    return problems


def find_deployment_for_service(service: dict[str, Any],
                                deployments: list[dict[str, Any]],
                                namespace_services: list[dict[str, Any]]) -> dict[str, Any] | None:
    """寻找该 Service 对应的 Deployment。

    只按同分支标签配对；若同分支存在多个 Deployment / 多个 Service，则证据不足，
    交由调用方显示“待核实”，不猜测。
    """
    branch = k8s_client.branch_of(service)
    if not branch:
        return None
    same_branch_svcs = [s for s in namespace_services
                        if k8s_client.branch_of(s) == branch]
    same_branch_deps = [d for d in deployments if k8s_client.branch_of(d) == branch]
    if len(same_branch_svcs) != 1 or len(same_branch_deps) != 1:
        return None
    return same_branch_deps[0]
