/**
 * dsh-mem0-plugins — 工具输出硬化（截断 + 紧凑格式化）。
 *
 * 纯函数模块，零外部依赖、独立可测（node:test 直测，不需 mock 任何 runtime）。
 * 只服务于模型可见的 render 层；不触碰工具参数、execute 返回的 data 结构、
 * 召回/写入链路、client 侧与服务端。
 *
 * 阈值不硬编码在本模块内：全部由调用方（spec() 归一化后的设置值）传入，
 * 模块内仅保留 DEFAULT_* 常量作为未传参时的兜底，量级对齐官方 deepseek-plugin
 * output.ts，但 itemMaxChars 上调为 1000（2026-08-26 全库实测：我方记忆
 * p95=497 / max=999 字符，官方 500 会日常误伤 5.3% 技术档长记忆）。
 *
 * 两个实现坑（2026-08-26 实证补充，详见 plan/2026-08-26-tool-output-hardening.md）：
 * ① 记忆文本含换行（版本记录等多行档）——formatMemoryLine 先净化换行为空格，
 *    保证「一行 = 一条」的行结构与总行数统计不失真；
 * ② /search 结果可混入 source=graph 片段（实测占比可达 1/3）：无 score（字段是
 *    rerank_score）/created_at/updated_at/metadata 但有 memory 文本——自然落入
 *    缺失省略分支，无需特判，但必须容忍。
 */

export const DEFAULT_MAX_RESULT_LINES = 200 // 总行数上限（对齐官方 output.ts）
export const DEFAULT_MAX_RESULT_BYTES = 50_000 // 总字节上限
export const DEFAULT_MAX_ITEM_CHARS = 1000 // 单条记忆文本上限（按码点）

const MINUTE = 60 * 1000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

const encoder = new TextEncoder()
const utf8Length = (text) => encoder.encode(text).length

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0
}

/**
 * ISO 时间戳 → 人类可读的粗略年龄。无效/缺失 → null（调用方省略该段）。
 * 未来时间（时钟偏差）clamp 到 0。「对齐官方语义」：<60m → "Xm ago"；
 * <24h → "Xh ago"；否则 → "Xd ago"，向下取整。
 */
export function formatAge(iso) {
  if (!isNonEmptyString(iso)) return null
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return null
  const diff = Math.max(0, Date.now() - t)
  if (diff < HOUR) return Math.floor(diff / MINUTE) + 'm ago'
  if (diff < DAY) return Math.floor(diff / HOUR) + 'h ago'
  return Math.floor(diff / DAY) + 'd ago'
}

/**
 * 类别降级链：metadata.memory_type → metadata.categories[0] → 'memory'。
 * metadata 非对象（graph 片段无此字段）时直接回退 'memory'。
 */
export function formatCategory(item) {
  const meta = item && item.metadata
  if (meta && typeof meta === 'object' && !Array.isArray(meta)) {
    if (isNonEmptyString(meta.memory_type)) return meta.memory_type
    if (Array.isArray(meta.categories)) {
      const first = meta.categories.find((c) => typeof c === 'string' && c.length > 0)
      if (first !== undefined) return first
    }
  }
  return 'memory'
}

/**
 * 按码点截断文本到 itemMaxChars，尾部追加显式截断标记（模型可见，不留静默截断）。
 * 中文/emoji 用 Array.from 取码点，天然不拆半字符。超限才动作，未超原样返回。
 */
function truncateItemText(text, itemMaxChars) {
  const limit = Number.isFinite(itemMaxChars) && itemMaxChars > 0 ? Math.trunc(itemMaxChars) : DEFAULT_MAX_ITEM_CHARS
  const chars = Array.from(text)
  if (chars.length <= limit) return text
  return chars.slice(0, limit).join('') + '…[截断]'
}

/**
 * 单条记忆 → 紧凑行：
 *   `1. [类别] 文本 (3d ago) [mem0:id] (score 0.87)`
 * 缺失字段对应省略：score 缺失省略 "(score …)"；created_at 缺失省略 "(age)"；
 * id 缺失省略 "[mem0:…]"（均为 graph 片段等真实场景）。
 * 记忆文本先净化换行/CR 为空格（多行档不破坏行结构），再按码点截断。
 */
export function formatMemoryLine(item, index, { itemMaxChars } = {}) {
  const rawText = item && typeof item.memory === 'string' ? item.memory : ''
  const text = truncateItemText(rawText.replace(/\r\n|\r|\n/g, ' '), itemMaxChars)
  let line = index + '. [' + formatCategory(item) + '] ' + text
  const age = formatAge(item && item.created_at)
  if (age !== null) line += ' (' + age + ')'
  if (item && isNonEmptyString(item.id)) line += ' [mem0:' + item.id + ']'
  if (item && typeof item.score === 'number' && Number.isFinite(item.score)) {
    line += ' (score ' + item.score.toFixed(2) + ')'
  }
  return line
}

/**
 * 搜索结果列表 → 紧凑文本块。空数组/非数组 → "No relevant memories found."。
 * 每条经 formatMemoryLine 单行化（内部换行已净化），join 后行数即条数，
 * 供 truncateOutput 的行数统计保持真实语义。
 */
export function buildResultList(items, { itemMaxChars } = {}) {
  if (!Array.isArray(items) || items.length === 0) return 'No relevant memories found.'
  return items.map((item, i) => formatMemoryLine(item, i + 1, { itemMaxChars })).join('\n')
}

/**
 * 输出总上限截断：超行数/超字节时保留头部，尾部追加显式说明：
 *   "[Output truncated: showing 200 of 500 lines, cut at 50KB]"
 * 行数与字节两个上限独立判定、先行后字节；只触发其一则只报其一；
 * 截断标记本身不参与字节预算（显式信号优先，计划书明定）。
 * 字节超限时按行从尾部移除，单行仍超则对行内按码点线性削减——不拆半字符。
 */
export function truncateOutput(text, { maxLines, maxBytes } = {}) {
  if (typeof text !== 'string') return text === undefined || text === null ? '' : String(text)
  if (text === '') return text
  const lineLimit = Number.isFinite(maxLines) && maxLines > 0 ? Math.trunc(maxLines) : DEFAULT_MAX_RESULT_LINES
  const byteLimit = Number.isFinite(maxBytes) && maxBytes > 0 ? Math.trunc(maxBytes) : DEFAULT_MAX_RESULT_BYTES
  const reasons = []

  let out = text
  const lines = out.split('\n')
  const totalLines = lines.length
  if (totalLines > lineLimit) {
    out = lines.slice(0, lineLimit).join('\n')
    reasons.push('showing ' + lineLimit + ' of ' + totalLines + ' lines')
  }

  const totalBytes = utf8Length(out)
  if (totalBytes > byteLimit) {
    // 逐行从尾部移除，累计释放字节（含被移除行的换行符）
    let removed = 0
    let keptLines = out.split('\n')
    while (keptLines.length > 1 && totalBytes - removed > byteLimit) {
      removed += utf8Length(keptLines[keptLines.length - 1]) + 1
      keptLines.pop()
    }
    out = keptLines.join('\n')
    if (utf8Length(out) > byteLimit) {
      // 单行仍超：按码点线性削减，不拆半字符
      let acc = 0
      let keep = 0
      for (const ch of Array.from(out)) {
        const b = utf8Length(ch)
        if (acc + b > byteLimit) break
        acc += b
        keep += 1
      }
      out = Array.from(out).slice(0, keep).join('')
    }
    reasons.push('cut at ' + Math.round(byteLimit / 1024) + 'KB')
  }

  if (reasons.length === 0) return out
  return out + '\n[Output truncated: ' + reasons.join(', ') + ']'
}