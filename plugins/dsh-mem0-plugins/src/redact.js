/**
 * dsh-mem0-plugins — 上传前 payload 脱敏（B 组，2026-08-29 memorax 吸收一期）。
 *
 * 泄露路径：潮浪合并/直冲把「用户消息+助手回复」原文交给服务端 infer 抽取，
 * 原文里贴过的 API key / 私钥 / 口令会随抽取请求出网。本模块在写链路唯一入口
 * （coalesce.route）做正则闸：命中即替换 [REDACTED:label]，保留上下文语义，
 * 不整条丢弃——宁误杀（假 key / 占位符被打码）不漏放，会话原文与 UI 不受影响。
 *
 * 纯函数：无 IO、无 LLM；所有 pattern 均为 ASCII 级匹配，只做整段替换、
 * 不做 .length 切片，不会切开增补平面字符（emoji 等 surrogate pair 安全）。
 */

/**
 * 替换规则表。每条：label（打进 [REDACTED:label]）+ 全局正则 + 替换函数。
 * 顺序即执行顺序：块级（env/PEM）先于行级（头/键值）先于 token 级。
 */
const RULES = [
  {
    label: 'private-key',
    // PEM 私钥块：BEGIN...END 成对时整块打码（含 base64 体）
    re: /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----/g,
    replace: () => '[REDACTED:private-key]'
  },
  {
    label: 'private-key',
    // 孤儿 BEGIN 头（无 END 配对）：只打码头行，块体是不完整 base64、单独不可用
    re: /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/g,
    replace: () => '[REDACTED:private-key]'
  },
  {
    label: 'bearer-token',
    // Authorization: Bearer <token> 行：保头打值
    re: /(Authorization\s*:\s*Bearer\s+)([^\s'"`]+)/gi,
    replace: (m, head) => head + '[REDACTED:bearer-token]'
  },
  {
    label: 'api-key',
    // X-API-Key: <value> 行：保头打值（覆盖 curl 示例最常见的自托管 mem0 头）
    re: /(X-API-Key\s*:\s*)([^\s'"`]+)/gi,
    replace: (m, head) => head + '[REDACTED:api-key]'
  },
  {
    label: 'password',
    // password=/passwd= 键值：保键打值（值可为裸词或引号串）。
    // 前缀用 (?<![A-Za-z0-9]) 而非 \b（2026-09-01 审计 P2）：'_' 是词字符，
    // \b 在 DB_PASSWORD= / my_password= 这类下划线键名前不成立 → 单行 env 赋值
    // 命令/日志里漏放（5 行+ 的整段 .env 另有 env-block 折叠兜底，单行无兜）。
    // 后缀仍由 (\s*=\s*) 收紧：passwordhash= 不命中。
    re: /(?<![A-Za-z0-9])(passwd|password)(\s*=\s*)("[^"]*"|'[^']*'|\S+)/gi,
    replace: (m, key, eq) => key + eq + '[REDACTED:password]'
  },
  {
    label: 'openai-key',
    // OpenAI/DeepSeek 风格：sk- + ≥20 位字母数字（短示例串 sk-123 天然不命中）
    re: /sk-[A-Za-z0-9]{20,}/g,
    replace: () => '[REDACTED:openai-key]'
  },
  {
    label: 'aws-key',
    // AWS AccessKeyId：AKIA + 16 位大写/数字，两侧词边界防误吞更长 token
    re: /\bAKIA[0-9A-Z]{16}\b/g,
    replace: () => '[REDACTED:aws-key]'
  }
]

/** .env 形态判定：KEY=VALUE 行（大写下划线键 + 非空值）。 */
const ENV_LINE = /^[A-Z][A-Z0-9_]*=.+$/

/** .env 段内中性行：空行或 # 注释——不参与计数，也不打断连续段（真实 .env 常夹注释）。 */
const ENV_NEUTRAL = /^(#.*|\s*)$/

/** 累计 ≥ENV_MIN_LINES 行 .env 形态才视为整文件粘贴（避免误伤散文/代码里的个别赋值行）。 */
const ENV_MIN_LINES = 5

/**
 * 把文本中累计 ≥ENV_MIN_LINES 行（注释/空行夹缝不打断）的 .env 段整体折叠为一个占位行。
 * @returns {{ text: string, folded: number }} folded=折叠出的 env-block 段数
 */
function foldEnvBlocks(text) {
  const lines = text.split('\n')
  let out = ''
  let run = []       // 当前段的行（含夹缝中性行，不折叠时需原样回填）
  let envCount = 0   // 段内 KEY=VALUE 行数（阈值只数它）
  let folded = 0
  const flush = () => {
    if (!run.length) return
    if (envCount >= ENV_MIN_LINES) {
      out += (out ? '\n' : '') + '[REDACTED:env-block]'
      folded += 1
    } else {
      out += (out ? '\n' : '') + run.join('\n')
    }
    run = []
    envCount = 0
  }
  for (const line of lines) {
    // CRLF 兼容（2026-08-29 审计 R2）：JS 正则 . 与 $ 均不跨 \r，'A=1\r' 会判非 env 行
    // 致 Windows 粘贴的 .env 永不折叠——判定用剥 \r 裸行，回填保留原行不改字节
    const bare = line.endsWith('\r') ? line.slice(0, -1) : line
    if (ENV_LINE.test(bare)) {
      run.push(line)
      envCount += 1
    } else if (run.length && ENV_NEUTRAL.test(bare)) {
      run.push(line)
    } else {
      flush()
      out += (out ? '\n' : '') + line
    }
  }
  flush()
  return { text: out, folded }
}

/**
 * 对一段文本做 secrets 打码。
 * @param {string} text
 * @returns {{ text: string, hits: Array<{ label: string, count: number }> }}
 *   text=打码后文本（无命中时与入参同串）；hits=按 label 聚合的命中清单。
 *   env-block 折叠必须计入 hits——route 以 hits 非空为落替换文本的前提，
 *   漏报 = 折叠结果被整段丢弃、防线失效（2026-08-29 审计 P1 教训）。
 */
export function redactSecrets(text) {
  if (typeof text !== 'string' || !text) return { text: text || '', hits: [] }
  const env = foldEnvBlocks(text)
  let out = env.text
  const hits = []
  if (env.folded > 0) hits.push({ label: 'env-block', count: env.folded })
  for (const rule of RULES) {
    out = out.replace(rule.re, (...args) => {
      const replaced = rule.replace(...args)
      const found = hits.find((h) => h.label === rule.label)
      if (found) found.count += 1
      else hits.push({ label: rule.label, count: 1 })
      return replaced
    })
  }
  return { text: out, hits }
}
