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

## P3 残留（记录，不修）

1. dispose 冲刷遇失败桶挂回属既定权衡（dispose 后无下一 tick，靠下次会话/agent 生命周期续冲）。
2. `[REDACTED:*]` 标记理论上可被硬切边界拆开——密文本体已被替换，无泄漏，仅标记显示残缺。
3. ≤20 轮的桶超时后不裁剪，若服务端存活但对该 payload 病态慢，退避循环持续（每 5min 一次有界成本；服务端恢复即收敛；大桶路径已收敛）。
