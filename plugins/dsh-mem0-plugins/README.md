# dsh-mem0-plugins — Mem0 持久记忆插件（DSH 自托管版）

把 hermes 版 mem0 记忆插件的「全自动记忆」移植到 DeepSeek Harness (dsh)，做成标准
bundle 插件：`dsh plugin add` 安装、`dsh plugin remove` 卸载，**不改任何 dsh 源码**。
只支持**自托管 Mem0 server**（HTTP + `X-API-Key`），不做 cloud/OSS 模式。

## 它会自动做什么

| 能力 | 触发时机 | 说明 |
|------|----------|------|
| **自动召回** | 用户消息进入会话 | 后台立即发起语义搜索；系统提示装配时等待预取结果（默认上限 8s），命中则注入「Mem0 Memory」事实块 |
| **使用引导** | 常驻 | 系统提示中注册使用说明节，引导模型对用户相关的问题主动调 `mem0_search`（多角度多跳） |
| **自动写入** | 每轮对话结束 | 把「用户消息 + 助手回复」交给服务端 LLM 抽取事实（`infer: true`）；纯 JSON 的工具输出会被替换成占位符防污染 |
| **潮浪并忆** | 写入时 | 同一会话的短对话按 user 分桶合并：空闲 5s / 窗口 15s / 5 轮 / 4000 字符任一达标即合并为一次批量写入，摊薄抽取调用；超长消息(>2000 字符)走快速直写 |
| **反馈闭环** | update/delete 后 | best-effort 上报 `/evolve/feedback`（correction/useless），参与服务端 salience 进化 |

## 四个模型工具

| 工具 | 用途 |
|------|------|
| `mem0_search` | 语义搜索用户记忆（支持 top_k / rerank 覆盖） |
| `mem0_add` | 逐字存储持久事实（不走 LLM 抽取） |
| `mem0_update` | 按 ID 改错（上报 correction 反馈） |
| `mem0_delete` | 按 ID 遗忘（上报 useless 反馈） |

## 安装 / 卸载

```bash
# 安装（web profile；安装后重启 dsh 生效）
dsh plugin --profile web add /data/code/mem0_falkordb/plugins/dsh-mem0-plugins

# 卸载
dsh plugin --profile web remove dsh-mem0-plugins
```

装好后到 **设置 → 插件配置 → Mem0 记忆** 打开开关即可；设置页改动即时生效，
无需重启。

## 设置项

默认值（composition base）在 `cordis.patch.yml` 中声明：`enabled=false` 安全安装、
`host=http://127.0.0.1:8888`。设置页保存的值落在用户层，优先级更高。

### 连接与身份

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `false` | 总开关；关闭后工具调用会返回启用指引 |
| `host` | `http://127.0.0.1:8888` | 自托管 server URL |
| `apiKey` | 空 | 以 `X-API-Key` 头发送；`AUTH_DISABLED` 部署留空 |
| `userId` | `dsh-user` | 记忆归属 user_id，跨会话共享同一份记忆 |
| `agentId` | `dsh` | 写入附带的 agent_id |

### 自动召回

| 字段 | 默认 | 说明 |
|------|------|------|
| `recallEnabled` | `true` | claimed 预取 + 提示词注入总开关 |
| `recallWaitMs` | `8000` | 装配点等待预取结果的上限，超时跳过本次注入 |
| `topK` | `10` | 每次召回最大条数（1–50） |
| `rerank` | `false` | 开启则以全深度模式请求重排（服务端需配置 reranker） |

### 自动写入

| 字段 | 默认 | 说明 |
|------|------|------|
| `syncEnabled` | `true` | 每轮结束写入总开关 |
| `coalesceEnabled` | `true` | 潮浪并忆合并写入；关闭则逐条直写 |
| `coalesceIdleMs` | `5000` | 桶内空闲冲刷阈值 |
| `coalesceWindowMs` | `15000` | 桶窗口冲刷阈值 |
| `coalesceMaxTurns` | `5` | 桶内轮数上限 |
| `coalesceMaxChars` | `4000` | 桶内字符上限 |
| `fastpathChars` | `2000` | 单轮超过该长度直接落库 |

### 可靠性与超时

| 字段 | 默认 | 说明 |
|------|------|------|
| `queueMaxLen` | `50` | 待写队列上限，满时丢最旧 |
| `breakerThreshold` | `5` | 连续失败达该次数熔断 |
| `breakerCooldownMs` | `120000` | 熔断冷却时长 |
| `requestTimeoutMs` | `60000` | 单次 HTTP 请求超时 |

要改 profile 层默认值，在 `~/.dsh/profiles/web/cordis.patch.yml` 追加：

```yaml
- id: mem0
  config:
    enabled: true
    host: http://10.200.0.5:8888
    apiKey: your-admin-api-key
```

## 可靠性设计

- **熔断器**：连续失败 ≥5 次（可配）暂停所有 mem0 调用，冷却 120s 后自动恢复；
  404/not found 类客户端错误不计入熔断。
- **连接级重试**：连接拒绝/DNS 类失败自动重试一次（此时请求大概率没到达服务端，
  不会造成重复写入）。
- **有界队列**：待写队列满（默认 50）丢最旧，防止服务端长时间不可用时内存膨胀。
- **兜底冲刷**：插件 dispose 时冲刷全部合并桶，记忆不丢失。

## 本地验证

```bash
cd /data/code/mem0_falkordb/plugins/dsh-mem0-plugins
node test/smoke.mjs         # Host 半：apply 全链路 + 工具执行 + 召回/写入链路（28 项）
node test/client-smoke.mjs  # Client 半：bundle 加载 + locale/slot 注册 + 表单（16 项）
```

真机联测记录（2026-08-23，本机 mem0-dev 栈）：

```
Mem0Client.search OK in 2121 ms; hits: 1; breaker failures: 0
no-auth rejected as expected: Mem0HttpError | HTTP 401
```

## 排障

| 现象 | 处置 |
|------|------|
| 工具返回「插件未启用」 | 设置页打开 `enabled` 并确认 `host` 已填 |
| 「circuit breaker open」 | 服务端连挂多次触发熔断；检查 server 后等冷却或调低阈值 |
| HTTP 401 | `apiKey` 缺失或错误（非 AUTH_DISABLED 部署必须填 ADMIN_API_KEY） |
| 「server unreachable」 | `curl http://<host>/openapi.json` 先确认可达性 |
| 记忆没被召回 | 该 user_id 下无相关记忆（`GET /memories` 查看）；或 recallWaitMs 太短 |
