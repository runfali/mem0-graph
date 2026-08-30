# 未推送代码审计报告 — 2026-08-29

审计对象：upstream `main` 领先 origin 6 commits（`e71fb22`→`71d2238`，mem0 server + dsh-mem0-plugins 插件），workspace 副本 `dsh-mem0-plugins` 领先 4 mirror commits。
模式：循环审计（修复→重审），停止线 = 连续两轮 P0/P1/P2 = 0（dsh-plugin-audit 纪律）。
方法：全量代码通读 + 契约级验证（cordis waterfall / Mem0HttpError / redactSecrets / undici headersTimeout 均以真实依赖源码为准）+ 图谱刷新（code-review-graph 5125 节点健康）。

## 第一轮 — 发现与修复

| 级别 | 编号 | 问题 | 修复 commit |
|------|------|------|-------------|
| P2 | 1 | `agent/pre-step` catch 里 `return next()` 二次触发下游链（cordis waterfall = `cbs.shift() ?? inner`，下游含其他插件注入副作用被原样重放） | `c184f40` |
| P2 | 2 | `sliceText` 硬切 `cut=limit` 可切半 emoji 代理对，产坏码点入库 | `3a16d15` |
| P2 | 3 | 超时类毒桶残留：timeout 错误无 status → 存活上限永不生效；退避(30s→5min)>冷却(120s) → 跨冷却重置钉死 retries=1/20 → 大桶无限重投（undici 300s headersTimeout 硬顶，`requestTimeoutMs` 调大无效） | `29790f8` |
| P3 | 1 | `maxBucketAgeMs` 注释称可配置但 schema/spec/resolve 三处未接线，设置页调不到 | `fc8a137` |
| P3 | 2 | 切片破坏成对假设 → `stats.dropped` 可为分数（仅统计显示） | 记录不修 |

修复要点：
- **单次调用形态**（P2-1）：`next()` 只调一次，上游失败原样上抛（返回 undefined 会令运行时读 `decision.kind` TypeError），注入逻辑独立 try/catch 失败返回原 decision。同 session-track B 组 P2-2 教训闭环。
- **码点安全**（P2-2）：切点落在代理对中间回退 1 个 UTF-16 单元；回退后不为 0（空片+rest 原样=死循环守卫）。旧代码反向验证必炸新断言。
- **超时裁桶**（P2-3）：`/timed out/` 判定与 backend.js 同源（网络级 fetch failed/ECONNREFUSED 不命中，宕机不丢语义不变），超时且桶 >20 轮裁到最近 20 轮、chars 重算、dropped 计数——payload 变小后服务端可在超时窗内跑完，收敛退出死循环；原文仍可按 sessionId 从 dsh 会话日志回捞。
- **接线补齐**（P3-1）：`z.number().step(1).min(60000).max(7200000).default(1800000)` + clampInt + resolve 透传 + README 行。

## 第二、三轮 — 复验（连续零 P0-P2，停止线达成）

换角度清单覆盖：trim+merge 竞态（先裁后合 ✓）、shortCircuited 文案无误裁 ✓、桶消息奇偶性（成对入桶）✓、clamp 下限与 resolveConfig 守卫兼容 ✓、dispose 冲刷路径 ✓、决策 undefined 守卫 ✓、监听生命周期（agent.ctx.on 随 agent 回收）✓、redact→slice 顺序 ✓、无新增 GET 副作用 ✓、客户端文件未在改动面 ✓、server 侧 chunk bailout（`test_chunk_bailout.py` 2 用例钉住）与 reasoning_effort 链（env→LLM_CONFIG→base.py supported_params→azure_openai）✓。

## 验证证据（2026-08-29）

- upstream：`node test/smoke.mjs` **147 通过**（含新增 12 项断言：码点安全×1、单次调用×2、超时裁桶×3、接线×1 及既有回归）· client-smoke 33 · formatting 18/0 · redact 14/0 · `pytest tests/memory/test_chunk_bailout.py` 2 passed
- workspace 副本（mirror commit `415e690`，仅 src×2+test 同步，双语 README 按双仓契约自行维护）：smoke 147 · client-smoke 33
- 提交链（逐件独立可回滚）：`3a16d15` → `c184f40` → `29790f8` → `fc8a137`；副本 `415e690`

## P3 残留（同日二次清偿，commit `1ef48b9`）

一轮报告曾记录三条 P3 不修；发哥指示修 P3，同日全部清偿：

1. ~~dispose 冲刷遇失败桶挂回~~ → 已修：dispose 后无下一 tick，挂回是伪装成「待重试」的死数据；改为诚实丢弃 + dropped 计数 + warn 说明去向（原文在 dsh 会话日志）。smoke：dispose 失败桶不挂回、dropped=3。
2. ~~`[REDACTED:*]` 标记可被硬切拆开~~ → 已修：sliceText 对全文定位 cut 前最近标记起点（opening 自身被截也能定位），cut 落标记区间内则整标记让给下一片。密文本体本已被替换（无泄漏），修复的是跨片提取语义残缺。smoke：标记不跨片拆半；旧切点反向验证拆半必现。
3. ~~≤20 轮小桶超时退避循环~~ → 已修：丢弃线扩展为「服务端明确拒绝 **或** 超时」且超龄（>maxBucketAgeMs）即丢——timeout=服务端收到且在跑但 300s 窗口跑不完，超龄说明该载荷当前状态下无望；未超龄照旧退避，宕机（fetch failed）不计入，宕机不丢语义不变。smoke：超时+超龄小桶兜底丢弃、未超龄不受影响。

---

# 今日修改专项审计 — 2026-08-31

审计对象（今天全部改动面）：
1. **fallback 继承修复**（c2e413a）：`mem0/llms/fallback.py`（FALLBACK_INHERIT_KEYS + inherit_primary_config）、`mem0/memory/main.py::_build_llm`、`mem0/graphs/falkordb/graph_memory.py::_build_llm`、测试 ×2 文件。
2. **配置变更**（生产 only，不入 git）：server/.env（MEM0_LLM_MAX_TOKENS 4096→8192、新增 MEM0_LLM_FALLBACK_TIMEOUT=180）、server/config.json（layer_timeout 120.0→180.0）。
3. 配套回归测试（tests/utils/test_factory.py ×2、tests/memory/test_llm_fallback.py ×1）。

方法：图谱刷新（code-review-graph 5132 rows 健康）→ 全 diff 通读 → 契约级验证（LlmConfig dict 约束、factory create 三路径、各 provider config `__init__` 签名实证、update_config 合并且保留 layer_timeout）→ 换角度复核。

## 第一轮 — 发现与修复

| 级别 | 问题 | 修复 commit |
|------|------|-------------|
| P1 | **factory dict 路径无护栏**：继承注入把 L0 的 `reasoning_effort` 放进兜底层 dict 后，`AnthropicConfig` 等未声明该形参且无 `**kwargs` 的 provider config 类在 `LlmFactory.create` 时 `TypeError` 直接崩（兜底 anthropic + L0 带 reasoning_effort 的组合）。BaseLlmConfig 转换路径（107-114 行）早有同语义护栏，dict 路径漏了。**测试盲区**：既有单测全 Mock `LlmFactory.create`，绕过真实 config 类构建。 | `e34874a` |

修复要点：`factory.create` dict 分支按 `inspect.signature(config_class)` 过滤——未声明且无 `**kwargs` 的键剥掉（有 `**kwargs` 的如 AWSBedrockConfig 照常全收）；与既存 BaseLlmConfig 转换路径同语义。回归用例：dict 路径 anthropic 剥掉 / openai 保留 + `_build_llm` 真 config 类用例（只桩 load_class，跑真实 config 构建）。

## 第二轮、第三轮 — 复验（连续两轮零 P0-P2，停止线达成）

换角度清单覆盖：
- LlmConfig.config 类型（pydantic Optional[dict]，BaseLlmConfig 实例不可能流入 → `dict()` 安全）✓
- `inherit_primary_config` 纯函数无副作用、None/空 dict 防御 ✓
- 全仓 `FallbackLLM(` 构造点仅有 memory/main.py 与 graphs/falkordb/graph_memory.py 两处，均已接入继承 ✓
- `update_config` 全量持久化 `_current_config`（含 layer_timeout）→ dashboard 保存不回退 120 ✓（_merge_config dict 深合并，list 整体替换由运行时继承兜底）
- 各 provider config 签名实证：BaseLlmConfig 系全部声明 reasoning_effort；仅 AnthropicConfig 缺（已护栏）、AWSBedrockConfig 靠 `**kwargs`（放行）✓
- 生产 sync：4 个改动文件 prod == repo HEAD；uvicorn StatReload 日志确认 factory.py 热生效 ✓
- FallbackLLM 层内超时注入（layer_timeout=180）与 openai client timeout（MEM0_LLM_TIMEOUT=180）一致 ✓

## P3 残留（待发哥判断是否修复）

1. `inherit_primary_config` 无直接单测（仅经 `_build_llm` 间接覆盖 5 处；加一个 3-断言直测 <10 行）。
2. `server/README.md` 第 12 节（推理模型 results=0 排查）未提及「fallback 层自动继承 L0 reasoning_effort」——文档补注。
3. factory dict 过滤使「config 键名拼写错误」从 TypeError 变静默忽略——有意宽容，但配置错误不再报错（可考虑 warn 日志）。
4. layer1 `9router` 配置残留：key 401 失效 + 服务端 500（换 key 或移除，此前已列待办）。
5. `MEM0_LLM_MAX_RETRIES=2` 实际无效（FallbackLLM 强制 client.max_retries=0），.env 易误导——建议注释或移除。
6. config.json fallback 条目无显式 `reasoning_effort`——现由运行时继承兜底；若日后有人移除主层该键，fallback2 将回退思考模式（可考虑 config.json 双保险显式写，属冗余配置）。

## 验证证据（2026-08-31）

- `pytest tests/memory tests/graphs tests/utils` → **593 passed / 21 skipped**（新增 3 项回归）
- 生产容器 `_build_llm` 实测：L0/L1/L2 全 `reasoning_effort=none`、`max_tokens=8192`、`layer_timeout=180.0`
- 生产配置核验：MAX_TOKENS=8192、FALLBACK_TIMEOUT=180、config.json layer_timeout=180.0、备份 `*.bak-20260831`
- 提交链（逐件可回滚）：`c2e413a`（B 修复）→ `e34874a`（P1 修复）；均未推送。