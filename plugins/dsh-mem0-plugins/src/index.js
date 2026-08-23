/**
 * dsh-mem0-plugins — Mem0 持久记忆 bundle 插件（Host 半）。
 *
 * 把 hermes mem0 插件的「自动记忆」移植到 DSH：
 *
 * 1. 工具驱动召回：使用说明节强引导模型在回答一切依赖记忆的问题前先调
 *    `mem0_search`（UI 工具卡即召回动作的可见呈现）；工具内部先蒸馏长文本
 *    提炼检索意图（超时/双飞/漂移防护/失败回退原文），再语义搜索。
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
import { installSettingsSection, settingsNamespace } from '@deepseek-ai/dsh-settings'
import { isTrivialPrompt } from './guards.js'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { CircuitBreaker, Mem0Client, isClientError, retuneBreaker } from './backend.js'
import { TidalCoalescer } from './coalesce.js'
import { distillQuery } from './distill.js'

/** Cordis 插件短名（路由/日志用）。 */
export const name = 'mem0'

/** Settings 命名空间（浏览器卡片与 host 共用同一字符串）。 */
export const MEM0_SETTINGS_NAMESPACE = settingsNamespace('mem0')

/** 需要工具注册表与提示词注册表就绪再 apply。 */
export const inject = ['tools', 'systemPrompt']

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
  distillBaseUrl: z.string().default('http://10.220.0.35:8090/v1'),
  distillApiKey: z.string().default('devops'),
  distillModel: z.string().default('Qwen3.5-9B'),
  distillTimeoutMs: z.number().step(1).min(1000).max(600000).default(90000),
  distillRetryAfterMs: z.number().step(1).min(500).max(120000).default(20000),
  syncEnabled: z.boolean().default(true),
  feedbackEnabled: z.boolean().default(true),
  coalesceEnabled: z.boolean().default(true),
  coalesceIdleMs: z.number().step(1).min(500).max(300000).default(5000),
  coalesceWindowMs: z.number().step(1).min(1000).max(600000).default(15000),
  coalesceMaxTurns: z.number().step(1).min(1).max(50).default(5),
  coalesceMaxChars: z.number().step(1).min(200).max(200000).default(4000),
  fastpathChars: z.number().step(1).min(200).max(200000).default(2000),
  queueMaxLen: z.number().step(1).min(5).max(1000).default(50),
  breakerThreshold: z.number().step(1).min(1).max(100).default(5),
  breakerCooldownMs: z.number().step(1).min(1000).max(3600000).default(120000),
  requestTimeoutMs: z.number().step(1).min(1000).max(900000).default(300000)
})

const METADATA_CHANNEL = 'dsh'

/** 第一步强制搜索提醒（方案 B）：注入为 plugin-source 用户消息，随本轮进入模型上下文。 */
const RECALL_REMINDER = (
  '[mem0 requirement] This step MUST call mem0_search before producing any final answer. ' +
  'Run one or several searches with different wording as needed, then answer using the ' +
  'recalled memories together with your own knowledge. Do not skip the search.'
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
  installSettingsSection(ctx, MEM0_SETTINGS_NAMESPACE, Config, config, {
    setSource: (source) => {
      current = source
    },
    onChange: () => {
      // 各消费点每 tick / 每次调用读取 current()，无需主动刷新
    }
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
      feedbackEnabled: value.feedbackEnabled !== false,
      coalesceEnabled: value.coalesceEnabled !== false,
      coalesceIdleMs: clampInt(value.coalesceIdleMs, 500, 300000, 5000),
      coalesceWindowMs: clampInt(value.coalesceWindowMs, 1000, 600000, 15000),
      coalesceMaxTurns: clampInt(value.coalesceMaxTurns, 1, 50, 5),
      coalesceMaxChars: clampInt(value.coalesceMaxChars, 200, 200000, 4000),
      fastpathChars: clampInt(value.fastpathChars, 200, 200000, 2000),
      queueMaxLen: clampInt(value.queueMaxLen, 5, 1000, 50),
      breakerThreshold: clampInt(value.breakerThreshold, 1, 100, 5),
      breakerCooldownMs: clampInt(value.breakerCooldownMs, 1000, 3600000, 120000),
      requestTimeoutMs: clampInt(value.requestTimeoutMs, 1000, 900000, 300000)
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

  // 全局监听 agent/created（载荷带 agent，实测确认），在 agent 级 scoped ctx 上注册
  // claimed/turn-stopping——这两个事件的运行时载荷仅有 {message,turn}/{turn,signal}，
  // 没有 agent 字段（dsh-agent-loop emit 实现与 .d.ts 声明漂移），会话标识从闭包拿。
  ctx.effect(() => ctx.on('agent/created', (payload) => {
    const agent = payload && payload.agent
    if (!agent || !agent.id || !agent.ctx) {
      log.debug('agent/created without usable agent, skipping mem0 hooks')
      return
    }
    // 不变量：Agent.id 就是 Session.id（dsh-agent runtime-types：single identity shared with session）
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
      try {
        const decision = await next()
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
          role: 'user',
          content: [{ type: 'text', text: RECALL_REMINDER }],
          source: { kind: 'plugin', plugin: 'dsh-mem0-plugins' }
        }
        return { kind: 'enter', messages: [...decision.messages, reminder] }
      } catch (error) {
        log.debug('recall reminder injection failed: ' + String((error && error.message) || error))
        return next()
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
        if (breaker.open) return
        coalescer.enqueue({ userId: s.userId, sessionId, userContent: userText, assistantContent: assistantText })
      } catch (error) {
        log.debug('enqueue failed: ' + String((error && error.message) || error))
      }
    })
  }), 'mem0:agent-hooks')
  // ---------------------------------------------------------------------------
  // 自动写入：session/event 捕获配对 + turn-stopping 出队 + 潮浪并忆
  // ---------------------------------------------------------------------------
  const coalescer = new TidalCoalescer({
    resolve: () => {
      const s = spec()
      return {
        enabled: s.coalesceEnabled,
        idleMs: s.coalesceIdleMs,
        windowMs: s.coalesceWindowMs,
        maxTurns: s.coalesceMaxTurns,
        maxChars: s.coalesceMaxChars,
        fastpathChars: s.fastpathChars,
        queueMaxLen: s.queueMaxLen
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
      if (event.type === 'user/message') {
        const message = event.message
        // 只认真人输入：plugin 注入的通知与 tool 回执虽是 user 角色，但不是用户的话
        if (!message || !message.source || message.source.kind !== 'user') return
        const text = textOfBlocks(message.content)
        if (!text) return
        lastUserBySession.set(session.id, text)
        capMap(lastUserBySession)
      } else if (event.type === 'assistant/message') {
        const message = event.message
        if (!message || !message.source || message.source.kind !== 'model') return
        const text = textOfBlocks(message.content)
        if (!text) return
        // 中断的部分输出不是持久对话真相（hermes #15218）：标记随文本入栈，出队时跳过
        lastAssistantByTurn.set(session.id + '\u0000' + event.turn, {
          text,
          interrupted: event.interrupted === true
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
      coalescer.flushAll('dispose')
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
        'For multi-part or multi-hop questions, run several searches with different wording ' +
        'and follow up on what earlier results reveal; one search is rarely enough.\n' +
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
      'on what earlier results reveal; one search is rarely enough.',
    parameters: {
      query: { type: 'string', required: true, description: 'What to search for.' },
      top_k: { type: 'integer', description: 'Max results (default from settings, max 50).' },
      rerank: { type: 'boolean', description: 'Rerank results for relevance (server must have a reranker configured).' }
    },
    output: {
      schema: TOOL_OUTPUT_SCHEMA,
      render: (_args, value) => [{ type: 'text', text: renderToolValue(value) }]
    },
    isConcurrencySafe: () => true,
    async execute(args, exec) {
      const s = spec()
      try {
        ready(s)
        if (breaker.open) return toolFail('Mem0 temporarily unavailable (repeated failures); will retry automatically in ' + Math.ceil(breaker.remainingMs / 1000) + 's')
        // 工具内蒸馏：query 超过阈值（用户可能把整段日志/长文直接丢给搜索）先提炼
        // 检索意图（超时/双飞/漂移防护/失败回退原文全套保留），再语义搜索
        const query = await distillQuery(args.query, {
          enabled: s.distillEnabled,
          minChars: s.distillMinChars,
          inputMaxChars: s.distillInputMaxChars,
          baseUrl: s.distillBaseUrl,
          apiKey: s.distillApiKey,
          model: s.distillModel,
          timeoutMs: s.distillTimeoutMs,
          retryAfterMs: s.distillRetryAfterMs
        }, (info) => log.debug(info))
        const results = await clientFor(s).search({
          query,
          filters: { user_id: s.userId },
          topK: args.top_k !== undefined ? clampInt(args.top_k, 1, 50, s.topK) : s.topK,
          rerank: typeof args.rerank === 'boolean' ? args.rerank : s.rerank,
          signal: exec.signal
        })
        if (!results || results.length === 0) return toolOk('No relevant memories found.')
        return toolOk({
          count: results.length,
          results: results.map((item) => ({
            id: item && item.id ? String(item.id) : '',
            memory: item && typeof item.memory === 'string' ? item.memory : '',
            score: item && typeof item.score === 'number' ? item.score : 0
          }))
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
      "and facts you've already stored.",
    parameters: {
      content: { type: 'string', required: true, description: 'The fact to store.' }
    },
    output: {
      schema: TOOL_OUTPUT_SCHEMA,
      render: (_args, value) => [{ type: 'text', text: renderToolValue(value) }]
    },
    async execute(args, exec) {
      const s = spec()
      try {
        ready(s)
        if (breaker.open) return toolFail('Mem0 temporarily unavailable; will retry automatically in ' + Math.ceil(breaker.remainingMs / 1000) + 's')
        await clientFor(s).addMessages(
          [{ role: 'user', content: args.content }],
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
      render: (_args, value) => [{ type: 'text', text: renderToolValue(value) }]
    },
    async execute(args, exec) {
      const s = spec()
      try {
        ready(s)
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
      render: (_args, value) => [{ type: 'text', text: renderToolValue(value) }]
    },
    async execute(args, exec) {
      const s = spec()
      try {
        ready(s)
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
}
