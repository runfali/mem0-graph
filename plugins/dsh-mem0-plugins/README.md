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

装好即默认启用（`enabled` 默认 `true`，指向本机 server 时零配置可用）；
设置页改动即时生效，无需重启。要关闭记忆，在卡片里关掉「启用插件」开关——
卡片描述行实时显示 **已启用/未启用 + host**，一眼可见。

## 设置项

默认值：`enabled=true`（schema 默认，patch 不覆盖——配置即启用）、
`host=http://127.0.0.1:8888`、`apiKey=''`（本机 server 无鉴权时零配置可用）。
设置页保存的值落在用户层，优先级更高。

### 连接与身份

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 总开关，默认开启；关闭后不再召回/写入，工具调用提示未启用 |
| `host` | `http://127.0.0.1:8888` | 自托管 server URL |
| `apiKey` | 空 | 以 `X-API-Key` 头发送；`AUTH_DISABLED` 部署留空 |
| `userId` | `dsh-user` | 记忆归属 user_id，跨会话共享同一份记忆 |
| `agentId` | `dsh` | 写入附带的 agent_id |

### 自动召回

| 字段 | 默认 | 说明 |
|------|------|------|
| `recallEnabled` | `true` | claimed 预取 + 提示词注入总开关 |
| `recallWaitMs` | `15000` | 装配点等待预取结果的上限（对齐 hermes `_PREFETCH_WAIT_SECS=15`），超时跳过本次注入 |
| `topK` | `10` | 每次召回最大条数（1–50） |
| `rerank` | `false` | 开启则以全深度模式请求重排（服务端需配置 reranker） |
| `distillEnabled` | `true` | 长文本查询蒸馏总开关（见下方「查询蒸馏」） |
| `distillMinChars` | `500` | 不超过该长度的消息原样直查，零损失零开销 |
| `distillInputMaxChars` | `8000` | 送入蒸馏模型的原文截断上限 |
| `distillBaseUrl` | `http://10.220.0.35:8090/v1` | 蒸馏端点（OpenAI 兼容）；留空跳过蒸馏直查原文 |
| `distillApiKey` | `devops` | Bearer 鉴权，与 hermes 默认一致 |
| `distillModel` | `Qwen3.5-9B` | 蒸馏模型（本地部署） |
| `distillTimeoutMs` | `30000` | 蒸馏单次超时 |
| `distillRetryAfterMs` | `20000` | 双飞触发阈值：首请求无响应超过该时长即并发第二请求，先完成者胜出 |

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
| `feedbackEnabled` | `true` | update/delete 成功后上报 evolve 反馈（可关） |

### 可靠性与超时

| 字段 | 默认 | 说明 |
|------|------|------|
| `queueMaxLen` | `50` | 待写队列上限，满时丢最旧 |
| `breakerThreshold` | `5` | 连续失败达该次数熔断 |
| `breakerCooldownMs` | `120000` | 熔断冷却时长 |
| `requestTimeoutMs` | `300000` | 单请求总闸，search/add 共用（对齐 hermes `httpx timeout=300.0`） |

要改 profile 层默认值，在 `~/.dsh/profiles/web/cordis.patch.yml` 追加：

```yaml
- id: mem0
  config:
    enabled: true
    host: http://10.200.0.5:8888
    apiKey: your-admin-api-key
```

## 消息卫生

- **琐碎输入跳过**：纯问候/确认/斜杠命令不触发预取，零网络往返——移植自
  hermes `is_trivial_prompt` 并扩充中文高频词表（好的/嗯嗯/收到/继续/下一步/
  在吗/辛苦了…三分类等价，只整串匹配、带正文永不误伤）；
- **中断轮不入记忆**：被打断的半截回复不会写进 mem0（部分输出不是持久对话真相，
  对齐 hermes #15218）。

## 查询蒸馏（防长文本打爆服务端）

移植自 hermes `agent/memory_manager.py::_distill_query`，只作用于**召回查询**，
不碰写入路径：

1. 消息 ≤500 字符：原样直查——零语义损失、零额外调用；
2. 超长消息（贴日志/代码）：截断前 8000 字符送本地小模型提炼成「2–4 关键词或
   一句检索意图」再去 `/search`，embedding 与检索不再吃整段噪音；
3. **语言漂移防护**：中文输入的蒸馏结果若出现越南语重音字符或非拉丁非 CJK
   文字（聚合网关路由漂移到多语小模型的实证症状），判为污染即回退；
4. **并发双飞**：首请求 20s 无响应即并发第二请求（首个不取消），先完成者胜出；
5. 全部失败/超时：回退原始 query，检索永不静默丢失。

真机记录（2026-08-23，Qwen3.5-9B @10.220.0.35:8090）：

```
原文长度: 4250 → distilled 4250 -> 17 chars (6480 ms)
蒸馏结果: mem0 服务端部署端口和内网地址
```

## 可靠性设计

- **熔断器**：连续失败 ≥5 次（可配）暂停所有 mem0 调用，冷却 120s 后自动恢复；
  404/not found 类客户端错误不计入熔断。
- **连接级重试**：连接拒绝/DNS 类失败自动重试一次（此时请求大概率没到达服务端，
  不会造成重复写入）。
- **有界队列**：待写队列满（默认 50）丢最旧，防止服务端长时间不可用时内存膨胀。
- **兜底冲刷**：插件 dispose 时冲刷全部合并桶，记忆不丢失。

## 超时分层（与 hermes 同步）

| 层级 | 默认值 | 说明 |
|------|--------|------|
| HTTP 总闸 `requestTimeoutMs` | 300s | 插件→server 单请求上限；server 内 LLM 三层兜底最坏 180s，正常召回摸不到总闸 |
| 召回热等待 `recallWaitMs` | 15s | 主回复前最多等预取这么久，超时不阻塞对话，记忆后台异步补上 |
| 工具级额外限時 | 无 | 有意不设——只有总闸一层，与 hermes 行为一致 |

## 本地验证

```bash
cd /data/code/mem0_falkordb/plugins/dsh-mem0-plugins
node test/smoke.mjs         # Host 半：apply 全链路 + 工具执行 + 召回/写入链路 + 蒸馏 + 卫生（63 项）
node test/client-smoke.mjs  # Client 半：bundle 加载 + locale/slot 注册 + 表单 save 真链（22 项）
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
