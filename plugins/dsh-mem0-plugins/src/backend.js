/**
 * dsh-mem0-plugins — 自托管 Mem0 server 的 HTTP 客户端（唯一后端模式）。
 *
 * 对应 hermes 插件的 SelfHostedBackend：X-API-Key 鉴权（key 为空时不发，
 * 适配 AUTH_DISABLED 部署），路由 /search、/memories、/memories/{id}、
 * /evolve/feedback。不依赖任何第三方 SDK，纯 fetch。
 *
 * 可靠性语义与 hermes 一致：
 * - 连接级失败（请求很可能没到达服务端）自动重试一次；
 * - 连续失败达阈值后熔断冷却，期间调用方直接短路；
 * - 404 / not found / valid uuid 类客户端错误不计入熔断。
 */

/** 熔断器：连续失败 threshold 次后，冷却 cooldownMs 内一律视为不可用。 */
export class CircuitBreaker {
  constructor(options = {}) {
    this.threshold = options.threshold > 0 ? Math.trunc(options.threshold) : 5;
    this.cooldownMs = options.cooldownMs > 0 ? Math.trunc(options.cooldownMs) : 120000;
    this.consecutiveFailures = 0;
    this.openUntil = 0;
    this.lastError = null;
  }

  /** 熔断是否打开；冷却到期时顺带复位。 */
  get open() {
    if (this.consecutiveFailures < this.threshold) return false;
    if (Date.now() >= this.openUntil) {
      this.consecutiveFailures = 0;
      return false;
    }
    return true;
  }

  /** 距离熔断复位还剩多少毫秒（未熔断时为 0）。 */
  get remainingMs() {
    if (!this.open) return 0;
    return Math.max(0, this.openUntil - Date.now());
  }

  recordSuccess() {
    this.consecutiveFailures = 0;
    this.lastError = null;
  }

  /** @returns 是否刚刚触发熔断 */
  recordFailure(error) {
    this.lastError = error instanceof Error ? error : new Error(String(error));
    this.consecutiveFailures += 1;
    if (this.consecutiveFailures >= this.threshold) {
      this.openUntil = Date.now() + this.cooldownMs;
      return true;
    }
    return false;
  }

  reset() {
    this.consecutiveFailures = 0;
    this.openUntil = 0;
    this.lastError = null;
  }
}

/**
 * 熔断器热调：应用新阈值/冷却；熔断已打开时按新冷却重算窗口，
 * 让设置页的「即时生效」语义对已打开的窗口同样成立。
 */
export function retuneBreaker(breaker, threshold, cooldownMs) {
  breaker.threshold = threshold;
  breaker.cooldownMs = cooldownMs;
  // 计数已达新阈值：未打开则立即打开（调低阈值即时收紧，而不是等 get open
  // 读时静默清零）；已打开时按新冷却重算窗口（只缩短不延长）
  if (breaker.consecutiveFailures >= breaker.threshold) {
    const newUntil = Date.now() + breaker.cooldownMs;
    if (breaker.openUntil > 0) {
      if (newUntil < breaker.openUntil) breaker.openUntil = newUntil;
    } else {
      breaker.openUntil = newUntil;
    }
  }
}

/** 带状态码的 HTTP 错误，供调用方区分客户端错误与服务端故障。 */
export class Mem0HttpError extends Error {
  constructor(status, path, body) {
    const detail = typeof body === 'string' && body.trim() ? body.trim().slice(0, 300) : '';
    super('mem0 ' + path + ' returned HTTP ' + status + (detail ? ': ' + detail : ''));
    this.name = 'Mem0HttpError';
    this.status = status;
    this.path = path;
  }
}

/**
 * 客户端错误（请求方问题：坏 ID、不存在、服务端校验拒绝）——不应计入熔断。
 * 与 hermes _is_client_error 同源：404/400 状态 + "not found"/"valid uuid" 文案。
 * 本 fork 服务端把 ValueError/Mem0ValidationError 统一映射 400，因此 400 也
 * 视为客户端错误（服务端故障都以 5xx 呈现）。
 */
export function isClientError(error) {
  if (!error) return false;
  if (error instanceof Mem0HttpError) return error.status === 404 || error.status === 400;
  const text = String(error && error.message ? error.message : error).toLowerCase();
  return text.includes('404') || text.includes('400') || text.includes('not found') || text.includes('valid uuid');
}

/** 归一化响应体：{results:[...]} 或裸数组 → 数组。 */
export function unwrapResults(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && typeof payload === 'object' && Array.isArray(payload.results)) return payload.results;
  return [];
}

/**
 * 带超时的 fetch；网络级失败（连接拒绝/DNS——请求大概率没到达）重试一次。
 * HTTP 非 2xx 不重试，直接抛 Mem0HttpError。
 */
async function requestJson(method, url, headers, body, timeoutMs, signal) {
  const attempts = 2;
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const controller = new AbortController();
    const onAbort = () => controller.abort(signal && signal.reason !== undefined ? signal.reason : new Error('aborted'));
    if (signal && signal.aborted) onAbort();
    else if (signal) signal.addEventListener('abort', onAbort, { once: true });
    const timer = setTimeout(
      () => controller.abort(new Error('mem0 request timed out after ' + timeoutMs + ' ms')),
      timeoutMs
    );
    try {
      const response = await fetch(url, {
        method,
        headers,
        ...(body !== undefined ? { body } : {}),
        signal: controller.signal
      });
      if (!response.ok) throw new Mem0HttpError(response.status, new URL(url).pathname, await response.text().catch(() => ''));
      const text = await response.text();
      return text ? JSON.parse(text) : {};
    } catch (error) {
      lastError = error;
      const networkLevel =
        error instanceof TypeError ||
        (error instanceof Error && (error.name === 'FetchError' || /fetch failed|econnrefused|enotfound|socket/i.test(String(error.message))));
      const timedOut = error instanceof Error && /timed out/.test(String(error.message));
      const abortedByCaller = signal && signal.aborted && !timedOut && !(error instanceof Error && /mem0 request timed out/.test(String(error.message)));
      if (abortedByCaller) {
        // 打标记供 #call 区分：调用方主动取消是用户行为，不得污染熔断计数
        error.abortedByCaller = true;
        throw error;
      }
      if (!(networkLevel && attempt < attempts - 1)) {
        if (timedOut || networkLevel) {
          let origin = url;
          try {
            origin = new URL(url).origin;
          } catch {
            // host 配置缺协议时 new URL 会二次抛错，保留原始网络错误即可
          }
          throw new Error('mem0 server unreachable at ' + origin + ' (' + String(error.message || error) + ')');
        }
        throw error;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    } finally {
      clearTimeout(timer);
      if (signal) signal.removeEventListener('abort', onAbort);
    }
  }
  throw lastError;
}

/**
 * 自托管 Mem0 server 客户端。所有方法在熔断打开时直接抛出短路错误，
 * 由调用方决定文案；成功路径统一 recordSuccess。
 */
export class Mem0Client {
  constructor(options = {}) {
    const host = String(options.host || '').trim().replace(/\/+$/, '');
    if (!host) throw new Error('mem0 host is not configured');
    // 缺协议的 host（如 "10.0.0.5:8888"）fetch 会抛难懂的 parse 错误，构造期给出可行动的提示
    if (!/^https?:\/\//i.test(host)) {
      throw new Error('mem0 host must start with http:// or https:// — got: ' + host);
    }
    this.host = host;
    this.apiKey = String(options.apiKey || '').trim();
    this.timeoutMs = options.timeoutMs > 0 ? Math.trunc(options.timeoutMs) : 60000;
    this.breaker = options.breaker instanceof CircuitBreaker ? options.breaker : new CircuitBreaker();
  }

  #headers(extra) {
    const headers = { 'content-type': 'application/json' };
    if (this.apiKey) headers['x-api-key'] = this.apiKey;
    return { ...headers, ...extra };
  }

  #guard(signal) {
    if (this.breaker.open) {
      const error = new Error('mem0 temporarily unavailable: circuit breaker open, retries in ' + Math.ceil(this.breaker.remainingMs / 1000) + 's');
      error.shortCircuited = true;
      throw error;
    }
    if (signal && signal.aborted) throw new Error('aborted');
  }

  async #call(method, path, payload, signal) {
    this.#guard(signal);
    try {
      const result = await requestJson(
        method,
        this.host + path,
        this.#headers(),
        payload !== undefined ? JSON.stringify(payload) : undefined,
        this.timeoutMs,
        signal
      );
      this.breaker.recordSuccess();
      return result;
    } catch (error) {
      // 调用方主动取消（signal.abort）是用户行为而非服务端故障：
      // 计入熔断会让「连续取消几次慢请求」错误触发熔断、短路后续正常读写
      const cancelled = (error && error.abortedByCaller === true) || (signal && signal.aborted);
      if (!cancelled && !isClientError(error)) this.breaker.recordFailure(error);
      throw error;
    }
  }

  /** 语义搜索。filters 里带 user_id（服务端已废弃顶层 user_id）。 */
  async search({ query, filters, topK, rerank, signal }) {
    const body = { query, top_k: topK, filters: filters || {} };
    if (rerank) {
      body.rerank = true;
      body.depth = 'full'; // rerank 仅在全深度模式下生效
    }
    return unwrapResults(await this.#call('POST', '/search', body, signal));
  }

  /**
   * 写入记忆。infer=true 服务端 LLM 抽取（潮浪并忆批量）；infer=false 逐字存储。
   * 返回服务端原始响应（含 results / event_id）。
   */
  async addMessages(messages, { userId, agentId, infer, metadata, signal } = {}) {
    const body = { messages, user_id: userId, agent_id: agentId, infer };
    if (metadata && Object.keys(metadata).length > 0) body.metadata = metadata;
    return this.#call('POST', '/memories', body, signal);
  }

  async updateMemory(memoryId, text, signal) {
    await this.#call('PUT', '/memories/' + encodeURIComponent(memoryId), { text }, signal);
    return { result: 'Memory updated.', memory_id: memoryId };
  }

  async deleteMemory(memoryId, signal) {
    await this.#call('DELETE', '/memories/' + encodeURIComponent(memoryId), undefined, signal);
    return { result: 'Memory deleted.', memory_id: memoryId };
  }

  /**
   * 上报 evolve 反馈（update→correction、delete→useless）。尽力而为：
   * 失败只记调试日志，绝不影响刚成功的工具调用。
   * @returns 是否送达
   */
  async reportFeedback(memoryId, feedbackType, { note, source } = {}, log) {
    try {
      this.#guard();
      await requestJson('POST', this.host + '/evolve/feedback', this.#headers(), JSON.stringify({
        memory_id: memoryId,
        feedback_type: feedbackType,
        source: source || 'auto',
        ...(note ? { note: note.length > 200 ? note.slice(0, 200) + '…' : note } : {})
      }), this.timeoutMs);
      return true;
    } catch (error) {
      if (log && log.debug) log.debug('mem0 feedback report skipped (memory=' + memoryId + '): ' + String(error.message || error));
      return false;
    }
  }
}
