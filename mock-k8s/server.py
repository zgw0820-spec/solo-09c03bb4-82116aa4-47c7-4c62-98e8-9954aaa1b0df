"""本地 Kubernetes API 模拟端（仅用于演示，非生产代码）。

实现的语义与真实 Kubernetes 对齐的部分：
- GET 单个对象不存在时返回 404 Status JSON
- DELETE 不会立刻让对象消失，而是设置 metadata.deletionTimestamp，
  对象在终止期内仍可 GET；延迟到期后才真正移除
- 对已在终止中的对象再次 DELETE 返回 409 Conflict
- 同名重建会得到新的 metadata.uid
- 改属通过修改分支标签体现，uid 保持不变
- Deployment 带 metadata.generation 与 status.observedGeneration
- ReplicaSet / Pod 通过 controller ownerReference 与 pod-template-hash 关联
- EndpointSlice 通过 kubernetes.io/service-name 标签与 Service 关联
- 删除 Deployment / Service 时，延迟到期后级联回收其 ReplicaSet、Pod、EndpointSlice

状态持久化到 /data/mock-state.json（可用 STATE_FILE 覆盖），
模拟端重启后终止中的对象仍保留 deletionTimestamp，贴近 etcd 持久化语义。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

STATE_FILE = os.environ.get("STATE_FILE", "/data/mock-state.json")
NAMESPACE = os.environ.get("NAMESPACE", "previews")
BRANCH_LABEL = os.environ.get("BRANCH_LABEL", "preview.example.com/branch")
DEFAULT_DELETE_DELAY = float(os.environ.get("DEFAULT_DELETE_DELAY", "0"))

_LOCK = threading.RLock()
_STORE: dict = {"objects": {}}  # key: str((kind, namespace, name)) -> object
# 一次性“检查途中换版”触发器：在收到第 N 个匹配请求时先改变集群状态
_CHURN: dict = {"remaining": 0, "match": "", "action": ""}


def _key(kind: str, namespace: str, name: str) -> str:
    return str((kind, namespace, name))


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bump(meta: dict) -> None:
    meta["resourceVersion"] = str(int(meta.get("resourceVersion", "1")) + 1)


def _template_hash(template: dict) -> str:
    canonical = json.dumps(template or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:10]


def _strip_controller_labels(template: dict) -> dict:
    """去掉控制器注入的 pod-template-hash，便于与 Deployment spec.template 比较。"""
    out = json.loads(json.dumps(template or {}))
    labels = (out.get("metadata") or {}).get("labels")
    if isinstance(labels, dict):
        labels.pop("pod-template-hash", None)
    return out


def _template(name: str, branch: str, version: str) -> dict:
    return {
        "metadata": {
            "labels": {
                "app": name,
                BRANCH_LABEL: branch,
                "app.kubernetes.io/version": version,
            },
        },
        "spec": {
            "containers": [{
                "name": "app",
                "image": f"registry.example.com/{name}:{version}",
            }],
        },
    }


def _replicaset(branch: str, name: str, template: dict, version: str,
                rs_suffix: str, rv: str = "1", deployment_uid: str = "") -> dict:
    rs_name = f"{name}-{rs_suffix}"
    hash_ = _template_hash(template)
    # 贴近真实集群：控制器把 pod-template-hash 注入 ReplicaSet 的 spec.template
    rs_template = json.loads(json.dumps(template))
    rs_template.setdefault("metadata", {}).setdefault("labels", {})["pod-template-hash"] = hash_
    return {
        "apiVersion": "apps/v1",
        "kind": "ReplicaSet",
        "metadata": {
            "name": rs_name,
            "namespace": NAMESPACE,
            "uid": f"uid-rs-{branch}-{rs_suffix}",
            "labels": {
                "app": name,
                BRANCH_LABEL: branch,
                "pod-template-hash": hash_,
            },
            "ownerReferences": [{
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": name,
                "uid": deployment_uid,
                "controller": True,
                "blockOwnerDeletion": True,
            }] if deployment_uid else [],
            "resourceVersion": rv,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {
                "app": name,
                "pod-template-hash": hash_,
            }},
            "template": rs_template,
        },
        "status": {"replicas": 1, "readyReplicas": 1},
    }


def _pod(branch: str, name: str, template: dict, rs: dict, version: str,
         ready: bool = True, rv: str = "1") -> dict:
    hash_ = _template_hash(template)
    pod_name = f"{rs['metadata']['name']}-{uuid.uuid4().hex[:5]}"
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": NAMESPACE,
            "uid": f"uid-pod-{branch}-{uuid.uuid4().hex[:8]}",
            "labels": {
                "app": name,
                BRANCH_LABEL: branch,
                "pod-template-hash": hash_,
                "app.kubernetes.io/version": version,
            },
            "ownerReferences": [{
                "apiVersion": "apps/v1",
                "kind": "ReplicaSet",
                "name": rs["metadata"]["name"],
                "uid": rs["metadata"]["uid"],
                "controller": True,
                "blockOwnerDeletion": True,
            }],
            "resourceVersion": rv,
        },
        "spec": {"containers": template["spec"]["containers"]},
        "status": {"phase": "Running", "conditions": [
            {"type": "Ready", "status": "True" if ready else "False"},
        ]},
    }


def _endpoint(pod: dict, ready: bool = True, address: str | None = None) -> dict:
    uid = pod["metadata"]["uid"]
    octet = 10 + int(hashlib.sha256(uid.encode()).hexdigest()[:2], 16) % 200
    return {
        "addresses": [address or f"10.0.{octet}.{abs(hash(uid)) % 200 + 1}"],
        "conditions": {
            "ready": ready,
            "serving": ready,
            "terminating": False,
        },
        "nodeName": "node-a",
        "targetRef": {
            "kind": "Pod",
            "namespace": NAMESPACE,
            "name": pod["metadata"]["name"],
            "uid": pod["metadata"]["uid"],
        },
        "port": "http",
    }


def _endpointslice(branch: str, svc: dict, pods: list, rv: str = "1") -> dict:
    name = f"{svc['metadata']['name']}-{branch}-{_template_hash(svc)}"
    return {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "uid": f"uid-slice-{branch}-0001",
            "labels": {
                "kubernetes.io/service-name": svc["metadata"]["name"],
                BRANCH_LABEL: branch,
            },
            "ownerReferences": [{
                "apiVersion": "v1",
                "kind": "Service",
                "name": svc["metadata"]["name"],
                "uid": svc["metadata"]["uid"],
                "controller": True,
            }],
            "resourceVersion": rv,
        },
        "addressType": "IPv4",
        "ports": [{"name": "http", "port": 8080, "protocol": "TCP"}],
        "endpoints": [_endpoint(p) for p in pods],
    }


def _objects(kind: str, namespace: str) -> list[dict]:
    return [o for o in _STORE["objects"].values()
            if o["kind"] == kind and o["metadata"]["namespace"] == namespace]


def _seed_branch(branch: str) -> None:
    name = f"preview-{branch}"
    delay = os.environ.get(f"DELAY_{branch.replace('-', '_').upper()}", "")
    annotations = {"mock.preview/delete-delay": delay} if delay else {}

    svc = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "uid": f"uid-svc-{branch}-0001",
            "labels": {BRANCH_LABEL: branch, "app": name},
            "annotations": dict(annotations),
            "resourceVersion": "1",
        },
        "spec": {
            "type": "ClusterIP",
            "ports": [{"name": "http", "port": 80, "targetPort": 8080, "protocol": "TCP"}],
            "selector": {"app": name},
        },
    }
    template = _template(name, branch, "v1")
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "uid": f"uid-deploy-{branch}-0001",
            "labels": {BRANCH_LABEL: branch, "app": name},
            "annotations": dict(annotations),
            "resourceVersion": "1",
            "generation": 1,
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": template,
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "readyReplicas": 1,
            "updatedReplicas": 1,
            "availableReplicas": 1,
        },
    }
    rs = _replicaset(branch, name, template, "v1", "r1",
                     deployment_uid=deployment["metadata"]["uid"])
    pod = _pod(branch, name, template, rs, "v1")

    _STORE["objects"][_key("Service", NAMESPACE, name)] = svc
    _STORE["objects"][_key("Deployment", NAMESPACE, name)] = deployment
    _STORE["objects"][_key("ReplicaSet", NAMESPACE, rs["metadata"]["name"])] = rs
    _STORE["objects"][_key("Pod", NAMESPACE, pod["metadata"]["name"])] = pod
    slice_ = _endpointslice(branch, svc, [pod])
    _STORE["objects"][_key("EndpointSlice", NAMESPACE, slice_["metadata"]["name"])] = slice_


def _seed() -> None:
    """构造初始预览环境：三个分支，每个分支一对 Service + Deployment（各带 RS/Pod/Slice）。"""
    with _LOCK:
        _STORE["objects"].clear()
        for branch in ["feature-a", "feature-b", "hotfix-1"]:
            _seed_branch(branch)
        _persist_locked()


def _persist_locked() -> None:
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_STORE, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _load() -> None:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and "objects" in loaded:
                _STORE.update(loaded)
                # 启动即清扫：重启后已过延迟期的终止对象应立即消失，
                # 未到期的保留 deletionTimestamp，贴近真实控制器的恢复语义
                _sweep_locked()
                return
        except (json.JSONDecodeError, OSError):
            pass
    _seed()


def _delete_delay(obj: dict) -> float:
    ann = obj.get("metadata", {}).get("annotations", {}) or {}
    try:
        return float(ann.get("mock.preview/delete-delay", DEFAULT_DELETE_DELAY))
    except (TypeError, ValueError):
        return DEFAULT_DELETE_DELAY


def _owned_by(obj: dict, owner_kind: str, owner_uid: str) -> bool:
    return any(ref.get("kind") == owner_kind and ref.get("uid") == owner_uid
               for ref in obj.get("metadata", {}).get("ownerReferences") or [])


def _sweep_locked() -> None:
    """删除到期的终止对象，并按 ownerReference 级联回收子对象。

    删除 Deployment 时回收其 ReplicaSet 与 Pod；删除 Service 时回收其 EndpointSlice。
    """
    now = time.time()
    expired: list[dict] = []
    for key, obj in list(_STORE["objects"].items()):
        meta = obj["metadata"]
        if meta.get("deletionTimestamp") \
                and now - float(meta["_deleteStartedAt"]) >= _delete_delay(obj):
            expired.append(obj)

    if not expired:
        return

    # 反复回收：先删父对象，再删仍以已删对象 ownerReference 为父的子对象
    removed = expired
    while removed:
        for obj in removed:
            _STORE["objects"].pop(
                _key(obj["kind"], obj["metadata"]["namespace"],
                     obj["metadata"]["name"]), None)
        gone_uids = {obj["metadata"]["uid"] for obj in removed}
        removed = [
            obj for obj in list(_STORE["objects"].values())
            if any(ref.get("uid") in gone_uids
                   for ref in obj.get("metadata", {}).get("ownerReferences") or [])
        ]
    _persist_locked()


def _find(kind: str, namespace: str, name: str):
    _sweep_locked()
    return _STORE["objects"].get(_key(kind, namespace, name))


def _list(kind: str, namespace: str) -> list:
    _sweep_locked()
    return _objects(kind, namespace)


def _status(code: int, reason: str, message: str) -> dict:
    return {
        "kind": "Status",
        "apiVersion": "v1",
        "status": "Failure",
        "code": code,
        "reason": reason,
        "message": message,
    }


def _parse_resource(parts: list) -> tuple[str, str, str | None] | None:
    """把 REST 路径映射成 (kind, namespace, name|None)。"""
    table = {
        ("api", "v1"): {"services": "Service", "pods": "Pod"},
        ("apis", "apps", "v1"): {"deployments": "Deployment", "replicasets": "ReplicaSet"},
        ("apis", "discovery.k8s.io", "v1"): {"endpointslices": "EndpointSlice"},
    }
    for prefix, resources in table.items():
        n = len(prefix)
        if tuple(parts[:n]) != prefix:
            continue
        rest = parts[n:]
        if len(rest) < 3 or rest[0] != "namespaces" or rest[2] not in resources:
            continue
        kind = resources[rest[2]]
        if len(rest) == 3:
            return kind, rest[1], None
        if len(rest) == 4:
            return kind, rest[1], rest[3]
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-k8s/1.0"

    def log_message(self, fmt, *args):  # 精简日志
        print(f"[mock-k8s] {self.address_string()} - {fmt % args}")

    def _send(self, code: int, payload: dict | list) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ---- 路由 ----
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = parse_qs(parsed.query)
        with _LOCK:
            if parts == ["mock", "state"]:
                _sweep_locked()
                self._send(200, {"objects": list(_STORE["objects"].values())})
                return
            self._maybe_churn_locked(parts)
            res = _parse_resource(parts)
            if res is not None:
                kind, namespace, name = res
                if name is None:
                    self._handle_list(kind, namespace, qs)
                else:
                    self._handle_get(kind, namespace, name)
                return
            self._send(404, _status(404, "NotFound", f"no handler for {parsed.path}"))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        with _LOCK:
            res = _parse_resource(parts)
            if res is None or res[2] is None:
                self._send(404, _status(404, "NotFound", parsed.path))
                return
            kind, namespace, name = res
            obj = _find(kind, namespace, name)
            if obj is None:
                self._send(404, _status(404, "NotFound",
                                        f'{kind.lower()}s "{name}" not found'))
                return
            meta = obj["metadata"]
            if meta.get("deletionTimestamp"):
                self._send(409, _status(409, "Conflict",
                                        f'operation cannot be fulfilled on {kind.lower()}s "{name}": '
                                        "the object is being deleted"))
                return
            meta["deletionTimestamp"] = _now()
            meta["_deleteStartedAt"] = time.time()
            _bump(meta)
            _persist_locked()
            self._send(200, obj)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        body = self._read_json()
        with _LOCK:
            if parts == ["mock", "reset"]:
                if os.path.exists(STATE_FILE):
                    os.remove(STATE_FILE)
                _CHURN.update({"remaining": 0, "match": "", "action": ""})
                _seed()
                self._send(200, {"ok": True, "message": "reseeded"})
                return
            if parts[:2] == ["mock", "scenarios"]:
                self._handle_scenario(parts[2:] if len(parts) > 2 else [], body,
                                      parse_qs(parsed.query))
                return
            self._send(404, _status(404, "NotFound", parsed.path))

    # ---- 业务处理 ----
    def _handle_get(self, kind: str, namespace: str, name: str) -> None:
        obj = _find(kind, namespace, name)
        if obj is None:
            self._send(404, _status(404, "NotFound",
                                    f'{kind.lower()}s "{name}" not found'))
            return
        self._send(200, obj)

    def _handle_list(self, kind: str, namespace: str, qs: dict) -> None:
        items = _list(kind, namespace)
        selector = (qs.get("labelSelector") or [""])[0]
        if selector:
            wanted = dict(
                part.split("=", 1) for part in selector.split(",") if "=" in part)
            items = [
                o for o in items
                if all((o["metadata"].get("labels") or {}).get(k) == v
                       for k, v in wanted.items())
            ]
        self._send(200, {"kind": f"{kind}List", "apiVersion": "v1", "items": items})

    # ---- 场景 ----
    def _deployment_children(self, name: str):
        ds = _find("Deployment", NAMESPACE, name)
        svc = _find("Service", NAMESPACE, name)
        dep_uid = ds["metadata"]["uid"] if ds else None
        svc_uid = svc["metadata"]["uid"] if svc else None
        rss = [o for o in _objects("ReplicaSet", NAMESPACE)
               if dep_uid and _owned_by(o, "Deployment", dep_uid)]
        pods = [p for p in _objects("Pod", NAMESPACE)
                if any(_owned_by(p, "ReplicaSet", rs["metadata"]["uid"]) for rs in rss)]
        slices = [o for o in _objects("EndpointSlice", NAMESPACE)
                  if svc_uid and _owned_by(o, "Service", svc_uid)]
        return ds, svc, rss, pods, slices

    def _rollout_locked(self, name: str, version: str, keep_old: bool,
                        observe: bool = True) -> dict:
        """模拟一次 Deployment 滚动发布：新模板、新 generation、新 RS/Pod/端点。"""
        ds, svc, rss, pods, slices = self._deployment_children(name)
        if ds is None or svc is None:
            raise LookupError("deployment/service not found")
        branch = ds["metadata"]["labels"].get(BRANCH_LABEL, "")
        new_template = _template(name, branch, version)
        new_hash = _template_hash(new_template)
        ds["spec"]["template"] = new_template
        ds["metadata"]["generation"] = int(ds["metadata"].get("generation", 1)) + 1
        if observe:
            ds["status"]["observedGeneration"] = ds["metadata"]["generation"]
        _bump(ds["metadata"])

        new_rs = _replicaset(branch, name, new_template, version, f"r{new_hash[:6]}",
                             deployment_uid=ds["metadata"]["uid"])
        existing = {rs["metadata"]["name"] for rs in rss}
        while new_rs["metadata"]["name"] in existing:
            new_rs["metadata"]["name"] += "x"
        new_pod = _pod(branch, name, new_template, new_rs, version)
        _STORE["objects"][_key("ReplicaSet", NAMESPACE, new_rs["metadata"]["name"])] = new_rs
        _STORE["objects"][_key("Pod", NAMESPACE, new_pod["metadata"]["name"])] = new_pod

        slice_ = slices[0] if slices else None
        if slice_ is None:
            slice_ = _endpointslice(branch, svc, [])
            _STORE["objects"][_key("EndpointSlice", NAMESPACE,
                                   slice_["metadata"]["name"])] = slice_
        endpoint_by_uid = {ep["targetRef"]["uid"]: ep
                           for ep in slice_.get("endpoints") or []}

        if not keep_old:
            # 新版就绪后立即摘除旧版：删除旧 RS/Pod 与旧端点
            old_pod_uids = [p["metadata"]["uid"] for p in pods]
            for p in pods:
                _STORE["objects"].pop(_key("Pod", NAMESPACE, p["metadata"]["name"]), None)
            for rs in rss:
                _STORE["objects"].pop(
                    _key("ReplicaSet", NAMESPACE, rs["metadata"]["name"]), None)
            for uid in old_pod_uids:
                endpoint_by_uid.pop(uid, None)

        endpoint_by_uid[new_pod["metadata"]["uid"]] = _endpoint(new_pod)
        slice_["endpoints"] = list(endpoint_by_uid.values())
        _bump(slice_["metadata"])
        ds["status"]["updatedReplicas"] = 1
        ds["status"]["readyReplicas"] = len(slice_["endpoints"])
        ds["status"]["availableReplicas"] = len(slice_["endpoints"])
        _persist_locked()
        return {
            "ok": True,
            "version": version,
            "generation": ds["metadata"]["generation"],
            "observedGeneration": ds["status"].get("observedGeneration"),
            "newReplicaSet": new_rs["metadata"]["name"],
            "newPod": new_pod["metadata"]["name"],
            "endpoints": len(slice_["endpoints"]),
            "keptOld": keep_old,
        }

    def _finish_rollout_locked(self, name: str) -> dict:
        ds, _svc, rss, pods, slices = self._deployment_children(name)
        if ds is None:
            raise LookupError("deployment not found")
        current_template = ds["spec"]["template"]
        current_hash = _template_hash(current_template)
        removed_pods = []
        current_rs_names = set()
        for rs in rss:
            # 与工具一致：比较模板时忽略控制器注入的 pod-template-hash
            if _template_hash(_strip_controller_labels(rs["spec"].get("template") or {})) \
                    == current_hash:
                current_rs_names.add(rs["metadata"]["name"])
                continue
            for p in list(pods):
                if _owned_by(p, "ReplicaSet", rs["metadata"]["uid"]):
                    removed_pods.append(p["metadata"]["uid"])
                    _STORE["objects"].pop(_key("Pod", NAMESPACE, p["metadata"]["name"]), None)
            _STORE["objects"].pop(
                _key("ReplicaSet", NAMESPACE, rs["metadata"]["name"]), None)
        for slice_ in slices:
            slice_["endpoints"] = [
                ep for ep in slice_.get("endpoints") or []
                if ep["targetRef"]["uid"] not in removed_pods
            ]
            _bump(slice_["metadata"])
        ds["status"]["observedGeneration"] = ds["metadata"]["generation"]
        ds["status"]["readyReplicas"] = sum(
            len(s.get("endpoints") or []) for s in slices)
        _persist_locked()
        return {"ok": True, "removedOldPods": len(removed_pods),
                "replicaSets": sorted(current_rs_names)}

    def _maybe_churn_locked(self, parts: list) -> None:
        """一次性故障注入：在第 N 个匹配的 GET 请求前改变集群状态。

        用于验证“检查途中换版”——判定第一遍读到的状态与复核时不一致，
        页面必须显示待核实而不是给出结论。
        """
        if _CHURN["remaining"] <= 0:
            return
        match = _CHURN["match"]
        path = "/" + "/".join(parts)
        if match and match not in path:
            return
        _CHURN["remaining"] -= 1
        if _CHURN["remaining"] > 0:
            return
        action = _CHURN["action"]
        _CHURN.update({"remaining": 0, "match": "", "action": ""})
        if action.startswith("rollout:"):
            name, version = action.split(":", 1)[1].split("@", 1)
            try:
                self._rollout_locked(name, version, keep_old=True)
            except LookupError:
                pass
        elif action.startswith("ready:"):
            name, ready = action.split(":", 1)[1].split("@", 1)
            self._set_ready_locked(name, ready == "true")

    def _set_ready_locked(self, name: str, ready: bool) -> None:
        _, _, _, _, slices = self._deployment_children(name)
        for slice_ in slices:
            for ep in slice_.get("endpoints") or []:
                ep.setdefault("conditions", {})["ready"] = ready
                ep["conditions"]["serving"] = ready
            _bump(slice_["metadata"])
        _persist_locked()

    def _set_terminating_locked(self, name: str, terminating: bool) -> None:
        """只改端点的 terminating 条件（ready 保持不变）。"""
        _, _, _, _, slices = self._deployment_children(name)
        for slice_ in slices:
            for ep in slice_.get("endpoints") or []:
                ep.setdefault("conditions", {})["terminating"] = terminating
            _bump(slice_["metadata"])
        _persist_locked()

    def _handle_scenario(self, path: list, body: dict, qs: dict) -> None:
        """演示用故障注入。

        原有：
        POST /mock/scenarios/recreate?kind=Service&name=preview-feature-a
            同名重建：保留名称与分支标签，uid 换成新值
        POST /mock/scenarios/reown?kind=Deployment&name=preview-feature-a&branch=...
            改属：uid 不变，分支标签改成 other-branch
        POST /mock/scenarios/delay?kind=Service&name=...&seconds=15
            给对象设置删除延迟

        验收判定相关：
        POST /mock/scenarios/rollout?name=preview-feature-a&version=v2&keepOld=1
            滚动发布：新 RS/Pod/端点就绪；keepOld=1 时旧端点仍 ready（新旧并存）
        POST /mock/scenarios/finish-rollout?name=preview-feature-a
            发布收尾：移除旧 RS/Pod 与旧端点
        POST /mock/scenarios/set-observed?name=...&value=0
            把 status.observedGeneration 改小，模拟控制器尚未观察当前 generation
        POST /mock/scenarios/set-ready?name=...&ready=false
            把该 Service 的所有端点 ready 置为指定值
        POST /mock/scenarios/drop-ready?name=...
            删除端点的 conditions.ready（模拟就绪证据缺失）
        POST /mock/scenarios/set-policy?name=...&externalTrafficPolicy=Local
            修改 Service 流量策略 / publishNotReadyAddresses / type
        POST /mock/scenarios/churn?name=...&on=pod&count=1&version=v3
            一次性触发：在第 count 个包含 on 的 GET 请求前做一次滚动发布
        POST /mock/scenarios/set-terminating?name=...&value=true
            只改端点 terminating 条件（ready 不变），用于验证“未终止”过滤
        POST /mock/scenarios/add-slice?name=...
            追加一个没有 targetRef 的 ready 端点，用于验证归属证据缺失
        """
        if not path:
            self._send(400, _status(400, "BadRequest", "missing scenario name"))
            return
        scenario = path[0]

        def q(key: str, default: str = "") -> str:
            return (qs.get(key) or body.get(key) or [default])[0]

        name = q("name")
        if scenario == "churn":
            if not name:
                self._send(400, _status(400, "BadRequest", "name required"))
                return
            version = q("version", "v-churn")
            _CHURN.update({
                "remaining": int(q("count", "1")),
                "match": q("on", "pod"),
                "action": f"rollout:{name}@{version}",
            })
            self._send(200, {"ok": True, "scenario": "churn", "armed": _CHURN})
            return
        if not name:
            self._send(400, _status(400, "BadRequest", "name required"))
            return
        try:
            if scenario == "rollout":
                out = self._rollout_locked(
                    name, q("version", "v2"),
                    keep_old=q("keepOld", "1") not in ("0", "false", "False"),
                    observe=q("observe", "1") not in ("0", "false", "False"))
                self._send(200, {"ok": True, "scenario": "rollout", **out})
                return
            if scenario == "finish-rollout":
                self._send(200, {"ok": True, "scenario": "finish-rollout",
                                 **self._finish_rollout_locked(name)})
                return
            if scenario == "set-observed":
                ds = _find("Deployment", NAMESPACE, name)
                if ds is None:
                    raise LookupError("deployment not found")
                ds["status"]["observedGeneration"] = int(q("value", "0"))
                _bump(ds["metadata"])
                _persist_locked()
                self._send(200, {"ok": True, "scenario": "set-observed",
                                 "observedGeneration": ds["status"]["observedGeneration"],
                                 "generation": ds["metadata"]["generation"]})
                return
            if scenario == "set-ready":
                self._set_ready_locked(name, q("ready", "false") not in ("0", "false", "False"))
                self._send(200, {"ok": True, "scenario": "set-ready"})
                return
            if scenario == "drop-ready":
                _, _, _, _, slices = self._deployment_children(name)
                for slice_ in slices:
                    for ep in slice_.get("endpoints") or []:
                        (ep.get("conditions") or {}).pop("ready", None)
                    _bump(slice_["metadata"])
                _persist_locked()
                self._send(200, {"ok": True, "scenario": "drop-ready"})
                return
            if scenario == "set-terminating":
                self._set_terminating_locked(
                    name, q("value", "true") not in ("0", "false", "False"))
                self._send(200, {"ok": True, "scenario": "set-terminating"})
                return
            if scenario == "add-slice":
                # 追加一个没有 targetRef 的端点（模拟归属证据缺失）
                svc = _find("Service", NAMESPACE, name)
                if svc is None:
                    raise LookupError("service not found")
                branch = svc["metadata"]["labels"].get(BRANCH_LABEL, "")
                slice_ = _endpointslice(branch, svc, [])
                slice_["metadata"]["name"] += "-extra"
                slice_["metadata"]["uid"] += "-extra"
                slice_["endpoints"] = [{
                    "addresses": ["10.9.9.9"],
                    "conditions": {"ready": True, "serving": True, "terminating": False},
                    "port": "http",
                }]
                _STORE["objects"][_key("EndpointSlice", NAMESPACE,
                                       slice_["metadata"]["name"])] = slice_
                _persist_locked()
                self._send(200, {"ok": True, "scenario": "add-slice",
                                 "slice": slice_["metadata"]["name"]})
                return
            if scenario == "set-policy":
                svc = _find("Service", NAMESPACE, name)
                if svc is None:
                    raise LookupError("service not found")
                spec = svc.setdefault("spec", {})
                if q("externalTrafficPolicy"):
                    spec["externalTrafficPolicy"] = q("externalTrafficPolicy")
                if q("internalTrafficPolicy"):
                    spec["internalTrafficPolicy"] = q("internalTrafficPolicy")
                if q("type"):
                    spec["type"] = q("type")
                if q("publishNotReadyAddresses"):
                    spec["publishNotReadyAddresses"] = \
                        q("publishNotReadyAddresses") not in ("0", "false", "False")
                _bump(svc["metadata"])
                _persist_locked()
                self._send(200, {"ok": True, "scenario": "set-policy", "spec": spec})
                return
        except LookupError as exc:
            self._send(404, _status(404, "NotFound", str(exc)))
            return

        kind = q("kind")
        namespace = q("namespace", NAMESPACE)
        if kind not in ("Service", "Deployment") or not name:
            self._send(400, _status(400, "BadRequest",
                                    "kind must be Service/Deployment and name required"))
            return
        obj = _find(kind, namespace, name)
        if obj is None:
            self._send(404, _status(404, "NotFound", f'{kind.lower()}s "{name}" not found'))
            return
        meta = obj["metadata"]
        if scenario == "recreate":
            new_uid = str(uuid.uuid4())
            meta["uid"] = new_uid
            meta.pop("deletionTimestamp", None)
            meta.pop("_deleteStartedAt", None)
            _bump(meta)
            _persist_locked()
            self._send(200, {"ok": True, "scenario": "recreate", "newUid": new_uid})
        elif scenario in ("reown", "relabel"):
            new_branch = q("branch", "other-branch")
            old = meta.get("labels", {}).get(BRANCH_LABEL)
            meta.setdefault("labels", {})[BRANCH_LABEL] = new_branch
            _bump(meta)
            _persist_locked()
            self._send(200, {"ok": True, "scenario": "reown",
                             "label": BRANCH_LABEL, "oldBranch": old,
                             "newBranch": new_branch, "uid": meta["uid"]})
        elif scenario == "delay":
            seconds = q("seconds", "15")
            meta.setdefault("annotations", {})["mock.preview/delete-delay"] = str(seconds)
            _persist_locked()
            self._send(200, {"ok": True, "scenario": "delay", "seconds": seconds})
        else:
            self._send(400, _status(400, "BadRequest", f"unknown scenario {scenario}"))


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    _load()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[mock-k8s] listening on 0.0.0.0:{port}, state={STATE_FILE}, namespace={NAMESPACE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
