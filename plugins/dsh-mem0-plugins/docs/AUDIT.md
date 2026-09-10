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


---

# Round 5 — DSH 0.1.5-rc.1 适配审计（2026-09-10）

> 范围：从 0.1.2-rc.1（宿主 0.1.2-alpha.3）适配到 **0.1.5-rc.1**（宿主实测版本 0.1.5-rc.1）。
> 方法：dsh-plugin-audit（代码级全读 + 契约级源码对照真实依赖 + entry-smoke + 隔离实例真机 E2E）。
> **本轮抓到 1 个 P1 级真实缺陷（设置页文案与生效值不一致）+ 1 个 P2 级声明缺陷（engines 区间不覆盖目标版本）+ 1 个 P1 级测试盲区（缺 entry-smoke），均已修复并固化回归。**

## 一、契约级源码对照（对照真实 0.1.5-rc.1 宿主逐文件读源码）

| 契约 | 结论 | 证据 |
|---|---|---|
| `settings.installSection(owner, ns, schema, entry, hooks)` | 不变（register(base=entry) → setSource → 卸载回落 effect → onChange 同步首发 → scope.watch 持续通知） | dsh-settings/lib/index.js:327-343 **逐字节相同** |
| `settings/updated` 载荷 `(ns, next, prev, source)` | 不变 | dsh-settings/lib/index.js:565-567 |
| 命名空间 kebab-case 校验 | 不变 | dsh-settings/lib/index.js:281 |
| `ctx.tools.register(defineTool({...}))` | 不变；`output.render` 仍为必填校验项；`layers.effect(ctx, ...)` 仍插调用方 layer | dsh-tools/lib/index.js:2773-2784 |
| `defineTool` 无 Symbol/brand 跨副本身份校验 | 是（插件用自身嵌套副本调用安全） | dsh-tools/lib/index.js:837-872，全文无 `Symbol.for`/`instanceof` 品牌校验 |
| `systemPrompt.section({name, order, text})` | 不变；`order` 必须有限数 | dsh-system-prompt/lib/index.js:238-240 |
| 服务名 `tools` / `systemPrompt` / `settings` / `agents` | 四个全在 | dsh-tools:2606、dsh-agent:299 |
| `agents.list()`（补注册路径） | 不变，返回所有 live agent | dsh-agent/lib/index.js:581-583 |
| `agent/inbox/claimed` 载荷 `{message, turn}` | 不变（**顶层**，非 data 嵌套） | dsh-agent-loop/lib/index.js:107-110 |
| `agent/pre-step` 载荷 `{messages, turn, step, signal}` + waterfall next | 不变；`messages` 仍是 claim 出的批次 | dsh-agent-loop/lib/index.js:894-898 |
| `agent/turn-stopping` 载荷 `{turn, signal}`，serial 派发 | 不变 | dsh-agent-loop/lib/index.js:967-970 |
| `agent/created` 载荷 `{agent}` | 不变 | dsh-agent/lib/index.js:541-545 |
| `session/event` 载荷 `(session, event)`，event `{type, seq, time, data}` | 不变；session 仍作第 1 参 | dsh-session/lib/index.js:1181-1202 |
| `user/message` 的 `data` **即消息本体** `{id, role, content, source}` | 不变（plugins 读 `p.message || p` 的兼容分支仍必需） | dsh-session/lib/index.js:928-931 |
| `assistant/message` 的 `data` 是 `{turn, step, message, usage?, stream}`；中断轮另有 `interrupted: true` | 不变 | dsh-session/lib/index.js:1050-1064、1108-1114 |
| `exec.signal` 透传（用户中断取消进行中蒸馏） | 不变 | dsh-tools/lib/types/index.d.ts:32-46、index.js:3033 |
| client `slots.inject(slot, cb)` + `register(options, comp)`（key/locale/inject） | 不变；`hooks` → `use<Name>` 映射仍在 | dsh-client-ui-renderer/lib/client.js:389-395 |
| client `settingsScope.bind({namespace})` → `getSnapshot/subscribe/set/unset` | 不变（`set/unset` 即 `mutate` 单字段封装） | dsh-client-ui-settings/lib/client.js:1169-1176 |
| client `locale.register(ns, {zh, en})` | 不变 | dsh-client-locale/lib/client.js:1256 |
| `settings.plugin.item` 槽位按命名空间分发 | 不变 | dsh-client-ui-settings-plugins/lib/client.js:398-422 |

**结论：0.1.2-alpha.3 → 0.1.5-rc.1 无 API 断点。** 核心包 diff 量极小且与本插件无关：
`dsh-settings` **逐字节相同**；`dsh-tools` 仅 70 行 diff，全部集中在 PTC（`run_code` 模式）
——`tool/code-dispatch` → `tool/ptc-dispatch` 事件改名、`tools-code-mode` → `tools-ptc` 插件名、
子调用 id 前缀 `:code:` → `:ptc:`，**本插件不注册/不读取任何一项**（且新增了
`run_code` 名称保留校验：本插件四工具名均不与它冲突）。

## 二、发现与修复

### P1-1（真实缺陷，已修复）设置页「单次请求总超时」文案与生效值不一致

`lib/client.js` 的 `hint.requestTimeoutMs` 写「与 hermes 一致**默认 300000**」，
而 Host schema（`src/index.js:84`）、`spec()` 归一化（:215）、README 双语、
`docs/COMPARISON.md` **四处都是 420000**。设置页是用户判断超时行为的唯一界面，
文案写 300 s 而实际闸门 420 s ⇒ 用户会误判「为什么 300 秒还没断」。

- 修复：文案与内嵌注释（`requestTimeoutMs=300s` → `420s`）一并对齐到 420000。
- **回归守卫（本项的核心价值）**：`test/entry.test.mjs` 新增「文案 ↔ schema 默认值」
  扫描——凡 hint 里以「默认 <数字>」形式点名的键，其数字必须等于 `Config` 解析出的
  默认值，否则断言失败并点名违规键。
- 反证：把文案改回 300000 → 立即红（`requestTimeoutMs: 文案写 300000，实际 420000`）；恢复即绿。

### P2-1（声明缺陷，已修复）`dsh.engines.dsh` 缺失，补声明时须真正覆盖目标版本

原 `package.json` 完全没有 `dsh.engines.dsh`（顶层 `engines` 是 Node 版本，勿混）。
补声明时按 npm semver **预发布规则**取：

```
">=0.1.2-alpha.3 <0.2.0 || >=0.1.5-alpha.1 <0.1.6"
```

理由：单区间 `>=0.1.2-alpha.3 <0.2.0` 内的预发布只有 `0.1.2-alpha.3`；
「预发布只被含同 `[major,minor,patch]` 元组预发布的区间满足」⇒ **不覆盖 `0.1.5-rc.1`**，
即「声明适配 0.1.5 却不被自己的声明覆盖」。**析取不是冗余，是本轮适配的实质内容之一。**

- **守护测试**：`test/entry.test.mjs` 内置判定表（不引 `semver` 依赖，避免测试随依赖漂移），
  逐行断言 `0.1.2-alpha.3 / 0.1.2-rc.1 / 0.1.5-alpha.1 / 0.1.5-alpha.2 / 0.1.5-rc.1 / 0.1.5 / 0.1.6`
  覆盖，`0.1.3-alpha.1 / 0.2.0 / 0.0.1` 排除；并**显式断言旧单区间不覆盖 0.1.5-rc.1**。
- **交叉验证**：手写比较器的 10 行判定表与宿主 `semver.satisfies` **逐行一致**（宿主 semver
  不可解析时显式跳过并打印，不假绿）。
- 反证：区间改回旧值 → 立即红；恢复即绿。

### P1-2（测试盲区，已修复）缺 entry-smoke：三条既有测试都没走宿主入口真实加载路径

按 dsh-plugin-audit「测试盲区」纪律核对：`node --check` 只查语法；`smoke.mjs` 虽
`import { apply } from '../src/index.js'` 但用 mock ctx 只驱动 apply 逻辑；`client-smoke.mjs`
只测浏览器半。**三路都不校验「插件入口在真实宿主里能否加载」** —— dsh-prompt-injector 的
P0 先例（`import { z } from '@deepseek-ai/schemastery'` 而 schemastery 只有 default export，
整包静默不生效）正是从这里溜过去的。

- 修复：新增 `test/entry.test.mjs`（已在 `npm test` 中），真实
  `await import('../src/index.js')`，断言 apply / Config / name / 命名空间导出齐备、
  `inject` 面恰为 `['tools','systemPrompt','agents']`、Config 可解析出默认值表，
  并顺带承载 engines 区间与文案漂移两项守护。
- 注：`import { z } from '@deepseek-ai/schemastery'` 在本仓**已是对的** —— schemastery 3.18.2
  的 ESM 入口确实有命名导出 `z`（源码级核对 `lib/index.mjs`），无需改动；现在有测试钉住它。

### 顺带核对（非缺陷，留档）

- `Config` / `client FIELDS` / `client GROUPS` 三处键集合**全等（35 键）**，zh label/hint
  全覆盖 —— 已由 entry.test.mjs 固化为断言（缺一键 = 设置页调不到该开关）。
- `spec()` 里 `outputMaxKb` → `outputMaxBytes` 是**有意的单位换算**（×1024），非键名漂移；
  两者在键集合断言里以 `outputMaxKb` 对齐。
- 句柄卫生：本插件无 `fs.watch`、无裸 `setInterval`（tick 定时器已 `unref`、
  dispose 返回冲刷 promise）、无 server/socket —— soul-md 同轮抓到的「重试链钉事件循环」
  与「孤儿 watcher 泄漏」两类 P1 在本插件不适用（实测：headless 单跑 3.55 s 正常退出，
  web 实例 SIGTERM 后 2 s 干净退出）。

## 三、依赖与发布面

- 依赖升到 `^0.1.5-rc.1`（`@deepseek-ai/dsh-settings` / `@deepseek-ai/dsh-tools`），
  `@deepseek-ai/schemastery` 保持 `^3.18.2`（该版本在新旧宿主中逐字节相同）。
- `pnpm-workspace.yaml` 的 `minimumReleaseAgeExclude` 白名单从 `@0.1.2-alpha.3` 刷到
  `@0.1.5-rc.1`（16 条），锁文件重生成。
- 版本号 `0.1.2-rc.1` → `0.1.5-rc.1`（跟宿主发布号，家族惯例）。
- 新增 `scripts.test`（`node --test test/*.test.mjs && node test/smoke.mjs && node test/client-smoke.mjs`），
  对齐 dsh-login-gateway 等姊妹仓。

## 四、测试证据（**全部在 0.1.5-rc.1 真实依赖下运行**，非旧依赖）

`node_modules/@deepseek-ai/{dsh-tools,dsh-settings}` 实测 `0.1.5-rc.1`：

| 测试 | 结果 |
|---|---|
| `node --test test/*.test.mjs` | **33/33 ✓**（32 → 33：新增 entry.test.mjs） |
| `node test/smoke.mjs` | **156/156 ✓**（apply 链路 / 捕获 / 潮浪 / 熔断 / dispose 兜底） |
| `node test/client-smoke.mjs` | **33/33 ✓**（bundle 加载 / locale / slots / 表单保存真链） |

## 五、隔离实例真机 E2E（独立 `DSH_HOME=<隔离目录>`、独立端口，绝不碰线上端口）

| 项 | 结果 |
|---|---|
| `dsh plugin --profile web add <仓库路径>` | ✅ 一步装好（走 `dsh.bundle.patch`） |
| 组合树挂载 | ✅ `--dump-config` 末层出现 `- id: mem0 / name: dsh-mem0-plugins` + config 四项 |
| 前端加载清单 | ✅ `__DSH_BOOT__` entries 含 `"id":"dsh-mem0-plugins"`，client bundle HTTP 200 |
| 设置命名空间注册 | ✅ `settings/describe` 返回 `mem0`：`applies:"live"`、`revision:0`、`base/value` 35 键完整、schema uid 663 |
| **设置卡真机渲染** | ✅ 设置 → 插件 → Mem0 卡片：35 个 input 全部可编辑（无只读态误锁），保存/放弃初始 disabled，标题显示「已启用 · http://127.0.0.1:8888」 |
| **设置卡保存真链** | ✅ 改 `topK` 10→17 + 勾 `rerank` → 保存 → 隔离 `settings.yaml` 落盘 `mem0: {topK: 17, rerank: true}`，脏标记清零、无失败提示 |
| **文案真机核对** | ✅ 设置页实际显示「…与 hermes 一致默认 **420000**」（P1-1 修复生效） |
| **工具注册（活体探针）** | ✅ 探针插件读活体注册表：`mem0_search/add/update/delete` 四工具在 global layer 可见；`systemPrompt` 全局段落含 `mem0:usage`；`agents.list()` 可用；零错误 |
| **完整对话轮（真 LLM）** | ✅ 「记住：我的缓存后端是 Redis 7」→ 模型先 `mem0_search` 再 `mem0_add`（5 次工具调用）→ 回复确认 |
| **会话日志实证：提醒注入** | ✅ `user/message` seq 10 `source:{kind:"plugin", plugin:"dsh-mem0-plugins", form:"notice", summary:"【记忆提醒】回答前必须先调 mem0_search（先搜再答）"}` |
| **会话日志实证：工具回执** | ✅ `tool/result` 渲染为紧凑行（`No relevant memories found.` / `Fact stored.`），无 JSON 壳 |
| **服务端落库（端到端）** | ✅ `GET /memories?user_id=dsh-probe-web` 返回 2 条：`用户的缓存后端是 Redis 7。` / `用户的主力数据库是 FalkorDB…`，均带 `metadata.channel="dsh"` |
| **跨会话召回** | ✅ 另起会话问「我用什么编辑器」（Neovim/~/dotfiles/nvim），模型先搜后答并正确复述 |
| **自动写入（潮浪链路）** | ✅ headless 与 web 两条通路均落库 `channel: dsh`（非仅 `mem0_add` 直写路径） |
| **关停句柄卫生** | ✅ headless 单跑 3.55 s 正常退出；web 实例 SIGTERM 后 2 s 退出、端口即刻释放 |
| **线上零影响核实** | ✅ 线上 `settings.yaml` mtime **全程未变**、内容段数一致；线上 profile 的组合树未被改动；线上端口全程正常；隔离实例已关、独立端口已释放 |

### 探针保真度记录（防假证据）

- **首轮 curl 取首页 303 是预期行为**，不是故障：token 换 cookie 后需带 cookie jar 重放才得 200
  （与 dsh-plugin-development「探针顺序必须复现真实客户端」同族）。用 `curl -c/-b + -L` 或
  Playwright 才对。
- **设置 RPC 请求形状**：`POST /api/settings/describe`，body 必须是
  `{type:"client-request", rpcId, method:"settings/describe", payload:{args:{…}}}`。
  缺 `args` 包装会得 `gateway/internal: Remote payload must contain exactly one plain-object args field`
  —— 这是**探针写错**，不是插件缺陷。
- **`pkill -f "port 3599"` 会自杀**（匹配到发起命令自身的 cmdline）：本会话改用
  `ss -ltnp | grep 3599 | grep -o 'pid=[0-9]*'` 或 `ps … | grep 'dsh --profile web --port 3599'` 精确定位。
- **`pnpm install` 在无 TTY 下会 abort 删 `node_modules`**：需 `CI=true`（或 `confirmModulesPurge=false`）。
- **`npm` 缓存目录不可写会导致 `npm view/pack` 失败**（宿主 npm 缓存目录权限）：用 `npm --cache /tmp/npmcache`。
- 隔离实例复用了线上 `.credentials.yaml` 的**副本**（`$DSH_HOME/.credentials.yaml`，0600），
  未改写线上原件；隔离 `settings.yaml` 独立，线上文件 mtime 前后逐秒一致。

## 六、停止线判定

| 轮次 | P0 | P1 | P2 | P3 |
|---|---|---|---|---|
| Round 5（本轮） | 0 | 2 项修复（P1-1 设置页文案漂移、P1-2 缺 entry-smoke） | 1 项修复（P2-1 engines 区间不覆盖目标版本） | 0 |

本轮 3 项发现全部修复并各配回归/反证（文案漂移与 engines 区间两项均通过**反向验证**：
改回旧值立即变红、恢复即绿，证明断言非空）。**修复后全量复跑绿（33/156/33）+ 真机 E2E 通过。**

**已知可接受缺口（诚实记录）**：
- 设置卡 35 键在真机逐项保存未做穷举（已验证文本/数字两类代表键 + 布尔键的落盘真链）；
  其余键同属一条 FIELDS 循环，路径同构。
- 潮浪桶的「服务端宕机不按龄丢」等可靠性语义未在本轮真机复现（需构造宕机窗口），
  由 smoke 的 156 项断言覆盖。

---

# Round 6 — 换角度复核 Round 5 修复面（2026-09-10）

按审计循环纪律：Round 5 修完必须换新角度复核**修复面本身**（不是只复核改动内容）。
本轮**零新增 P0/P1/P2**，但抓到并修掉一处**我自己引入的假守护**（探针保真度问题）。

## 一、发现与修复

### P0-0（探针缺陷，已修复）Round 5 新增的 spec() 守护一度是「假守护」

Round 5 追加的「spec() 数值回落值 ↔ schema 默认值」断言里，单位换算写成
`spec.fallback * spec.multiplier` 对比 `schemaDefault * spec.multiplier` ——
**倍率在两侧同时相乘被约掉**，等于根本没校验倍率本身。用「把 `* 1024` 改成
`* 1000`」定向扰动复现：**测试仍然全绿** ⇒ 断言非但不能发现该缺陷，
还给人「已守护」的错觉（比没有断言更危险）。

- 测得的真实后果：`outputMaxKb`（设置页单位 KB）→ `outputMaxBytes`（回执截断
  实际使用的字节预算）若误写 ×1000，50KB 的预算会悄悄变成 48.8KB（缩水 2.4%），
  设置页写 50 而实际生效 48.8 —— 与 Round 5 抓到的 P1 文案漂移同一性质。
- 修复：倍率改为**独立断言** `assert.equal(specNumeric.outputMaxBytes.multiplier, 1024)`，
  不再依赖两侧同乘。
- 反证：`* 1024` → `* 1000` ⇒ 立即红（`outputMaxKb → outputMaxBytes 的换算倍率必须是 1024`）；恢复即绿。
- **纪律补充（可迁移）**：写「A 换算成 B 也对」这类等比断言时，务必问一句
  **「两侧同乘的因子被约掉了吗？」** —— 倍率必须单独钉，不能藏在等式里。

### 顺带核对（零发现，留档）

| 复核项 | 手段 | 结果 |
|---|---|---|
| 同步脚本「预览模式零写入」 | 跑 `apply-to-upstream.sh`（默认预览）前后对上游文件取 md5 | ✅ 一致，预览确为只读 |
| engines 守护是否只对「本仓这一条区间」成立 | 定向扰动 4 个变体：删左侧析取 / 删右侧析取 / 右侧上界 `0.1.5-alpha.2` / 下界放宽到 `0.0.1` | ✅ 4/4 全部被抓（守护非空、方向敏感） |
| FIELDS/Config 键集合守护 | 往 `FIELDS` 插一个 schema 里不存在的 `ghostKey` | ✅ 立即红（`deepStrictEqual` 列出多余键） |
| spec() 守护的解析计数断言 | 断言按「20 个数值键」精确比对（而非「≥N」） | ✅ spec 结构变更或探针写错时先炸，不会静默漏检 |
| 守护测试是否读错文件路径 | `entry.test.mjs` 用 `join(here,'..','package.json')` 相对自身定位 | ✅ 从任意 cwd 调用都读到本仓 package.json |

> 探针保真度记录：本轮一次「ghostKey 未被抓」是**探针自己写错**（bash heredoc 里
> 的中文/转义导致 `python3 - <<'PY'` 的替换静默未生效，`grep -c ghostKey` 返回 0 当场暴露）。
> 改用落盘脚本文件后复现成功。判据：**先自证探针真的改动了目标文件，再解读测试结果。**

## 二、停止线判定

| 轮次 | P0 | P1 | P2 | P3 |
|---|---|---|---|---|
| Round 5（0.1.5-rc.1 适配） | 0 | 2 项修复 | 1 项修复 | 0 |
| Round 6（换角度复核） | 0 | 0（1 项探针缺陷修掉） | 0 | 0 |

Round 6 未发现新的 P0/P1/P2，且修复面全部通过定向扰动反证。加上 Round 5 本身
零 P0，**达成「连续两轮零 P0/P1/P2」停止线**，转按需审计模式。

最终证据：`npm test` = entry 10/10 + `node --test` 33/33 + smoke 156/156 + client-smoke 33/33，
全部在 `@deepseek-ai/dsh-tools@0.1.5-rc.1` 真实依赖下运行。
