"""清理任务的持久化存储。

清单文件采用"写临时文件再原子替换"，重启后可沿原清单复查并继续清理。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

STORE_PATH = os.environ.get("JOB_STORE", "/data/jobs.json")
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty() -> dict[str, Any]:
    return {"jobs": {}}


def _load_locked() -> dict[str, Any]:
    if not os.path.exists(STORE_PATH):
        return _empty()
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("jobs"), dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return _empty()


def _persist_locked(data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(STORE_PATH) or ".", exist_ok=True)
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)


def list_jobs() -> list[dict[str, Any]]:
    with _LOCK:
        data = _load_locked()
        return sorted(data["jobs"].values(), key=lambda j: j.get("createdAt", ""), reverse=True)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        return _load_locked()["jobs"].get(job_id)


def create_job(namespace: str, branch: str, service: dict, deployment: dict) -> dict[str, Any]:
    """确认清理时落盘：记录当时的名称、UID、分支标签作为后续核对基线。"""
    from . import k8s_client

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "namespace": namespace,
        "branch": branch,
        "phase": "confirmed",
        "createdAt": _now(),
        "updatedAt": _now(),
        "events": [{"at": _now(), "level": "info",
                    "message": "已确认清理，保存 Service/Deployment 清单快照"}],
        "service": {
            "kind": "Service",
            "name": service["metadata"]["name"],
            "uid": k8s_client.uid_of(service),
            "branch": k8s_client.branch_of(service),
        },
        "deployment": {
            "kind": "Deployment",
            "name": deployment["metadata"]["name"],
            "uid": k8s_client.uid_of(deployment),
            "branch": k8s_client.branch_of(deployment),
        },
        "conflict": None,
        "pendingReason": None,
    }
    with _LOCK:
        data = _load_locked()
        data["jobs"][job_id] = job
        _persist_locked(data)
    return job


def update_job(job_id: str, mutator) -> dict[str, Any] | None:
    """在锁内读取-修改-写回，保证并发点击不会写丢状态。"""
    with _LOCK:
        data = _load_locked()
        job = data["jobs"].get(job_id)
        if job is None:
            return None
        mutator(job)
        job["updatedAt"] = _now()
        _persist_locked(data)
        return job


def add_event(job: dict, level: str, message: str) -> None:
    job.setdefault("events", []).append({"at": _now(), "level": level, "message": message})
