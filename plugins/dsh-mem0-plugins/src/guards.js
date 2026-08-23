/**
 * dsh-mem0-plugins — 记忆路径的消息卫生判断，移植自 hermes
 * agent/memory_provider.py::is_trivial_prompt。
 *
 * 空输入、斜杠命令、纯问候/确认类消息没有语义信号：跳过预取与注入，
 * 省一次网络往返，也防止单字回复被旧记忆带偏。
 */

const TRIVIAL_PROMPT_RE = new RegExp(
  '^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|' +
  'hi|hey|hello|yo|sup|' +
  'continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)' +
  '[\\s!?.:;,"\'~’“”—–…()\\[\\]{}<>*&^%$#@!+=`\\u00a0]*$',
  'i'
)

/** 是否属于不值得召回的琐碎输入（空/斜杠命令/纯问候确认）。 */
export function isTrivialPrompt(text) {
  if (!text) return true
  const stripped = String(text).trim()
  if (!stripped) return true
  if (stripped.startsWith('/')) return true
  return TRIVIAL_PROMPT_RE.test(stripped)
}
