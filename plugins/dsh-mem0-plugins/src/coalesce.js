/**
 * dsh-mem0-plugins — 潮浪并忆（Tidal Coalescing）与写路径卫生。
 *
 * 从 hermes mem0 插件移植：把同一 user+session 的多条短对话合并成一次
 * infer:true 批量写入，摊薄服务端 LLM 事实提取调用次数。JS 单线程运行，
 * 无需 hermes 版的线程锁；冲刷节奏由宿主侧的定时 tick 驱动。
 *
 * - 有界队列：上限满时丢最旧（防服务端长时间不可用时内存无界增长）；
 * - 纯 JSON 消息剥除：整条消息可被 JSON.parse 且以 { [ 开头时替换占位符，
 *   防止提取模型把工具输出/配置原文的键名当「事实」入库；
 * - 分桶合并：空闲超时 / 窗口超时 / 轮数上限 / 字符上限任一达标即冲刷；
 * - 快速直写：单轮字符数超过 fastpathChars 直接落库，不让长内容等合并。
 */

/** 判断整条消息是否为可解析的 JSON 结构（工具输出/配置原文）。 */
export function looksLikeJson(text) {
  if (!text || !text.trim()) return false;
  const trimmed = text.trim();
  if (trimmed[0] !== '{' && trimmed[0] !== '[') return false;
  try {
    JSON.parse(trimmed);
    return true;
  } catch {
    return false;
  }
}

export const JSON_PLACEHOLDER = '<JSON 结构化数据，已省略>';

/** 整条是 JSON 的消息替换为占位符；自然语言（即使内嵌 JSON 片段）原样放行。 */
export function sanitizeJsonMessage(content) {
  return looksLikeJson(content) ? JSON_PLACEHOLDER : content;
}

function now() {
  return Date.now();
}

export class TidalCoalescer {
  /**
   * @param {object} options 全部参数可热更新（resolve() 每次取最新配置）
   * @param {() => object} resolve 返回当前生效参数：
   *   {enabled, idleMs, windowMs, maxTurns, maxChars, fastpathChars}
   * @param {(item: {userId,sessionId,messages,infer:boolean}) => Promise<any>} addFn 写入后端
   * @param {{debug?, info?, warn?}} [log]
   */
  constructor({ resolve, addFn, queueMaxLen = 50, log } = {}) {
    this.resolveConfig = resolve || (() => ({}));
    this.addFn = addFn || (async () => {});
    this.log = log || {};
    this.queueMaxLen = queueMaxLen > 0 ? Math.trunc(queueMaxLen) : 50;
    this.queue = [];
    this.buckets = new Map(); // key `${userId}\u0000${sessionId}` → {created,last,chars,messages}
    this.stats = { batches: 0, direct: 0, savedCalls: 0, jsonSanitized: 0, dropped: 0, bucketTurns: {} };
    this.flushing = 0;
  }

  /** 非阻塞入队；队满丢最旧。 */
  enqueue(item) {
    if (this.queue.length >= this.queueMaxLen) {
      this.queue.shift();
      this.stats.dropped += 1;
      if (this.log.warn) this.log.warn('mem0 sync queue full (' + this.queueMaxLen + '), dropped oldest pending turn');
    }
    this.queue.push(item);
  }

  get pending() {
    return this.queue.length + this.buckets.size;
  }

  /** 排空队列并逐项路由；返回路由条数。 */
  drain() {
    let routed = 0;
    while (this.queue.length > 0) {
      const item = this.queue.shift();
      try {
        this.route(item);
        routed += 1;
      } catch (error) {
        if (this.log.warn) this.log.warn('mem0 route item failed: ' + String((error && error.message) || error));
      }
    }
    return routed;
  }

  #key(userId, sessionId) {
    return String(userId || '') + '\u0000' + String(sessionId || '');
  }

  /** 单条入队项路由：JSON 剥除 → 直写或进桶。 */
  route(item) {
    const config = this.resolveConfig();
    const userContent = sanitizeJsonMessage(String(item.userContent || ''));
    const assistantContent = sanitizeJsonMessage(String(item.assistantContent || ''));
    if (userContent !== item.userContent || assistantContent !== item.assistantContent) {
      this.stats.jsonSanitized += 1;
      if (this.log.debug) this.log.debug('mem0 sanitized pure-JSON message(s) for session ' + (item.sessionId || '<empty>'));
    }
    const messages = [
      { role: 'user', content: userContent },
      { role: 'assistant', content: assistantContent }
    ];
    const chars = userContent.length + assistantContent.length;

    if (config.enabled === false) {
      this.#addDirect(item, messages, chars, 'coalescing disabled');
      return;
    }
    if (chars > (config.fastpathChars > 0 ? config.fastpathChars : 2000)) {
      this.#addDirect(item, messages, chars, 'fastpath');
      return;
    }

    const key = this.#key(item.userId, item.sessionId);
    const ts = now();
    let bucket = this.buckets.get(key);
    if (!bucket) {
      bucket = { created: ts, last: ts, chars: 0, messages: [], userId: item.userId, sessionId: item.sessionId };
      this.buckets.set(key, bucket);
    }
    bucket.messages.push(...messages);
    bucket.chars += chars;
    bucket.last = ts;
    const turns = bucket.messages.length / 2;
    if (chars >= (config.maxChars > 0 ? config.maxChars : 4000) || turns >= (config.maxTurns > 0 ? config.maxTurns : 5)) {
      void this.flushBucket(key, 'cap');
    }
  }

  #addDirect(item, messages, chars, reason) {
    this.stats.direct += 1;
    if (this.log.debug) this.log.debug('mem0 direct write (' + reason + ', chars=' + chars + ', session=' + (item.sessionId || '<empty>') + ')');
    this.flushing += 1;
    Promise.resolve(
      this.addFn({ userId: item.userId, sessionId: item.sessionId, messages, infer: true })
    )
      .then(() => {
        this.stats.batches += 1;
        this.#countTurns(item.sessionId, messages.length / 2);
      })
      .catch((error) => {
        if (this.log.warn) this.log.warn('mem0 direct write failed: ' + String((error && error.message) || error));
      })
      .finally(() => {
        this.flushing -= 1;
      });
  }

  #countTurns(sessionId, turns) {
    const sid = sessionId || '<empty>';
    this.stats.savedCalls += Math.max(0, turns - 1);
    this.stats.bucketTurns[sid] = (this.stats.bucketTurns[sid] || 0) + turns;
  }

  /** 冲刷单个桶为一次批量写入。 */
  async flushBucket(key, trigger) {
    const bucket = this.buckets.get(key);
    if (!bucket || !bucket.messages || bucket.messages.length === 0) return;
    this.buckets.delete(key);
    const turns = bucket.messages.length / 2;
    this.flushing += 1;
    try {
      await this.addFn({
        userId: bucket.userId,
        sessionId: bucket.sessionId,
        messages: bucket.messages,
        infer: true
      });
      this.stats.batches += 1;
      this.#countTurns(bucket.sessionId, turns);
      if (this.log.info) {
        this.log.info('mem0 coalesced ' + turns + ' turn(s) into 1 write (session=' + (bucket.sessionId || '<empty>') +
          ', saved ' + Math.max(0, turns - 1) + ' call(s), chars=' + bucket.chars +
          (trigger ? ', trigger=' + trigger : '') + ')');
      }
    } catch (error) {
      if (this.log.warn) {
        this.log.warn('mem0 coalesced flush failed (session=' + (bucket.sessionId || '<empty>') +
          ', turns=' + turns + ', trigger=' + (trigger || 'unknown') + '): ' + String((error && error.message) || error));
      }
    } finally {
      this.flushing -= 1;
    }
  }

  /** 冲刷空闲/窗口到期的桶。 */
  flushDue(ts) {
    const config = this.resolveConfig();
    const clock = ts || now();
    for (const key of [...this.buckets.keys()]) {
      const bucket = this.buckets.get(key);
      if (!bucket) continue;
      const idleMs = config.idleMs > 0 ? config.idleMs : 5000;
      const windowMs = config.windowMs > 0 ? config.windowMs : 15000;
      if (clock - bucket.last >= idleMs) void this.flushBucket(key, 'idle');
      else if (clock - bucket.created >= windowMs) void this.flushBucket(key, 'window');
    }
  }

  /** 兜底冲刷全部桶（dispose / 关停前），保证记忆不丢。 */
  flushAll(trigger) {
    for (const key of [...this.buckets.keys()]) void this.flushBucket(key, trigger || 'final');
  }

  /** 全部桶中最早的下一次到期时刻；无桶返回 null。 */
  nextDeadline(ts) {
    const config = this.resolveConfig();
    const clock = ts || now();
    let deadline = null;
    for (const bucket of this.buckets.values()) {
      const idleMs = config.idleMs > 0 ? config.idleMs : 5000;
      const windowMs = config.windowMs > 0 ? config.windowMs : 15000;
      const due = Math.min(bucket.last + idleMs, bucket.created + windowMs);
      if (deadline === null || due < deadline) deadline = due;
    }
    return deadline;
  }
}
