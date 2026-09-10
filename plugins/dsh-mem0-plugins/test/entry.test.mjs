/**
 * dsh-mem0-plugins 宿主入口契约测试（entry-smoke）。
 *
 * 目的（dsh-plugin-audit「测试盲区」纪律）：node --check 只查语法、smoke.mjs
 * 只驱动 apply 桩、client-smoke.mjs 只测浏览器半——三路都绕开宿主入口的真实加载
 * 路径。本文件直接 import('../src/index.js')，任何导入名错误 / 顶层求值异常 /
 * 导出形状漂移都会在真实加载期当场炸出（dsh-prompt-injector 的 P0 先例：
 * `import { z } from '@deepseek-ai/schemastery'` 而 schemastery 只有 default
 * export，整包无法加载、插件装而不生效，而当时全部测试全绿）。
 *
 * 另守护两项声明契约：
 * - 宿主注入面：inject 必须只命名真实存在的服务（写错服务名插件会永远 pending）；
 * - dsh.engines.dsh 区间必须覆盖声明适配的目标版本（npm semver 预发布规则：
 *   预发布只被「区间内含同 [major,minor,patch] 元组预发布」的区间满足，
 *   单区间 >=0.1.2-alpha.3 <0.2.0 不覆盖 0.1.5-rc.1）。
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { createRequire } from 'node:module'

const PASS = []
const ok = (label) => { PASS.push(label); console.log('  ✓ ' + label) }
const here = dirname(fileURLToPath(import.meta.url))

// ---------------------------------------------------------------------------
// 1. 真实入口加载（P0 守卫）
// ---------------------------------------------------------------------------
const mod = await import('../src/index.js')
assert.equal(typeof mod.apply, 'function', 'src/index.js 必须导出 apply 函数')
assert.equal(typeof mod.Config, 'function', 'src/index.js 必须导出可调用的 Config schema（schemastery Schema 是可调用对象）')
assert.equal(mod.name, 'mem0', 'Cordis 插件短名必须是 mem0')
assert.equal(mod.MEM0_SETTINGS_NAMESPACE, 'mem0', 'settings 命名空间必须是 mem0')
ok('宿主入口真实 import 成功（apply / Config / name / 命名空间导出齐备）')

// ---------------------------------------------------------------------------
// 2. 注入面与配置默认值
// ---------------------------------------------------------------------------
assert.ok(Array.isArray(mod.inject), 'inject 必须是数组')
for (const svc of mod.inject) assert.equal(typeof svc, 'string', 'inject 项必须是字符串')
assert.deepEqual([...mod.inject].sort(), ['agents', 'systemPrompt', 'tools'], 'inject 面必须恰为 tools/systemPrompt/agents')
ok('注入面恰为 tools / systemPrompt / agents（含补注册所需的 agents）')

const resolved = mod.Config(undefined)
assert.equal(typeof resolved, 'object', 'Config 必须可解析为对象')
assert.equal(resolved.enabled, true, 'enabled 默认必须是 true（配置即启用）')
assert.equal(resolved.host, 'http://127.0.0.1:8888', 'host 默认值漂移')
assert.equal(resolved.requestTimeoutMs, 420000, 'requestTimeoutMs 默认值漂移')
assert.equal(resolved.outputMaxKb, 50, 'outputMaxKb 默认值漂移')
ok('Config 解析出默认值表（enabled / host / requestTimeoutMs=420000 / outputMaxKb=50）')

// ---------------------------------------------------------------------------
// 3. engines 区间守护（内置判定表，不引 semver 依赖，防测试随依赖漂移）
// ---------------------------------------------------------------------------
const pkg = JSON.parse(readFileSync(join(here, '..', 'package.json'), 'utf8'))
const range = pkg.dsh && pkg.dsh.engines && pkg.dsh.engines.dsh
assert.equal(typeof range, 'string', 'package.json 缺 dsh.engines.dsh 声明')
ok('package.json 声明了 dsh.engines.dsh')

/** 手写 semver 比较器：只处理本仓区间用到的形状（不引依赖，防测试随依赖漂移）。 */
function parseVersion(v) {
  const m = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/.exec(String(v).trim())
  if (!m) throw new Error('unparseable version: ' + v)
  return { nums: [Number(m[1]), Number(m[2]), Number(m[3])], pre: m[4] ? m[4].split('.') : [] }
}
function cmpPre(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i += 1) {
    const x = a[i]
    const y = b[i]
    if (x === undefined) return -1
    if (y === undefined) return 1
    const nx = /^\d+$/.test(x)
    const ny = /^\d+$/.test(y)
    if (nx && ny) {
      if (Number(x) !== Number(y)) return Number(x) < Number(y) ? -1 : 1
      continue
    }
    if (nx !== ny) return nx ? -1 : 1
    if (x !== y) return x < y ? -1 : 1
  }
  return 0
}
function compare(a, b) {
  for (let i = 0; i < 3; i += 1) {
    if (a.nums[i] !== b.nums[i]) return a.nums[i] < b.nums[i] ? -1 : 1
  }
  if (a.pre.length === 0 && b.pre.length === 0) return 0
  if (a.pre.length === 0) return 1
  if (b.pre.length === 0) return -1
  return cmpPre(a.pre, b.pre)
}
/** npm semver 预发布可见性规则：预发布版本只被「区间内含同 [M,m,p] 元组预发布」的区间满足。 */
function preReleaseVisible(version, comparators) {
  if (version.pre.length === 0) return true
  return comparators.some((c) => {
    const mv = parseVersion(c.version)
    return mv.nums[0] === version.nums[0] && mv.nums[1] === version.nums[1] && mv.nums[2] === version.nums[2] && mv.pre.length > 0
  })
}
function satisfies(version, rng) {
  const v = parseVersion(version)
  for (const group of String(rng).split('||')) {
    const parts = group.trim().split(/\s+/).filter(Boolean)
    const comparators = []
    let groupOk = true
    for (const part of parts) {
      const m = /^(>=|<=|>|<|=)?\s*(.+)$/.exec(part)
      const op = m[1] || '='
      comparators.push({ op, version: m[2] })
      const c = compare(v, parseVersion(m[2]))
      if (op === '>=' && c < 0) groupOk = false
      else if (op === '<=' && c > 0) groupOk = false
      else if (op === '>' && c <= 0) groupOk = false
      else if (op === '<' && c >= 0) groupOk = false
      else if (op === '=' && c !== 0) groupOk = false
    }
    if (groupOk && preReleaseVisible(v, comparators)) return true
  }
  return false
}

const TABLE = [
  ['0.1.2-alpha.3', true],
  ['0.1.2-rc.1', true],
  ['0.1.5-alpha.1', true],
  ['0.1.5-alpha.2', true],
  ['0.1.5-rc.1', true],
  ['0.1.5', true],
  ['0.1.6', true],
  ['0.1.3-alpha.1', false],
  ['0.2.0', false],
  ['0.0.1', false]
]
for (const [version, expected] of TABLE) {
  assert.equal(satisfies(version, range), expected, 'engines 区间对 ' + version + ' 的判定应为 ' + expected + '（区间=' + range + '）')
}
ok('engines 判定表 10 行逐行通过（含 0.1.5-rc.1 覆盖）')

// 反证：旧单区间不覆盖 0.1.5-rc.1 —— 这正是本次必须加析取的原因
assert.equal(satisfies('0.1.5-rc.1', '>=0.1.2-alpha.3 <0.2.0'), false, '旧单区间本不应覆盖 0.1.5-rc.1，判定器写反了')
ok('反证：旧单区间不覆盖 0.1.5-rc.1（故必须加析取，非冗余声明）')

// 交叉验证：同一判定表与宿主真实 semver 逐行一致（宿主不可解析则显式跳过，不假绿）
const req = createRequire(import.meta.url)
let semver = null
for (const candidate of ['/usr/lib/node_modules/@deepseek-ai/dsh/node_modules/semver', 'semver']) {
  try { semver = req(candidate); break } catch { /* next */ }
}
if (semver && typeof semver.satisfies === 'function') {
  for (const [version, expected] of TABLE) {
    assert.equal(semver.satisfies(version, range), expected, '宿主 semver 对 ' + version + ' 的判定与内置判定表不一致')
  }
  ok('内置判定器与宿主真实 semver.satisfies 逐行一致（10/10）')
} else {
  ok('宿主 semver 不可解析，跳过交叉验证（不假绿）')
}


// ---------------------------------------------------------------------------
// 4. 设置页文案 ↔ Host schema 默认值一致性（四处同步纪律的第五处：文案）
// ---------------------------------------------------------------------------
// 历史缺陷：hint.requestTimeoutMs 写「默认 300000」而 schema/spec/README 都是
// 420000 —— 设置页展示的数字与实际生效值不一致，用户按文案判断超时会误判。
// 判据：凡 hint 里以「默认 <数字>」形式点名的键，其数字必须等于 Config 默认值。
const clientSrc = readFileSync(join(here, '..', 'lib', 'client.js'), 'utf8')
const hintRe = /"hint\.([A-Za-z][\w]*)":\s*"((?:[^"\\]|\\.)*)"/g
const staleHints = []
let hintMatch
while ((hintMatch = hintRe.exec(clientSrc)) !== null) {
  const key = hintMatch[1]
  const text = hintMatch[2]
  const stated = [...text.matchAll(/默认\s*(\d+)/g)].map((m) => Number(m[1]))
  if (stated.length === 0) continue
  const actual = resolved[key]
  if (typeof actual !== 'number') continue
  if (!stated.includes(actual)) staleHints.push(key + ': 文案写 ' + stated.join('/') + '，实际 ' + actual)
}
assert.deepEqual(staleHints, [], '设置页 hint 文案里的「默认 N」与 schema 默认值不一致: ' + staleHints.join(' | '))
ok('设置页 hint 文案的「默认 N」与 schema 默认值逐键一致（含 requestTimeoutMs=420000 回归）')

// 键集合三处同步：Config / FIELDS / GROUPS 全等（缺一键 = 设置页调不到该开关）
const fFields = [...clientSrc.split('const FIELDS = [')[1].split('];')[0].matchAll(/key: "([\w]+)"/g)].map((m) => m[1])
const groupsBlock = clientSrc.split('const GROUPS = [')[1].split('];')[0]
const fGroups = [...groupsBlock.matchAll(/"([a-zA-Z][\w]*)"/g)].map((m) => m[1]).filter((k) => !k.startsWith('group.'))
const configKeys = Object.keys(resolved)
assert.deepEqual([...fFields].sort(), [...configKeys].sort(), 'client FIELDS 与 Host Config 键集合不一致')
assert.deepEqual([...fGroups].sort(), [...configKeys].sort(), 'client GROUPS 与 Host Config 键集合不一致')
ok('Config / client FIELDS / client GROUPS 三处键集合全等（' + configKeys.length + ' 键）')



// ---------------------------------------------------------------------------
// 5. spec() 数值归一化的回落值 ↔ schema 默认值一致（第五处同步：归一化层）
// ---------------------------------------------------------------------------
// 历史缺陷类别：src/index.js 的 spec() 里每个数值键都带一个 clampInt 回落默认值
// （第 4 参），它是 settings 层拿不到值时工具真正生效的值——与 schema 默认值不一致
// 就是「设置页显示 A、实际生效 B」。本仓历史上 fastpathChars 等键就出现过
// 「注释称可配置但三处接线缺失」的漂移，故这里把该层钉死。
// 只覆盖数值键（clampInt 第 4 参语义无歧义）；字符串/布尔键有意不纳入——它们的
// spec 表达式带两级回落（如 String(value.userId || '').trim() || 'dsh-user'），
// 且 host/userId/agentId/distillModel 的「全空」降级路径与 schema 的 composition
// 默认值本就不同（前者触发「未配置」提示，后者是 patch 层给的默认配置），
// 强行比对只会产生假警报。
const indexSrc = readFileSync(join(here, '..', 'src', 'index.js'), 'utf8')
const specBlock = indexSrc.split('const spec = () => {')[1].split('\n  }')[0]
const specNumeric = {}
// 抓 clampInt(...) 整体（含闭合括号）以及紧随其后的可选单位换算 `* 1024`，
// 例如 outputMaxBytes: clampInt(value.outputMaxKb, 1, 500, 50) * 1024
const specTermRe = /^\s{6}([a-zA-Z][\w]*):\s*clampInt\(([^)]*)\)\s*(?:\*\s*(\d+))?/gm
for (const m of specBlock.matchAll(specTermRe)) {
  const args = m[2].split(',').map((a) => a.trim())
  specNumeric[m[1]] = {
    schemaKey: m[1] === 'outputMaxBytes' ? 'outputMaxKb' : m[1],
    fallback: Number(args[3]),
    multiplier: m[3] === undefined ? 1 : Number(m[3])
  }
}
assert.equal(Object.keys(specNumeric).length, 20, 'spec() 数值键解析数应为 20（探针写错或 spec 结构变更时此断言先炸）')
const specDrift = []
for (const [key, spec] of Object.entries(specNumeric)) {
  const schemaDefault = resolved[spec.schemaKey]
  if (typeof schemaDefault !== 'number') { specDrift.push(key + ': schema 默认（' + spec.schemaKey + '）不是数字'); continue }
  if (!Number.isFinite(spec.fallback)) { specDrift.push(key + ': spec 回落值不可解析'); continue }
  const actual = spec.fallback * spec.multiplier
  const expected = schemaDefault * spec.multiplier
  if (actual !== expected) {
    specDrift.push(key + ': spec 回落 ' + actual + '（对照 ' + spec.schemaKey + '），schema 默认 ' + expected)
  }
}
assert.deepEqual(specDrift, [], 'spec() 归一化的数值回落值与 schema 默认值不一致: ' + specDrift.join(' | '))
// 单位换算倍率单独钉：上面的「倍率两侧同乘」会被约掉，等于没查倍率本身。
// outputMaxKb（设置页单位 KB）→ outputMaxBytes（回执截断实际用的字节）必须是 ×1024；
// 改成 ×1000 这类「看起来无害」的写法会让 50KB 预算悄悄缩水 2.4%。
assert.equal(specNumeric.outputMaxBytes && specNumeric.outputMaxBytes.multiplier, 1024,
  'outputMaxKb → outputMaxBytes 的换算倍率必须是 1024（×1000 会让字节预算缩水）')
ok('spec() 数值回落值与 schema 默认值逐键一致（' + Object.keys(specNumeric).length + ' 键；倍率 1024 独立断言）')

console.log('\n全部通过：' + PASS.length + ' 项 ✓')
