/**
 * dsh-mem0-plugins — Mem0 持久记忆 bundle 插件（Host 半）。
 *
 * 把 hermes mem0 插件的「自动记忆」移植到 DSH：
 *
 * 1. 工具驱动召回：使用说明节强引导模型在回答一切依赖记忆的问题前先调
 *    `mem0_search`（UI 工具卡即召回动作的可见呈现）；整串仅为应答/问候/推进的
 *    琐碎消息（与 guards.js 词表同一标准）在使用说明节中明确豁免搜索；
 *    工具内部先蒸馏长文本提炼检索意图（超时/双飞/漂移防护/失败回退原文），
 *    再语义搜索；英文空结果自动用最近中文上下文兜底重搜。
 * 2. 自动写入：`session/event` 按 source.kind 捕获真人输入与模型回复；
 *    claimed/turn-stopping 配对出队入潮浪并忆缓冲，合并为一次 infer:true
 *    批量写入，服务端 LLM 抽取事实。纯 JSON 消息替换占位符防污染。
 * 3. 四个工具：mem0_search / mem0_add / mem0_update / mem0_delete；
 *    update/delete 后 best-effort 上报 /evolve/feedback。
 * 4. 可靠性：熔断器、有界队列、连接级重试、dispose 兜底冲刷。
 *
 * 召回形态说明：dsh 平台在「消息回显」后没有内容注入钩子（详见
 * docs/COMPARISON.md「平台时序约束」），所以召回走显式工具链路而非后台注入——
 * 模型先调 mem0_search，工具卡让召回动作对用户可见。
 *
 * 设置命名空间 mem0 与浏览器半共享；设置页改动即时生效（applies=live）。
 */
import z from '@deepseek-ai/schemastery'
import { isTrivialPrompt } from './guards.js'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { CircuitBreaker, Mem0Client, isClientError, retuneBreaker } from './backend.js'
import { TidalCoalescer } from './coalesce.js'
import { redactSecrets } from './redact.js'
import { distillQuery } from './distill.js'
import { buildResultList, truncateOutput } from './formatting.js'

/** Cordis 插件短名（路由/日志用）。 */
export const name = 'mem0'

/** Settings 命名空间（浏览器卡片与 host 共用同一字符串）。
 * dsh 0.1.2-alpha 起 settingsNamespace() brand 辅助已从 dsh-settings 移除；
 * 命名空间改为在 settings.register/installSection 处校验（小写连字符标识符）。 */
export const MEM0_SETTINGS_NAMESPACE = 'mem0'

/** 需要工具注册表与提示词注册表就绪再 apply；agents 保证补注册时 registry 可用。 */
export const inject = ['tools', 'systemPrompt', 'agents']

/** 设置命名空间的字段模式（也是 Settings 页面渲染/校验的依据）。 */
export const Config = z.object({
  enabled: z.boolean().default(true),
  host: z.string().default('http://127.0.0.1:8888'),
  apiKey: z.string().default(''),
  userId: z.string().default('dsh-user'),
  agentId: z.string().default('dsh'),
  forceRecallStep: z.boolean().default(true),
  topK: z.number().step(1).min(1).max(50).default(10),
  rerank: z.boolean().default(false),
  distillEnabled: z.boolean().default(true),
  distillMinChars: z.number().step(1).min(1).max(100000).default(500),
  distillInputMaxChars: z.number().step(1).min(200).max(200000).default(8000),
  distillBaseUrl: z.string().default(''),
  distillApiKey: z.string().default(''),
  distillModel: z.string().default('Qwen3.5-9B'),
  distillTimeoutMs: z.number().step(1).min(1000).max(600000).default(90000),
  distillRetryAfterMs: z.number().step(1).min(500).max(120000).default(20000),
  syncEnabled: z.boolean().default(true),
  // 上传脱敏闸（B 组，memorax 吸收）：写服务端前对 user/assistant 文本打码 secrets
  redactEnabled: z.boolean().default(true),
  feedbackEnabled: z.boolean().default(true),
  coalesceEnabled: z.boolean().default(true),
  coalesceIdleMs: z.number().step(1).min(500).max(300000).default(5000),
  coalesceWindowMs: z.number().step(1).min(1000).max(600000).default(15000),
  coalesceMaxTurns: z.number().step(1).min(1).max(50).default(5),
  coalesceMaxChars: z.number().step(1).min(200).max(200000).default(4000),
  fastpathChars: z.number().step(1).min(200).max(200000).default(2000),
  // 单条超长消息切片（2026-08-29 大 payload 教训：skill review 子代理 13202
  // chars 单条直写 → 服务端分块按消息粒度不拆单条 → chunk 超 context_window →
  // LLM 截断 → 502）。超过 sliceThreshold 的 user/assistant 消息按段落切成
  // ≤slicePieceChars 的多条消息，全量保留；服务端逐条分块、accumulated 合并。
  sliceThreshold: z.number().step(1).min(200).max(200000).default(8000),
  slicePieceChars: z.number().step(1).min(200).max(200000).default(2000),
  // 潮浪桶存活上限（2026-08-29 毒桶事故）：超龄且「服务端明确拒绝过」才丢弃；
  // 连接级失败不计龄（宕机不丢）。此前注释称可配置但三处接线缺失，设置页调不到。
  maxBucketAgeMs: z.number().step(1).min(60000).max(7200000).default(1800000),
  queueMaxLen: z.number().step(1).min(5).max(1000).default(50),
  breakerThreshold: z.number().step(1).min(1).max(100).default(5),
  breakerCooldownMs: z.number().step(1).min(1000).max(3600000).default(120000),
  requestTimeoutMs: z.number().step(1).min(1000).max(900000).default(420000),
  // 工具输出硬化（2026-08-26，详见 plan/tool-output-hardening）：
  // 总行数上限是保险丝（topK≤50 时日常不触发）；常用的是单条截断与紧凑格式。
  outputMaxLines: z.number().step(1).min(10).max(2000).default(200), // 工具回执总行数上限
  outputMaxKb: z.number().step(1).min(1).max(500).default(50), // 工具回执总字节上限（KB 单位，人性化）
  itemMaxChars: z.number().step(1).min(50).max(10000).default(1000) // 单条记忆文本上限（字符）
})

const METADATA_CHANNEL = 'dsh'

/** 第一步强制搜索提醒（方案 B）：注入为 plugin-source 用户消息，随本轮进入模型上下文。
 * 消息带 form:'notice'：客户端把它渲染为「上下文注入·记忆提醒」行，折叠状态下摘要
 * （source.summary）即直接可见——用户一眼能看到『必须先调 mem0_search』，展开可见全文。
 * （2026-08-23 实测：无 form 的 plugin 消息走 opaque 呈现，UI 折叠行无摘要、文本
 * 要点开才见，用户感知为「没有提醒」；notice 形态是该 UI 专为可读提醒设计的呈现。）
 * （2026-08-25 豁免双写：提醒文本自身也带 SOLE EXCEPTION——本提醒虽只在非琐碎轮
 * 注入，但会留在会话历史里；后续琐碎轮回看历史时若只见无条件 "Do not skip"，
 * 模型可能照样搜索并扩写查询。词表与 guards.js / usage 节 / 工具描述保持一致。） */
const RECALL_REMINDER = (
  '[mem0 requirement] This step MUST call mem0_search before producing any final answer. ' +
  'Run one or several searches with different wording as needed, then answer using the ' +
  'recalled memories together with your own knowledge. Do not skip the search. ' +
  'SOLE EXCEPTION: if THIS user message is itself only a bare acknowledgement, greeting or ' +
  'continuation carrying no question or task of its own (好的、嗯、收到、明白了、继续、下一步、' +
  '开始吧、ok、continue、thanks), skip mem0_search and answer directly — the mandate applies ' +
  'again the moment real content appears (e.g. 继续帮我看看那个报错). ' +
  'CRITICAL LANGUAGE RULE: query language MUST match the user message language — ' +
  'if the user writes in Chinese, search in Chinese (e.g. Chinese keywords); ' +
  'NEVER translate Chinese to English; keep proper nouns verbatim. ' +
  'Most memories are in Chinese, English-only queries will miss them.'
)

/** 数值防御性收敛（外部编辑 settings.yaml 时兜底）。 */
function clampInt(value, min, max, fallback) {
  const n = Math.trunc(Number(value))
  if (!Number.isFinite(n)) return fallback
  return Math.min(max, Math.max(min, n))
}

function textOfBlocks(blocks) {
  if (!Array.isArray(blocks)) return ''
  return blocks
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
    .trim()
}

function toolOk(data) {
  return { ok: true, ...(data === undefined ? {} : { data }) }
}

function toolFail(error) {
  return { ok: false, error: String((error && error.message) || error) }
}

/**
 * Cordis apply：注册设置命名空间、四个 mem0 工具、自动召回与自动写回接线。
 * @param {object} ctx - cordis 上下文（已注入 tools/systemPrompt）。
 * @param {object} config - composition base 层配置（patch 行的 config）。
 */
export function apply(ctx, config = {}) {
  let current = () => config
  // dsh 0.1.2-alpha：独立 installSettingsSection 帮助函数已从 dsh-settings 移除，
  // 同样的接线改为 provider 上的 settings.installSection(owner, ns, schema, entry, hooks)
  // （源码级核对：register(base=entry) → setSource(scope.get) → 卸载回落 effect →
  // onChange() 同步首发 → scope.watch 持续通知）。
  // hooks 在 inject 回调内同步执行——此处 onChange 为空操作、setSource 只赋值上方
  // 已声明的 current，无 TDZ 风险，故保持原位置。
  ctx.inject(['settings'], (sctx) => {
    sctx.settings.installSection(ctx, MEM0_SETTINGS_NAMESPACE, Config, config, {
      setSource: (source) => {
        current = source
      },
      onChange: () => {
        // 各消费点每 tick / 每次调用读取 current()，无需主动刷新
      }
    })
  })

  /** 带前缀的结构化日志（保持 ctx.logger 方法调用形式）。
   * dsh 的 LoggerService 默认不透出 stdout——用户可见信息（潮浪合并收益、
   * 熔断告警）需双通道：ctx.logger 落内部日志 + console.log 直出宿主 stdout */
  const log = {
    debug: (message) => ctx.logger.debug('[dsh-mem0] ' + message),
    info: (message) => {
      ctx.logger.info('[dsh-mem0] ' + message)
      console.log('[dsh-mem0] ' + message)
    },
    warn: (message) => {
      ctx.logger.warn('[dsh-mem0] ' + message)
      console.warn('[dsh-mem0] ' + message)
    }
  }

  const breaker = new CircuitBreaker()
  /** 归一化后的当前生效配置。 */
  const spec = () => {
    const value = current() || {}
    return {
      enabled: value.enabled !== false, // 默认值 true：配置即启用；显式关闭才为 false
      host: String(value.host || '').trim(),
      apiKey: String(value.apiKey || '').trim(),
      userId: String(value.userId || '').trim() || 'dsh-user',
      agentId: String(value.agentId || '').trim() || 'dsh',
      forceRecallStep: value.forceRecallStep !== false,
      topK: clampInt(value.topK, 1, 50, 10),
      rerank: value.rerank === true,
      distillEnabled: value.distillEnabled !== false,
      distillMinChars: clampInt(value.distillMinChars, 1, 100000, 500),
      distillInputMaxChars: clampInt(value.distillInputMaxChars, 200, 200000, 8000),
      distillBaseUrl: String(value.distillBaseUrl || '').trim(),
      distillApiKey: String(value.distillApiKey || '').trim(),
      distillModel: String(value.distillModel || '').trim() || 'Qwen3.5-9B',
      distillTimeoutMs: clampInt(value.distillTimeoutMs, 1000, 600000, 90000),
      distillRetryAfterMs: clampInt(value.distillRetryAfterMs, 500, 120000, 20000),
      syncEnabled: value.syncEnabled !== false,
      redactEnabled: value.redactEnabled !== false,
      feedbackEnabled: value.feedbackEnabled !== false,
      coalesceEnabled: value.coalesceEnabled !== false,
      coalesceIdleMs: clampInt(value.coalesceIdleMs, 500, 300000, 5000),
      coalesceWindowMs: clampInt(value.coalesceWindowMs, 1000, 600000, 15000),
      coalesceMaxTurns: clampInt(value.coalesceMaxTurns, 1, 50, 5),
      coalesceMaxChars: clampInt(value.coalesceMaxChars, 200, 200000, 4000),
      fastpathChars: clampInt(value.fastpathChars, 200, 200000, 2000),
      sliceThreshold: clampInt(value.sliceThreshold, 200, 200000, 8000),
      slicePieceChars: clampInt(value.slicePieceChars, 200, 200000, 2000),
      maxBucketAgeMs: clampInt(value.maxBucketAgeMs, 60000, 7200000, 1800000),
      queueMaxLen: clampInt(value.queueMaxLen, 5, 1000, 50),
      breakerThreshold: clampInt(value.breakerThreshold, 1, 100, 5),
      breakerCooldownMs: clampInt(value.breakerCooldownMs, 1000, 3600000, 120000),
      requestTimeoutMs: clampInt(value.requestTimeoutMs, 1000, 900000, 420000),
      outputMaxLines: clampInt(value.outputMaxLines, 10, 2000, 200),
      outputMaxBytes: clampInt(value.outputMaxKb, 1, 500, 50) * 1024,
      itemMaxChars: clampInt(value.itemMaxChars, 50, 10000, 1000)
    }
  }

  // 配置变化时同步熔断参数（阈值/冷却时间可热调）
  ctx.effect(() => ctx.on('settings/updated', (ns) => {
    if (ns !== MEM0_SETTINGS_NAMESPACE) return
    const s = spec()
    retuneBreaker(breaker, s.breakerThreshold, s.breakerCooldownMs)
  }), 'mem0:breaker-tuning')

  const ready = (s) => {
    if (!s.enabled) throw new Error('Mem0 插件未启用：请到「设置 → 插件配置 → Mem0 记忆」打开开关')
    if (!s.host) throw new Error('Mem0 server 地址未配置：请到「设置 → 插件配置 → Mem0 记忆」填写 URL')
  }

  const clientFor = (s) =>
    new Mem0Client({ host: s.host, apiKey: s.apiKey, timeoutMs: s.requestTimeoutMs, breaker })

  // 幂等保护：同一 agent 只挂一次 hook。agent/created 与 apply 期补注册（下述
  // existingAgents）两条路径可能先后到达同一 agent（比如插件 apply 前 agent 已
  // 创建、created 事件已错过，apply 时才补挂），重复注册会让 pre-step 双触发。
  const hookedAgents = new WeakSet()

  /** 给单个 agent 挂 mem0 hook（claimed 捕获 / pre-step 提醒注入 / turn-stopping 写入配对）。
   * 不变量：Agent.id 就是 Session.id（dsh-agent runtime-types：single identity shared with session）。 */
  const installAgentHooks = (agent) => {
    if (!agent || !agent.id || !agent.ctx) {
      log.debug('agent without usable ctx, skipping mem0 hooks')
      return
    }
    if (hookedAgents.has(agent)) return
    hookedAgents.add(agent)
    const sessionId = agent.id
    agent.ctx.on('agent/inbox/claimed', (claimed) => {
      try {
        // 仅记录真人输入文本供 turn-stopping 写入配对（召回已改为工具驱动，不再预取）
        const claimedText = textOfBlocks(claimed.message && claimed.message.content)
        if (claimedText) {
          userByTurn.set(sessionId + '\u0000' + claimed.turn, claimedText)
          capMap(userByTurn)
        }
      } catch (error) {
        log.debug('claimed capture failed: ' + String((error && error.message) || error))
      }
    })
    agent.ctx.on('agent/pre-step', async (payload, next) => {
      // next() 单次调用（2026-08-29 一轮审计 P2-1，同 session-track B 组 P2-2 教训）：
      // cordis waterfall 的 next() 是 `cbs.shift() ?? inner` 链——catch 里再调一次会把
      // 下游全部监听器（含其他插件的注入副作用）原样重放一遍。上游失败原样上抛
      // （不能返回 undefined：运行时读 decision.kind 会 TypeError）；
      // 注入逻辑单独 try/catch，失败返回原 decision。
      let decision
      try {
        decision = await next()
      } catch (error) {
        log.debug('pre-step upstream failed: ' + String((error && error.message) || error))
        throw error
      }
      try {
        if (!decision || decision.kind !== 'enter' || !decision.messages) return decision
        const s = spec()
        if (!s.enabled || !s.host) return decision
        if (s.forceRecallStep !== true) return decision
        // 按「该 step 是否携带新的真人输入」注入而非按 step 编号：
        // 同一 turn 内连续多段用户消息（step=1,5,9…）每一段都要提醒；
        // 工具回执步（payload.messages 无 kind=user）不打扰
        const freshUser = (payload.messages || []).find((m) => m && m.source && m.source.kind === 'user')
        if (!freshUser) return decision
        const firstText = textOfBlocks(freshUser.content)
        if (isTrivialPrompt(firstText)) return decision // 琐碎输入（问候/确认）不打扰
        const reminder = {
          id: globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
            ? globalThis.crypto.randomUUID()
            : ('mem0-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10)),
          role: 'user',
          content: [{ type: 'text', text: RECALL_REMINDER }],
          source: {
            kind: 'plugin',
            plugin: 'dsh-mem0-plugins',
            form: 'notice',
            summary: '【记忆提醒】回答前必须先调 mem0_search（先搜再答）'
          }
        }
        log.debug('recall reminder injected (session=' + sessionId + ')')
        return { kind: 'enter', messages: [...decision.messages, reminder] }
      } catch (error) {
        log.debug('recall reminder injection failed: ' + String((error && error.message) || error))
        return decision
      }
    })
    agent.ctx.on('agent/turn-stopping', (stopping) => {
      try {
        const s = spec()
        const userTurnKey = sessionId + '\u0000' + stopping.turn
        const userText = userByTurn.get(userTurnKey) || lastUserBySession.get(sessionId) || ''
        const assistantEntry = lastAssistantByTurn.get(userTurnKey)
        const assistantText = assistantEntry ? assistantEntry.text : ''
        const interrupted = assistantEntry ? assistantEntry.interrupted === true : false
        lastUserBySession.delete(sessionId)
        userByTurn.delete(userTurnKey)
        lastAssistantByTurn.delete(userTurnKey)
        if (!s.enabled || !s.syncEnabled || !s.host) return
        if (interrupted) {
          log.debug('turn interrupted, skipping memory sync for session ' + sessionId)
          return
        }
        if (!userText && !assistantText) return
        // （2026-08-26 二轮审计）：短路期不再拦截不入队——入队口拦截会让
        // 熔断窗口内的整轮对话永久丢失（捕获缓存已提前 delete 无恢复路径）；
        // coalescer 桶路径已保证短路冲刷不丢（shortCircuited 不消耗 retries），
        // 队列有界（queueMaxLen）不会因短路堆积，统一交给 coalescer 挂桶等冷却。
        coalescer.enqueue({ userId: s.userId, sessionId, userContent: userText, assistantContent: assistantText })
      } catch (error) {
        log.debug('enqueue failed: ' + String((error && error.message) || error))
      }
    })
    log.debug('mem0 hooks installed for agent ' + sessionId)
  }

  // 全局监听 agent/created（载荷带 agent，实测确认），在 agent 级 scoped ctx 上注册
  // claimed/turn-stopping——这两个事件的运行时载荷仅有 {message,turn}/{turn,signal}，
  // 没有 agent 字段（dsh-agent-loop emit 实现与 .d.ts 声明漂移），会话标识从闭包拿。
  ctx.effect(() => ctx.on('agent/created', (payload) => {
    installAgentHooks(payload && payload.agent)
  }), 'mem0:agent-hooks')
  // ---------------------------------------------------------------------------
  // 自动写入：session/event 捕获配对 + turn-stopping 出队 + 潮浪并忆
  // ---------------------------------------------------------------------------
  const coalescer = new TidalCoalescer({
    resolve: () => {
      const s = spec()
      return {
        enabled: s.coalesceEnabled,
        redactEnabled: s.redactEnabled,
        idleMs: s.coalesceIdleMs,
        windowMs: s.coalesceWindowMs,
        maxTurns: s.coalesceMaxTurns,
        maxChars: s.coalesceMaxChars,
        fastpathChars: s.fastpathChars,
        sliceThreshold: s.sliceThreshold,
        slicePieceChars: s.slicePieceChars,
        maxBucketAgeMs: s.maxBucketAgeMs,
        queueMaxLen: s.queueMaxLen,
        // 供 coalescer 区分「同段连续故障」与「跨冷却的新故障段」：
        // 半开窗口的真实失败间隔≈冷却时长，超过即重置重试计数
        cooldownMs: s.breakerCooldownMs
      }
    },
    queueMaxLen: 50,
    addFn: async ({ userId, messages }) => {
      const s = spec()
      await clientFor(s).addMessages(messages, {
        userId: s.userId || userId,
        agentId: s.agentId,
        infer: true,
        metadata: { channel: METADATA_CHANNEL }
      })
    },
    log
  })

  /** 真人输入文本（按 session 覆盖）；助手回复文本（按 session+turn 覆盖）。 */
  const CAP_LIMIT = 256
  const lastUserBySession = new Map()
  const userByTurn = new Map()
  const lastAssistantByTurn = new Map()
  const capMap = (map) => {
    while (map.size > CAP_LIMIT) map.delete(map.keys().next().value)
  }

  // 不变量：Agent.id === session.id（dsh-agent 单一身份）；此监听以 session.id 写键，
  // 与 agent/created 闭包里的 sessionId（=agent.id）同源，配对必然命中。
  ctx.effect(() => ctx.on('session/event', (session, event) => {
    try {
      // 真实载荷（2026-08-29 rc.2 源码 + 真机持久日志双实证，D5 教训）：字段嵌 data 层——
      // user/message 的 data 即消息本体（append(message) 直存 {role,content,source,id}）；
      // assistant/message 的 data 为 {turn,step,message,usage?,interrupted?}。
      // 归一化兼容旧平铺形状（无 data 时回落 event 本体）。
      const p = (event && event.data) ? event.data : event
      if (event.type === 'user/message') {
        const message = p.message || p
        // 只认真人输入：plugin 注入的通知与 tool 回执虽是 user 角色，但不是用户的话
        if (!message || !message.source || message.source.kind !== 'user') return
        const text = textOfBlocks(message.content)
        if (!text) return
        lastUserBySession.set(session.id, text)
        capMap(lastUserBySession)
      } else if (event.type === 'assistant/message') {
        const message = p.message
        if (!message || !message.source || message.source.kind !== 'model') return
        const text = textOfBlocks(message.content)
        if (!text) return
        // 中断的部分输出不是持久对话真相（hermes #15218）：标记随文本入栈，出队时跳过
        lastAssistantByTurn.set(session.id + '\u0000' + p.turn, {
          text,
          interrupted: p.interrupted === true
        })
        capMap(lastAssistantByTurn)
      }
    } catch (error) {
      log.debug('capture failed: ' + String((error && error.message) || error))
    }
  }), 'mem0:capture-listener')

  // 冲刷节拍：排空队列 → 冲刷到期桶；dispose 时 clearInterval + 兜底冲刷全部桶
  ctx.effect(() => {
    const timer = setInterval(() => {
      try {
        coalescer.drain()
        coalescer.flushDue()
      } catch (error) {
        log.debug('coalesce tick failed: ' + String((error && error.message) || error))
      }
    }, 300)
    timer.unref && timer.unref()
    return () => {
      clearInterval(timer)
      lastUserBySession.clear()
      userByTurn.clear()
      lastAssistantByTurn.clear()
      // 先标记卸载：此后失败的 in-flight 直写降级直接诚实丢弃（不再回插无人
      // 冲刷的桶，2026-09-01 审计 P3），再冲剩余桶；返回冲刷 promise：
      // cordis teardown 会 await，关停时 in-flight 写入不丢
      coalescer.dispose()
      return coalescer.flushAll('dispose')
    }
  }, 'mem0:coalesce-tick')

  // ---------------------------------------------------------------------------
  // 常驻使用说明节（enabled 且已配置时出现）
  // ---------------------------------------------------------------------------
  ctx.effect(() => ctx.systemPrompt.section({
    name: 'mem0:usage',
    order: 150,
    text: () => {
      const s = spec()
      if (!s.enabled || !s.host) return ''
      const rerankNote = s.rerank ? ' Reranking is enabled for searches.' : ''
      return (
        '# Mem0 Memory\n' +
        'Active (self-hosted). User: ' + s.userId + '.' + rerankNote + '\n' +
        'You have persistent memory of this user from past conversations. ' +
        'BEFORE answering anything that could depend on prior context (preferences, facts, ' +
        'history, people, projects, past decisions), you MUST call mem0_search first — ' +
        'do not rely on the chat window alone, and never claim there is no memory without ' +
        'having searched.\n' +
        'SOLE EXCEPTION — bare acknowledgements/continuations: when the ENTIRE user message is only a short ' +
        'acknowledgement, greeting or continuation carrying no question or task of its own ' +
        '(e.g. 好的、嗯、收到、明白了、继续、下一步、开始吧、ok、continue、thanks), skip mem0_search and answer ' +
        'directly — there is nothing memory-dependent to recall. The moment the message carries any actual ' +
        'request or content (e.g. 继续帮我看看那个报错), the search requirement applies again.\n' +
        'For multi-part or multi-hop questions, run several searches with different wording ' +
        'and follow up on what earlier results reveal; one search is rarely enough.\n' +
        'LANGUAGE RULE (critical): mem0_search query MUST be in the SAME language as the user message. ' +
        'Most memories are in Chinese — if the user writes in Chinese, you MUST search in Chinese ' +
        '(e.g. 中文关键词). NEVER translate Chinese keywords to English; keep proper nouns verbatim. ' +
        'English-only queries will miss Chinese memories.\n' +
        'ROUTE BY CONTENT NATURE, NOT VERBS: words like remember/recall/update do not decide ' +
        'whether to search or store — judge what the content IS (preference / procedure / project ' +
        'fact / decision => mem0_add; question about past context => mem0_search). If the target ' +
        'is ambiguous, ask one focusing question first.\n' +
        'Tools: mem0_search to find memories, mem0_add to store durable facts verbatim the ' +
        'moment the user states them, mem0_update and mem0_delete to correct or forget by ID.'
      )
    }
  }), 'mem0:usage-section')

  // ---------------------------------------------------------------------------
  // 四个模型工具
  // ---------------------------------------------------------------------------
  const TOOL_OUTPUT_SCHEMA = {
    type: 'object',
    additionalProperties: false,
    properties: {
      ok: { type: 'boolean', required: true },
      error: { type: 'string' },
      data: { type: 'json' }
    }
  }

  const renderToolValue = (value) => {
    if (!value || value.ok !== true) {
      return 'Error: ' + ((value && value.error) || 'unknown error')
    }
    const data = value.data
    if (data === undefined || data === null) return 'OK'
    if (typeof data === 'string') return data
    try {
      return JSON.stringify(data, null, 2)
    } catch {
      return String(data)
    }
  }

  // 工具输出硬化（2026-08-26）：
  // · mem0_search 专用 render——紧凑行格式（省 token、id 行内可引用、age 带时间感、
  //   类别来自服务端已有 metadata）+ 单条截断（itemMaxChars，按码点不拆半）；
  // · 其余工具渲染统一套 truncateOutput 兜底（总行数/总字节上限，保险丝语义）。
  // data 结构不变（count/results[{id,memory,score,…}]），只改模型可见层；
  // score/created_at 缺失（graph 片段）时紧凑行对应省略。
  const renderSearchResult = (value) => {
    if (!value || value.ok !== true) {
      return 'Error: ' + ((value && value.error) || 'unknown error')
    }
    const s = spec()
    const data = value.data
    if (typeof data === 'string') return data // 空结果等纯文本回执原样透传
    if (data === undefined || data === null) return 'OK'
    let text
    try {
      text = buildResultList(data.results, { itemMaxChars: s.itemMaxChars })
    } catch {
      text = JSON.stringify(data, null, 2) // 防御兜底：格式器异常不吞回执
    }
    return truncateOutput(text, { maxLines: s.outputMaxLines, maxBytes: s.outputMaxBytes })
  }

  const renderTruncated = (_args, value) => {
    const s = spec()
    return [{ type: 'text', text: truncateOutput(renderToolValue(value), { maxLines: s.outputMaxLines, maxBytes: s.outputMaxBytes }) }]
  }

  const registerTool = (definition) => {
    ctx.tools.register(definition)
  }

  // -- mem0_search ------------------------------------------------------------
  registerTool(defineTool({
    name: 'mem0_search',
    description:
      "Search the user's persistent memories by meaning; returns facts ranked by relevance. " +
      'Use this before answering any question that may depend on what you know about the user ' +
      '(preferences, facts, history, people, projects, past decisions). For multi-part or ' +
      'multi-hop questions, call it several times — vary the wording and run follow-up searches ' +
      'on what earlier results reveal; one search is rarely enough. ' +
      'Memories are historical clues, not proof of the current state — verify against the current code/config before citing. ' +
      'Skip this search ONLY when the entire user message is a bare acknowledgement/greeting/continuation ' +
      'with no question or task of its own (好的、嗯、收到、继续、ok、continue) — any real content requires the search. ' +
      'CRITICAL: query language MUST match user message language — if user writes Chinese, query MUST be Chinese (e.g. 中文关键词), never translate to English; most memories are Chinese so English-only queries will miss.',
    parameters: {
      query: { type: 'string', required: true, description: 'What to search for. MUST use SAME language as user input — Chinese input => Chinese query, do NOT translate. Keep proper nouns verbatim.' },
      top_k: { type: 'integer', description: 'Max results (default from settings, max 50).' },
      rerank: { type: 'boolean', description: 'Rerank results for relevance (server must have a reranker configured).' }
    },
    output: {
      schema: TOOL_OUTPUT_SCHEMA,
      render: (_args, value) => [{ type: 'text', text: renderSearchResult(value) }]
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const s = spec()
      try {
        ready(s)
        if (breaker.open) return toolFail('Mem0 temporarily unavailable (repeated failures); will retry automatically in ' + Math.ceil(breaker.remainingMs / 1000) + 's')
        // 工具内蒸馏：query 超过阈值（用户可能把整段日志/长文直接丢给搜索）先提炼
        // 检索意图（超时/双飞/漂移防护/失败回退原文全套保留），再语义搜索。
        // signal 透传蒸馏：用户中断生成后，进行中的蒸馏请求随之取消（不再白跑）。
        const query = await distillQuery(args.query, {
          enabled: s.distillEnabled,
          minChars: s.distillMinChars,
          inputMaxChars: s.distillInputMaxChars,
          baseUrl: s.distillBaseUrl,
          apiKey: s.distillApiKey,
          model: s.distillModel,
          timeoutMs: s.distillTimeoutMs,
          retryAfterMs: s.distillRetryAfterMs
        }, (info) => log.debug(info), exec.signal)
        let results = await clientFor(s).search({
          query,
          filters: { user_id: s.userId },
          topK: args.top_k !== undefined ? clampInt(args.top_k, 1, 50, s.topK) : s.topK,
          rerank: typeof args.rerank === 'boolean' ? args.rerank : s.rerank,
          signal: exec.signal
        })
        // （2026-08-25 审计 IDX3：原「英文空结果时取最近中文用户原文重搜」的兜底已删——
        // 它遍历的是全部会话的捕获缓存，多会话并行时会拿无关会话的文本当查询，
        // 静默召回错位内容；中文查询质量已由 usage 节 + 提醒 + 工具描述三处强约束。）
        if (!results || results.length === 0) return toolOk('No relevant memories found.')
        return toolOk({
          count: results.length,
          results: results.map((item) => {
            // 注意：不能给缺失字段写 undefined——dsh-session 要求工具 data 是
            // lossless JSON，undefined 无法无损 JSON 往返，会直接报
            // “value is not lossless JSON”。缺失字段应整体省略，而不是置 undefined。
            const row = {
              id: item && item.id ? String(item.id) : '',
              memory: item && typeof item.memory === 'string' ? item.memory : ''
            }
            if (item && typeof item.score === 'number' && Number.isFinite(item.score)) row.score = item.score
            if (item && typeof item.created_at === 'string') row.created_at = item.created_at
            if (item && typeof item.updated_at === 'string') row.updated_at = item.updated_at
            if (item && item.metadata && typeof item.metadata === 'object') row.metadata = item.metadata
            return row
          })
        })
      } catch (error) {
        // 熔断计数由 Mem0Client 内部统一记录，这里不重复计
        return toolFail(error)
      }
    }
  }))

  // -- mem0_add -----------------------------------------------------------------
  registerTool(defineTool({
    name: 'mem0_add',
    description:
      'Store a durable fact about the user, verbatim (no LLM extraction). Call this the moment ' +
      'the user states a lasting preference, correction, decision, or personal detail worth ' +
      "recalling on future turns — don't wait to be asked to remember. Skip transient chit-chat " +
      "and facts you've already stored. " +
      'Judge WHAT to store by content nature, four categories: preferences ("我喜欢中文简短回答"), ' +
      'procedures ("先测试再提 PR，帮我记住" — store the procedure even though it carries the word "remember"), ' +
      'project facts ("部署在 X，compose 项目名 Y"), decisions with their reasons. ' +
      'Do NOT store task-scoped temporary instructions or one-off plans — they serve only the current turn.',
    parameters: {
      content: { type: 'string', required: true, description: 'The fact to store.' }
    },
    output: {
      schema: TOOL_OUTPUT_SCHEMA,
      render: renderTruncated
    },
    async execute(args, exec) {
      const s = spec()
      try {
        ready(s)
        if (breaker.open) return toolFail('Mem0 temporarily unavailable; will retry automatically in ' + Math.ceil(breaker.remainingMs / 1000) + 's')
        // （2026-08-29 审计：mem0_add 直写路径曾不过脱敏闸——闸只在潮浪 route()
        // 内生效；模型把含 key 文本直接 add 会明文入库。与潮浪同语义：命中替换
        // [REDACTED:label] 再落库，宁误杀不漏放。）
        let content = String(args.content || '')
        if (s.redactEnabled !== false) {
          const red = redactSecrets(content)
          if (red.hits.length) {
            content = red.text
            log.warn('mem0_add redacted ' + red.hits.length + ' secret label(s) [' +
              red.hits.map((h) => h.label + 'x' + h.count).join(', ') + '] before store')
          }
        }
        await clientFor(s).addMessages(
          [{ role: 'user', content }],
          { userId: s.userId, agentId: s.agentId, infer: false, metadata: { channel: METADATA_CHANNEL }, signal: exec.signal }
        )
        return toolOk('Fact stored.')
      } catch (error) {
        return toolFail(error)
      }
    }
  }))

  // -- mem0_update ---------------------------------------------------------------
  registerTool(defineTool({
    name: 'mem0_update',
    description:
      "Replace the text of an existing memory by its ID (take the ID from a mem0_search result). " +
      'Use when a stored fact has changed or was wrong — correct it in place instead of adding a duplicate.',
    parameters: {
      memory_id: { type: 'string', required: true, description: 'Memory UUID to update.' },
      text: { type: 'string', required: true, description: 'New text content.' }
    },
    output: {
      schema: TOOL_OUTPUT_SCHEMA,
      render: renderTruncated
    },
    async execute(args, exec) {
      const s = spec()
      try {
        ready(s)
        if (breaker.open) return toolFail('Mem0 temporarily unavailable; will retry automatically in ' + Math.ceil(breaker.remainingMs / 1000) + 's')
        const result = await clientFor(s).updateMemory(args.memory_id, args.text, exec.signal)
        if (s.feedbackEnabled) {
          void clientFor(s).reportFeedback(args.memory_id, 'correction', { source: 'auto', note: args.text }, log)
        }
        return toolOk(result.result || 'Memory updated.')
      } catch (error) {
        if (error && error.status === 404) return toolFail('Memory not found: ' + args.memory_id)
        if (error && error.status === 400) return toolFail('Mem0 rejected the request: ' + String(error.message || ''))
        if (isClientError(error)) return toolFail('Memory not found: ' + args.memory_id)
        return toolFail(error)
      }
    }
  }))

  // -- mem0_delete -----------------------------------------------------------------
  registerTool(defineTool({
    name: 'mem0_delete',
    description:
      'Delete a memory by its ID (take the ID from a mem0_search result). Use when a stored fact ' +
      'is obsolete or the user asks you to forget it; prefer mem0_update if the fact merely changed.',
    parameters: {
      memory_id: { type: 'string', required: true, description: 'Memory UUID to delete.' }
    },
    output: {
      schema: TOOL_OUTPUT_SCHEMA,
      render: renderTruncated
    },
    async execute(args, exec) {
      const s = spec()
      try {
        ready(s)
        if (breaker.open) return toolFail('Mem0 temporarily unavailable; will retry automatically in ' + Math.ceil(breaker.remainingMs / 1000) + 's')
        const result = await clientFor(s).deleteMemory(args.memory_id, exec.signal)
        if (s.feedbackEnabled) {
          void clientFor(s).reportFeedback(args.memory_id, 'useless', { source: 'auto' }, log)
        }
        return toolOk(result.result || 'Memory deleted.')
      } catch (error) {
        if (error && error.status === 404) return toolFail('Memory not found: ' + args.memory_id)
        if (error && error.status === 400) return toolFail('Mem0 rejected the request: ' + String(error.message || ''))
        if (isClientError(error)) return toolFail('Memory not found: ' + args.memory_id)
        return toolFail(error)
      }
    }
  }))

  // ---------------------------------------------------------------------------
  // 补注册：插件 apply 时枚举已存在的 agents。
  // dsh 重启后（或任何插件晚于 agent 创建的时序下），agent 的 `agent/created`
  // 事件在插件 `ctx.on` 注册之前已经 emit 过，错过即永久错过——该 agent 上
  // 永远不会挂上 pre-step 提醒（2026-08-25 实测：重启后最早创建的会话第一条
  // 消息无【记忆提醒】，之后创建的会话正常）。host 的 agents registry 可枚举
  // 现存 live agents，apply 尾声统一补挂；与 agent/created 路径共用
  // installAgentHooks（WeakSet 幂等，重复到达不会双触发）。
  const agentsRegistry = ctx.get && typeof ctx.get === 'function' ? ctx.get('agents') : undefined
  const agentsService = agentsRegistry || ctx.agents
  if (agentsService && typeof agentsService.list === 'function') {
    try {
      const existing = agentsService.list()
      if (existing && existing.length) {
        for (const agent of existing) installAgentHooks(agent)
        log.debug('backfilled mem0 hooks for ' + existing.length + ' pre-existing agent(s)')
      }
    } catch (error) {
      log.debug('existing-agent backfill failed: ' + String((error && error.message) || error))
    }
  }
}
