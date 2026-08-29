import { test } from 'node:test'
import assert from 'node:assert/strict'
import { redactSecrets } from '../src/redact.js'

const hitsOf = (r, label) => (r.hits.find((h) => h.label === label) || { count: 0 }).count

// ---- openai-key（sk- + ≥20 位） ------------------------------------------------

test('openai-key: sk- 长串打码，label 计数正确', () => {
  const r = redactSecrets('用这个 key 跑：sk-Abc123Def456Ghi789Jkl0')
  assert.equal(r.text.includes('sk-Abc123Def456Ghi789Jkl0'), false)
  assert.ok(r.text.includes('[REDACTED:openai-key]'))
  assert.equal(hitsOf(r, 'openai-key'), 1)
})

test('openai-key 反例：短示例串 sk-123 与普通英文句不命中', () => {
  const r = redactSecrets('示例写法 sk-12345 只是占位符；risk-averse 是普通单词，没事。')
  assert.deepEqual(r.hits, [])
  assert.ok(r.text.includes('sk-12345'))
})

// ---- aws-key ------------------------------------------------------------------

test('aws-key: AKIA+16 位打码；15 位与更长短语不命中', () => {
  const r = redactSecrets('AKIAIOSFODNN7EXAMPLE 是文档经典示例')
  assert.ok(r.text.includes('[REDACTED:aws-key]'))
  const neg = redactSecrets('AKIAIOSFODNN7EXAMPL 只有 15 位后缀，别误杀')
  assert.deepEqual(neg.hits, [])
})

// ---- private-key（PEM） --------------------------------------------------------

test('private-key: 成对 PEM 块整体折叠为一个占位符', () => {
  const pem = '-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA7abc\n-----END RSA PRIVATE KEY-----'
  const r = redactSecrets('贴个证书看看\n' + pem + '\n完事')
  assert.equal(r.text.includes('MIIEowIBAAKCAQEA7abc'), false)
  assert.equal(r.text.match(/REDACTED/g).length, 1)
  assert.equal(hitsOf(r, 'private-key'), 1)
})

test('private-key: 孤儿 BEGIN 头（无 END）也打码', () => {
  const r = redactSecrets('-----BEGIN OPENSSH PRIVATE KEY----- 后面断线了')
  assert.equal(r.text.includes('-----BEGIN OPENSSH PRIVATE KEY-----'), false)
  assert.equal(hitsOf(r, 'private-key'), 1)
})

// ---- bearer-token / api-key 头行 ----------------------------------------------

test('bearer-token: 保头打值；api-key: X-API-Key 值打码', () => {
  const r = redactSecrets('curl -H "Authorization: Bearer eyJhbGciOiJIUzI1.abc" -H "X-API-Key: super-secret-key-42" ...')
  assert.equal(r.text.includes('eyJhbGciOiJIUzI1.abc'), false)
  assert.equal(r.text.includes('super-secret-key-42'), false)
  assert.ok(r.text.includes('Authorization: Bearer [REDACTED:bearer-token]'))
  assert.ok(r.text.includes('X-API-Key: [REDACTED:api-key]'))
})

// ---- password 键值 -------------------------------------------------------------

test('password: 裸词与引号串都打码，键保留；无 = 的普通词不命中', () => {
  const r = redactSecrets('数据库 password=hunter2，另外 passwd="let me in" 都要换掉')
  assert.ok(r.text.includes('password=[REDACTED:password]'))
  assert.ok(r.text.includes('passwd=[REDACTED:password]'))
  const neg = redactSecrets('password 这个词单独出现没有值，别打码')
  assert.deepEqual(neg.hits, [])
})

// ---- .env 块（KEY=VALUE 累计 ≥5 行；注释/空行夹缝不打断） -----------------------

test('env-block: 连续 5 行 KEY=VALUE 整块折叠并计入 hits', () => {
  const env5 = 'DB_HOST=localhost\nDB_USER=root\nDB_PASS=secret\nAPP_ENV=prod\nAPP_PORT=8888'
  const r = redactSecrets(env5)
  assert.equal(r.text, '[REDACTED:env-block]')
  assert.equal(hitsOf(r, 'env-block'), 1)
})

test('env-block: 注释/空行夹缝不打断——累计 5 行仍整体折叠（2026-08-29 审计 P2 回归）', () => {
  const mixed = 'DB_HOST=localhost\nDB_USER=root\nDB_PASS=secret\n# 口令在下一行\nAPP_ENV=prod\n\nAPP_PORT=8888'
  const r = redactSecrets(mixed)
  assert.equal(r.text, '[REDACTED:env-block]')
  assert.equal(r.text.includes('DB_PASS=secret'), false)
})

test('env-block: 累计 4 行不折叠，原文逐行保留（阈值边界）', () => {
  const env4 = 'DB_HOST=localhost\nDB_USER=root\n# 注释夹缝\nDB_PASS=secret\nAPP_ENV=prod'
  const r = redactSecrets(env4)
  assert.equal(r.text, env4)
  assert.deepEqual(r.hits, [])
})

// ---- 组合与边界 ----------------------------------------------------------------

test('多类命中聚合到同一 hits；无命中返回原文与空清单', () => {
  const r = redactSecrets('key1=sk-Abc123Def456Ghi789Jkl0 key2=AKIAIOSFODNN7EXAMPLE')
  assert.equal(r.hits.length, 2)
  const clean = redactSecrets('今天天气不错，聊点别的。')
  assert.equal(clean.hits.length, 0)
  assert.equal(clean.text, '今天天气不错，聊点别的。')
})

test('码点安全：emoji 与增补平面字符在打码后原样保留', () => {
  const r = redactSecrets('部署好了 🚀🎉 key 是 sk-Abc123Def456Ghi789Jkl0 谢谢 🀄')
  assert.ok(r.text.includes('🚀🎉') && r.text.includes('🀄'))
  assert.ok(r.text.includes('[REDACTED:openai-key]'))
})

test('边界输入：空串/非字符串安全返回', () => {
  assert.deepEqual(redactSecrets(''), { text: '', hits: [] })
  assert.deepEqual(redactSecrets(null), { text: '', hits: [] })
})
