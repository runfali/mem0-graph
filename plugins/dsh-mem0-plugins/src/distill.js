/**
 * dsh-mem0-plugins — 查询蒸馏（Query Distillation），移植自 hermes
 * agent/memory_manager.py::_distill_query。
 *
 * 解决的问题：用户贴入超长日志/代码时，把原文直接塞给 mem0 /search 会打爆
 * embedding 且语义全无。蒸馏用一个小模型把超长消息提炼成「2-4 关键词或一句
 * 检索意图」，只作用于召回查询，不碰写入路径。
 *
 * 忠实保留 hermes 的三道防线：
 * 1. 短消息（<= minChars）原样通过——零语义损失、零额外调用；
 * 2. 语言漂移防护——中文输入的蒸馏结果若出现越南语重音字符或非拉丁非 CJK
 *    文字（聚合网关路由漂移到多语小模型的实证症状），判为污染即回退；
 * 3. 并发双飞——首请求 retryAfter 无响应则并发第二请求（首个不取消），
 *    先完成者胜出；全部失败/超时回退原文，检索永不静默丢失。
 */

export const DISTILL_PROMPT =
  '提取用户消息的记忆检索意图（2-4 关键词或一句查询）。' +
  '忽略日志代码噪音，只输出意图。' +
  '语言与用户一致，不翻译；专有名词保留原文（如 mem0、tirith）。'

/** 中文（含扩展区）判定：输入含汉字才启用漂移校验（纯英文输入本就不校验）。 */
const HAN_RE = /[一-鿿]/
/** 越南语等带重音拉丁组合——英文不会出现，出现即漂移。 */
const VIET_ACCENT_RE = /[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]/
/** 非拉丁非 CJK 文字：泰/阿/希伯来/西里尔/希腊/韩/日假名/天城文等。 */
const FOREIGN_SCRIPT_RE = /[฀-๿؀-ۿ֐-׿Ѐ-ӿͰ-Ͽ가-힯぀-ヿऀ-ॿ]/

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 单次蒸馏调用；成功返回意图字符串，失败/空结果抛错。 */
async function callOnce(text, options, signal) {
  const body = {
    model: options.model,
    messages: [
      {
        role: 'user',
        content: DISTILL_PROMPT + '\n\n消息：' + text.slice(0, options.inputMaxChars)
      }
    ],
    max_tokens: 3000,
    temperature: 0,
    // 聚合网关不传 stream 会返回 SSE 混合格式导致解析失败，必须显式关流
    stream: false,
    // 意图提取无需思维链；部分网关无视此参数，靠 max_tokens 留足 content 空间
    reasoning_effort: 'none'
  }
  const response = await fetch(options.baseUrl.replace(/\/+$/, '') + '/chat/completions', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      ...(options.apiKey ? { authorization: 'Bearer ' + options.apiKey } : {})
    },
    body: JSON.stringify(body),
    signal
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new Error('distill endpoint HTTP ' + response.status + (detail ? ': ' + detail.slice(0, 200) : ''))
  }
  const rawBody = await response.text()
  let payload
  try {
    payload = JSON.parse(rawBody)
  } catch {
    payload = null // 落入下方 SSE 混合格式兜底
  }
  // 防御兜底：个别网关即使 stream=false 仍返回 SSE 混合格式
  if (!payload || !Array.isArray(payload.choices)) {
    const raw = typeof rawBody === 'string' ? rawBody : JSON.stringify(rawBody)
    const start = raw.indexOf('{')
    const end = raw.lastIndexOf('}')
    if (start !== -1 && end > start) payload = JSON.parse(raw.slice(start, end + 1))
  }
  const message = payload.choices && payload.choices[0] && payload.choices[0].message
  if (!message) throw new Error('distill endpoint returned no message')
  // 本地模型可能把答案放 content（reasoning_effort=none）或只在 reasoning_content（思考变体）
  let rawText = String(message.content || '').trim()
  if (!rawText) rawText = String(message.reasoning_content || '').trim()
  if (!rawText) throw new Error('empty distillation result')
  let distilled = ''
  try {
    const obj = JSON.parse(rawText)
    if (obj && typeof obj === 'object') {
      distilled = String(obj.intent || obj.query || obj.result || '').trim()
    }
  } catch {
    /* 非 JSON 输出按纯文本意图处理 */
  }
  if (!distilled) distilled = rawText
  if (HAN_RE.test(text) && (VIET_ACCENT_RE.test(distilled) || FOREIGN_SCRIPT_RE.test(distilled))) {
    throw new Error('distillation language drift: ' + distilled.slice(0, 60))
  }
  return distilled
}

/**
 * 把超长用户消息蒸馏成检索意图；短消息原样返回，任何失败回退原文。
 * @param {string} query 用户消息文本
 * @param {object} options {enabled,minChars,inputMaxChars,baseUrl,apiKey,model,timeoutMs,retryAfterMs}
 * @param {(info:string)=>void} [log]
 * @param {AbortSignal} [signal] 外部取消信号（用户中断生成后蒸馏随之中止，不再白跑满超时）
 * @returns {Promise<string>} 用于 /search 的查询串（可能是原文）
 */
export async function distillQuery(query, options, log, signal) {
  if (!options || options.enabled === false) return query
  if (!query || query.length <= (options.minChars > 0 ? options.minChars : 500)) return query
  if (!options.baseUrl) {
    if (log) log('distill skipped: no baseUrl configured, using original query')
    return query
  }
  const timeoutMs = options.timeoutMs > 0 ? options.timeoutMs : 30000
  const retryAfterMs = options.retryAfterMs > 0 ? options.retryAfterMs : 20000

  const attempt = () => {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(new Error('distill timed out after ' + timeoutMs + ' ms')), timeoutMs)
    timer.unref && timer.unref()
    // 外部取消联动（2026-08-25 审计 IDX4）：用户中断生成后蒸馏请求随之中止；
    // 已中止的 signal 立即触发，保证重发分支也不会绕过取消
    const onOuterAbort = () => controller.abort(signal && signal.reason !== undefined ? signal.reason : new Error('aborted'))
    if (signal && signal.aborted) onOuterAbort()
    else if (signal) signal.addEventListener('abort', onOuterAbort, { once: true })
    return callOnce(query, options, controller.signal).finally(() => {
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', onOuterAbort)
    })
  }

  let firstResult = null // {value} | {error}
  const first = attempt().then(
    (value) => {
      firstResult = { value }
      return value
    },
    (error) => {
      firstResult = { error }
      throw error
    }
  )

  // 分支判定：首请求在阈值内结束（成功或失败都算 settled，失败走重发分支，
  // 绝不让拒绝抛穿——回退原文是最后防线）
  const firstSettled = first.then(
    () => true,
    () => true
  )
  const verdict = await Promise.race([firstSettled, sleep(retryAfterMs).then(() => false)])
  if (verdict === true) {
    if (firstResult.value !== undefined) {
      if (log) log('distilled ' + query.length + ' -> ' + String(firstResult.value).length + ' chars')
      return firstResult.value
    }
    // 首请求已失败 → 单次重发，不再等满阈值
    try {
      const value = await attempt()
      if (log) log('distilled on retry: ' + query.length + ' -> ' + value.length + ' chars')
      return value
    } catch (error) {
      if (log) log('distillation failed (' + String(error.message || error) + '), using original')
      return query
    }
  }

  // 分支二：首请求仍无响应 → 并发第二请求，先完成者胜出
  if (log) log('distillation slow (>=' + Math.round(retryAfterMs / 1000) + 's), issuing parallel retry')
  const second = attempt()
  const winner = await Promise.any([first, second]).catch(() => null)
  if (winner !== null && winner !== undefined && typeof winner === 'string') {
    if (log) log('distilled via parallel race: ' + query.length + ' -> ' + winner.length + ' chars')
    return winner
  }
  if (log) log('distillation failed/timed out, using original query (' + query.length + ' chars)')
  return query
}
