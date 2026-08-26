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

  /**
   * 桶全局预算（三轮审计）：短路期不拦截入队后，桶只在成功冲刷时移除——
   * 长故障 + 多会话下桶数可能无界增长（queueMaxLen 只管 queue 不管桶）。
   * 超限时丢最旧桶（created 最早）并计数，以内存换不丢的承诺保留在预算内。
   */
  #enforceBucketBudget() {
    const MAX_BUCKETS = 64;
    while (this.buckets.size > MAX_BUCKETS) {
      let oldestKey = null;
      let oldest = Infinity;
      for (const [k, b] of this.buckets) {
        if (b.created < oldest) {
          oldest = b.created;
          oldestKey = k;
        }
      }
      if (oldestKey === null) break;
      const victim = this.buckets.get(oldestKey);
      this.buckets.delete(oldestKey);
      this.stats.dropped += victim.messages.length / 2;
      if (this.log.warn) {
        this.log.warn('mem0 bucket budget exceeded, dropped oldest bucket (session=' +
          (victim.sessionId || '<empty>') + ', messages=' + victim.messages.length + ')');
      }
    }
  }

  /** 非阻塞入队；队满丢最旧。上限每次入队动态读取（设置卡可热调）。 */
  enqueue(item) {
    const config = this.resolveConfig()
    const maxLen = config.queueMaxLen > 0 ? Math.trunc(config.queueMaxLen) : this.queueMaxLen
    if (this.queue.length >= maxLen) {
      this.queue.shift();
      this.stats.dropped += 1;
      if (this.log.warn) this.log.warn('mem0 sync queue full (' + maxLen + '), dropped oldest pending turn');
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
    this.#enforceBucketBudget();
    const turns = bucket.messages.length / 2;
    // 字符上限按「桶累积值」判定：当条 chars 已被 fastpathChars(默认2000) 挡住，
    // 用当条值对比 maxChars(默认4000) 永不触发，活跃会话的桶会无限膨胀
    // （2026-08-25 审计 C2：原代码 `chars >= maxChars` 是笔误）。
    if (bucket.chars >= (config.maxChars > 0 ? config.maxChars : 4000) || turns >= (config.maxTurns > 0 ? config.maxTurns : 5)) {
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
        // 快路径失败必须降级入桶（2026-08-26 二轮审计）：直写只有一次机会，
        // 短路/网络失败即丢——而长消息（用户贴长文/代码）通常正是高价值内容。
        // 降级后借桶路径语义：短路挂桶等冷却、非短路重试至上限。
        if (this.log.warn) {
          this.log.warn('mem0 direct write failed, demoting to bucket (reason=' + reason +
            ', session=' + (item.sessionId || '<empty>') + '): ' + String((error && error.message) || error));
        }
        this.#demoteToBucket(item, messages, chars);
      })
      .finally(() => {
        this.flushing -= 1;
      });
  }

  /** 快路径失败降级：路由进同 key 桶，复用挂起/重试/短路语义。 */
  #demoteToBucket(item, messages, chars) {
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
    this.#enforceBucketBudget();
    const turns = bucket.messages.length / 2;
    if (bucket.chars >= (this.resolveConfig().maxChars > 0 ? this.resolveConfig().maxChars : 4000)
        || turns >= (this.resolveConfig().maxTurns > 0 ? this.resolveConfig().maxTurns : 5)) {
      void this.flushBucket(key, 'cap-after-demote');
    }
  }

  #countTurns(sessionId, turns) {
    const sid = sessionId || '<empty>';
    this.stats.savedCalls += Math.max(0, turns - 1);
    this.stats.bucketTurns[sid] = (this.stats.bucketTurns[sid] || 0) + turns;
  }

  /** 冲刷单个桶为一次批量写入。失败把消息放回桶内等下一 tick 重试
   * （删桶发生在写入前，直接吞错会静默丢整批记忆；与熔断叠加时——打开期
   * 所有冲刷被短路——必须能存活到冷却结束。retries 达上限才放弃并计数）。 */
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
        // 累计可观测摘要：省下调用/丢最旧/JSON 剥除 计数随每次合并冲刷可见
        this.log.info('mem0 coalesced ' + turns + ' turn(s) into 1 write (session=' + (bucket.sessionId || '<empty>') +
          ', saved ' + Math.max(0, turns - 1) + ' call(s), chars=' + bucket.chars +
          (trigger ? ', trigger=' + trigger : '') +
          '; totals: batches=' + this.stats.batches + ' savedCalls=' + this.stats.savedCalls +
          ' dropped=' + this.stats.dropped + ' jsonSanitized=' + this.stats.jsonSanitized + ')');
      }
    } catch (error) {
      // 放回桶首并保留最早 created（window 语义不因重试漂移）；冲刷期间同 key
      // 若已落了新消息则合并，避免覆盖。MAX_FLUSH_RETRIES 防永久性坏数据（如
      // 服务端 400 拒绝整批）无限占用内存。
      //
      // 熔断短路（shortCircuited，backend #guard 抛出）是暂时性失败：冷却结束前
      // 每次重试都必然短路，若照常消耗 retries，20 次 × tick(300ms) ≈ 6-11s 就会
      // 把整桶静默丢弃——远早于默认冷却(120s)结束。故短路错误不计数、低噪挂回，
      // 冷却结束后第一次真实重试自然成功；非短路失败仍按原上限防坏数据占内存。
      const shortCircuited = !!(error && error.shortCircuited === true);
      bucket.retries = bucket.retries || 0;
      if (!shortCircuited) {
        // 跨冷却的新故障段重置计数（2026-08-26 二轮审计）：半开窗口的真实
        // 失败间隔≈冷却时长，若不重置，20 次半开失败≈40 分钟持续故障后整桶
        // 仍会被丢弃。同段连续故障（间隔<冷却）依旧累计，保留防坏数据占内存。
        const cooldownMs = this.resolveConfig().cooldownMs > 0 ? this.resolveConfig().cooldownMs : 120000;
        const sinceLast = bucket.lastFailAt ? Date.now() - bucket.lastFailAt : cooldownMs + 1;
        bucket.retries = sinceLast > cooldownMs ? 1 : bucket.retries + 1;
        bucket.lastFailAt = Date.now();
      }
      const MAX_FLUSH_RETRIES = 20;
      if (bucket.retries <= MAX_FLUSH_RETRIES) {
        const existing = this.buckets.get(key);
        if (existing && existing !== bucket) {
          existing.messages = bucket.messages.concat(existing.messages);
          existing.chars += bucket.chars;
          existing.created = Math.min(existing.created, bucket.created);
        } else {
          this.buckets.set(key, bucket);
        }
        if (this.log.warn && !shortCircuited) {
          this.log.warn('mem0 coalesced flush failed (session=' + (bucket.sessionId || '<empty>') +
            ', turns=' + turns + ', trigger=' + (trigger || 'unknown') + '), will retry (' +
            bucket.retries + '/' + MAX_FLUSH_RETRIES + '): ' + String((error && error.message) || error));
        } else if (this.log.debug && shortCircuited) {
          this.log.debug('mem0 coalesced flush short-circuited by open breaker (session=' +
            (bucket.sessionId || '<empty>') + ', turns=' + turns + '), held for breaker cooldown: ' +
            String((error && error.message) || error));
        }
      } else {
        this.stats.dropped += turns;
        if (this.log.warn) {
          this.log.warn('mem0 coalesced flush dropped after ' + bucket.retries + ' retries (session=' +
            (bucket.sessionId || '<empty>') + ', turns=' + turns + '): ' + String((error && error.message) || error));
        }
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

  /** 兜底冲刷全部桶（dispose / 关停前），保证记忆不丢。
   * @returns Promise<allSettled> 宿主 dispose 可 await，避免关停时 in-flight 丢失。 */
  flushAll(trigger) {
    const jobs = [];
    for (const key of [...this.buckets.keys()]) jobs.push(this.flushBucket(key, trigger || 'final'));
    return Promise.allSettled(jobs);
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
