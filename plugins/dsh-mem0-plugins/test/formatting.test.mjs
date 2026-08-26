/**
 * dsh-mem0-plugins — formatting.js 单元测试（node:test，零依赖零 mock）。
 * 运行：node --test test/
 *
 * 覆盖 plan/2026-08-26-tool-output-hardening.md §4.3 全部条目：
 * age 边界（含无效 ISO/未来时间）、类别降级链、单条截断（中文/emoji 不拆半）、
 * 换行净化、graph 片段（无 score/created_at/metadata）缺失省略、行数/字节截断
 * 标记（单触发与双触发）、空结果、score 缺失省略、阈值参数化、缺省回落 DEFAULT_*。
 */
import assert from 'node:assert/strict'
import { test } from 'node:test'

import {
  DEFAULT_MAX_ITEM_CHARS,
  DEFAULT_MAX_RESULT_BYTES,
  DEFAULT_MAX_RESULT_LINES,
  buildResultList,
  formatAge,
  formatCategory,
  formatMemoryLine,
  truncateOutput
} from '../src/formatting.js'

const HOUR = 3600 * 1000
const DAY = 24 * HOUR

// ---- formatAge ----------------------------------------------------------------

test('formatAge: 分钟级边界', () => {
  const now = Date.now()
  assert.equal(formatAge(new Date(now - 5 * 60 * 1000).toISOString()), '5m ago')
  assert.equal(formatAge(new Date(now - 59 * 60 * 1000).toISOString()), '59m ago')
  // 59m59s 仍属分钟档（floor）
  assert.equal(formatAge(new Date(now - 59 * 60 * 1000 - 59 * 1000).toISOString()), '59m ago')
})

test('formatAge: 小时/天边界', () => {
  const now = Date.now()
  assert.equal(formatAge(new Date(now - 60 * 60 * 1000).toISOString()), '1h ago')
  assert.equal(formatAge(new Date(now - 23 * HOUR).toISOString()), '23h ago')
  assert.equal(formatAge(new Date(now - 24 * HOUR).toISOString()), '1d ago')
  assert.equal(formatAge(new Date(now - 30 * DAY).toISOString()), '30d ago')
})

test('formatAge: 无效/缺失/未来时间', () => {
  assert.equal(formatAge(undefined), null)
  assert.equal(formatAge(null), null)
  assert.equal(formatAge(''), null)
  assert.equal(formatAge('not-a-date'), null)
  assert.equal(formatAge('2026-13-99T99:99:99Z'), null)
  // 未来时间（时钟偏差）clamp 到 0，不产生负数
  assert.equal(formatAge(new Date(Date.now() + HOUR).toISOString()), '0m ago')
})

// ---- formatCategory -----------------------------------------------------------

test('formatCategory: 降级链 memory_type → categories[0] → memory', () => {
  assert.equal(formatCategory({ metadata: { memory_type: 'FACTS' } }), 'FACTS')
  assert.equal(formatCategory({ metadata: { categories: ['alpha', 'beta'] } }), 'alpha')
  assert.equal(formatCategory({ metadata: { memory_type: '', categories: ['x'] } }), 'x')
  assert.equal(formatCategory({ metadata: {} }), 'memory')
  assert.equal(formatCategory({ metadata: 'not-an-object' }), 'memory')
  assert.equal(formatCategory({}), 'memory')
})

// ---- formatMemoryLine：单条截断 -----------------------------------------------

test('单条截断：超限按码点截断 + 显式标记，中文不拆半', () => {
  const item = { id: 'abc', memory: '中'.repeat(600), score: 0.87 }
  const line = formatMemoryLine(item, 1, { itemMaxChars: 500 })
  assert.ok(line.startsWith('1. [memory] ' + '中'.repeat(500) + '…[截断]'), '500 个完整汉字 + 截断标记')
  assert.ok(line.endsWith('[mem0:abc] (score 0.87)'))
  // 未超限原样返回
  const short = formatMemoryLine({ id: 'x', memory: '短' }, 1, { itemMaxChars: 500 })
  assert.ok(short.includes('短'))
  assert.ok(!short.includes('截断'))
})

test('单条截断：emoji（代理对）不拆半', () => {
  const emoji = '😀'.repeat(40) // 80 个 UTF-16 单元
  const item = { id: 'e1', memory: emoji + '尾部', score: 0.5 }
  const line = formatMemoryLine(item, 1, { itemMaxChars: 30 })
  // Array.from 按码点：30 个完整 emoji + 截断标记，无孤立代理对
  assert.ok(line.startsWith('1. [memory] ' + '😀'.repeat(30) + '…[截断]'))
  assert.ok(!line.includes('\uD83D\uDE00'.slice(0, 1) + '尾'), '无拆半字符')
})

test('单条截断：缺省回落 DEFAULT_MAX_ITEM_CHARS', () => {
  const item = { id: 'd1', memory: 'x'.repeat(DEFAULT_MAX_ITEM_CHARS + 10), score: 0.1 }
  const line = formatMemoryLine(item, 1, {})
  assert.ok(line.startsWith('1. [memory] ' + 'x'.repeat(DEFAULT_MAX_ITEM_CHARS) + '…[截断]'))
})

// ---- formatMemoryLine：换行净化 + graph 片段缺失省略 ---------------------------

test('换行净化：多行记忆 → 单行空格，一行=一条', () => {
  const item = { id: 'm1', memory: '第一行\n第二行\r\n第三行\r结尾', score: 0.9 }
  const line = formatMemoryLine(item, 3, { itemMaxChars: 1000 })
  assert.equal(line, '3. [memory] 第一行 第二行 第三行 结尾 [mem0:m1] (score 0.90)')
  assert.ok(!line.includes('\n'), '单条输出内部不出现换行')
})

test('graph 片段：无 score/created_at/metadata → 对应段省略、仍有文本与 id', () => {
  const graphItem = { id: 'g-1', memory: 'workspace 配置为 origin dsh-mem0-plugins' }
  const line = formatMemoryLine(graphItem, 1, {})
  assert.equal(line, '1. [memory] workspace 配置为 origin dsh-mem0-plugins [mem0:g-1]')
  assert.ok(!line.includes('score') && !line.includes('ago'))
})

test('score 缺失省略 / id 缺失省略 / created_at 存在则显示 age', () => {
  const noScore = formatMemoryLine({ id: 'x', memory: '文本', created_at: new Date(Date.now() - 3 * DAY).toISOString() }, 1, {})
  assert.ok(noScore.includes('(3d ago)') && !noScore.includes('score'), '无 score 只省略 score')
  const noId = formatMemoryLine({ memory: '文本', score: 0.42 }, 2, {})
  assert.equal(noId, '2. [memory] 文本 (score 0.42)')
})

// ---- buildResultList ----------------------------------------------------------

test('buildResultList: 空数组/非数组 → No relevant memories found.', () => {
  assert.equal(buildResultList([]), 'No relevant memories found.')
  assert.equal(buildResultList(null), 'No relevant memories found.')
  assert.equal(buildResultList(undefined), 'No relevant memories found.')
})

test('buildResultList: 多结果编号从 1 起、逐行输出、无 JSON 壳', () => {
  const items = [
    { id: 'a', memory: '偏好短句', score: 0.91, metadata: { memory_type: 'PREFERENCES' } },
    { id: 'b', memory: '部署基线', score: 0.87, created_at: new Date(Date.now() - 2 * DAY).toISOString() },
    { id: 'g-2', memory: 'graph 片段样例' }
  ]
  const text = buildResultList(items, {})
  const lines = text.split('\n')
  assert.equal(lines.length, 3)
  assert.ok(lines[0].startsWith('1. [PREFERENCES] 偏好短句 '))
  assert.ok(lines[0].includes('[mem0:a] (score 0.91)'))
  assert.ok(lines[1].includes('(2d ago)') && lines[1].includes('(score 0.87)'))
  assert.equal(lines[2], '3. [memory] graph 片段样例 [mem0:g-2]')
  assert.ok(!text.includes('{') && !text.includes('"id"'))
})

// ---- truncateOutput -----------------------------------------------------------

test('truncateOutput: 行数超限保留头部 + 显式标记', () => {
  const text = Array.from({ length: 5 }, (_, i) => 'line' + i).join('\n')
  const out = truncateOutput(text, { maxLines: 3, maxBytes: 1e9 })
  assert.equal(out, 'line0\nline1\nline2\n[Output truncated: showing 3 of 5 lines]')
  // 行+字节双触发（行截断后仍超字节）→ 两个原因都报，顺序行先字节后
  const both = truncateOutput(text, { maxLines: 4, maxBytes: 10 })
  assert.ok(both.includes('[Output truncated: showing 4 of 5 lines, cut at 0KB]'))
})

test('truncateOutput: 字节超限按行先砍、单行仍超则码点削减（不拆半）', () => {
  // 每行 "中"x40 = 120 字节；4 行共 483 字节（含换行）；上限 250 字节 → 砍到 2 行
  const text = ['中'.repeat(40), '中'.repeat(40), '中'.repeat(40), '中'.repeat(40)].join('\n')
  const out = truncateOutput(text, { maxLines: 1e9, maxBytes: 250 })
  assert.ok(out.startsWith('中'.repeat(40) + '\n' + '中'.repeat(40)), '保留头部两行')
  assert.ok(out.endsWith('cut at 0KB]'))
  // 单行超限：40 个汉字 = 120 字节，上限 60 → 保留 20 个完整汉字
  const single = truncateOutput('中'.repeat(40), { maxLines: 1e9, maxBytes: 60 })
  assert.ok(single.startsWith('中'.repeat(20)), '单行按码点削减不拆半')
  assert.ok(single.endsWith('cut at 0KB]'))
})

test('truncateOutput: 行+字节双触发 → 两个原因都报，顺序行先字节后', () => {
  const text = Array.from({ length: 5 }, (_, i) => 'abc-' + i).join('\n') // 5 行 24 字节
  const out = truncateOutput(text, { maxLines: 2, maxBytes: 10 })
  assert.ok(out.includes('[Output truncated: showing 2 of 5 lines, cut at 0KB]'))
})

test('truncateOutput: 未超限原样返回、非字符串兜底、空串直返', () => {
  const text = 'a\nb\nc'
  assert.equal(truncateOutput(text, { maxLines: 100, maxBytes: 1e6 }), text)
  assert.equal(truncateOutput(undefined), '')
  assert.equal(truncateOutput(null), '')
  assert.equal(truncateOutput(42), '42')
  assert.equal(truncateOutput(''), '')
})

test('truncateOutput: 缺省回落 DEFAULT_* 常量', () => {
  const manyLines = Array.from({ length: DEFAULT_MAX_RESULT_LINES + 5 }, (_, i) => 'l' + i).join('\n')
  const byLines = truncateOutput(manyLines, {})
  assert.ok(byLines.includes('showing ' + DEFAULT_MAX_RESULT_LINES + ' of ' + (DEFAULT_MAX_RESULT_LINES + 5) + ' lines'))
  // 字节：构造一个超 DEFAULT_MAX_RESULT_BYTES 的单行
  const big = 'x'.repeat(DEFAULT_MAX_RESULT_BYTES + 1000)
  const byBytes = truncateOutput(big, {})
  assert.ok(byBytes.includes('[Output truncated: cut at ' + Math.round(DEFAULT_MAX_RESULT_BYTES / 1024) + 'KB]'))
})

test('truncateOutput: 截断标记本身不参与字节预算（显式信号优先）', () => {
  // 上限极小、必触发字节截断：输出以截断标记收尾而不是被砍掉的残句
  const out = truncateOutput('一二三四五', { maxLines: 1e9, maxBytes: 1 })
  assert.ok(out.endsWith(']'), '标记完整保留')
  assert.ok(out.includes('[Output truncated:'))
})