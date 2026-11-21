"""本地 Kubernetes API 模拟端（仅用于演示，非生产代码）。

实现的语义与真实 Kubernetes 对齐的部分：
- GET 单个对象不存在时返回 404 Status JSON
- DELETE 不会立刻让对象消失，而是设置 metadata.deletionTimestamp，
  对象在终止期内仍可 GET；延迟到期后才真正移除
- 对已在终止中的对象再次 DELETE 返回 409 Conflict
- 同名重建会得到新的 metadata.uid
- 改属通过修改分支标签体现，uid 保持不变

状态持久化到 /data/mock-state.json（可用 STATE_FILE 覆盖），
模拟端重启后终止中的对象仍保留 deletionTimestamp，贴近 etcd 持久化语义。
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STATE_FILE = os.environ.get("STATE_FILE", "/data/mock-state.json")
NAMESPACE = os.environ.get("NAMESPACE", "previews")
BRANCH_LABEL = os.environ.get("BRANCH_LABEL", "preview.example.com/branch")
DEFAULT_DELETE_DELAY = float(os.environ.get("DEFAULT_DELETE_DELAY", "0"))

_LOCK = threading.RLock()
_STORE: dict = {"objects": {}}  # key: (kind, namespace, name) -> object


def _key(kind: str, namespace: str, name: str) -> tuple:
    return kind, namespace, name


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed() -> None:
    """构造初始预览环境：三个分支，每个分支一对 Service + Deployment。"""
    branches = ["feature-a", "feature-b", "hotfix-1"]
    with _LOCK:
        for branch in branches:
            name = f"preview-{branch}"
            delay = os.environ.get(f"DELAY_{branch.replace('-', '_').upper()}", "")
            annotations = {}
            if delay:
                annotations["mock.preview/delete-delay"] = delay
            _STORE["objects"][str(_key("Service", NAMESPACE, name))] = {
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
                "spec": {"ports": [{"port": 80, "targetPort": 8080}]},
            }
            _STORE["objects"][str(_key("Deployment", NAMESPACE, name))] = {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": name,
                    "namespace": NAMESPACE,
                    "uid": f"uid-deploy-{branch}-0001",
                    "labels": {BRANCH_LABEL: branch, "app": name},
                    "annotations": dict(annotations),
                    "resourceVersion": "1",
                },
                "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}},
            }
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


def _sweep_locked() -> None:
    """删除到期的终止对象。"""
    now = time.time()
    gone = []
    for key, obj in _STORE["objects"].items():
        meta = obj["metadata"]
        if meta.get("deletionTimestamp"):
            if now - float(meta["_deleteStartedAt"]) >= _delete_delay(obj):
                gone.append(key)
    for key in gone:
        _STORE["objects"].pop(key, None)
    if gone:
        _persist_locked()


def _find(kind: str, namespace: str, name: str):
    _sweep_locked()
    return _STORE["objects"].get(str(_key(kind, namespace, name)))


def _list(kind: str, namespace: str) -> list:
    _sweep_locked()
    return [
        obj
        for obj in _STORE["objects"].values()
        if obj["kind"] == kind and obj["metadata"]["namespace"] == namespace
    ]


def _status(code: int, reason: str, message: str) -> dict:
    return {
        "kind": "Status",
        "apiVersion": "v1",
        "status": "Failure",
        "code": code,
        "reason": reason,
        "message": message,
    }


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
        self.wfile.write(body)

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
            if len(parts) == 6 and parts[0] == "api" and parts[1] == "v1" \
                    and parts[2] == "namespaces" and parts[4] == "services":
                self._handle_list("Service", parts[3], parts[5])
                return
            if len(parts) == 7 and parts[:2] == ["apis", "apps"] and parts[2] == "v1" \
                    and parts[3] == "namespaces" and parts[5] == "deployments":
                self._handle_list("Deployment", parts[4], parts[6])
                return
            if len(parts) == 5 and parts[0] == "api" and parts[1] == "v1" \
                    and parts[2] == "namespaces" and parts[4] == "services":
                self._send(200, {"kind": "ServiceList", "items": _list("Service", parts[3])})
                return
            if len(parts) == 6 and parts[:2] == ["apis", "apps"] and parts[2] == "v1" \
                    and parts[3] == "namespaces" and parts[5] == "deployments":
                self._send(200, {"kind": "DeploymentList", "items": _list("Deployment", parts[4])})
                return
            self._send(404, _status(404, "NotFound", f"no handler for {parsed.path}"))

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        with _LOCK:
            kind = namespace = name = None
            if len(parts) == 6 and parts[0] == "api" and parts[1] == "v1" \
                    and parts[2] == "namespaces" and parts[4] == "services":
                kind, namespace, name = "Service", parts[3], parts[5]
            elif len(parts) == 7 and parts[:2] == ["apis", "apps"] and parts[2] == "v1" \
                    and parts[3] == "namespaces" and parts[5] == "deployments":
                kind, namespace, name = "Deployment", parts[4], parts[6]
            if not kind:
                self._send(404, _status(404, "NotFound", parsed.path))
                return
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
            meta["resourceVersion"] = str(int(meta.get("resourceVersion", "1")) + 1)
            _persist_locked()
            self._send(200, obj)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        body = self._read_json()
        with _LOCK:
            if parts == ["mock", "reset"]:
                _STORE["objects"].clear()
                if os.path.exists(STATE_FILE):
                    os.remove(STATE_FILE)
                _seed()
                self._send(200, {"ok": True, "message": "reseeded"})
                return
            if parts[:2] == ["mock", "scenarios"]:
                self._handle_scenario(parts[2:] if len(parts) > 2 else [], body, parse_qs(parsed.query))
                return
            self._send(404, _status(404, "NotFound", parsed.path))

    # ---- 业务处理 ----
    def _handle_list(self, kind: str, namespace: str, name: str) -> None:
        obj = _find(kind, namespace, name)
        if obj is None:
            self._send(404, _status(404, "NotFound",
                                    f'{kind.lower()}s "{name}" not found'))
            return
        self._send(200, obj)

    def _handle_scenario(self, path: list, body: dict, qs: dict) -> None:
        """演示用故障注入。

        POST /mock/scenarios/recreate?kind=Service&name=preview-feature-a
            同名重建：保留名称与分支标签，uid 换成新值（终止中也会立即重建）
        POST /mock/scenarios/reown?kind=Deployment&name=preview-feature-a
            改属：uid 不变，分支标签改成 other-branch
        POST /mock/scenarios/delay?kind=Service&name=...&seconds=15
            给对象设置删除延迟
        POST /mock/scenarios/relabel?kind=...&name=...&branch=...
            通用改标签（reown 的可指定版本）
        """
        if not path:
            self._send(400, _status(400, "BadRequest", "missing scenario name"))
            return
        scenario = path[0]
        kind = (qs.get("kind") or body.get("kind") or [""])[0]
        name = (qs.get("name") or body.get("name") or [""])[0]
        namespace = (qs.get("namespace") or body.get("namespace") or [NAMESPACE])[0]
        if kind not in ("Service", "Deployment") or not name:
            self._send(400, _status(400, "BadRequest", "kind must be Service/Deployment and name required"))
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
            meta["resourceVersion"] = str(int(meta.get("resourceVersion", "1")) + 1)
            _persist_locked()
            self._send(200, {"ok": True, "scenario": "recreate", "newUid": new_uid})
        elif scenario in ("reown", "relabel"):
            new_branch = (qs.get("branch") or body.get("branch") or ["other-branch"])[0]
            old = meta.get("labels", {}).get(BRANCH_LABEL)
            meta.setdefault("labels", {})[BRANCH_LABEL] = new_branch
            meta["resourceVersion"] = str(int(meta.get("resourceVersion", "1")) + 1)
            _persist_locked()
            self._send(200, {"ok": True, "scenario": "reown",
                             "label": BRANCH_LABEL, "oldBranch": old, "newBranch": new_branch,
                             "uid": meta["uid"]})
        elif scenario == "delay":
            seconds = (qs.get("seconds") or body.get("seconds") or ["15"])[0]
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
