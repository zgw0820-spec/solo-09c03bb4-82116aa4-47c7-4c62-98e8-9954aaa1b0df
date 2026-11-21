#!/usr/bin/env python3
"""验收就绪检查的端到端试跑（通过 HTTP 驱动 mock-k8s 与 FastAPI 控制台）。

覆盖：
  1. 基线：滚动发布前的 Service 可验收（有就绪端点、追到当前模板）
  2. 新旧端点并存：发布后旧端点仍就绪 → 不可验收，并列出旧端点
  3. 发布收尾：旧端点移除后 → 可验收
  4. 控制器尚未观察当前 generation → 待核实
  5. 端点 ready=false → 待核实（无可验收证据）
  6. 端点缺少 conditions.ready → 待核实（就绪状态无证据）
  7. 非默认流量策略 / publishNotReadyAddresses / 非 ClusterIP → 不适用
  8. 检查途中换版（判定第一遍读完后集群变化）→ 待核实，不给结论
  9. 端点 terminating / 归属证据缺失
  10. 判定结果带一致性快照（readEpoch）

用法: python tests/acceptance_e2e.py   （先启动两个服务）
"""
from __future__ import annotations

import json
import sys
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


def scenario(action: str, **params) -> dict:
    q = urllib.parse.urlencode(params)
    resp = httpx.post(f"{MOCK}/mock/scenarios/{action}?{q}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def accept(branch: str) -> dict:
    resp = httpx.get(f"{WEB}/api/acceptance", params={"branch": branch}, timeout=15)
    resp.raise_for_status()
    return resp.json()["branches"][0]


def main() -> int:
    reset()

    print("\n=== 场景1：发布前基线可验收 ===")
    b = accept("feature-a")
    check("基线判定为可验收", b["verdict"] == "acceptable", b["detail"])
    check("每个就绪端点都追到当前模板",
          b["endpoints"] and all(e["kind"] == "current" for e in b["endpoints"]),
          json.dumps([e["kind"] for e in b["endpoints"]]))
    check("带一致性快照且复核未变化",
          b["readEpoch"].get("consistent") is True, json.dumps(b["readEpoch"]))
    check("一致性快照包含端点归属用到的 Pod / ReplicaSet",
          b["readEpoch"].get("pods") and b["readEpoch"].get("replicaSets"),
          json.dumps(b["readEpoch"]))
    check("检查结果字段包含 generation 与端点检查项",
          any("generation" in c["name"] and c["ok"] is True for c in b["checks"]))

    print("\n=== 场景2：滚动发布后新旧端点并存 ===")
    out = scenario("rollout", name="preview-feature-a", version="v2", keepOld="1")
    check("发布产生新 generation 并已观察", out["generation"] == 2
          and out["observedGeneration"] == 2, json.dumps(out))
    b = accept("feature-a")
    check("发布后为不可验收", b["verdict"] == "blocked", b["detail"])
    kinds = sorted(e["kind"] for e in b["endpoints"])
    check("端点中同时有新（current）与旧（stale）",
          kinds == ["current", "stale"], json.dumps(kinds))
    check("列出仍就绪且未终止的旧版端点",
          len(b["staleEndpoints"]) == 1
          and b["staleEndpoints"][0]["ready"] is True
          and b["staleEndpoints"][0]["terminating"] is False,
          json.dumps([e["address"] for e in b["staleEndpoints"]]))
    old = next(e for e in b["endpoints"] if e["kind"] == "stale")
    check("旧版端点证据含 Pod → ReplicaSet 且指出模板不是当前 spec",
          old["pod_name"] and old["replicaset"] and "旧版" in old["evidence"],
          old["evidence"])

    print("\n=== 场景3：发布收尾后恢复可验收 ===")
    fin = scenario("finish-rollout", name="preview-feature-a")
    check("收尾移除了旧 Pod", fin["removedOldPods"] == 1, json.dumps(fin))
    b = accept("feature-a")
    check("收尾后判定为可验收", b["verdict"] == "acceptable", b["detail"])
    check("不再有旧版端点", not b["staleEndpoints"])

    print("\n=== 场景4：控制器尚未观察当前 generation ===")
    reset()
    scenario("set-observed", name="preview-feature-b", value="0")
    b = accept("feature-b")
    check("判定为待核实", b["verdict"] == "pending", b["detail"])
    check("原因指出 observedGeneration < generation",
          "observedGeneration" in b["detail"], b["detail"])

    print("\n=== 场景5：端点未就绪 ===")
    reset()
    scenario("set-ready", name="preview-hotfix-1", ready="false")
    b = accept("hotfix-1")
    check("端点 ready=false 是明确结论：不可验收", b["verdict"] == "blocked", b["detail"])
    check("原因指出没有就绪且未终止的端点",
          "没有 ready=true 且未终止的端点" in b["detail"], b["detail"])
    check("保留端点记录但 ready 为 false",
          b["endpoints"] and all(e["ready"] is False for e in b["endpoints"]),
          json.dumps([e["ready"] for e in b["endpoints"]]))

    print("\n=== 场景6：端点缺少 conditions.ready ===")
    reset()
    scenario("drop-ready", name="preview-hotfix-1")
    b = accept("hotfix-1")
    check("判定为待核实", b["verdict"] == "pending", b["detail"])
    check("原因指出就绪状态缺失", "ready" in b["detail"], b["detail"])
    check("端点就绪值为未知（非猜测）",
          all(e["ready"] is None for e in b["endpoints"]))

    print("\n=== 场景7：端点终止中 / 归属证据缺失 ===")
    reset()
    scenario("set-terminating", name="preview-feature-a", value="true")
    b = accept("feature-a")
    check("ready 但 terminating=true 的端点不计入可用后端 → 不可验收",
          b["verdict"] == "blocked"
          and all(e["terminating"] for e in b["endpoints"]),
          b["detail"])
    reset()
    scenario("add-slice", name="preview-feature-a")
    b = accept("feature-a")
    check("ready 端点 targetRef 缺失 → 待核实（归属证据不足）",
          b["verdict"] == "pending" and "归属证据不足" in b["detail"],
          b["detail"])
    check("缺 targetRef 的端点被记录为未确认，而不是猜成当前模板",
          any(e["kind"] == "unknown" and e["target_name"] is None
              for e in b["endpoints"]),
          json.dumps([(e["kind"], e["target_name"]) for e in b["endpoints"]]))

    print("\n=== 场景8：不满足前置条件的 Service 不参与判定 ===")
    reset()
    scenario("set-policy", name="preview-feature-b", externalTrafficPolicy="Local")
    b = accept("feature-b")
    check("externalTrafficPolicy=Local → 不适用",
          b["verdict"] == "unsupported", b["detail"])
    reset()
    scenario("set-policy", name="preview-feature-b", publishNotReadyAddresses="true")
    b = accept("feature-b")
    check("publishNotReadyAddresses=true → 不适用",
          b["verdict"] == "unsupported", b["detail"])
    reset()
    scenario("set-policy", name="preview-feature-b", type="NodePort")
    b = accept("feature-b")
    check("type=NodePort → 不适用", b["verdict"] == "unsupported", b["detail"])

    print("\n=== 场景9：检查途中换版 ===")
    reset()
    scenario("churn", name="preview-feature-a", on="pods", count="1", version="v3")
    b = accept("feature-a")
    check("判定期间状态变化 → 待核实", b["verdict"] == "pending", b["detail"])
    check("原因指出 resourceVersion 变化或 EndpointSlice 变化",
          "resourceVersion" in b["detail"] or "EndpointSlice" in b["detail"],
          b["detail"])
    check("一致性快照标记为不一致",
          b["readEpoch"].get("consistent") is False, json.dumps(b["readEpoch"]))

    print("\n=== 场景10：再次检查（状态已稳定）看到新旧并存 ===")
    b = accept("feature-a")
    check("稳定后能给出明确结论（不可验收：仍有旧版）",
          b["verdict"] == "blocked", b["detail"])
    check("旧版端点仍被列出", len(b["staleEndpoints"]) == 1,
          json.dumps([e["address"] for e in b["staleEndpoints"]]))

    print("\n========== 汇总 ==========")
    failed = [n for n, ok, _ in results if not ok]
    for name, ok, detail in results:
        if not ok:
            print(f"[{FAIL}] {name} — {detail}")
    print(f"\n共 {len(results)} 项，失败 {len(failed)} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
