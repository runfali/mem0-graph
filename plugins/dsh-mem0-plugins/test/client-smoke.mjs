/**
 * dsh-mem0-plugins client bundle 结构加载测试。
 *
 * 运行：node test/client-smoke.mjs
 * 构造最小 window.__ModuleLoader__ + require stub（react/jsx-runtime/
 * primitives），加载 lib/client.js，执行 apply，验证：
 * 1. bundle id 与包名一致（dsh-client-modules 契约）
 * 2. locale 词典注册（zh/en 键集合一致、覆盖全部字段 label/hint）
 * 3. settingsScope 绑定 namespace=mem0
 * 4. settings.plugin.item 槽位注册：key/locale 正确，inject() 提供 hooks+actions
 * 5. 组件可创建元素（jsxs 调用不炸）且布尔/文本字段渲染分支齐全
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const PASS = []
const ok = (label) => { PASS.push(label); console.log('  ✓ ' + label) }

// ---- react stub（记录 createElement 调用树）----
function makeElement(type, props, ...children) {
  return { type, props: props || {}, children: children.flat().filter((c) => c !== null && c !== undefined) }
}
// 递归执行组件树（模拟 React 渲染）：子组件内部的 prop 缺陷在这里必须炸出来
let renderDepth = 0
function renderTree(node) {
  if (node === null || node === undefined || typeof node === 'string' || typeof node === 'number') return
  if (typeof node.type === 'function') {
    renderDepth += 1
    if (renderDepth > 50) throw new Error('component tree too deep — likely infinite recursion')
    const children = node.type(node.props)
    renderTree(children)
    renderDepth -= 1
    return
  }
  for (const child of node.children || []) renderTree(child)
}
let elementCount = 0
const reactStub = {
  // 默认展开卡片（Mem0Card 的折叠 useState(false) 会被强制 true），
  // 让 body 内全部字段组件进入渲染路径——折叠态会掩盖子组件缺陷
  useState: (init) => [typeof init === 'boolean' ? true : (typeof init === 'function' ? init() : init), () => {}],
  useSyncExternalStore: (subscribe, getSnapshot) => {
    subscribe(() => {})
    return getSnapshot()
  }
}
const jsxStub = (type, props) => { elementCount += 1; return makeElement(type, props) }
const jsxsStub = (type, props) => { elementCount += 1; return makeElement(type, ...(props.children !== undefined && Array.isArray(props.children) ? props.children : [])) }

// ---- primitives stub ----
const primitivesStub = new Proxy({}, { get: (target, name) => function Icon() {} })

// ---- ModuleLoader / require stub ----
let bundleFactory = null
let bundleId = null
globalThis.window = {
  __ModuleLoader__: {
    load({ id, factory }) {
      bundleId = id
      bundleFactory = factory
    }
  }
}

const requireStub = (specifier) => {
  if (specifier === 'react') return reactStub
  if (specifier === 'react/jsx-runtime') return { jsx: jsxStub, jsxs: jsxsStub }
  if (specifier === '@deepseek-ai/dsh-client-ui-primitives') return primitivesStub
  throw new Error('unexpected require: ' + specifier)
}

new Function('code', 'return eval(code)')(
  readFileSync(new URL('../lib/client.js', import.meta.url), 'utf8')
)

console.log('== bundle 加载 ==')
assert.equal(bundleId, 'dsh-mem0-plugins', 'bundle id 必须等于包名'); ok('bundle id = dsh-mem0-plugins')
assert.ok(bundleFactory, 'factory 存在')

const exportsRef = bundleFactory(requireStub)
assert.equal(typeof exportsRef.apply, 'function'); ok('exports.apply 可调用')
assert.deepEqual(exportsRef.inject, ['slots', 'locale', 'settingsScope']); ok('inject 服务清单正确')

console.log('== apply 注册链 ==')
const registeredLocales = new Map()
const slotRegistrations = []
const effects = []
const ctx = {
  fiber: { state: 0 },
  effect(fn, label) { effects.push({ label, disposer: fn() }) },
  locale: { register: (ns, dict) => registeredLocales.set(ns, dict) },
  settingsScope: { bind: ({ namespace }) => { assert.equal(namespace, 'mem0'); lastScope = makeScope(namespace); return lastScope } },
  slots: {
    inject(slotName, gen) {
      const iterator = gen()
      for (const reg of iterator) slotRegistrations.push({ slotName, reg })
    },
    register(def, component) { return { def, component } }
  }
}

function makeScope(namespace) {
  assert.equal(namespace, 'mem0', 'settingsScope namespace 应为 mem0')
  const snapshotValue = {
    status: 'ready',
    writable: true,
    value: { enabled: false, host: 'http://127.0.0.1:8888', apiKey: '', topK: 10, syncEnabled: true, coalesceIdleMs: 5000, breakerCooldownMs: 120000 },
    base: {},
    user: { apiKey: 'sk-test' }
  }
  const listeners = new Set()
  const writes = []
  const defaultImpl = async (k, v) => {
    writes.push([k, v])
    // 模拟宿主 settingsScope.set 的副作用：镜像快照 user 层出现新值
    snapshotValue.user = { ...(snapshotValue.user || {}), [k]: v }
    listeners.forEach((fn) => fn())
    return true
  }
  let setImpl = defaultImpl
  const scope = {
    getSnapshot: () => snapshotValue,
    subscribe: (fn) => { listeners.add(fn); return () => listeners.delete(fn) },
    set: (k, v) => setImpl(k, v),
    unset: async () => true,
    writes,
    set0: defaultImpl,
    overrideSet: (impl) => { setImpl = impl }
  }
  return scope
}

let lastScope = null

exportsRef.apply(ctx)

const dict = registeredLocales.get('mem0')
assert.ok(dict, 'locale 词典已注册'); ok('locale[mem0] 注册')
assert.ok(dict.zh['card.title'] && dict.en['card.title']); ok('zh/en 卡片标题齐备')
for (const key of ['enabled', 'host', 'apiKey', 'userId', 'agentId', 'forceRecallStep', 'topK', 'rerank', 'distillEnabled', 'distillMinChars', 'distillInputMaxChars', 'distillBaseUrl', 'distillApiKey', 'distillModel', 'distillTimeoutMs', 'distillRetryAfterMs', 'syncEnabled', 'redactEnabled', 'coalesceEnabled', 'coalesceIdleMs', 'coalesceWindowMs', 'coalesceMaxTurns', 'coalesceMaxChars', 'fastpathChars', 'sliceThreshold', 'slicePieceChars', 'queueMaxLen', 'maxBucketAgeMs', 'breakerThreshold', 'breakerCooldownMs', 'requestTimeoutMs', 'feedbackEnabled', 'outputMaxLines', 'outputMaxKb', 'itemMaxChars']) {
  assert.ok(dict.zh['field.' + key], '缺少 field.' + key)
  assert.ok(dict.zh['hint.' + key], '缺少 hint.' + key)
}
ok('35 个字段的 label/hint 文案全覆盖（含切片三键与脱敏开关）')
// 工具输出硬化三键（2026-08-26）：host schema/spec/client FIELDS/翻译键四处同步
// 的最后一环——漏加翻译键时设置页显示英文键名（dsh-skill-curator 同类教训）
for (const key of ['outputMaxLines', 'outputMaxKb', 'itemMaxChars']) {
  assert.ok(dict.zh['field.' + key] && dict.zh['hint.' + key], '输出硬化键缺中文文案: ' + key)
}
assert.ok(dict.zh['group.output'], '缺少 group.output 分组标题')
ok('输出硬化三键中文 label/hint + 分组标题齐备')

assert.equal(slotRegistrations.length, 1); ok('settings.plugin.item 槽位注册 ×1')
const { def, component } = slotRegistrations[0].reg
assert.equal(def.key, 'mem0'); ok('槽位 key 与命名空间一致（服务端 served 集合才能派发）')
assert.equal(def.locale, 'mem0'); ok('槽位 locale 指向词典')

console.log('== 卡片渲染与表单动作 ==')
const injected = def.inject()
assert.ok(injected.hooks && injected.hooks.mem0, 'inject 提供 hooks.mem0 store')
assert.equal(typeof injected.save, 'function')
assert.equal(typeof injected.discard, 'function')
assert.equal(typeof injected.edit, 'function')
assert.equal(typeof injected.toggle, 'function')
assert.equal(typeof injected.resetField, 'function'); ok('hooks + 五个动作齐全')

const beforeCount = elementCount
const el = component({
  t: (key) => dict.zh[key] || key,
  useMem0: (selector) => selector(injected.hooks.mem0.getSnapshot()),
  ...injected
})
assert.ok(el); ok('卡片组件可渲染出元素')
renderTree(el); ok('组件树递归渲染无异常（FieldRow t 透传等子组件缺陷会在此炸出）')
// 展开 state 默认 false → 只渲染折叠头；用内部展开无法直接驱动（useState stub），
// 但投影快照字段完整性可以验证：
const snap = injected.hooks.mem0.getSnapshot()
assert.ok(snap.shell.available && snap.shell.writable); ok('shell 快照 available/writable 正确')
assert.ok(snap.apiKey.overridden === true, 'user 层覆盖字段应显示已覆盖'); ok('apiKey 覆盖态识别')
assert.equal(snap.host.stagedText, 'http://127.0.0.1:8888'); ok('文本字段回显 section 值')
assert.equal(snap.enabled.stagedBool, false); ok('布尔字段回显 section 值')
assert.ok(elementCount > beforeCount); ok('jsx 渲染计数增长（真实走到组件体）')

console.log('== 表单 save 流程（真链） ==')
{
  // 编辑为不同值 → dirty；save → 落库写 host + 脏标记清除
  injected.edit('host', 'http://changed:9999')
  let snap = injected.hooks.mem0.getSnapshot()
  assert.equal(snap.shell.dirty, true); ok('编辑不同值后 dirty=true')
  await injected.save()
  snap = injected.hooks.mem0.getSnapshot()
  assert.equal(snap.shell.dirty, false); ok('保存后 dirty=false')
  assert.ok(lastScope.writes.some(([k]) => k === 'host'), 'host 写入已记录')
  ok('scope.set 被调用（host 变更落库）')

  // 暂存等于生效值：plan 空 → 无脏标记
  const beforeWrites = lastScope.writes.length
  injected.edit('host', 'http://127.0.0.1:8888') // 等于 mock section 原值
  snap = injected.hooks.mem0.getSnapshot()
  assert.equal(snap.shell.dirty, false); ok('暂存等于生效值时不产生脏标记')
  await injected.save()
  snap = injected.hooks.mem0.getSnapshot()
  assert.equal(snap.shell.dirty, false); ok('空 plan save 不炸且保持干净')
  assert.equal(lastScope.writes.length, beforeWrites); ok('无变化时不触发写入')
}



console.log('== 审计回归：invalid 原文与保存期并发编辑 ==')
{
  // 1. 非法数字编辑：输入框必须显示用户原文，绝不显示 "undefined"
  injected.edit('topK', 'abc')
  let snap = injected.hooks.mem0.getSnapshot()
  assert.equal(snap.topK.stagedText, 'abc'); ok('非法数字暂存显示原文')
  assert.equal(snap.topK.invalid, true); ok('非法数字标记 invalid')
  injected.discard()

  // 2. 保存(挂起)期间并发编辑同一字段：完成后该编辑必须保留
  let release
  const gate = new Promise((r) => { release = r })
  let saveDone = false
  lastScope.overrideSet(async (k, v) => {
    await gate // 挂起直到测试放行
    return lastScope.set0(k, v)
  })
  injected.edit('topK', '38')
  const savePromise = injected.save().then(() => { saveDone = true })
  // 保存挂起中并发编辑
  injected.edit('topK', '49')
  release()
  await savePromise
  snap = injected.hooks.mem0.getSnapshot()
  assert.equal(snap.topK.stagedText, '49'); ok('保存期并发编辑保留（引用级删除）')
  assert.equal(snap.shell.dirty, true); ok('并发编辑后仍标记未保存')
  assert.ok(lastScope.writes.some(([k, v]) => k === 'topK' && v === 38), '保存确实写了 topK'); ok('保存写入发生')
  injected.discard()
  lastScope.overrideSet(null)
}

console.log('== 审计回归：数字字段范围 clamp（CL1）==')
{
  // 越界输入在编辑时即收敛到宿主 schema 范围，不再被 scope.set 静默拒绝
  injected.edit('topK', '1000')
  let snap = injected.hooks.mem0.getSnapshot()
  assert.equal(snap.topK.stagedText, '50'); ok('越界上限自动 clamp：1000 → 50')
  assert.equal(snap.topK.invalid, false); ok('clamp 后不标 invalid')
  injected.discard()

  injected.edit('queueMaxLen', '1')
  snap = injected.hooks.mem0.getSnapshot()
  assert.equal(snap.queueMaxLen.stagedText, '5'); ok('越界下限自动 clamp：1 → 5')
  assert.equal(snap.queueMaxLen.invalid, false); ok('下限 clamp 不标 invalid')
  injected.discard()
}

console.log('\n全部通过：' + PASS.length + ' 项 ✓')
