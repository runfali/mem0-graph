# dsh-mem0-plugins 审计报告 — 2026-09-01（DSH 0.1.2-alpha.3 适配后全量）

范围：src/* 全量通读 + lib/client.js 全量 + test/* 全量 + package.json/cordis.patch.yml/docs
基准：commit 03ec1db（0.1.2-alpha.3 适配）之后；本轮修复 commit 见文末。
方法：dsh-plugin-audit 双证纪律（代码级全读 + 契约级对照 dsh 0.1.2-alpha.3 真实源码）。

## Round 1 — 发现与修复（全部固化为回归测试）

| 级别 | 问题 | 修复 |
|---|---|---|
| P1 | isClientError 豁免表只有 404/400——指向第三方标准 mem0 server 时超长/非法 payload 的 422 校验拒绝会计熔断，单点用户错误放大成全局短路（session-track 快轨同款教训） | backend.js 增 422 豁免（判据：4xx=请求方错误不计熔断，5xx/网络错/超时=计） |
| P2 | client FIELDS/GROUPS/翻译键缺 sliceThreshold / slicePieceChars / maxBucketAgeMs 三键（四处同步纪律：schema 有、spec() 有、设置页无）——切片与毒桶存活上限无法从设置卡调节 | lib/client.js FIELDS +3、GROUPS 两组补入、zh 翻译键 +6（field./hint.）；client-smoke 键清单同步补 4 键（含此前遗漏的 redactEnabled） |
| P2 | redact.js password 规则用 \b 词边界——DB_PASSWORD=secret 单行 env 赋值漏放（_ 是词字符，边界不成立；5 行+ 整段 .env 有 env-block 折叠兜底，单行无兜），违背「宁误杀不漏放」 | 前缀改 (?<![A-Za-z0-9])，后缀 (\s*=\s*) 保留（passwordhash= 不误伤） |
| P3 | sliceText 的 REDACTED 起点回退条件 open > 0——标记恰在文本 0 位时不回退 | 改 open >= 0 |
| P3 | 插件卸载（dispose）后 in-flight 直写才失败时，demote 仍回插桶——tick 已停、无人冲刷 = 内存泄漏 + 数据假装待重试 | coalescer 增 dispose() 标记；demote 时已卸载则诚实计数丢弃 + warn（原文仍在 dsh 会话日志可回捞）；index.js teardown 先 dispose 再 flushAll |

## Round 2 — 换角度复核（零新发现）

- 自引用环：RECALL_REMINDER / usage 节均为静态文本，不引用自身来源 ✓
- 异步回收：全局监听全部走 ctx.effect（卸载自动移除）；agent 级监听挂 agent.ctx（随 agent 生命周期）；tools.register 走 layers.effect 随 fiber 卸载（热载无重复注册，源码级核验）✓
- 短路污染：breaker 计数/冷却窗口由 retuneBreaker 热调，无全局水位残留；distill 失败回退原文不污染状态 ✓
- 只读端点：无 GET 带副作用；工具全 POST/PUT/DELETE ✓
- 并发写：服务端 MAX+1 单调键兜底，客户端只保证单线程串行 ✓
- 钩子 next 单次调用：pre-step catch 不再二次 next（既有修复复核）✓
- 码点安全：truncateItemText / truncateOutput / sliceText 全部 Array.from/代理对回退 ✓
- 熔断器语义：短路不计 retries、422 豁免（本轮）、连接级失败按龄不丢 ✓

## 契约双证（alpha.3 源码级对照）

- settings：installSection(owner, ns, schema, entry, hooks) 同步执行 setSource/onChange（TDZ 已规避）；settings/updated 载荷 (ns, next, prev, source) 未变 ✓
- 事件面：agent/inbox/claimed {message, turn}、agent/pre-step {messages, ...position, signal}、agent/turn-stopping {turn, signal}、session/event (session, {seq, time, data, ...}) 全部与插件读取形状一致（D5 教训面零漂移）✓
- 服务名：agents/tools/systemPrompt/settings 四服务 alpha.3 全在 ✓
- client：slots/locale/settingsScope 短服务注入不变；dsh.client 无 inject 数组（soul-md 同款适配形态）✓

## 测试证据

- node --test：32/32 ✓（redact 新增 DB_PASSWORD 2 组 + passwordhash 反例）
- smoke：156/156 ✓（+5：422/400/5xx 归类、dispose 后降级诚实丢弃、REDACTED 0 位边界）
- client-smoke：33/33 ✓（键清单 35 键全覆盖断言）

## 停止线判定

Round 1 修复清零 → Round 2 换角度复核零 P0/P1/P2 → 达成「连续两轮零 P0/P1/P2」停止线，转按需审计模式（大改动后审改动面 + 定期专项）。P3 backlog：distill 输入 UTF-16 slice（发 LLM 无害，留档）。

提交：audit: 2026-09-01 一轮审计五修 + audit: 报告落 docs/AUDIT.md（副本仓；上游镜像待同步）
