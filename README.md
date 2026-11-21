# 预览环境清理控制台（Preview Environment Cleaner）

分支合并后，发布工程师用它收掉 Kubernetes 上的预览环境。网页按分支列出同一命名空间内
成对的 Service 与 Deployment，确认后按严格核对规则清理，并提供延迟删除、改属、同名重建
的完整演示。另有一个分支页，在滚动发布后判定“原来的 Service 能否用于验收新版”。

## 做成了什么

### 验收就绪判定（分支页 `/acceptance`）

只检查同一命名空间内 **type=ClusterIP、使用默认流量策略
（external/internalTrafficPolicy 未设置或为 Cluster）、未开启
publishNotReadyAddresses** 的 Service。全部成立才显示“可验收”：

1. **控制器已观察当前 generation**：`status.observedGeneration >= metadata.generation`。
2. **至少一个就绪且未终止的端点**：汇总该 Service 的 EndpointSlice
   （按 `kubernetes.io/service-name` 关联），要求存在 `ready=true` 且
   `terminating` 未设置的端点。
3. **每个就绪端点都能追到当前 Pod 模板**：按 `targetRef.uid` → Pod →
   `ownerReference(ReplicaSet, UID 核对)` → `ownerReference(Deployment, UID 核对)`，
   并要求 ReplicaSet 的 `spec.template` 与 Deployment 当前 `spec.template` 一致。
   用模板比较而非复算 `pod-template-hash`（真实集群的哈希由控制器生成，客户端无法稳定复现）。
4. **同一集群状态**：判定前后各重读 Deployment / Service / EndpointSlice，
   以及归属取证用到的每个 Pod / ReplicaSet 的 `resourceVersion`，任一变化即判“待核实”。

四种显示状态：

| 状态 | 含义 |
| --- | --- |
| 可验收 | 上述条件全部成立；所有就绪端点都在当前 Pod 模板上 |
| 不可验收 | 证据完整但结论为否：没有就绪端点，或仍有旧版/其他分支的就绪端点 |
| 待核实 | 读取失败、判定期间状态变化、端点 `ready` 非布尔值、targetRef 缺失/UID 不符、Pod 或 ReplicaSet 读不到、owner 链断裂 |
| 不适用 | Service 不是默认流量策略的 ClusterIP，不参与判定 |

页面同时列出**仍就绪且未终止的旧版 / 其他分支端点**及其 Pod、ReplicaSet 与归属证据。

### 核对与清理规则（核心）

1. **按分支列对象**：读取同一命名空间的 Service / Deployment，按分支标签
   `preview.example.com/branch` 配对展示（未成对的分支不可确认）。
2. **确认即固化清单**：点击“确认清理”时重新从 API 拉取两个对象，把当时的
   **名称、UID、分支标签**作为基线落盘（`jobs.json`，原子写）。
3. **删除受理前身份核对**：每一步删除前重新 GET 对象，与清单基线比对：
   - UID 不一致 → 判定**同名重建**，停止清理，页面指出对象、期望值与实际值；
   - 分支标签不一致（UID 不变）→ 判定**改属**，同样停止并指出变更项；
   - 任一不符都不会对该对象发起删除。
4. **先 Service 后 Deployment**：只有原 Service 的 GET 返回 404（确认已消失）后，
   才允许删除 Deployment；reconcile 每一轮都会重新确认这一前提。
5. **待核查（pending_review）**：删除调用超时/网络错误、对象仍带
   `deletionTimestamp` 终止中时，任务进入待核查并写明原因，绝不越步。
6. **重启续跑**：任务清单与模拟端状态都持久化到磁盘；进程或容器重启后，
   不重新确认，沿原清单继续复查和清理。冲突态需人工知情后在页面“刷新清单基线”才能继续。

### 组件

| 路径 | 说明 |
| --- | --- |
| `app/main.py` | FastAPI 路由与页面编排 |
| `app/cleanup.py` | 清理状态机、UID/标签核对、冲突与待核查判定 |
| `app/acceptance.py` | 验收就绪判定、端点归属取证、一致性快照 |
| `app/k8s_client.py` | httpx 封装的 K8s REST 客户端（404/409/超时分类） |
| `app/store.py` | 任务清单持久化（临时文件 + 原子替换） |
| `app/templates/index.html` | 单页清理控制台（原生 HTML/JS，无前端构建） |
| `app/templates/acceptance.html` | 分支验收就绪页 |
| `mock-k8s/server.py` | 本地 Kubernetes API 模拟端（标准库实现，磁盘持久化） |
| `tests/e2e.py` | 28 项清理端到端断言（5 类场景） |
| `tests/acceptance_e2e.py` | 32 项验收判定端到端断言（10 类场景） |
| `tests/restart-check.sh` | 真正杀进程重启后的续跑验证 |
| `scripts/run-local.sh` | 无 Docker 时的本地启动脚本 |

## 用 Docker Compose 启动（正式入口）

```bash
docker compose up --build
# 网页:      http://localhost:8000      （清理控制台）
#            http://localhost:8000/acceptance （验收就绪检查）
# 模拟端:    http://localhost:18080 （/mock/state 可看全部对象）
```

Compose 已把 `feature-a` 分支的删除延迟设为 15 秒（`DELAY_FEATURE_A=15`），
用于演示“入口延迟消失”；两个容器共享命名卷 `mock-data` 持久化状态。

模拟端故障注入接口：

```bash
# 同名重建：立即重建对象，UID 换成新值
curl -X POST "http://localhost:18080/mock/scenarios/recreate?kind=Service&name=preview-feature-b"
# 改属：UID 不变，分支标签改为 stolen-branch
curl -X POST "http://localhost:18080/mock/scenarios/reown?kind=Service&name=preview-feature-b&branch=stolen-branch"
# 调整删除延迟
curl -X POST "http://localhost:18080/mock/scenarios/delay?kind=Service&name=preview-hotfix-1&seconds=30"
# 恢复初始种子
curl -X POST http://localhost:18080/mock/reset

# —— 验收判定相关 ——
# 滚动发布：新 RS/Pod/端点就绪；keepOld=1 时旧端点仍 ready（新旧并存）
curl -X POST "http://localhost:18080/mock/scenarios/rollout?name=preview-feature-a&version=v2&keepOld=1"
# 发布收尾：移除旧 RS/Pod 与旧端点
curl -X POST "http://localhost:18080/mock/scenarios/finish-rollout?name=preview-feature-a"
# 控制器尚未观察当前 generation
curl -X POST "http://localhost:18080/mock/scenarios/set-observed?name=preview-feature-b&value=0"
# 端点就绪状态 / 缺失就绪证据
curl -X POST "http://localhost:18080/mock/scenarios/set-ready?name=preview-hotfix-1&ready=false"
curl -X POST "http://localhost:18080/mock/scenarios/drop-ready?name=preview-hotfix-1"
# 修改流量策略（判定前置条件）
curl -X POST "http://localhost:18080/mock/scenarios/set-policy?name=preview-feature-b&externalTrafficPolicy=Local"
curl -X POST "http://localhost:18080/mock/scenarios/set-policy?name=preview-feature-b&publishNotReadyAddresses=true"
# 检查途中换版：在第 count 个包含 on 的 GET 前触发一次滚动发布
curl -X POST "http://localhost:18080/mock/scenarios/churn?name=preview-feature-a&on=pods&count=1&version=v3"
```

验收检查既可在网页 `/acceptance` 点“运行验收检查”，也可直接读 JSON：

```bash
curl -s "http://localhost:8000/api/acceptance?branch=feature-a"
```

## 无 Docker 的本地运行

```bash
pip install -r requirements.txt
./scripts/run-local.sh          # 同时起模拟端(18080)与网页(8000)
# 另开终端：
python tests/e2e.py             # 清理流程端到端断言
python tests/acceptance_e2e.py  # 验收判定端到端断言
./tests/restart-check.sh        # 进程重启续跑验证（使用独立端口 18090/8090）
```

## 试跑场景说明

1. **正常清理（hotfix-1）**：一次执行内按 Service→确认404→Deployment→确认404 完成，
   事件流里保留完整顺序；重复执行幂等。
2. **入口延迟消失（feature-a，15s）**：DELETE 受理后对象带 deletionTimestamp 仍可 GET，
   任务显示待核查且不触碰 Deployment；延迟到期后续跑，Deployment 同样延迟时再次待核查。
3. **确认后改属（feature-b）**：改分支标签后 reconcile 立即停在冲突，
   明确指出“分支标签由 feature-b 变为 stolen-branch，UID 未变”，且未发起任何删除。
4. **同名重建**：Service 与 Deployment 各覆盖一种；UID 变化即停止，新对象不受影响。
5. **重启续跑**：Service 终止中时杀掉两个进程再启动，任务从 jobs.json 恢复，
   模拟端保留 deletionTimestamp，延迟到期后沿原清单完成。
6. **验收判定（分支页）**：发布前“可验收”；`rollout&keepOld=1` 后新旧端点并存，
   页面“不可验收”并列出旧端点；`finish-rollout` 后恢复“可验收”；
   `set-observed=0`、端点缺 ready、端点缺 targetRef 落入“待核实”，
   端点 ready=false/terminating=true、非默认流量策略分别落入“不可验收/不适用”；
   `churn` 触发检查途中换版，判定显示“待核实”而非错误结论。
   模拟端重启后（同一状态文件）新旧端点的归属判定保持一致。

## 遗留事项与边界

- 模拟端只实现了本工具用到的 Service/Deployment/ReplicaSet/Pod/EndpointSlice
  REST 子集，不含 watch、admission webhook、RBAC；接真实集群时把 `KUBE_API_BASE`
  指向 apiserver 并补上认证（ServiceAccount token / kubeconfig）即可，客户端接口不变。
- 验收判定按时点快照读取（重读 resourceVersion 比对）。真实集群上，控制器与
  EndpointSlice 控制器可能持续写入，繁忙分支容易出现“判定期间状态变化 → 待核实”，
  需要重试；这是有意为之：宁可让人再看一眼，也不在变动中的状态上给结论。
  每次判定是一个瞬时快照，不保证之后仍是该状态；页面显示判定时间，重试即重新取值。
- 端点归属按 `spec.template` 深度比较判定“当前模板”。若 Deployment 使用
  非标准注入（例如 admission webhook 改写 Pod），模板比较可能判为旧版；
  此时应按证据人工确认，工具不会自动放宽。
- Service 与 Deployment 的配对仍依赖分支标签：同分支存在多个 Service/Deployment、
  或 Service 没有分支标签时显示“待核实”，不猜测对象。
- 判定结果只缓存在内存里供页面展示，进程重启后需重新点“运行验收检查”；
  这不会改变集群状态，也不落盘。
- 真实集群的删除延迟来自 finalizer 与控制器回收，模拟端用注解
  `mock.preview/delete-delay` 仿真。
- 网页没有登录鉴权，定位为内网运维工具；如需上线应放在 SSO/网关之后。
- 冲突后的“刷新清单基线”是显式人工动作，工具不会自动接受新身份。
- 清单与状态文件没有并发多实例保护（锁是单进程内的）；多副本部署需换 SQLite/Redis。
