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
 * - 上传脱敏：route 单点对 user/assistant 文本过 redactSecrets 闸，命中 secrets
 *   替换为 [REDACTED:label] 再入桶/直冲（B 组，宁误杀不漏放，详见 redact.js）。
 */

import { redactSecrets } from './redact.js'

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

/**
 * 潮浪桶最长存活时间：超过且**服务端明确拒绝过**（HTTP 状态码）才丢弃并计数。
 * 2026-08-29 毒桶事故：某轮对话（2166 chars）在服务端必然抽取失败
 * （分块 + 推理模型把 max_tokens 烧光 → finish_reason=length → 502），
 * 而「跨冷却新故障段重置 retries」的防误丢设计让 retries 永远停在 1/20，
 * 该桶每 5-10 分钟重投一次、每次占用三层 LLM 各 120s，无限循环。
 * retries 防的是「连续快速失败」，时间上限防的是「永久性坏数据」——两者互补。
 * 连接级失败（宕机/超时）不计龄：那不是这条数据的错，按龄丢就是拿防刷屏换丢记忆。
 * 可由 resolveConfig().maxBucketAgeMs 覆盖（毫秒）。
 */
export const DEFAULT_MAX_BUCKET_AGE_MS = 30 * 60 * 1000;

/** 整条是 JSON 的消息替换为占位符；自然语言（即使内嵌 JSON 片段）原样放行。 */
export function sanitizeJsonMessage(content) {
  return looksLikeJson(content) ? JSON_PLACEHOLDER : content;
}

/**
 * 把超长文本按段落边界切成多条，全量保留（不丢尾部）。
 * 2026-08-29 大 payload 教训：服务端分块按「逐条消息」粒度、单条不拆，
 * 13202 chars 单条独占 chunk（模板+内容 ≈17000 tokens 超 context_window=10000）
 * → LLM 输出截断 → 502。客户端先切片，服务端逐条分块、accumulated 合并。
 * 段落优先（\n\n）→ 行（\n）→ 硬切；每片 ≤ pieceChars。
 * 硬切边界码点安全：切点落在代理对（emoji 等）中间时回退一个 UTF-16 单元，
 * 把完整码点让给下一片（2026-08-29 审计：UTF-16 slice 切半 surrogate 是既有审计分级 P2）。
 * @param {string} text
 * @param {number} pieceChars 每片字符上限（默认 2000，实测服务端单条安全值）
 * @returns {string[]} 切片数组（未超限时 [原文]）
 */
export function sliceText(text, pieceChars) {
  const limit = pieceChars > 0 ? pieceChars : 2000
  if (!text || text.length <= limit) return [text]
  const pieces = []
  let rest = text
  while (rest.length > limit) {
    const window = rest.slice(0, limit)
    let cut = window.lastIndexOf('\n\n')
    if (cut < limit / 2) cut = window.lastIndexOf('\n')
    if (cut < limit / 2) cut = limit
    // 码点安全：cut-1 是高代理且 cut 是低代理 → 边界切半了一对，回退让整码点进下一片
    // （回退后 cut 不得为 0——空片 + rest 原样 = 死循环；实际 limit≥200 不会触底）
    if (cut > 1 && cut < rest.length) {
      const prev = rest.charCodeAt(cut - 1)
      if (prev >= 0xd800 && prev <= 0xdbff) {
        const next = rest.charCodeAt(cut)
        if (next >= 0xdc00 && next <= 0xdfff) cut -= 1
      }
    }
    pieces.push(rest.slice(0, cut))
    rest = rest.slice(cut).replace(/^[\s\n]+/, '')
  }
  if (rest) pieces.push(rest)
  return pieces
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
    this.stats = { batches: 0, direct: 0, savedCalls: 0, jsonSanitized: 0, redacted: 0, sliced: 0, dropped: 0, bucketTurns: {} };
    // 同会话同 label 的脱敏告警去重（有界，防长会话泄漏）
    this.redactWarned = new Map();
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

  /**
   * 单桶消息对硬上限（四轮审计）：短路期 cap 冲刷失败回插循环会让单桶消息
   * 无限累积（短错误不消耗 retries、无 MAX_FLUSH_RETRIES 兜底）——恢复后
   * 一次性写入超大批可能被服务端 400 拒绝导致整桶二次丢失。超限时丢最旧
   * 消息对并计数（与桶预算同语义：以内存换不丢，但保留硬边界）。
   */
  #capBucketLength(bucket) {
    const MAX_BUCKET_TURNS = 100;
    while (bucket.messages.length / 2 > MAX_BUCKET_TURNS) {
      const removed = bucket.messages.splice(0, 2);
      let removedChars = 0;
      for (const m of removed) {
        if (m && typeof m.content === 'string') removedChars += m.content.length;
      }
      bucket.chars = Math.max(0, bucket.chars - removedChars);
      this.stats.dropped += 1;
      if (this.log.warn) {
        this.log.warn('mem0 bucket turns cap exceeded, dropped oldest turn (session=' +
          (bucket.sessionId || '<empty>') + ')');
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

  /** 脱敏命中记账：累加计数 + 同会话同 label 只 warn 一次（映射有界防泄漏）。 */
  noteRedactHits(sessionId, hits) {
    const sid = String(sessionId || '<empty>');
    const merged = new Map();
    for (const h of hits) {
      this.stats.redacted += 1;
      merged.set(h.label, (merged.get(h.label) || 0) + h.count);
    }
    const fresh = [];
    for (const [label, count] of merged) {
      const key = sid + '\u0000' + label;
      if (this.redactWarned.has(key)) continue;
      this.redactWarned.set(key, 1);
      fresh.push(label + 'x' + count);
    }
    if (this.redactWarned.size > 256) this.redactWarned.clear();
    if (fresh.length && this.log.warn) {
      this.log.warn('mem0 upload redacted ' + fresh.length + ' secret label(s) [' + fresh.join(', ') +
        '] before infer (session=' + sid + ')');
    }
  }

  /** 单条入队项路由：JSON 剥除 → 直写或进桶。 */
  route(item) {
    const config = this.resolveConfig();
    let userContent = sanitizeJsonMessage(String(item.userContent || ''));
    let assistantContent = sanitizeJsonMessage(String(item.assistantContent || ''));
    if (userContent !== item.userContent || assistantContent !== item.assistantContent) {
      this.stats.jsonSanitized += 1;
      if (this.log.debug) this.log.debug('mem0 sanitized pure-JSON message(s) for session ' + (item.sessionId || '<empty>'));
    }
    // 上传脱敏闸（B 组）：抽取请求会把原文送云端 LLM，出门前替换 secrets。
    // 默认开启（resolve 未给 redactEnabled 时按 true 处理），关闭即完全旁路。
    if (config.redactEnabled !== false) {
      const u = redactSecrets(userContent);
      const a = redactSecrets(assistantContent);
      if (u.hits.length || a.hits.length) {
        this.noteRedactHits(item.sessionId, u.hits.concat(a.hits));
        userContent = u.text;
        assistantContent = a.text;
      }
    }
    // 单条超长消息切片（2026-08-29 大 payload 教训，替代早期的截断保头）：
    // 服务端分块按「逐条消息」粒度、单条不拆，13202 chars 单条独占 chunk
    // （模板约 9400t + 内容 7600t ≈ 17000t 超 context_window=10000）→ LLM 输出
    // 截断 → 502。客户端按段落切片（sliceThreshold 触发、slicePieceChars 每片
    // 上限，实测服务端单条安全值 ≈2000 chars），全量保留不丢尾部；服务端
    // 逐条分块提取后 accumulated_memories 合并为一条事实集。
    const SLICE_AT = config.sliceThreshold > 0 ? config.sliceThreshold : 8000;
    const PIECE = config.slicePieceChars > 0 ? config.slicePieceChars : 2000;
    const userParts = userContent.length > SLICE_AT ? sliceText(userContent, PIECE) : [userContent];
    const assistantParts = assistantContent.length > SLICE_AT ? sliceText(assistantContent, PIECE) : [assistantContent];
    if (userParts.length + assistantParts.length > 2) {
      this.stats.sliced += 1
      if (this.log.warn) {
        this.log.warn('mem0 payload sliced into user=' + userParts.length + ' assistant=' + assistantParts.length +
          ' piece(s) (session=' + (item.sessionId || '<empty>') + ')')
      }
    }
    const messages = [
      ...userParts.map((c) => ({ role: 'user', content: c })),
      ...assistantParts.map((c) => ({ role: 'assistant', content: c }))
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
    this.#capBucketLength(bucket);
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
    this.#capBucketLength(bucket);
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
          ' dropped=' + this.stats.dropped + ' jsonSanitized=' + this.stats.jsonSanitized +
          ' redacted=' + this.stats.redacted + ' sliced=' + this.stats.sliced + ')');
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
      const ageMs = Date.now() - bucket.created;
      const maxAgeCfg = this.resolveConfig().maxBucketAgeMs;
      const maxAgeMs = maxAgeCfg > 0 ? Math.trunc(maxAgeCfg) : DEFAULT_MAX_BUCKET_AGE_MS;
      // 存活上限只针对「服务端真的处理过并拒绝」的确定性毒桶（Mem0HttpError 带 status）。
      // 纯连接级失败（服务端宕机、或还在慢慢跑这一单）不是这条数据的错——若也按年龄丢，
      // 宕机 30 分钟就会误杀一批本可成功的记忆，那是拿"防刷屏"换"丢记忆"，方向错了。
      const serverRejected = Number.isInteger(error && error.status);
      if (serverRejected && ageMs > maxAgeMs) {
        // retries 管「连续快速失败」，存活时间管「确定性毒桶」：跨冷却重置让 retries
        // 永远停在 1/20，服务端对该载荷必然失败时就会无限重投（2026-08-29 事故）。
        // 丢的是「抽取从未成功过」的载荷，不是已入库的记忆；原文仍在 dsh 会话日志里，
        // 按 sessionId 可回捞重放。
        this.stats.dropped += turns;
        if (this.log.warn) {
          this.log.warn('mem0 coalesced flush dropped as rejected-payload after ' + Math.round(ageMs / 60000) +
            ' min / ' + bucket.retries + ' attempt(s), raw text still in dsh session log (session=' +
            (bucket.sessionId || '<empty>') + ', turns=' + turns + '): ' + String((error && error.message) || error));
        }
      } else if (bucket.retries <= MAX_FLUSH_RETRIES) {
        // 指数退避（30s→5min 封顶）：网络级失败往往意味着服务端还在慢慢跑这一单，
        // 立刻重投只是叠一个同样的长请求。短路不额外退避（熔断自有冷却节奏）。
        const backoffMs = shortCircuited ? 0 : Math.min(30000 * 2 ** Math.max(0, bucket.retries - 1), 300000);
        const nextAttemptAt = Date.now() + backoffMs;
        const existing = this.buckets.get(key);
        if (existing && existing !== bucket) {
          existing.messages = bucket.messages.concat(existing.messages);
          existing.chars += bucket.chars;
          existing.created = Math.min(existing.created, bucket.created);
          // 四轮审计：merge 必须继承失败桶的重试状态——否则每次 merge 后
          // retries 归零，MAX_FLUSH_RETRIES 被冲刷窗口内新轮次无限续命
          // （服务端挂起 300s 周期下每周期都获得全新 20 次额度）
          existing.retries = Math.max(existing.retries || 0, bucket.retries || 0);
          if (bucket.lastFailAt) existing.lastFailAt = bucket.lastFailAt;
          existing.nextAttemptAt = Math.max(existing.nextAttemptAt || 0, nextAttemptAt);
          this.#capBucketLength(existing);
        } else {
          bucket.nextAttemptAt = nextAttemptAt;
          this.buckets.set(key, bucket);
          this.#capBucketLength(bucket);
        }
        if (this.log.warn && !shortCircuited) {
          this.log.warn('mem0 coalesced flush failed (session=' + (bucket.sessionId || '<empty>') +
            ', turns=' + turns + ', trigger=' + (trigger || 'unknown') + ', age=' + Math.round(ageMs / 1000) +
            's), will retry in ' + Math.round(backoffMs / 1000) + 's (' +
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
      if (bucket.nextAttemptAt > clock) continue; // 失败退避窗口内不重投
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
      const due = Math.max(bucket.nextAttemptAt || 0, Math.min(bucket.last + idleMs, bucket.created + windowMs));
      if (deadline === null || due < deadline) deadline = due;
    }
    return deadline;
  }
}
