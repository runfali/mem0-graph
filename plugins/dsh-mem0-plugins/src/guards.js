/**
 * dsh-mem0-plugins — 记忆路径的消息卫生判断，移植并扩充自 hermes
 * agent/memory_provider.py::is_trivial_prompt。
 *
 * 空输入、斜杠命令、纯问候/确认类消息没有语义信号：跳过预取与注入，
 * 省一次网络往返，也防止单字回复被旧记忆带偏。
 *
 * 词表在 hermes 英文表基础上按三分类等价扩充中文高频词（应答/问候/继续）；
 * 只匹配整串恰好等于词项（允许尾随标点），带实际内容的句子永不误伤。
 */

const TRIVIAL_PROMPT_RE = new RegExp(
  '^(?:' +
  // -- 应答（agree/deny）：yes/no 族 --
  'yes|no|ok|okay|sure|thanks|thank you|yep|nope|yeah|nah|y|n|k|' +
  '好|好的|好哒|好嘞|嗯|嗯嗯|哦|噢|噢噢|行|行吧|可以|对|对的|是的|是|没错|' +
  '收到|了解|明白了|明白|知道了|知道|中|妥|' +
  '不|不用|不用了|不了|算了|没有|没|' +
  '谢谢|多谢|感谢|辛苦了|麻烦了|' +
  // -- 问候（greetings）：hi 族 --
  'hi|hey|hello|yo|sup|' +
  '你好|您好|哈喽|嗨|嗨嗨|在吗|在么|' +
  // -- 推进（continue/proceed）：continue 族 --
  'continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|' +
  '继续|接着来|请继续|下一步|开始吧|就这样|搞定|完成' +
  ')' +
  '[\\s!?.:;,"\'~’“”—–…()\\[\\]{}<>*&^%$#@!+=`\\u00a0' +
  '。，！？；：、“”‘’《》【】（）～…·—]*$',
  'i'
)

/** 斜杠命令形态：单段、字母开头、≤24 字符（/compact /help /start 这类）。
 * 不能用「以 / 开头」粗判——「/etc/hosts 里改了什么」「/api/v1 报错怎么办」
 * 「/data/x 有什么文件」这类路径/端点查询恰是记忆检索最该介入的场景。 */
const SLASH_COMMAND_RE = /^\/[a-zA-Z][\w-]{0,23}$/

/** 是否属于不值得召回的琐碎输入（空/斜杠命令/纯问候确认）。 */
export function isTrivialPrompt(text) {
  if (!text) return true
  const stripped = String(text).trim()
  if (!stripped) return true
  // 剥掉全部标点/符号后为空的串（......、！！！、。。。）无语义信号
  if (!stripped.replace(/[\p{P}\p{S}\p{Z}\s]/gu, '')) return true
  if (SLASH_COMMAND_RE.test(stripped)) return true
  return TRIVIAL_PROMPT_RE.test(stripped)
}
