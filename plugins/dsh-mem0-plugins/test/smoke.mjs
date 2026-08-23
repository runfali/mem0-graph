/**
 * dsh-mem0-plugins 离线冒烟测试。
 *
 * 运行：node test/smoke.mjs
 * - 真实加载 @deepseek-ai/schemastery、dsh-settings、dsh-tools（经 node_modules 符号链接）
 * - mock cordis ctx（effect/on/inject/settings/tools/systemPrompt/logger）
 * - mock 全局 fetch（按路由应答，统计调用）
 *
 * 验证：
 * 1. apply 全链路注册成功（四工具经真实 defineTool 编译、prompt 节、五类监听、tick）
 * 2. 自动召回链路：claimed 预取 → assemble 瀑布有界等待 → 注入 sections
 * 3. 自动写入链路：session/event 捕获 → turn-stopping 入队 → tick 冲刷 → POST /memories
 * 4. 工具执行路径：未配置报错 / 搜索成功形态
 * 5. 纯 JSON 剥除与熔断器单元行为
 * 6. dispose 兜底冲刷不抛错
 */
import assert from 'node:assert/strict'
import { apply, Config, MEM0_SETTINGS_NAMESPACE } from '../src/index.js'
import { CircuitBreaker, Mem0HttpError, isClientError } from '../src/backend.js'
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
  if (path.startsWith('/memories/') && init.method === 'PUT') return jsonResponse(200, {})
  if (path.startsWith('/memories/') && init.method === 'DELETE') return jsonResponse(200, {})
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
    systemPrompt: { section: (def) => sections.push(def) }
  }
  scopeValue = resolveConfigManually(Config, config)
  return { ctx, effects, listeners, tools, sections, setScope: (v) => { scopeValue = v }, getScope: () => scopeValue }
}

/** 手工把 patch base 与 Config 默认值合并（模拟 settings 解析结果）。 */
function resolveConfigManually(schema, entry) {
  const defaults = {
    enabled: false,
    host: 'http://127.0.0.1:8888',
    apiKey: '',
    userId: 'dsh-user',
    agentId: 'dsh',
    topK: 10,
    rerank: false,
    recallEnabled: true,
    recallWaitMs: 15000,
    distillEnabled: true,
    distillMinChars: 500,
    distillInputMaxChars: 8000,
    distillBaseUrl: 'http://mock-distill/v1',
    distillApiKey: 'devops',
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
    requestTimeoutMs: 300000
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
    baseUrl: 'http://mock-distill/v1', apiKey: 'devops', model: 'Qwen3.5-9B',
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
console.log('== 单元：琐碎输入守卫 ==')
{
  for (const t of ['hi', 'ok!', 'thanks.', '/compact', '继续', '', '   ']) {
    // 注意 hermes 词表是英文的；中文「继续」不在词表——如实断言行为
  }
  assert.equal(isTrivialPrompt('hi'), true); ok('英文问候判琐碎')
  assert.equal(isTrivialPrompt('OK!!!'), true); ok('带标点确认判琐碎')
  assert.equal(isTrivialPrompt('/compact'), true); ok('斜杠命令判琐碎')
  assert.equal(isTrivialPrompt('帮我看看这个报错'), false); ok('正常中文请求不误伤')
  assert.equal(isTrivialPrompt('continue'), true); ok('continue 判琐碎')
}

console.log('== Host apply 全链路 ==')
const env = makeCtx({})
apply(env.ctx, { host: 'http://mock:9999' })
assert.equal(env.tools.length, 4); ok('注册四个工具（defineTool 真实编译通过）')
assert.deepEqual(env.tools.map((t) => t.name), ['mem0_search', 'mem0_add', 'mem0_update', 'mem0_delete']); ok('工具名正确')
assert.equal(env.sections.length, 1); ok('常驻使用说明节已注册')
for (const ev of ['agent/inbox/claimed', 'system-prompt/assemble', 'session/event', 'agent/turn-stopping', 'settings/updated']) {
  assert.ok(env.listeners.has(ev), ev + ' 监听缺失')
}
ok('五类事件监听全部挂载')

const emit = (event, ...args) => Promise.all((env.listeners.get(event) || []).map((cb) => cb(...args)))

console.log('== 未配置(enabled=false)时工具给出明确指引 ==')
{
  const searchTool = env.tools[0]
  const result = await searchTool.execute({ query: '测试' }, { signal: new AbortController().signal })
  assert.equal(result.ok, false)
  assert.match(result.error, /未启用/)
  ok('disabled 时返回启用指引: ' + result.error.slice(0, 30) + '…')
}

console.log('== 启用后：召回链路 ==')
env.setScope({ ...env.getScope(), enabled: true, recallWaitMs: 2000, requestTimeoutMs: 5000 })
{
  const agent = { id: 'sess-A' }
  await emit('agent/inbox/claimed', { agent, message: { content: [{ type: 'text', text: '我喜欢什么风格？' }] }, turn: 1 })
  const callsBefore = fetchCalls.filter((c) => c.path === '/search').length
  assert.ok(callsBefore >= 1, '预取搜索未发出'); ok('claimed 即发起后台 /search 预取')

  const assembly = { sections: [], contexts: [], tools: [] }
  await emit('system-prompt/assemble', assembly, { agent }, async () => assembly)
  const injected = assembly.sections.find((s) => s.name === 'mem0:recall')
  assert.ok(injected, '召回块未注入')
  assert.match(injected.text, /发哥偏好结论先行的短句回复/); ok('召回块注入并携带命中事实')

  // 同 turn 二次装配不再重复注入
  const assembly2 = { sections: [] }
  await emit('system-prompt/assemble', assembly2, { agent }, async () => assembly2)
  assert.equal(assembly2.sections.find((s) => s.name === 'mem0:recall'), undefined); ok('召回消费一次即清理')

  // 超长用户消息 → 蒸馏后的意图作为 /search query
  {
    const longAgent = { id: 'sess-L' }
    const longText = '下面是我贴的服务器日志，帮我看看有没有问题：' + 'log line; '.repeat(120)
    await emit('agent/inbox/claimed', { agent: longAgent, message: { content: [{ type: 'text', text: longText }] }, turn: 1 })
    const assembly = { sections: [] }
    await emit('system-prompt/assemble', assembly, { agent: longAgent }, async () => assembly)
    const searchCall = fetchCalls.filter((c) => c.path === '/search').at(-1)
    const sentQuery = JSON.parse(searchCall.body).query
    assert.equal(sentQuery, '部署端口配置'); ok('/search 收到的是蒸馏意图而非整段日志')
    assert.ok(sentQuery.length < longText.length / 10); ok('查询长度被压缩两个数量级')
  }

  // 琐碎输入跳过预取
  {
    const callsBefore = fetchCalls.filter((c) => c.path === '/search').length
    const agent = { id: 'sess-T' }
    await emit('agent/inbox/claimed', { agent, message: { content: [{ type: 'text', text: 'thanks!' }] }, turn: 1 })
    await emit('system-prompt/assemble', { sections: [] }, { agent }, async () => ({ sections: [] }))
    assert.equal(fetchCalls.filter((c) => c.path === '/search').length, callsBefore); ok('琐碎输入零网络往返')
  }

  // 使用说明节出现
  const usageText = env.sections[0].text()
  assert.match(usageText, /# Mem0 Memory/); ok('使用说明节随启用而生效')
}

console.log('== 启用后：写入链路 ==')
{
  const agent = { id: 'sess-A' }
  await emit('session/event', { id: 'sess-A' }, { type: 'user/message', message: { source: { kind: 'user' }, content: [{ type: 'text', text: '记住：我的部署端口是 8888' }] } })
  await emit('session/event', { id: 'sess-A' }, { type: 'assistant/message', turn: 1, message: { source: { kind: 'model' }, content: [{ type: 'text', text: '好的，记住了。' }] } })
  // 插件通知不应被当作用户输入
  await emit('session/event', { id: 'sess-A' }, { type: 'user/message', message: { source: { kind: 'plugin', plugin: 'x' }, content: [{ type: 'text', text: '文件变更通知' }] } })
  await emit('agent/turn-stopping', { agent, turn: 1 })

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

console.log('== 中断轮不入记忆 ==')
{
  const agent = { id: 'sess-I' }
  await emit('session/event', { id: 'sess-I' }, { type: 'user/message', message: { source: { kind: 'user' }, content: [{ type: 'text', text: '写一半被打断的问题' }] } })
  await emit('session/event', { id: 'sess-I' }, { type: 'assistant/message', turn: 1, interrupted: true, message: { source: { kind: 'model' }, content: [{ type: 'text', text: '回答到一半就被用户中止了' }] } })
  const before = fetchCalls.filter((c) => c.path === '/memories').length
  await emit('agent/turn-stopping', { agent, turn: 1 })
  env.setScope({ ...env.getScope(), coalesceIdleMs: 500 })
  await new Promise((r) => setTimeout(r, 1300))
  const after = fetchCalls.filter((c) => c.path === '/memories').length
  const bodies = fetchCalls.filter((c) => c.path === '/memories').map((c) => c.body)
  assert.ok(!bodies.some((b) => b && b.includes('写一半被打断')), '中断轮内容不得落库'); ok('中断轮整体跳过写入')
}

console.log('== 工具执行：search 成功形态 ==')
{
  const searchTool = env.tools[0]
  const result = await searchTool.execute({ query: '端口' }, { signal: new AbortController().signal })
  assert.equal(result.ok, true)
  assert.equal(result.data.count, 1)
  assert.equal(result.data.results[0].id, 'm-1'); ok('search 返回归一化 results')
  const rendered = searchTool.output.render({}, result)
  assert.equal(rendered[0].type, 'text'); ok('render 输出 text block')
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

console.log('== 网络失败重试与熔断 ==')
{
  failNextNetwork = true
  const searchTool = env.tools[0]
  const result = await searchTool.execute({ query: '重试' }, { signal: new AbortController().signal })
  assert.equal(result.ok, true); ok('连接级失败自动重试一次后成功')
}

console.log('== dispose 兜底 ==')
{
  for (const effect of [...env.effects].reverse()) {
    if (typeof effect.disposer === 'function') effect.disposer()
  }
  ok('全部 effect disposer 执行无异常')
}

console.log('\n全部通过：' + PASS.length + ' 项 ✓')
