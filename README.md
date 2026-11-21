# 预览环境清理控制台（Preview Environment Cleaner）

分支合并后，发布工程师用它收掉 Kubernetes 上的预览环境。网页按分支列出同一命名空间内
成对的 Service 与 Deployment，确认后按严格核对规则清理，并提供延迟删除、改属、同名重建
的完整演示。

## 做成了什么

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
| `app/k8s_client.py` | httpx 封装的 K8s REST 客户端（404/409/超时分类） |
| `app/store.py` | 任务清单持久化（临时文件 + 原子替换） |
| `app/templates/index.html` | 单页控制台（原生 HTML/JS，无前端构建） |
| `mock-k8s/server.py` | 本地 Kubernetes API 模拟端（标准库实现，磁盘持久化） |
| `tests/e2e.py` | 28 项端到端断言（5 类场景） |
| `tests/restart-check.sh` | 真正杀进程重启后的续跑验证 |
| `scripts/run-local.sh` | 无 Docker 时的本地启动脚本 |

## 用 Docker Compose 启动（正式入口）

```bash
docker compose up --build
# 网页:      http://localhost:8000
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
```

## 无 Docker 的本地运行

```bash
pip install -r requirements.txt
./scripts/run-local.sh          # 同时起模拟端(18080)与网页(8000)
# 另开终端：
python tests/e2e.py             # 端到端断言
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

## 遗留事项与边界

- 模拟端只实现了本工具用到的 Service/Deployment REST 子集，不含 watch、
  admission webhook、RBAC；接真实集群时把 `KUBE_API_BASE` 指向 apiserver
  并补上认证（ServiceAccount token / kubeconfig）即可，客户端接口不变。
- 真实集群的删除延迟来自 finalizer 与控制器回收，模拟端用注解
  `mock.preview/delete-delay` 仿真。
- 网页没有登录鉴权，定位为内网运维工具；如需上线应放在 SSO/网关之后。
- 冲突后的“刷新清单基线”是显式人工动作，工具不会自动接受新身份。
- 清单与状态文件没有并发多实例保护（锁是单进程内的）；多副本部署需换 SQLite/Redis。
