"""Kubernetes API 的极简同步客户端，只覆盖本工具需要的接口。

通过 httpx 访问标准 REST 端点。所有调用带超时，
网络异常 / 超时 / 409 统一抛 K8sOperationError，由上层转成"待核查"。
"""
from __future__ import annotations

import os
from typing import Any

import httpx

BRANCH_LABEL = os.environ.get("BRANCH_LABEL", "preview.example.com/branch")


class K8sNotFound(Exception):
    """对象不存在（GET 返回 404）。"""


class K8sConflict(Exception):
    """对象正在终止中（DELETE 返回 409）。"""


class K8sOperationError(Exception):
    """网络、超时或其它非预期失败，需要人工核查。"""


def _base() -> str:
    return os.environ.get("KUBE_API_BASE", "http://mock-k8s:8080").rstrip("/")


def _timeout() -> float:
    return float(os.environ.get("KUBE_TIMEOUT", "5"))


def _svc_path(namespace: str, name: str) -> str:
    return f"/api/v1/namespaces/{namespace}/services/{name}"


def _deploy_path(namespace: str, name: str) -> str:
    return f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}"


def _pod_path(namespace: str, name: str) -> str:
    return f"/api/v1/namespaces/{namespace}/pods/{name}"


def _rs_path(namespace: str, name: str) -> str:
    return f"/apis/apps/v1/namespaces/{namespace}/replicasets/{name}"


def _slice_path(namespace: str) -> str:
    return f"/apis/discovery.k8s.io/v1/namespaces/{namespace}/endpointslices"


def _request(method: str, path: str, **kwargs) -> dict[str, Any]:
    try:
        resp = httpx.request(method, _base() + path, timeout=_timeout(), **kwargs)
    except httpx.TimeoutException as exc:
        raise K8sOperationError(f"请求超时: {method} {path}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise K8sOperationError(f"API 调用失败: {method} {path}: {exc}") from exc
    if resp.status_code == 404:
        raise K8sNotFound(path)
    if resp.status_code == 409:
        raise K8sConflict(resp.text or "object is being deleted")
    if resp.status_code >= 400:
        raise K8sOperationError(f"API 返回 {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def list_services(namespace: str) -> list[dict[str, Any]]:
    return _request("GET", f"/api/v1/namespaces/{namespace}/services").get("items", [])


def list_deployments(namespace: str) -> list[dict[str, Any]]:
    return _request(
        "GET", f"/apis/apps/v1/namespaces/{namespace}/deployments"
    ).get("items", [])


def get_service(namespace: str, name: str) -> dict[str, Any]:
    return _request("GET", _svc_path(namespace, name))


def get_deployment(namespace: str, name: str) -> dict[str, Any]:
    return _request("GET", _deploy_path(namespace, name))


def get_pod(namespace: str, name: str) -> dict[str, Any]:
    return _request("GET", _pod_path(namespace, name))


def get_replicaset(namespace: str, name: str) -> dict[str, Any]:
    return _request("GET", _rs_path(namespace, name))


def list_endpointslices(namespace: str, label_selector: str | None = None) -> list[dict[str, Any]]:
    params = {"labelSelector": label_selector} if label_selector else None
    return _request("GET", _slice_path(namespace), params=params).get("items", [])


def get_endpointslice(namespace: str, name: str) -> dict[str, Any]:
    return _request("GET", f"{_slice_path(namespace)}/{name}")


def delete_service(namespace: str, name: str) -> dict[str, Any]:
    return _request("DELETE", _svc_path(namespace, name))


def delete_deployment(namespace: str, name: str) -> dict[str, Any]:
    return _request("DELETE", _deploy_path(namespace, name))


def branch_of(obj: dict[str, Any]) -> str | None:
    return (obj.get("metadata", {}).get("labels", {}) or {}).get(BRANCH_LABEL)


def uid_of(obj: dict[str, Any]) -> str | None:
    return obj.get("metadata", {}).get("uid")


def is_terminating(obj: dict[str, Any]) -> bool:
    return bool(obj.get("metadata", {}).get("deletionTimestamp"))
