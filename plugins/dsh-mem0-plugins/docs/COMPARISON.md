# dsh-mem0-plugins ↔ hermes mem0 全面对比矩阵

> 对比基准：hermes 运行版 `plugins/memory/mem0/`（v1.3.0，2026-08-23）+
> 主程序 `agent/memory_manager.py` / `agent/memory_provider.py` /
> `run_agent.py` / `turn_context.py` + 生产 env `~/.hermes/.env`。
> 结论：功能全对齐，4 处差距已补齐，5 处有意不同，4 处 DSH 更优。

## 一、召回路径

| 能力 | hermes | dsh-mem0-plugins | 状态 |
|------|--------|------------------|------|
| 用户消息进轮即后台预取 | `on_turn_start` → 后台线程 | **不做**（平台时序约束：assemble 在消息回显前，等待=卡回显；见「平台时序约束」节） | ⚖️ 架构差异 |
| 召回热等待 | `_PREFETCH_WAIT_SECS=15`，超时放行不阻塞 | **无**（取消等待注入）；usage 节引导 + **第一步强制提醒**（`forceRecallStep` 默认开，pre-step 注入 plugin-source 提醒，琐碎轮跳过） | ⚖️ 显式化+流程化 |
| 长文本查询蒸馏 | `_distill_query`：≤500 直通 / 8000 截断 / 本地小模型提炼意图 | `src/distill.js` 全套移植 | ✅ 对齐 |
| 蒸馏端点/模型 | 生产 env：Qwen3.5-9B @10.220.0.35:8090/v1, key=devops | 同款默认值，设置卡可改 | ✅ 对齐 |
| 蒸馏单次超时 | 生产 env `HERMES_DISTILL_TIMEOUT_S=90`（代码默认 30s） | 默认 **90000ms**（对齐生产值） | ✅ 已补齐 |
| 双飞重试 | 20s 无响应并发第二请求，先到先用 | `distillRetryAfterMs=20000` 同语义 | ✅ 对齐 |
| 语言漂移防护 | 中文输入→越南语/非拉丁非CJK输出判污染回退 | 正则逐字符移植 | ✅ 对齐 |
| 蒸馏失败回退原文 | 检索永不静默丢失 | 快速失败走单次重发分支（测试抓出并修复抛穿 bug） | ✅ 对齐 |
| **琐碎输入跳过** | `is_trivial_prompt`：空//命令/纯问候确认不预取 | `src/guards.js` 移植，claimed 守卫 | ✅ 已补齐 |
| **中断轮预取防污染** | 中断后下一轮大概率重试同一意图 | claimed 即时预取天然无此问题（按新消息起算） | ➖ 架构差异，DSH 无此风险 |

## 二、召回注入

| 能力 | hermes | dsh-mem0-plugins | 状态 |
|------|--------|------------------|------|
| 注入通道 | user 消息 API 副本尾部（`api_content` sidecar，字节稳定保 prompt cache） | system prompt section（`mem0:recall`，装配点推入） | ⚖️ 有意不同（见注） |
| System note 注记 | `[System note: recalled memory context, NOT new user input…]` | 同语义英文注记已加入注入块头部 | ✅ 已补齐 |
| 防记忆内容反注入 | `sanitize_context` 剥离嵌套 `<memory-context>` 围栏 | section 通道无围栏语法可劫持；工具返回值经 JSON schema 校验 | ✅ 等价安全 |
| 流式清洗 StreamingContextScrubber | 防 UI 泄漏 memory-context 标签 | 不需要——不走用户消息通道，标签不会出现在对话流 | ➖ 不适用 |
| 召回状态提示 describe_recall | `_emit_status` 给用户「已召回」指示 | DSH 无等价 status 通道；冲刷/命中走 logger | ⚖️ 暂缺（记录待办） |

> 注：section 方案不污染会话历史、多会话各自求值；代价是召回内容变化会改变
> system prompt 尾部（provider 前缀缓存少命中一段）。hermes 的 user-tail 方案
> 缓存友好但需要 api_content sidecar 保证跨轮字节稳定。两平台基建不同，
> 各取其稳。

## 三、写入路径

| 能力 | hermes | dsh-mem0-plugins | 状态 |
|------|--------|------------------|------|
| 每轮非阻塞入队 | `sync_all(user, assistant)` 单 worker 串行 | `agent/turn-stopping` 出队 → 有界队列 + tick 冲刷 | ✅ 对齐 |
| 潮浪并忆 | 分桶合并：空闲5s/窗口15s/5轮/4000字，fastpath 2000 | 全参数移植且全部可在设置卡热调 | ✅ 对齐 |
| 纯 JSON 消息剥除 | 整条 JSON 替换占位符 | `looksLikeJson`/占位符逐行移植 | ✅ 对齐 |
| **中断轮不写入** | `interrupted → return`（#15218：部分输出非持久真相） | capture 记录 `interrupted` 标记，turn-stopping 时整体跳过 | ✅ 已补齐 |
| 写入元数据 | `metadata.channel` = 网关名（cli/telegram…） | `channel: 'dsh'` 固定 | ✅ 等价 |
| 兜底冲刷 | atexit + shutdown 双保险 | effect disposer flushAll | ✅ 对齐 |
| 队列上限丢最旧 | deque(maxlen=50) | 数组 shift + dropped 计数 | ✅ 对齐 |

## 四、工具与反馈

| 能力 | hermes | dsh-mem0-plugins | 状态 |
|------|--------|------------------|------|
| mem0_search/add/update/delete | 4 工具 schema | defineTool 强校验（output schema 编译期验证） | ✅ 更优 |
| add 逐字存储 infer=false | ✓ | ✓ | ✅ 对齐 |
| update→correction / delete→useless 反馈 | best-effort `/evolve/feedback`，note 截 200 字符 | 同语义，note 截 200+省略号 | ✅ 对齐 |
| feedback 开关 | 无独立开关（始终上报） | `feedbackEnabled` 可关 | ✅ 更优 |
| 工具未配置时的行为 | is_available 门控整个 provider | 工具常驻，execute 返回启用指引（describe-image 先例） | ✅ 等价 |
| 客户端错误豁免熔断 | 404/not found/valid uuid | Mem0HttpError(404)+同文案正则 | ✅ 对齐 |

## 五、可靠性与配置

| 能力 | hermes | dsh-mem0-plugins | 状态 |
|------|--------|------------------|------|
| HTTP 总闸 | httpx timeout=300.0 | requestTimeoutMs 默认 300000 | ✅ 对齐 |
| 连接级重试 | httpx transport retries=2 | fetch 失败重试 1 次（250ms 退避） | ✅ 等价 |
| 熔断器 | 5 连败→120s 冷却 | CircuitBreaker 同参数，阈值/冷却可热调 | ✅ 对齐+更优 |
| 配置方式 | `$HERMES_HOME/mem0.json` + env，改后需重启 | 设置页卡片 29 字段 live 生效，无需重启 | ✅ 更优 |
| 多后端模式 | platform/selfhosted/oss 三模式 | 仅 selfhosted（发哥拍板裁剪） | ⚖️ 有意裁剪 |
| 设置向导 CLI | `hermes memory setup` curses UI | DSH 设置页替代 | ⚖️ 平台形态不同 |
| OSS 维度自适应重建集合 | qdrant/pgvector dims 变更删集合 | 不适用（server 模式） | ➖ 不适用 |

## 六、DSH 更优项汇总

1. **配置全部 live 热更**：29 个字段设置页即改即生效，hermes 改 mem0.json/env 要重启；
2. **多会话并发隔离**：预取/配对状态按 sessionId 分键（Map），hermes 是单实例全局单槽；
3. **工具输出强契约**：defineTool output schema 编译期校验 + 结构化 `{ok,data,error}`；
4. **可观测统计**：dropped/jsonSanitized/savedCalls 计数进日志，潮浪收益可见。

## 七·五、契约漂移警戒（本插件踩过，供后续插件开发者）

dsh 0.1.1-rc.2 实测：`agent/inbox/claimed` 运行时载荷为 `{message, turn}`、
`agent/turn-stopping` 为 `{turn, signal}`——与 `dsh-agent/lib/types/runtime-types.d.ts`
声明的 `{agent, message, turn}` / `{agent, turn, signal}` **不一致**（声明含 agent，
emit 实现不含）。依赖声明会被静默吞掉（监听器 TypeError 被 try/catch 吃成 debug 日志）。
规避：以 `agent/created`（载荷实测含 `{agent}`）闭包捕获 agent，在 agent 级 scoped ctx
上注册子监听。教训：**插件开发一律以运行时 emit 实现为准，d.ts 仅作参考**；
离线测试的 mock 载荷必须按实测形状构造，否则测试全绿真机全哑。

## 七、平台时序约束（决定召回注入形态的硬边界）

dsh-agent-loop 的 step 时序（0.1.1-rc.2 源码实证）：

1. `claim()` → **assemble()**（含 system-prompt/assemble 瀑布）
2. append `user/message`（**消息回显发生在 assemble 之后**）
3. `renderPrompt` → `buildRequest` → **llm/stream**（request 对象 deepFreeze）

推论：任何在 assemble/pre-step 瀑布里等网络的插件都会**延迟用户消息回显**（实测 15s 等待 = 每条消息卡 15s 才显示）。append 之后、请求组装之前**不存在**内容注入钩子（agent/request 只能换 config；llm/stream 的 options 深冻结只可 gate 不可改）。因此「回显零延迟」与「首次 LLM 调用前注入召回」在当前 dsh 版本**互斥**——本插件选择回显优先（默认 recallWaitMs=0），召回改由：
- 后台预取在装配前完成时直接注入（快 server 场景）；
- 未完成时由 usage 节引导模型先调 `mem0_search`——工具卡在 UI 可见，等价于 hermes 的召回状态行。
如需「等召回再回复」的旧语义，把 recallWaitMs 调成 >0（有界等待，代价是回显延迟）。

## 七、已知待办（记录不阻塞）

- [ ] `describe_recall` 式「已召回 N 条」用户可见状态提示——等 DSH 前端 status 通道；
- [ ] `queue_prefetch_all` 下轮预热——DSH claimed 即预取已覆盖主场景，暂不做。
