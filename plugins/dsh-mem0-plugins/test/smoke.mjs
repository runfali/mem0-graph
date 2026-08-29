/**
 * dsh-mem0-plugins 离线冒烟测试。
 *
 * 运行：node test/smoke.mjs
 * - 真实加载 @deepseek-ai/schemastery、dsh-settings、dsh-tools（经 node_modules 符号链接）
 * - mock cordis ctx（effect/on/inject/settings/tools/systemPrompt/logger）
 * - mock 全局 fetch（按路由应答，统计调用）
 *
 * 验证：
 * 1. apply 全链路注册成功（四工具经真实 defineTool 编译、prompt 节、事件监听、tick）
 * 2. 补注册路径：apply 晚于 agent 创建时枚举现存 agents 补挂 hook（幂等）
 * 3. 自动写入链路：session/event 捕获 → turn-stopping 入队 → tick 冲刷 → POST /memories
 * 4. 工具执行路径：未配置报错 / 搜索成功形态
 * 5. 纯 JSON 剥除与熔断器单元行为
 * 6. dispose 兜底冲刷不抛错
 */
import assert from 'node:assert/strict'
import { apply, Config, MEM0_SETTINGS_NAMESPACE } from '../src/index.js'
import { CircuitBreaker, Mem0HttpError, isClientError, retuneBreaker } from '../src/backend.js'
import { looksLikeJson, sanitizeJsonMessage } from '../src/coalesce.js'
import { distillQuery } from '../src/distill.js'
import { isTrivialPrompt } from '../src/guards.js'

const PASS = []
function ok(label) {
  PASS.push(label)
  console.log('  ✓ ' + label)
}

// ---------------------------------------------------------------------------
// mock fetch 路由器
// ---------------------------------------------------------------------------
const fetchCalls = []
let searchResponse = { results: [{ id: 'm-1', memory: '发哥偏好结论先行的短句回复', score: 0.91 }] }
let addResponse = { results: [{ id: 'new-1', memory: 'x' }], event_id: 'evt-1' }
let failNextNetwork = false

globalThis.fetch = async (url, init = {}) => {
  const path = new URL(url).pathname
  fetchCalls.push({ path, method: init.method || 'GET', body: init.body ? String(init.body) : '' })
  if (failNextNetwork && path === '/search') {
    failNextNetwork = false
    throw new TypeError('fetch failed')
  }
  if (path.endsWith('/chat/completions')) {
    const req = JSON.parse(String(init.body))
    const content = req.messages[0].content
    // 漂移用例：消息含「越南语污染」标记时返回越南语意图
    if (content.includes('DRIFT-CASE')) return jsonResponse(200, { choices: [{ message: { content: 'nhật ký triển khai máy chủ' } }] })
    return jsonResponse(200, { choices: [{ message: { content: JSON.stringify({ intent: '部署端口配置' }) } }] })
  }
  if (path === '/search') return jsonResponse(200, searchResponse)
  if (path === '/memories') {
    if (!init.body) return jsonResponse(400, { detail: 'bad' })
    const body = JSON.parse(String(init.body))
    if (!body.user_id && !body.agent_id) return jsonResponse(400, { detail: 'identifier required' })
    return jsonResponse(200, addResponse)
  }
  if (path.startsWith('/memories/') && init.method === 'PUT') {
    if (String(init.body || '').includes('BAD400')) return jsonResponse(400, { detail: 'memory text too long' })
    return jsonResponse(200, {})
  }
  if (path.startsWith('/memories/') && init.method === 'DELETE') {
    if (path.includes('not-exist')) return jsonResponse(404, { detail: 'Memory not found: not-exist' })
    return jsonResponse(200, {})
  }
  if (path === '/evolve/feedback') return jsonResponse(200, { memory_id: 'x', salience_score: 0.5 })
  return jsonResponse(404, { detail: 'no route' })
}

function jsonResponse(status, payload) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload)
  }
}

// ---------------------------------------------------------------------------
// mock cordis ctx
// ---------------------------------------------------------------------------
function makeCtx(config) {
  const effects = []
  const listeners = new Map()
  const tools = []
  const sections = []
  let scopeValue = Config.resolve ? null : null // placeholder
  const scope = {
    get: () => scopeValue,
    watch: () => () => {}
  }
  const ctx = {
    fiber: { state: 0 },
    logger: { debug: () => {}, info: () => {}, warn: () => {}, error: () => {} },
    effect(fn, label) {
      let disposer
      try {
        disposer = fn()
      } catch (error) {
        throw new Error('effect "' + label + '" threw: ' + error.message)
      }
      effects.push({ label, disposer })
      return typeof disposer === 'function' ? disposer : undefined
    },
    on(event, cb) {
      if (!listeners.has(event)) listeners.set(event, [])
      listeners.get(event).push(cb)
      return () => {
        const arr = listeners.get(event) || []
        const i = arr.indexOf(cb)
        if (i >= 0) arr.splice(i, 1)
      }
    },
    inject(services, cb) {
      cb(ctx)
    },
    settings: {
      register(ns, schema, options) {
        // 模拟宿主解析结果：schema 默认值 + composition base 合并成完整 section
        scopeValue = resolveConfigManually(schema, options.base || {})
        return scope
      }
    },
    tools: { register: (def) => tools.push(def) },
    systemPrompt: { section: (def) => sections.push(def) },
    get(key) {
      // host 服务查找：只暴露 agents registry（补注册路径用）
      if (key === 'agents') return ctx.agentsRegistry
      return undefined
    },
    // agent 级 scoped ctx 工厂：真实环境 agent.ctx.on 只收本 agent 事件
    agentCtxs: [],
    agentsRegistry: { list: () => ctx.registryAgents || [] },
    registryAgents: [],
    createAgent(id) {
      const actx = {
        id,
        ctx: null, // 自引用见下
        listeners: new Map(),
        fiber: { state: 0 },
        logger: ctx.logger,
        on(event, cb) {
          if (!actx.listeners.has(event)) actx.listeners.set(event, [])
          actx.listeners.get(event).push(cb)
          return () => {
            const arr = actx.listeners.get(event) || []
            const i = arr.indexOf(cb)
            if (i >= 0) arr.splice(i, 1)
          }
        }
      }
      actx.ctx = actx
      ctx.agentCtxs.push(actx)
      return actx
    }
  }
  scopeValue = resolveConfigManually(Config, config)
  return { ctx, effects, listeners, tools, sections, setScope: (v) => { scopeValue = v }, getScope: () => scopeValue }
}

/** 手工把 patch base 与 Config 默认值合并（模拟 settings 解析结果）。 */
function resolveConfigManually(schema, entry) {
  const defaults = {
    enabled: true,
    host: 'http://127.0.0.1:8888',
    apiKey: '',
    userId: 'dsh-user',
    agentId: 'dsh',
    topK: 10,
    rerank: false,
    distillEnabled: true,
    distillMinChars: 500,
    distillInputMaxChars: 8000,
    distillBaseUrl: 'http://mock-distill/v1',
    distillApiKey: 'test-key',
    distillModel: 'Qwen3.5-9B',
    distillTimeoutMs: 90000,
    distillRetryAfterMs: 20000,
    syncEnabled: true,
    feedbackEnabled: true,
    coalesceEnabled: true,
    coalesceIdleMs: 5000,
    coalesceWindowMs: 15000,
    coalesceMaxTurns: 5,
    coalesceMaxChars: 4000,
    fastpathChars: 2000,
    queueMaxLen: 50,
    breakerThreshold: 5,
    breakerCooldownMs: 120000,
    requestTimeoutMs: 420000,
    outputMaxLines: 200,
    outputMaxKb: 50,
    itemMaxChars: 1000
  }
  return { ...defaults, ...(entry || {}) }
}

// ===========================================================================
console.log('== 单元：JSON 剥离 ==')
assert.equal(looksLikeJson('{"a":1}'), true); ok('JSON 对象识别')
assert.equal(looksLikeJson('[1,2]'), true); ok('JSON 数组识别')
assert.equal(looksLikeJson('普通文本 {含花括号}'), false); ok('自然语言不误伤')
assert.equal(sanitizeJsonMessage('{"tool":"output"}'), '<JSON 结构化数据，已省略>'); ok('纯消息替换占位符')

console.log('== 单元：熔断器 ==')
{
  const br = new CircuitBreaker({ threshold: 3, cooldownMs: 50 })
  for (let i = 0; i < 2; i++) br.recordFailure(new Error('x'))
  assert.equal(br.open, false); ok('未达阈值不熔断')
  br.recordFailure(new Error('x'))
  assert.equal(br.open, true); ok('达阈值熔断')
  await new Promise((r) => setTimeout(r, 60))
  assert.equal(br.open, false); ok('冷却后自动复位')
  assert.equal(isClientError(new Mem0HttpError(404, '/memories/x', '')), true); ok('404 归类客户端错误')
}

console.log('== 单元：查询蒸馏 ==')
{
  const opts = (over) => ({
    enabled: true, minChars: 10, inputMaxChars: 8000,
    baseUrl: 'http://mock-distill/v1', apiKey: 'test-key', model: 'Qwen3.5-9B',
    timeoutMs: 5000, retryAfterMs: 20000, ...over
  })
  const short = await distillQuery('短消息直查', opts({ minChars: 50 }))
  assert.equal(short, '短消息直查'); ok('短消息原样通过（零调用）')

  const long = 'x'.repeat(600)
  const distilled = await distillQuery(long, opts())
  assert.equal(distilled, '部署端口配置'); ok('长文本提炼为检索意图')
  assert.ok(fetchCalls.some((c) => c.path.endsWith('/chat/completions')), '蒸馏请求已发出'); ok('蒸馏端点被调用')

  const drifted = await distillQuery('中文日志 DRIFT-CASE ' + 'y'.repeat(600), opts())
  assert.ok(drifted.startsWith('中文日志'), '漂移应回退原文，实际: ' + drifted.slice(0, 30)); ok('语言漂移回退原文')

  const disabled = await distillQuery('z'.repeat(600), opts({ enabled: false }))
  assert.equal(disabled.length, 600); ok('关闭蒸馏时原文直查')

  const fallback = await distillQuery('w'.repeat(600), opts({ baseUrl: '' }))
  assert.equal(fallback.length, 600); ok('未配端点回退原文')
}

console.log('== 召回链路含蒸馏（超长消息）==')
console.log('== 单元：双飞慢路径 ==')
{
  let slowCount = 0
  // 临时替换 fetch：第一个蒸馏请求挂起，随后所有请求立即成功
  const origFetch = globalThis.fetch
  let released = false
  globalThis.fetch = async (url, init = {}) => {
    const path = new URL(url).pathname
    if (path.endsWith('/chat/completions') && !released && slowCount === 0) {
      slowCount += 1
      await new Promise((r) => setTimeout(r, 300)) // 慢响应：超过 retryAfterMs=50
      return { ok: true, status: 200, text: async () => JSON.stringify({ choices: [{ message: { content: '慢路径结果' } }] }) }
    }
    return origFetch(url, init)
  }
  const opts = { enabled: true, minChars: 10, inputMaxChars: 8000, baseUrl: 'http://mock-distill/v1', apiKey: 'x', model: 'm', timeoutMs: 5000, retryAfterMs: 50 }
  const t0 = Date.now()
  const out = await distillQuery('z'.repeat(600), opts)
  const elapsed = Date.now() - t0
  assert.ok(elapsed < 250, '双飞应先到先用（' + elapsed + 'ms 太慢）'); ok('双飞先完成者胜出（' + elapsed + 'ms）')
  assert.ok(out === '部署端口配置' || out === '慢路径结果', '双飞结果被某一路成功携带，实际: ' + out); ok('双飞结果可确认')
  globalThis.fetch = origFetch
}

console.log('== 单元：外部取消联动（IDX4 回归）==')
{
  // 挂起的蒸馏请求在外部 signal abort 后应立即中止（而非白跑满超时）
  const origFetch = globalThis.fetch
  let abortedSeen = false
  globalThis.fetch = async (url, init = {}) => {
    if (new URL(url).pathname.endsWith('/chat/completions')) {
      // 真实 fetch 对已中止的 signal 立即以 reason 拒绝；mock 需同语义
      if (init.signal && init.signal.aborted) {
        abortedSeen = true
        return Promise.reject(init.signal.reason || new Error('aborted'))
      }
      return new Promise((resolve, reject) => {
        init.signal.addEventListener('abort', () => {
          abortedSeen = true
          reject(new Error('aborted'))
        })
      })
    }
    return origFetch(url, init)
  }
  const opts = { enabled: true, minChars: 10, inputMaxChars: 8000, baseUrl: 'http://mock-distill/v1', apiKey: 'x', model: 'm', timeoutMs: 30000, retryAfterMs: 5000 }
  const outer = new AbortController()
  const t0 = Date.now()
  const outPromise = distillQuery('z'.repeat(600), opts, null, outer.signal)
  await new Promise((r) => setTimeout(r, 30)) // 等请求挂起
  outer.abort()
  const out = await outPromise
  const elapsed = Date.now() - t0
  assert.equal(out, 'z'.repeat(600)); ok('外部取消后回退原文')
  assert.ok(abortedSeen, '蒸馏 fetch 收到中止信号'); ok('外部 abort 联动到蒸馏请求')
  assert.ok(elapsed < 500, '取消后立即返回而非等满超时（' + elapsed + 'ms）'); ok('取消即时生效')
  globalThis.fetch = origFetch
}

console.log('== 单元：琐碎输入守卫 ==')
{
  assert.equal(isTrivialPrompt('hi'), true); ok('英文问候判琐碎')
  assert.equal(isTrivialPrompt('OK!!!'), true); ok('带标点确认判琐碎')
  assert.equal(isTrivialPrompt('/compact'), true); ok('斜杠命令判琐碎（单段短命令形态）')
  assert.equal(isTrivialPrompt('帮我看看这个报错'), false); ok('正常中文请求不误伤')
  assert.equal(isTrivialPrompt('continue'), true); ok('continue 判琐碎')
  // 斜杠判定收紧（2026-08-25 审计 G1）：以 / 开头的真实内容查询不得误伤
  assert.equal(isTrivialPrompt('/etc/hosts 里改了什么'), false); ok('路径查询不误伤：/etc/hosts…')
  assert.equal(isTrivialPrompt('/api/v1 报错怎么办'), false); ok('端点查询不误伤：/api/v1…')
  assert.equal(isTrivialPrompt('/data/x 有什么文件'), false); ok('目录查询不误伤：/data/x…')
  assert.equal(isTrivialPrompt('/data/x'), false); ok('纯路径也不误伤（多段非命令）')
  // 纯符号串（2026-08-25 审计 G2）：剥标点后为空无语义信号
  assert.equal(isTrivialPrompt('。。。'), true); ok('纯中文句点判琐碎')
  assert.equal(isTrivialPrompt('......'), true); ok('纯西文句点判琐碎')
  // 中文扩充词表
  assert.equal(isTrivialPrompt('好的'), true); ok('中文应答判琐碎：好的')
  assert.equal(isTrivialPrompt('嗯嗯'), true); ok('中文应答判琐碎：嗯嗯')
  assert.equal(isTrivialPrompt('收到！'), true); ok('中文应答带标点：收到！')
  assert.equal(isTrivialPrompt('明白了。'), true); ok('中文应答带标点：明白了。')
  assert.equal(isTrivialPrompt('继续'), true); ok('中文推进判琐碎：继续')
  assert.equal(isTrivialPrompt('下一步'), true); ok('中文推进判琐碎：下一步')
  assert.equal(isTrivialPrompt('你好'), true); ok('中文问候判琐碎：你好')
  assert.equal(isTrivialPrompt('在吗？'), true); ok('中文问候带标点：在吗？')
  assert.equal(isTrivialPrompt('不用了'), true); ok('中文否定应答：不用了')
  assert.equal(isTrivialPrompt('辛苦了~'), true); ok('中文确认收货：辛苦了~')
  // 中文不误伤
  assert.equal(isTrivialPrompt('继续帮我优化这个插件'), false); ok('带实际内容不误伤：继续…')
  assert.equal(isTrivialPrompt('好的方案是什么？'), false); ok('疑问句不误伤：好的方案…')
  assert.equal(isTrivialPrompt('你好，我想聊聊记忆插件的架构'), false); ok('问候后带正文不误伤')
}

console.log('== Host apply 全链路 ==')
const env = makeCtx({})
apply(env.ctx, { host: 'http://mock:9999' })

const emit = (event, ...args) => Promise.all((env.listeners.get(event) || []).map((cb) => cb(...args)))
const emitOn = (actx, event, ...args) => Promise.all((actx.listeners.get(event) || []).map((cb) => cb(...args)))
const spawn = (id) => { const a = env.ctx.createAgent(id); return a }
const spawnAll = async (ids) => { const list = []; for (const id of ids) { const a = spawn(id); await emit('agent/created', { agent: a }); list.push(a) } return list }

// 补注册路径（2026-08-25 实测缺陷）：插件 apply 晚于 agent 创建时，agent/created
// 已被错过；host 在 apply 尾声枚举现存 agents 统一补挂 hook（幂等，不双触发）。
{
  const preExisting = spawn('sess-PreExisting')
  const hooksBefore = (preExisting.listeners.get('agent/pre-step') || []).length
  assert.equal(hooksBefore, 0, 'apply 前预存在 agent 尚未挂 pre-step（还原缺陷现场）')
  const env2 = makeCtx({})
  env2.ctx.registryAgents.push(preExisting) // host registry 里已有该 agent
  apply(env2.ctx, { host: 'http://mock:9999' })
  const hooksAfter = (preExisting.listeners.get('agent/pre-step') || []).length
  assert.ok(hooksAfter >= 1, 'apply 补注册后预存在 agent 挂上 pre-step 监听')
  assert.ok((preExisting.listeners.get('agent/inbox/claimed') || []).length >= 1, '补注册同时挂 claimed')
  assert.ok((preExisting.listeners.get('agent/turn-stopping') || []).length >= 1, '补注册同时挂 turn-stopping')
  // 幂等：同一 agent 补注册 + agent/created 都到达时只挂一份
  await Promise.all((env2.listeners.get('agent/created') || []).map((cb) => cb({ agent: preExisting })))
  const hooksDup = (preExisting.listeners.get('agent/pre-step') || []).length
  assert.equal(hooksDup, hooksAfter, '补注册与 created 重复到达不双触发')
  ok('apply 尾声枚举现存 agents 补挂 hook（幂等）')
}

assert.equal(env.tools.length, 4); ok('注册四个工具（defineTool 真实编译通过）')
assert.deepEqual(env.tools.map((t) => t.name), ['mem0_search', 'mem0_add', 'mem0_update', 'mem0_delete']); ok('工具名正确')
assert.equal(env.sections.length, 1); ok('常驻使用说明节已注册')
// 琐碎轮搜索豁免（方案 A）：强命令与守卫词表必须在提示词层对齐，
// 否则模型在「好的/继续」轮仍会服从 MUST search 去调工具（2026-08-24 实测缺陷）
{
  const sectionText = env.sections[0].text()
  assert.match(sectionText, /SOLE EXCEPTION/, '使用说明节缺琐碎轮豁免条款')
  assert.match(sectionText, /继续/, '豁免条款未列中文推进词例')
  assert.match(sectionText, /skip mem0_search/i, '豁免条款未指明跳过动作')
  assert.match(sectionText, /继续帮我看看那个报错/, '豁免条款缺少「带实义内容不豁免」的反例')
  ok('使用说明节含琐碎轮豁免（整串仅为应答/推进时跳过，带实义内容恢复强制）')
  const searchDesc = env.tools[0].description
  assert.match(searchDesc, /Skip this search ONLY when the entire user message/i, 'mem0_search 描述缺豁免提示')
  assert.match(searchDesc, /any real content requires the search/, '工具描述缺反例约束')
  ok('mem0_search 工具描述含同标准豁免提示')
}
for (const ev of ['agent/created', 'session/event', 'settings/updated']) {
  assert.ok(env.listeners.has(ev), '全局监听缺失: ' + ev)
}
ok('全局三类事件监听挂载（agent/created 驱动 scoped 注册）')
// claimed/turn-stopping 现在注册在 agent 级 ctx（真实契约：载荷无 agent）
{
  const probe = env.ctx.createAgent('sess-Probe')
  await emit('agent/created', { agent: probe })
  assert.ok((probe.listeners.get('agent/inbox/claimed') || []).length >= 1, 'claimed 未注册到 agent ctx')
  assert.ok((probe.listeners.get('agent/turn-stopping') || []).length >= 1, 'turn-stopping 未注册到 agent ctx')
  ok('claimed/turn-stopping 注册于 agent 级 scoped ctx')
}



console.log('== 显式关闭(enabled=false)时工具给出明确指引 ==')
{
  env.setScope({ ...env.getScope(), enabled: false })
  const searchTool = env.tools[0]
  const result = await searchTool.execute({ query: '测试' }, { signal: new AbortController().signal })
  assert.equal(result.ok, false)
  assert.match(result.error, /未启用/)
  ok('disabled 时返回启用指引: ' + result.error.slice(0, 30) + '…')
  env.setScope({ ...env.getScope(), enabled: true }) // 恢复启用态，避免污染后续用例
}

console.log('== 第一步强制搜索提醒 ==')
{
  const [agentM] = await spawnAll(['sess-M'])
  const preSteps = agentM.listeners.get('agent/pre-step') || []
  assert.ok(preSteps.length >= 1, 'pre-step 监听未注册'); ok('pre-step 监听注册于 agent ctx')
  const nextBase = async () => ({ kind: 'enter', messages: [{ role: 'user', content: [{ type: 'text', text: '原始问题' }], source: { kind: 'user' } }] })

  // 第一步（运行时 step=1）+ 非琐碎 → 注入提醒
  const d1 = await preSteps[0]({ messages: [{ role: 'user', content: [{ type: 'text', text: '我的记忆里有部署信息吗' }], source: { kind: 'user' } }], turn: 1, step: 1, signal: null }, nextBase)
  assert.equal(d1.kind, 'enter')
  assert.equal(d1.messages.length, 2); ok('第一步注入提醒消息')
  assert.equal(d1.messages[1].source.kind, 'plugin'); ok('提醒为 plugin-source（不写入记忆、UI 系统样式）')
  assert.match(d1.messages[1].content[0].text, /mem0_search/); ok('提醒文本包含 mem0_search 指令')
  assert.ok(typeof d1.messages[1].id === 'string' && d1.messages[1].id.length > 0, '提醒缺 id（会导致 SessionPersistenceCorruptionError: lacks an identified message）'); ok('提醒消息携带 id（防持久化校验崩溃回归）')

  // 语言约束：提醒文本必须包含「查询语言与用户一致」条款（中文记忆召回修复）
  assert.match(d1.messages[1].content[0].text, /language/i); ok('提醒文本含语言约束条款（中文关键词检索）')

  // 同一 turn 内后段新输入（step=5，携带真人 user 消息）→ 也要提醒
  const d2 = await preSteps[0]({ messages: [{ role: 'user', content: [{ type: 'text', text: '另外还有个问题想问你' }], source: { kind: 'user' } }], turn: 1, step: 5, signal: null }, nextBase)
  assert.equal(d2.messages.length, 2); ok('同 turn 后段新输入同样注入提醒')

  // 工具回执步（无真人 user 消息）→ 不打扰
  const d2b = await preSteps[0]({ messages: [{ content: [{ type: 'text', text: 'tool result' }], source: { kind: 'tool', callId: 'c1' } }], turn: 1, step: 6, signal: null }, nextBase)
  assert.equal(d2b.messages.length, 1); ok('工具回执步不重复提醒')

  // 琐碎输入 → 跳过提醒
  const d3 = await preSteps[0]({ messages: [{ role: 'user', content: [{ type: 'text', text: '好的' }], source: { kind: 'user' } }], turn: 2, step: 1, signal: null }, nextBase)
  assert.equal(d3.messages.length, 1); ok('琐碎轮（好的）不打扰')

  // 开关关闭 → 不注入
  env.setScope({ ...env.getScope(), forceRecallStep: false })
  const d4 = await preSteps[0]({ messages: [{ role: 'user', content: [{ type: 'text', text: '查一下端口' }], source: { kind: 'user' } }], turn: 3, step: 1, signal: null }, nextBase)
  assert.equal(d4.messages.length, 1); ok('forceRecallStep=off 不注入')
  env.setScope({ ...env.getScope(), forceRecallStep: true })
}

console.log('== 启用后：写入链路 ==')
{
  const [agentW] = await spawnAll(['sess-W'])
  // 夹具 = rc.2 真实嵌套形状（data 层；user/message 的 data 即消息本体）——平铺夹具曾全绿而真机静默死亡（D5）
  await emit('session/event', { id: 'sess-W' }, { type: 'user/message', seq: 1, time: 1, data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: '记住：我的部署端口是 8888' }] } })
  await emit('session/event', { id: 'sess-W' }, { type: 'assistant/message', seq: 2, time: 2, data: { turn: 1, step: 1, message: { source: { kind: 'model' }, content: [{ type: 'text', text: '好的，记住了。' }] } } })
  // 插件通知不应被当作用户输入
  await emit('session/event', { id: 'sess-W' }, { type: 'user/message', seq: 3, time: 3, data: { role: 'user', source: { kind: 'plugin', plugin: 'x' }, content: [{ type: 'text', text: '文件变更通知' }] } })
  await emitOn(agentW, 'agent/turn-stopping', { turn: 1, signal: new AbortController().signal })

  const memCallsBefore = fetchCalls.filter((c) => c.path === '/memories').length
  assert.ok(env.effects.some((e) => (e.label || '').includes('coalesce-tick')), '冲刷 tick 效果未注册'); ok('潮浪冲刷 tick 已挂载')
  // 手动驱动一次 tick：直接调用 tick 效果不可行——改为等待 interval？interval 300ms 太慢，
  // 这里通过触发 dispose 前的 flushAll 不合适；改用缩短 idle 的方式：直接调 coalescer 不可达。
  // 方案：临时把 idle 调到 0 以上最小值并等待 interval 触发。
  env.setScope({ ...env.getScope(), coalesceIdleMs: 500 })
  await new Promise((r) => setTimeout(r, 1300))
  const memCalls = fetchCalls.filter((c) => c.path === '/memories').length
  assert.ok(memCalls > memCallsBefore, '潮浪冲刷未落库')
  const lastBody = JSON.parse(fetchCalls.filter((c) => c.path === '/memories').at(-1).body)
  assert.equal(lastBody.infer, true); ok('合并写入走 infer=true 服务端抽取')
  assert.equal(lastBody.messages.length, 2); ok('一轮 user+assistant 成对入桶（插件通知被过滤）')
  assert.match(lastBody.messages[0].content, /8888/)
  assert.equal(lastBody.metadata.channel, 'dsh'); ok('metadata.channel=dsh 盖章')
}

console.log('== 兼容：旧平铺载荷仍可捕获（归一化回落回归）==')
{
  const [agentF] = await spawnAll(['sess-F'])
  await emit('session/event', { id: 'sess-F' }, { type: 'user/message', message: { source: { kind: 'user' }, content: [{ type: 'text', text: '平铺形状的用户问题' }] } })
  await emit('session/event', { id: 'sess-F' }, { type: 'assistant/message', turn: 1, message: { source: { kind: 'model' }, content: [{ type: 'text', text: '平铺形状的助手回答' }] } })
  await emitOn(agentF, 'agent/turn-stopping', { turn: 1, signal: new AbortController().signal })
  env.setScope({ ...env.getScope(), coalesceIdleMs: 500 })
  await new Promise((r) => setTimeout(r, 1300))
  const body = JSON.parse(fetchCalls.filter((c) => c.path === '/memories').at(-1).body)
  const joined = body.messages.map((m) => m.content).join('\n')
  assert.ok(joined.includes('平铺形状的用户问题') && joined.includes('平铺形状的助手回答'), '平铺载荷 user+assistant 均应入队')
  ok('旧平铺形状兼容（无 data 回落 event 本体）')
}

console.log('== 端到端：上传脱敏（B 组）==')
{
  const [agentR] = await spawnAll(['sess-R'])
  const FAKE = 'sk-E2eFake0123456789abcdef'
  await emit('session/event', { id: 'sess-R' }, { type: 'user/message', seq: 1, time: 1, data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: '记住我的 key：' + FAKE }] } })
  await emit('session/event', { id: 'sess-R' }, { type: 'assistant/message', seq: 2, time: 2, data: { turn: 1, step: 1, message: { source: { kind: 'model' }, content: [{ type: 'text', text: '好的，已记住。' }] } } })
  await emitOn(agentR, 'agent/turn-stopping', { turn: 1, signal: new AbortController().signal })
  env.setScope({ ...env.getScope(), coalesceIdleMs: 500 })
  await new Promise((r) => setTimeout(r, 1300))
  const redBody = JSON.parse(fetchCalls.filter((c) => c.path === '/memories').at(-1).body)
  const joined = redBody.messages.map((m) => m.content).join('\n')
  assert.equal(joined.includes(FAKE), false, '原 key 不应出现在上传 payload')
  assert.ok(joined.includes('[REDACTED:openai-key]'), '应出现 REDACTED 标记')
  ok('端到端：含假 key 的轮次上传已脱敏（addFn 收到 [REDACTED:...]，无原 key）')
}

console.log('== 端到端：mem0_add 工具直写脱敏（2026-08-29 盲区修复）==')
{
  const addTool = env.tools[1] // mem0_add
  const before = fetchCalls.filter((c) => c.path === '/memories').length
  const res = await addTool.execute({ content: '我的 API key 是 sk-AddToolFake0123456789abcdef，base url 是 http://x/v1' }, { signal: new AbortController().signal })
  assert.equal(res.ok, true, 'mem0_add 正常返回')
  const addCall = fetchCalls.filter((c) => c.path === '/memories').at(-1)
  const addBody = JSON.parse(addCall.body)
  assert.equal(addBody.infer, false, 'mem0_add 走 infer=false 直写')
  assert.equal(addBody.messages[0].content.includes('sk-AddToolFake0123456789abcdef'), false, '原 key 不应直写落库')
  assert.ok(addBody.messages[0].content.includes('[REDACTED:openai-key]'), '直写内容应含 REDACTED 标记')
  ok('mem0_add 直写过脱敏闸（infer=false 也不漏放）')
  // 关闭脱敏时原样直写
  env.setScope({ ...env.getScope(), redactEnabled: false })
  const res2 = await addTool.execute({ content: 'key2 是 sk-AddToolFake0123456789abcdef' }, { signal: new AbortController().signal })
  assert.equal(res2.ok, true)
  const addCall2 = fetchCalls.filter((c) => c.path === '/memories').at(-1)
  const addBody2 = JSON.parse(addCall2.body)
  assert.ok(addBody2.messages[0].content.includes('sk-AddToolFake0123456789abcdef'), 'redactEnabled=false 原样直写')
  ok('mem0_add 脱敏开关可关（旁路）')
  env.setScope({ ...env.getScope(), redactEnabled: true })
}

console.log('== 单元：上传脱敏闸（coalesce 接线）==')
{
  const C = (await import('../src/coalesce.js')).TidalCoalescer
  const q = new C({
    resolve: () => ({ enabled: true, idleMs: 5000, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000 }),
    addFn: async () => {}, log: {}
  })
  const warns = []
  q.log.warn = (m) => warns.push(m)
  q.enqueue({ userId: 'u', sessionId: 'r1', userContent: 'key 是 sk-Abc123Def456Ghi789Jkl0', assistantContent: '收到' })
  q.drain()
  assert.equal(q.stats.redacted, 1); ok('stats.redacted 计数')
  assert.ok(q.buckets.get('u\u0000r1').messages[0].content.includes('[REDACTED:openai-key]')); ok('入桶消息已替换')
  assert.equal(warns.length, 1); ok('命中 warn 一次')
  q.enqueue({ userId: 'u', sessionId: 'r1', userContent: '又一个 sk-Xyz987Wvu654Rst321Abc', assistantContent: '嗯' })
  q.drain()
  assert.equal(q.stats.redacted, 2)
  assert.equal(warns.length, 1); ok('同会话同 label 不重复 warn')
  // env-block 折叠必须经 route 落地：foldEnvBlocks 的结果靠 hits 非空才会替换文本，
  // 漏报 hits 时替换被整段丢弃、防线在链路上失效（2026-08-29 审计 P1 回归）
  const q3 = new C({
    resolve: () => ({ enabled: true, idleMs: 5000, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000 }),
    addFn: async () => {}, log: {}
  })
  q3.enqueue({ userId: 'u', sessionId: 'r3', userContent: 'DB_HOST=localhost\nDB_USER=root\nDB_PASS=secret\nAPP_ENV=prod\nAPP_PORT=8888', assistantContent: '收到' })
  q3.drain()
  assert.equal(q3.stats.redacted, 1); ok('env-block 折叠计入 redacted 计数')
  assert.ok(q3.buckets.get('u\u0000r3').messages[0].content.includes('[REDACTED:env-block]')); ok('env-block 折叠经 route 落桶（hits 上报接线）')
  const q2 = new C({
    resolve: () => ({ enabled: true, redactEnabled: false, idleMs: 5000, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000 }),
    addFn: async () => {}, log: {}
  })
  q2.enqueue({ userId: 'u', sessionId: 'r2', userContent: 'key 是 sk-Abc123Def456Ghi789Jkl0', assistantContent: 'ok' })
  q2.drain()
  assert.equal(q2.stats.redacted, 0)
  assert.ok(q2.buckets.get('u\u0000r2').messages[0].content.includes('sk-Abc123Def456Ghi789Jkl0'))
  ok('redactEnabled=false 完全旁路')
}

console.log('== 单元：大 payload 切片（2026-08-29 502 教训，全量保留）==')
{
  const { sliceText } = await import('../src/coalesce.js')
  const C = (await import('../src/coalesce.js')).TidalCoalescer
  // sliceText 纯函数：全量保留、段落边界优先
  const text = '段落一：这是第一段内容。\n\n段落二：这是第二段内容，稍微长一点用于测试。\n\n段落三：收尾。'
  const pieces = sliceText(text, 20)
  assert.ok(pieces.length >= 3, '长文本切成多片')
  // 切片点只丢边界空白（设计行为），非空白内容全量保留
  const joinedNoWs = pieces.join('').replace(/\s/g, '')
  const origNoWs = text.replace(/\s/g, '')
  assert.equal(joinedNoWs, origNoWs, '去空白后与原文一致（内容全量保留）')
  ok('sliceText 全量保留、段落边界优先')
  const hard = sliceText('x'.repeat(5000), 2000)
  assert.ok(hard.length >= 3 && hard.every((p) => p.length <= 2000), '无段落边界时硬切且每片不超限')
  assert.equal(hard.join(''), 'x'.repeat(5000), '硬切也不丢内容')
  ok('sliceText 无段落时硬切、不丢内容')
  // route 集成：13202 chars 单条 assistant → 切成多片消息，全量直写
  const sent = []
  const q = new C({
    resolve: () => ({ enabled: true, idleMs: 5000, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, sliceThreshold: 8000, slicePieceChars: 2000 }),
    addFn: async ({ messages }) => { sent.push(messages) }, log: {}
  })
  const warns = []
  q.log.warn = (m) => warns.push(m)
  const big = 'y'.repeat(13202)
  q.enqueue({ userId: 'u', sessionId: 'big1', userContent: '短问题', assistantContent: big })
  q.drain()
  await new Promise((r) => setTimeout(r, 10))
  assert.equal(q.stats.sliced, 1); ok('超限 payload 计入 sliced 计数')
  assert.ok(warns.some((m) => m.includes('payload sliced')), 'warn 报告切片')
  assert.equal(sent.length, 1, '切片后仍一次直写（fastpath）')
  const joined = sent[0].map((m) => m.content).join('')
  assert.equal(joined, '短问题' + 'y'.repeat(13202), '切片后全量保留（无截断）')
  assert.ok(sent[0].filter((m) => m.role === 'assistant').length >= 7, 'assistant 切成 ≥7 片（每片 ≤2000）')
  assert.ok(sent[0].every((m) => m.content.length <= 2000 || m.role === 'user'), '每片不超 slicePieceChars')
  ok('route 切片：13202 chars → 多片全量直写')
  // 未超阈值不切片
  const sent2 = []
  const q2 = new C({
    resolve: () => ({ enabled: true, idleMs: 5000, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, sliceThreshold: 8000, slicePieceChars: 2000 }),
    addFn: async ({ messages }) => { sent2.push(messages) }, log: {}
  })
  q2.enqueue({ userId: 'u', sessionId: 'big2', userContent: '短', assistantContent: '正常内容'.repeat(100) })
  q2.drain()
  await new Promise((r) => setTimeout(r, 10))
  assert.equal(q2.stats.sliced, 0); ok('未超阈值不切片')
  // 码点安全（2026-08-29 一轮审计 P2）：无换行长文硬切，切点恰落在 emoji 代理对中间时
  // 回退一个 UTF-16 单元，绝不出孤代理（旧行为 piece1 尾部残留孤立 D83D）
  const emojiText = 'a'.repeat(1997) + '😀' + 'b'.repeat(10)
  const emojiPieces = sliceText(emojiText, 1998)
  assert.equal(emojiPieces.join(''), emojiText, '码点全量保留（无截断）')
  for (const p of emojiPieces) {
    if (!p.length) continue
    const first = p.charCodeAt(0)
    const last = p.charCodeAt(p.length - 1)
    assert.ok(!(last >= 0xD800 && last <= 0xDBFF), '片尾不得残留孤立高代理')
    assert.ok(!(first >= 0xDC00 && first <= 0xDFFF), '片头不得残留孤立低代理')
  }
  ok('sliceText 硬切边界码点安全（emoji 不切半）')
}

console.log('== 单元：有界队列丢最旧 ==')
{
  const q = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 5000, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, queueMaxLen: 3 }),
    addFn: async () => {}, log: {}
  })
  for (let i = 0; i < 5; i += 1) q.enqueue({ userId: 'u', sessionId: 's' + i, userContent: 'msg' + i, assistantContent: 'a' })
  assert.equal(q.queue.length, 3); ok('队满只留 3 个')
  assert.equal(q.stats.dropped, 2); ok('丢最旧计数 2')
  assert.equal(q.queue[0].sessionId, 's2'); ok('最旧的 s0/s1 被丢')
}

console.log('== 单元：冲刷失败不丢数据（C1 回归）==')
{
  // addFn 先失败两次再成功：桶必须放回重试，消息零丢失
  let attempts = 0
  const written = []
  const q = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000 }),
    addFn: async ({ messages }) => {
      attempts += 1
      if (attempts <= 2) throw new Error('circuit open (simulated)')
      written.push(messages)
    },
    log: {}
  })
  q.enqueue({ userId: 'u', sessionId: 'sR', userContent: '不能丢的对话', assistantContent: '回答' })
  q.drain()
  // flushDue 对冲刷是 fire-and-forget 且按 idle/window 判到期：
  // 用未来时钟强制到期 + 让出事件循环等 addFn settle
  const settle = () => new Promise((r) => setTimeout(r, 10))
  const tickAt = async (t) => { q.flushDue(t); await settle() }
  await tickAt(Date.now() + 60000)
  assert.equal(written.length, 0); ok('失败首次未写入（前置）')
  assert.ok(q.buckets.size > 0 || q.pending > 0); ok('失败后桶已放回待重试')
  await tickAt(Date.now() + 60000) // 第二次仍失败
  await tickAt(Date.now() + 60000) // 第三次成功
  assert.equal(written.length, 1); ok('重试后成功写入，消息零丢失')
  const flushed = written[0]
  assert.ok(JSON.stringify(flushed).includes('不能丢的对话')); ok('放回后内容完整不丢字')
}

console.log('== 单元：熔断短路期冲刷不消耗重试（冷却竞赛回归）==')
{
  // 熔断打开期 shortCircuited 错误持续 30 次（> 旧 20 次上限），随后恢复：
  // 桶必须存活到「冷却结束」，零丢弃——旧实现 20 次 × tick(300ms) ≈ 6-11s 即丢整桶
  let attempts = 0
  const written = []
  const q = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000 }),
    addFn: async ({ messages }) => {
      attempts += 1
      if (attempts <= 30) {
        const e = new Error('mem0 temporarily unavailable: circuit breaker open, retries in 120s')
        e.shortCircuited = true
        throw e
      }
      written.push(messages)
    },
    log: {}
  })
  q.enqueue({ userId: 'u', sessionId: 'sBreaker', userContent: '熔断期间不能丢的记忆', assistantContent: '回答' })
  q.drain()
  const settle = () => new Promise((r) => setTimeout(r, 5))
  const tickAt = async (t) => { q.flushDue(t); await settle() }
  for (let i = 0; i < 30; i++) await tickAt(Date.now() + 60000) // 短路 30 次，远超旧上限
  assert.equal(written.length, 0)
  assert.equal(q.stats.dropped, 0); ok('持续短路 30 次零丢弃（retries 未被消耗）')
  await tickAt(Date.now() + 60000) // 「冷却结束」后第一次真实重试成功
  assert.equal(written.length, 1); ok('恢复后首试即成功，整桶记忆完整存活')
  assert.ok(JSON.stringify(written[0]).includes('熔断期间不能丢的记忆')); ok('存活桶内容完整不丢字')
}

console.log('== 单元：短路期入队不拦截 + fastpath 降级 + 半开重置（二轮审计回归）==')
{
  // A) 短路期 enqueue 照常进桶（不再 turn-stopping 拦截），恢复后一次冲刷成功
  let attempts = 0
  const written = []
  const q = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, cooldownMs: 120000 }),
    addFn: async ({ messages }) => {
      attempts += 1
      if (attempts <= 3) { const e = new Error('open'); e.shortCircuited = true; throw e }
      written.push(messages)
    },
    log: {}
  })
  // 模拟短路期 2 轮对话入队（旧实现会在入队口被拦截丢弃）
  q.enqueue({ userId: 'u', sessionId: 'sIn', userContent: '短路期间的对话一', assistantContent: '答' })
  q.enqueue({ userId: 'u', sessionId: 'sIn', userContent: '短路期间的对话二', assistantContent: '答' })
  q.drain()
  const settle = () => new Promise((r) => setTimeout(r, 5))
  for (let i = 0; i < 4; i++) { q.flushDue(Date.now() + 60000); await settle() }
  assert.equal(written.length, 1); ok('短路期入队消息恢复后一次冲刷成功')
  assert.ok(JSON.stringify(written[0]).includes('短路期间的对话一') && JSON.stringify(written[0]).includes('短路期间的对话二'))
  ok('短路期两轮对话完整存活')

  // B) fastpath 直写失败降级入桶：首次失败后恢复，消息通过桶路径写入
  let fa = 0
  const written2 = []
  const q2 = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, cooldownMs: 120000 }),
    addFn: async ({ messages }) => {
      fa += 1
      if (fa === 1) throw new Error('network reset')
      written2.push(JSON.stringify(messages))
    },
    log: {}
  })
  q2.enqueue({ userId: 'u', sessionId: 'sF', userContent: 'x'.repeat(2500) + '快路径长内容', assistantContent: '答' })
  q2.drain()
  await settle()
  assert.equal(written2.length, 0); ok('fastpath 首次失败未直接丢')
  await q2.flushDue(Date.now() + 60000); await settle()  // 降级后第二次成功
  assert.equal(written2.length, 1); ok('降级入桶后重试成功')
  assert.ok(written2[0].includes('快路径长内容')); ok('快路径长内容零丢失')

  // C) 半开重置：真实失败间隔超过冷却→retries 重置，永不累计到 20 丢弃
  let fc = 0
  const q3 = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, cooldownMs: 30 }),
    addFn: async () => { fc += 1; throw new Error('half-open fail') },
    log: {}
  })
  q3.enqueue({ userId: 'u', sessionId: 'sH', userContent: '半开期不能丢', assistantContent: '答' })
  q3.drain()
  for (let i = 0; i < 25; i++) {
    await q3.flushDue(Date.now() + 60005);          // 未来时钟强制到期
    await new Promise((r) => setTimeout(r, 40))     // 间隔 > cooldownMs 模拟半开轮
  }
  assert.equal(q3.stats.dropped, 0); ok('半开 25 次零丢弃（跨冷却重置）')
  const bucket = [...q3.buckets.values()][0]
  assert.ok(bucket.retries <= 5, 'retries 未累计到上限: ' + bucket.retries); ok('跨冷却失败间隔重置计数')
}

console.log('== 单元：桶全局预算（三轮审计）==')
{
  const q = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 60000, windowMs: 60000, maxTurns: 50, maxChars: 4000, fastpathChars: 2000 }),
    addFn: async () => { throw new Error('breaker open') },
    log: {}
  })
  // 短路期灌入 70 个会话的桶（> 64 上限）：最旧桶被丢、其余存活
  for (let i = 0; i < 70; i++) {
    q.enqueue({ userId: 'u', sessionId: 'sB' + i, userContent: '会话' + i + '的内容', assistantContent: '答' })
    q.drain()
  }
  assert.equal(q.buckets.size, 64); ok('桶数收敛到预算上限 64')
  assert.ok(q.stats.dropped >= 6); ok('超限最旧桶已丢并计数')
  const keys = [...q.buckets.keys()]
  // 精确匹配（'sB10'.includes('sB1') 为 true 会误判）；桶 key 是 'userId\u0000sessionId'
  const droppedNames = new Set(['sB0', 'sB1', 'sB2', 'sB3', 'sB4', 'sB5'])
  const kept = keys.map((k) => k.split('\u0000')[1])
  assert.ok(!kept.some((n) => droppedNames.has(n)), '最旧桶被优先丢弃: ' + kept.slice(0, 3).join(','))
  ok('最旧桶优先丢、新会话存活')
}

console.log('== 单元：单桶硬上限 + merge 重试继承（四轮审计）==')
{
  // A) 短路期同会话连续入队远超单桶上限：消息对截断到 100、最旧被丢
  let attempts = 0
  const q = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 60000, windowMs: 60000, maxTurns: 1000, maxChars: 1e9, fastpathChars: 2000, cooldownMs: 120000 }),
    addFn: async () => { attempts += 1; const e = new Error('open'); e.shortCircuited = true; throw e },
    log: {}
  })
  for (let i = 0; i < 130; i++) {
    q.enqueue({ userId: 'u', sessionId: 'sCap', userContent: '轮次' + i, assistantContent: '答' })
    q.drain()
  }
  const bucket = q.buckets.get('u\u0000sCap')
  assert.ok(bucket, '桶存在')
  assert.equal(bucket.messages.length / 2, 100); ok('单桶消息对收敛到 100')
  assert.ok(!JSON.stringify(bucket.messages).includes('轮次0'), '最旧消息对被截断')
  assert.ok(q.stats.dropped >= 30, '截断计数'); ok('截断有计数')

  // B) merge 继承 retries：失败桶与冲刷期新桶合并后 retries 不归零
  let fa = 0
  const q2 = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, cooldownMs: 120000 }),
    addFn: async () => { fa += 1; throw new Error('reject') },
    log: {}
  })
  q2.enqueue({ userId: 'u', sessionId: 'sR', userContent: '第一批', assistantContent: '答' })
  q2.drain()
  const settle = () => new Promise((r) => setTimeout(r, 5))
  await q2.flushDue(Date.now() + 60000); await settle()   // 失败 retries=1
  q2.enqueue({ userId: 'u', sessionId: 'sR', userContent: '第二批', assistantContent: '答' })
  q2.drain()
  await q2.flushDue(Date.now() + 60000); await settle()   // 失败 merge retries 应继承=2
  const merged = q2.buckets.get('u\u0000sR')
  assert.ok(merged.retries >= 2, 'merge 后 retries 继承不归零: ' + merged.retries); ok('merge 继承 retries')
  assert.ok(JSON.stringify(merged.messages).includes('第一批'), '旧消息未丢'); ok('合并内容完整')
}

console.log('== 单元：merge 分支真触达（五轮审计）==')
{
  // addFn 挂起期间 enqueue 第二批 → flushBucket 失败回插走 merge 分支
  let release
  const gate = new Promise((r) => { release = r })
  let attempts = 0
  const q = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, cooldownMs: 120000 }),
    addFn: async () => {
      attempts += 1
      if (attempts === 1) await gate   // 第一次挂起，制造 in-flight 窗口
      throw new Error('reject')
    },
    log: {}
  })
  q.enqueue({ userId: 'u', sessionId: 'sM', userContent: '第一批', assistantContent: '答' })
  q.drain()
  const flushP = q.flushDue(Date.now() + 60000)   // 进入 in-flight（addFn 挂起）
  await new Promise((r) => setTimeout(r, 10))
  q.enqueue({ userId: 'u', sessionId: 'sM', userContent: '第二批', assistantContent: '答' })
  q.drain()                                          // 在途冲刷期间新桶创建
  release()                                          // 放行失败
  await flushP
  await new Promise((r) => setTimeout(r, 10))
  const merged = q.buckets.get('u\u0000sM')
  assert.ok(merged, '合并后桶存在')
  assert.ok(JSON.stringify(merged.messages).includes('第一批'), '失败桶消息并入')
  assert.ok(merged.retries >= 1, 'merge 继承 retries: ' + merged.retries)
  ok('merge 分支真触达：retries 继承 + 内容合并')
}

console.log('== 单元：demote 路径 cap（六轮审计）==')
{
  // fastpath 失败降级入桶路径必须同样受单桶上限约束（maxTurns 放大绕过）
  let attempts = 0
  const q = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 60000, windowMs: 60000, maxTurns: 1000, maxChars: 1e9, fastpathChars: 1, cooldownMs: 120000 }),
    addFn: async () => { attempts += 1; throw new Error('network fail') },
    log: {}
  })
  for (let i = 0; i < 130; i++) {
    q.enqueue({ userId: 'u', sessionId: 'sD', userContent: '快路径内容' + i, assistantContent: '答' })
    q.drain()
  }
  const settle = () => new Promise((r) => setTimeout(r, 5))
  await settle()   // 等 fastpath 失败降级入桶完成
  const bucket = q.buckets.get('u\u0000sD')
  assert.ok(bucket, 'demote 桶存在')
  assert.equal(bucket.messages.length / 2, 100); ok('demote 桶消息对收敛到 100')
  assert.ok(!JSON.stringify(bucket.messages).includes('快路径内容0'), '最旧消息对被截断')
  assert.ok(q.stats.dropped >= 30, '截断计数: ' + q.stats.dropped); ok('demote 截断有计数')
}

console.log('== 单元：maxChars 按桶累积判定（C2 回归）==')
{
  const q2 = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 60000, windowMs: 60000, maxTurns: 50, maxChars: 4000, fastpathChars: 2000 }),
    addFn: async () => {}, log: {}
  })
  // 每条 1900 字符 < fastpathChars(2000) 入桶；旧笔误用当条值对比 maxChars(4000) 永不触发
  for (let i = 0; i < 3; i += 1) {
    q2.enqueue({ userId: 'u', sessionId: 'cap', userContent: 'x'.repeat(1900), assistantContent: '' })
    q2.drain()
  }
  assert.equal(q2.buckets.size, 0, '累积 chars 达 maxChars 应立即冲刷清桶'); ok('桶累积达 maxChars 触发冲刷（3×1900≥4000 在第 3 条触发）')
}

console.log('== 单元：毒桶存活上限 + 失败退避（2026-08-29 事故回归）==')
{
  const settle = () => new Promise((r) => setTimeout(r, 5))
  // A) 确定性失败（服务端对该载荷必然 502）：retries 因跨冷却重置永远停在 1/20，
  //    只有存活时间上限能让它落地，否则无限重投。
  let attempts = 0
  const q = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, cooldownMs: 30, maxBucketAgeMs: 1 }),
    addFn: async () => { attempts += 1; const e = new Error('mem0 /memories returned HTTP 502'); e.status = 502; throw e },
    log: {}
  })
  q.enqueue({ userId: 'u', sessionId: 'sPoison', userContent: '必然抽取失败的一轮', assistantContent: '答' })
  q.drain()
  await new Promise((r) => setTimeout(r, 10))                      // 让桶龄超过 maxBucketAgeMs=1
  await q.flushDue(Date.now() + 60000); await settle()
  assert.equal(q.buckets.size, 0, '服务端明确拒绝且超龄→必须落地'); ok('毒桶按存活上限落地，不再挂回')
  assert.equal(q.stats.dropped, 1, 'dropped 计数'); ok('超龄丢弃有计数')
  await q.flushDue(Date.now() + 60000); await settle()
  assert.equal(attempts, 1, '丢弃后不再重投'); ok('丢弃后彻底停止重投（无无限循环）')

  // A2) 反向保险：纯连接级失败（服务端宕机/还在跑）不是数据的错，超龄也绝不丢
  let netTries = 0
  const qn = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, cooldownMs: 30, maxBucketAgeMs: 1 }),
    addFn: async () => { netTries += 1; throw new Error('mem0 server unreachable at http://127.0.0.1:8888 (fetch failed)') },
    log: {}
  })
  qn.enqueue({ userId: 'u', sessionId: 'sDown', userContent: '宕机期间的对话', assistantContent: '答' })
  qn.drain()
  await new Promise((r) => setTimeout(r, 10))
  for (let i = 0; i < 6; i++) { await qn.flushDue(Date.now() + 600000); await settle() }   // 时钟拨到 10 分钟后
  assert.equal(qn.stats.dropped, 0, '宕机不得丢记忆'); ok('连接级失败超龄仍存活（宕机 30 分钟不误杀）')
  assert.ok(netTries >= 2, '宕机期间仍在重试: ' + netTries); ok('宕机期间持续重试')

  // B) 退避窗口：网络级失败后 30s 内不得重投（服务端可能还在慢慢跑同一单）
  let tries = 0
  const q2 = new (await import('../src/coalesce.js')).TidalCoalescer({
    resolve: () => ({ enabled: true, idleMs: 20, windowMs: 15000, maxTurns: 5, maxChars: 4000, fastpathChars: 2000, cooldownMs: 120000 }),
    addFn: async () => { tries += 1; throw new Error('fetch failed') },
    log: {}
  })
  q2.enqueue({ userId: 'u', sessionId: 'sBack', userContent: '一轮对话', assistantContent: '答' })
  q2.drain()
  await q2.flushDue(Date.now() + 60000); await settle()   // 失败 → nextAttemptAt = +30s
  assert.equal(tries, 1)
  await q2.flushDue(Date.now()); await settle()            // 退避窗口内
  assert.equal(tries, 1, '退避窗口内不得重投'); ok('失败退避：30s 内不重投')
  await q2.flushDue(Date.now() + 31000); await settle()    // 退避到期
  assert.equal(tries, 2, '退避到期后应重试'); ok('退避到期后照常重试')
}

console.log('== 中断轮不入记忆 ==')
{
  const [agentI] = await spawnAll(['sess-I'])
  await emit('session/event', { id: 'sess-I' }, { type: 'user/message', seq: 1, time: 1, data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text: '写一半被打断的问题' }] } })
  await emit('session/event', { id: 'sess-I' }, { type: 'assistant/message', seq: 2, time: 2, data: { turn: 1, step: 1, interrupted: true, message: { source: { kind: 'model' }, content: [{ type: 'text', text: '回答到一半就被用户中止了' }] } } })
  const before = fetchCalls.filter((c) => c.path === '/memories').length
  await emitOn(agentI, 'agent/turn-stopping', { turn: 1, signal: new AbortController().signal })
  env.setScope({ ...env.getScope(), coalesceIdleMs: 500 })
  await new Promise((r) => setTimeout(r, 1300))
  const after = fetchCalls.filter((c) => c.path === '/memories').length
  const bodies = fetchCalls.filter((c) => c.path === '/memories').map((c) => c.body)
  assert.ok(!bodies.some((b) => b && b.includes('写一半被打断')), '中断轮内容不得落库'); ok('中断轮整体跳过写入')
}

console.log('== 并发轮 user 文本配对 ==')
{
  const [agentP] = await spawnAll(['sess-P'])
  // 两轮交错：turn1 claimed -> turn2 claimed -> turn1 结束 -> turn2 结束
  await emitOn(agentP, 'agent/inbox/claimed', { message: { content: [{ type: 'text', text: '第一轮的问题' }] }, turn: 1 })
  await emitOn(agentP, 'agent/inbox/claimed', { message: { content: [{ type: 'text', text: '第二轮的问题' }] }, turn: 2 })
  const memBefore = fetchCalls.filter((c) => c.path === '/memories').length
  await emitOn(agentP, 'agent/turn-stopping', { turn: 1, signal: new AbortController().signal })
  await emitOn(agentP, 'agent/turn-stopping', { turn: 2, signal: new AbortController().signal })
  await new Promise((r) => setTimeout(r, 1300))
  const bodies = fetchCalls.filter((c) => c.path === '/memories').map((c) => c.body || '')
  const joined = bodies.join('')
  assert.ok(joined.includes('第一轮的问题'), 'turn1 配对错位'); ok('turn1 配对到自己的 user 文本')
  assert.ok(joined.includes('第二轮的问题'), 'turn2 配对错位'); ok('turn2 配对到自己的 user 文本')
}

console.log('== 工具执行：search 成功形态 ==')
console.log('== 工具蒸馏：mem0_search 长 query 走意图提炼 ==')
{
  const searchTool = env.tools[0]
  const longQuery = '帮我查一下我贴的这份服务器日志：' + 'log frame; '.repeat(120)
  const result = await searchTool.execute({ query: longQuery }, { signal: new AbortController().signal })
  assert.equal(result.ok, true); ok('长 query 工具调用成功')
  const searchCall = fetchCalls.filter((c) => c.path === '/search').at(-1)
  const sent = JSON.parse(searchCall.body).query
  assert.equal(sent, '部署端口配置'); ok('工具内蒸馏：/search 收到提炼意图而非整段日志')
  assert.ok(sent.length < longQuery.length / 10); ok('查询长度压缩两个数量级')
  const rendered = searchTool.output.render({}, result)
  assert.equal(rendered[0].type, 'text'); ok('渲染正常')
}
{
  const searchTool = env.tools[0]
  const result = await searchTool.execute({ query: '端口' }, { signal: new AbortController().signal })
  assert.equal(result.ok, true)
  assert.equal(result.data.count, 1)
  assert.equal(result.data.results[0].id, 'm-1'); ok('search 返回归一化 results')
  const hasUndefined = (v) => v === undefined || (Array.isArray(v) && v.some(hasUndefined)) || (v && typeof v === 'object' && Object.values(v).some(hasUndefined))
  assert.equal(hasUndefined(result.data), false); ok('search data 无 undefined，满足 lossless JSON')
  const rendered = searchTool.output.render({}, result)
  assert.equal(rendered[0].type, 'text'); ok('render 输出 text block')
}

console.log('== 工具输出硬化：紧凑行格式 + 截断 + clamp ==')
{
  const searchTool = env.tools[0]
  const renderText = (result) => searchTool.output.render({}, result)[0].text

  // 1) 紧凑行格式：类别/age/id/score 齐备，多行记忆净化成单行，graph 片段缺失省略，无 JSON 壳
  searchResponse = {
    results: [
      { id: 'm-1', memory: '发哥偏好\n结论先行', score: 0.91, created_at: new Date(Date.now() - 2 * 24 * 3600 * 1000).toISOString(), metadata: { memory_type: 'PREFERENCES' } },
      { id: 'g-1', memory: 'graph 片段无 score' }
    ]
  }
  const ok1 = await searchTool.execute({ query: '格式' }, { signal: new AbortController().signal })
  const t1 = renderText(ok1)
  assert.ok(t1.startsWith('1. [PREFERENCES] 发哥偏好 结论先行'), '类别 + 多行净化成单行')
  assert.ok(t1.includes('(2d ago) [mem0:m-1] (score 0.91)'), 'age/id/score 齐备')
  assert.ok(t1.includes('2. [memory] graph 片段无 score [mem0:g-1]'), 'graph 片段缺失项省略')
  assert.ok(!t1.includes('"results"') && !t1.includes('"count"'), '无 JSON 壳')
  ok('搜索回执为紧凑行格式（类别/age/id/score，无 JSON 壳）')

  // 2) 超长结果（60 条 × 2KB，单条截断 1000 后仍 ≈60KB > 50KB）：截断不抛错、显式标记存在
  searchResponse = { results: Array.from({ length: 60 }, (_, i) => ({ id: 'big-' + i, memory: 'x'.repeat(2000), score: 0.5 })) }
  const ok2 = await searchTool.execute({ query: '超长' }, { signal: new AbortController().signal })
  const t2 = renderText(ok2)
  assert.ok(t2.includes('[Output truncated:'), '显式截断标记')
  assert.ok(t2.includes('cut at 50KB'), '字节原因上报')
  ok('>50KB mock 回执截断不抛错且带标记')

  // 3) spec() clamp：行数阈值生效（1 → clamp 到下限 10），越界/非数回落默认值（itemMaxChars 回落 1000）
  env.setScope({ ...env.getScope(), itemMaxChars: 'abc', outputMaxKb: 99999, outputMaxLines: 1 })
  const ok3 = await searchTool.execute({ query: '超长' }, { signal: new AbortController().signal })
  const t3 = renderText(ok3)
  assert.ok(t3.includes('[Output truncated: showing 10 of 60 lines'), 'outputMaxLines=1 越界回落下限 10 生效')
  assert.ok(!t3.includes('cut at'), '行截断后字节已不超，不再报字节原因')
  assert.ok(t3.includes('…[截断]'), 'itemMaxChars 非数回落 1000 → 2KB 单条被截')
  ok('spec() clamp：行数阈值生效、越界/非数回落默认值')
  env.setScope({ ...env.getScope(), itemMaxChars: undefined, outputMaxKb: undefined, outputMaxLines: undefined })
}

console.log('== 工具执行：add/update/delete ==')
{
  const [,, updateTool, deleteTool] = env.tools
  const up = await updateTool.execute({ memory_id: 'm-1', text: '新事实' }, { signal: new AbortController().signal })
  assert.equal(up.ok, true); ok('update 成功')
  const fb = fetchCalls.filter((c) => c.path === '/evolve/feedback').length
  await new Promise((r) => setTimeout(r, 20))
  assert.ok(fb >= 1, 'update 后 evolve 反馈未上报'); ok('update 上报 correction 反馈')
  const del = await deleteTool.execute({ memory_id: 'm-1' }, { signal: new AbortController().signal })
  assert.equal(del.ok, true); ok('delete 成功')
}

console.log('== 客户端错误细分（400 不谎报 not found） ==')
{
  const [,, updateTool] = env.tools
  const up = await updateTool.execute({ memory_id: 'm-9', text: 'BAD400' }, { signal: new AbortController().signal })
  assert.equal(up.ok, false)
  assert.match(up.error, /Mem0 rejected/); ok('400 透传服务端拒绝原因')
  const [, , , deleteTool] = env.tools
  const del = await deleteTool.execute({ memory_id: 'not-exist' }, { signal: new AbortController().signal })
  assert.equal(del.ok, false)
  assert.match(del.error, /Memory not found/); ok('404 仍报 not found')
}

console.log('== 网络失败重试与熔断 ==')
{
  failNextNetwork = true
  const searchTool = env.tools[0]
  const result = await searchTool.execute({ query: '重试' }, { signal: new AbortController().signal })
  assert.equal(result.ok, true); ok('连接级失败自动重试一次后成功')

  // breaker 热调：熔断打开时改小冷却 → 窗口立即缩短
  const br2 = new CircuitBreaker({ threshold: 2, cooldownMs: 60000 })
  br2.recordFailure(new Error('x')); br2.recordFailure(new Error('x'))
  assert.equal(br2.open, true); ok('熔断已打开（前置条件）')
  const oldUntil = br2.openUntil
  retuneBreaker(br2, 2, 2000) // 等价于设置页改冷却触发的热调
  assert.equal(br2.cooldownMs, 2000); ok('冷却参数热调生效')
  assert.ok(br2.openUntil <= Date.now() + 2000 + 5 && br2.openUntil < oldUntil, '打开窗口未缩短'); ok('打开窗口按新冷却重算')
  // 接线确认：settings/updated 监听确实应用 retuneBreaker（改 scope 后不抛且仍可用）
  env.setScope({ ...env.getScope(), breakerThreshold: 2, breakerCooldownMs: 2000 })
  await emit('settings/updated', MEM0_SETTINGS_NAMESPACE, {}, {}, 'test')
  ok('settings/updated → retuneBreaker 接线无异常')
}

console.log('== dispose 兜底 ==')
{
  for (const effect of [...env.effects].reverse()) {
    if (typeof effect.disposer === 'function') effect.disposer()
  }
  ok('全部 effect disposer 执行无异常')
}

console.log('\n全部通过：' + PASS.length + ' 项 ✓')
