window.__ModuleLoader__.load({
  id: "dsh-mem0-plugins",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;

    let react = require("react");
    let jsxRuntime = require("react/jsx-runtime");
    let jsx = jsxRuntime.jsx;
    let jsxs = jsxRuntime.jsxs;
    let useState = react.useState;
    let useSyncExternalStore = react.useSyncExternalStore;

    const NS = "mem0";

    // ---- 最小快照 store ----
    function createStore(init) {
      let state = init;
      const listeners = new Set();
      return {
        getSnapshot() { return state; },
        subscribe(fn) { listeners.add(fn); return () => { listeners.delete(fn); }; },
        set(next) { state = next; listeners.forEach((fn) => fn()); }
      };
    }

    // ---- 字段规格 ----
    // type: text | number | bool；number/bool 的 parse 把表单值规整为 schema 期望的 JSON 值；
    // secret: true 的文本字段以 password 输入框呈现（apiKey 类，防旁观与 shoulder-surfing）；
    // number 的 min/max 与宿主 Config schema 一致：编辑时即 clamp，
    // 否则越界值会被宿主 scope.set 静默拒绝 → 部分提交 + 笼统报错（2026-08-25 审计 CL1）
    const FIELDS = [
      { key: "enabled", type: "bool" },
      { key: "host", type: "text" },
      { key: "apiKey", type: "text", secret: true },
      { key: "userId", type: "text" },
      { key: "agentId", type: "text" },
      { key: "forceRecallStep", type: "bool" },
      { key: "topK", type: "number", min: 1, max: 50 },
      { key: "rerank", type: "bool" },
      { key: "distillEnabled", type: "bool" },
      { key: "distillMinChars", type: "number", min: 1, max: 100000 },
      { key: "distillInputMaxChars", type: "number", min: 200, max: 200000 },
      { key: "distillBaseUrl", type: "text" },
      { key: "distillApiKey", type: "text", secret: true },
      { key: "distillModel", type: "text" },
      { key: "distillTimeoutMs", type: "number", min: 1000, max: 600000 },
      { key: "distillRetryAfterMs", type: "number", min: 500, max: 120000 },
      { key: "syncEnabled", type: "bool" },
      { key: "coalesceEnabled", type: "bool" },
      { key: "coalesceIdleMs", type: "number", min: 500, max: 300000 },
      { key: "coalesceWindowMs", type: "number", min: 1000, max: 600000 },
      { key: "coalesceMaxTurns", type: "number", min: 1, max: 50 },
      { key: "coalesceMaxChars", type: "number", min: 200, max: 200000 },
      { key: "fastpathChars", type: "number", min: 200, max: 200000 },
      { key: "feedbackEnabled", type: "bool" },
      { key: "queueMaxLen", type: "number", min: 5, max: 1000 },
      { key: "breakerThreshold", type: "number", min: 1, max: 100 },
      { key: "breakerCooldownMs", type: "number", min: 1000, max: 3600000 },
      { key: "requestTimeoutMs", type: "number", min: 1000, max: 900000 }
    ];

    function parseFieldValue(field, raw) {
      if (field.type === "bool") return typeof raw === "boolean" ? raw : null;
      const text = String(raw == null ? "" : raw).trim();
      if (text === "") return { cleared: true };
      if (field.type === "number") {
        const n = Number(text);
        if (!Number.isFinite(n)) return { invalid: true, raw: text };
        // clamp 到宿主 schema 范围：越界值会被宿主 scope.set 静默拒绝（部分提交 +
        // 笼统报错）；clamp 后与宿主 spec() 的 clampInt 行为一致，所见即所得
        const min = typeof field.min === "number" ? field.min : -Infinity;
        const max = typeof field.max === "number" ? field.max : Infinity;
        return { value: Math.min(max, Math.max(min, Math.trunc(n))) };
      }
      return { value: text };
    }

    // ---- 表单控制器：staging + revision-fenced scope 写入 ----
    function Mem0Form(scope) {
      this.scope = scope;
      this.staged = new Map(); // key → {cleared} | {invalid,value} | {value}
      this.listeners = new Set();
      this.saving = false;
      this.failed = false;
      const self = this;
      // scope 订阅由宿主的 ctx.effect 托管（apply 侧），插件卸载时自动退订
      this.store = createStore(this.projection());
      this.listeners.add(() => { this.store.set(this.projection()); });
    }
    Mem0Form.prototype.publish = function () { this.listeners.forEach((fn) => fn()); };
    Mem0Form.prototype.snapshotOf = function () { return this.scope.getSnapshot(); };
    Mem0Form.prototype.sectionValue = function (key) {
      const v = this.snapshotOf().value;
      return v === undefined || v === null ? undefined : v[key];
    };
    Mem0Form.prototype.userLayer = function () { return this.snapshotOf().user; };
    Mem0Form.prototype.stored = function (key) {
      const user = this.userLayer();
      return user !== undefined && user !== null && Object.prototype.hasOwnProperty.call(user, key);
    };
    Mem0Form.prototype.spec = function (key) {
      return FIELDS.find((f) => f.key === key);
    };
    Mem0Form.prototype.field = function (key) {
      const field = this.spec(key);
      const staged = this.staged.get(key);
      if (staged === undefined) {
        const value = this.sectionValue(key);
        return {
          stagedText: field.type === "bool" ? undefined : value === undefined || value === null ? "" : String(value),
          stagedBool: field.type === "bool" ? value === true : undefined,
          overridden: this.stored(key),
          invalid: false
        };
      }
      if (staged.cleared) {
        return { stagedText: "", stagedBool: false, overridden: true, invalid: false };
      }
      return {
        stagedText: field.type === "bool" ? undefined : (staged.invalid === true ? staged.raw : String(staged.value)),
        stagedBool: field.type === "bool" ? staged.value === true : undefined,
        overridden: true,
        invalid: staged.invalid === true
      };
    };
    Mem0Form.prototype.plan = function () {
      const plan = [];
      this.staged.forEach((staged, key) => {
        const field = this.spec(key);
        if (staged.cleared) {
          if (this.stored(key)) plan.push({ key, run: () => this.scope.unset(key).then(() => !this.stored(key)) });
          return;
        }
        if (staged.invalid) {
          plan.push({ key, run: undefined });
          return;
        }
        if (field.type === "bool") {
          if (this.sectionValue(key) === staged.value) return;
          plan.push({ key, run: () => this.scope.set(key, staged.value).then(() => {
            const user = this.userLayer();
            return user !== undefined && user !== null && user[key] === staged.value;
          }) });
          return;
        }
        const section = this.sectionValue(key);
        if (section === undefined || section === null) {
          if (staged.value === "" ) return;
        } else if (String(section) === String(staged.value)) return;
        plan.push({ key, run: () => this.scope.set(key, staged.value).then(() => {
          const user = this.userLayer();
          return user !== undefined && user !== null && user[key] === staged.value;
        }) });
      });
      return plan;
    };
    Mem0Form.prototype.shell = function () {
      const snapshot = this.snapshotOf();
      const plan = this.plan();
      return {
        available: snapshot.status === "ready",
        writable: snapshot.writable === true,
        dirty: plan.length > 0,
        invalid: plan.some((item) => item.run === undefined),
        saving: this.saving,
        failed: this.failed
      };
    };
    Mem0Form.prototype.projection = function () {
      const shell = this.shell();
      const result = { shell };
      FIELDS.forEach((f) => { result[f.key] = this.field(f.key); });
      // 衍生状态：供卡片头部展示「启用/未启用 + 服务地址」，一眼可判总算成败
      const section = this.sectionValue;
      const enabledField = this.field("enabled");
      const hostField = this.field("host");
      var enabledNow = false;
      if (enabledField.stagedBool !== undefined) enabledNow = enabledField.stagedBool === true;
      else enabledNow = section("enabled") === true;
      result.status = {
        enabled: enabledNow,
        host: hostField.stagedText || ""
      };
      return result;
    };
    Mem0Form.prototype.actions = function () {
      const self = this;
      return {
        edit: (key, raw) => {
          const field = self.spec(key);
          self.staged.set(key, parseFieldValue(field, raw));
          self.failed = false;
          self.publish();
        },
        toggle: (key, checked) => {
          self.staged.set(key, { value: checked === true });
          self.failed = false;
          self.publish();
        },
        resetField: (key) => {
          self.staged.delete(key);
          self.failed = false;
          self.publish();
        },
        save: async () => {
          const plan = self.plan();
          const runs = [];
          plan.forEach((item) => { if (item.run !== undefined) runs.push(item.run); });
          if (plan.length === 0) {
            // 暂存值等于生效值（如把 10 敲回 10）：没有可写项，直接清脏态
            if (self.saving) return;
            self.staged.clear();
            self.failed = false;
            self.publish();
            return;
          }
          if (self.saving || runs.length !== plan.length) return;
          self.saving = true;
          self.failed = false;
          self.publish();
          // 快照本次计划涉及的键（引用级）：保存(可能长达 requestTimeoutMs=300s)
          // 期间用户的并发编辑会替换 staged 里的对象——引用变化即保留，绝不静默丢输入
          const planned = [];
          plan.forEach((item) => {
            const ref = self.staged.get(item.key);
            if (ref !== undefined) planned.push([item.key, ref]);
          });
          let landed = true;
          for (let i = 0; i < runs.length; i += 1) {
            const okRun = await runs[i]();
            if (!okRun) landed = false;
          }
          if (landed) {
            for (const [key, ref] of planned) {
              if (self.staged.get(key) === ref) self.staged.delete(key);
            }
          }
          self.saving = false;
          self.failed = !landed;
          self.publish();
        },
        discard: () => {
          if (self.staged.size === 0 && !self.failed) return;
          self.staged.clear();
          self.failed = false;
          self.publish();
        }
      };
    };

    // ---- 样式（官方 plugin-card 风格，独立类名前缀防冲突）----
    const css = ".M0pl_card{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-3);border-radius:12px;list-style:none;transition:border-color .16s,background .16s}.M0pl_card:hover{border-color:var(--dsw-alias-label-dimmed)}.M0pl_cardOpen{background:var(--dsw-alias-bg-layer-2);border-color:var(--dsw-alias-label-dimmed)}.M0pl_header{appearance:none;width:100%;font:inherit;color:inherit;text-align:left;cursor:pointer;background:0 0;border:0;border-radius:12px;align-items:center;gap:12px;padding:14px 16px;display:flex}.M0pl_header:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:-2px}.M0pl_headText{flex-direction:column;flex:1;gap:4px;min-width:0;display:flex}.M0pl_name{color:var(--dsw-alias-label-primary);font-size:15px;font-weight:600;line-height:1.4}.M0pl_description{color:var(--dsw-alias-label-tertiary);font-size:13px;line-height:1.5}.M0pl_pending{white-space:nowrap;background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-secondary);border-radius:999px;flex:none;padding:1px 8px;font-size:11px;font-weight:500;line-height:17px}.M0pl_chevron{color:var(--dsw-alias-label-tertiary);flex:none;transition:transform .16s}.M0pl_chevronOpen{transform:rotate(180deg)}.M0pl_body{border-top:1px solid var(--dsw-alias-border-l2);margin:0 16px;padding-bottom:8px}.M0pl_readOnly{color:var(--dsw-alias-label-tertiary);margin:12px 0 0;font-size:12px;line-height:1.5}.M0pl_footer{border-top:1px solid var(--dsw-alias-border-l2);justify-content:flex-end;align-items:center;gap:8px;padding:12px 0 4px;display:flex}.M0pl_failed{min-width:0;color:var(--dsw-alias-label-error);text-overflow:ellipsis;white-space:nowrap;flex:1;margin:0;font-size:12px;line-height:1.5;overflow:hidden}.M0pl_discard,.M0pl_save{appearance:none;font:inherit;cursor:pointer;border:1px solid #0000;border-radius:8px;padding:5px 14px;font-size:13px;line-height:1.5}.M0pl_discard{border-color:var(--dsw-alias-border-l2);color:var(--dsw-alias-label-secondary);background:0 0}.M0pl_discard:hover:not(:disabled){color:var(--dsw-alias-label-primary);border-color:var(--dsw-alias-label-dimmed)}.M0pl_save{background:var(--dsw-alias-label-primary);color:var(--dsw-alias-bg-layer-3)}.M0pl_discard:disabled,.M0pl_save:disabled{opacity:.4;cursor:default}.M0pl_discard:focus-visible,.M0pl_save:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}.M0pl_field{flex-direction:column;gap:6px;padding:12px 0;display:flex}.M0pl_field+.M0pl_field{border-top:1px solid var(--dsw-alias-border-l2)}.M0pl_head{align-items:center;gap:8px;display:flex}.M0pl_label{min-width:0;color:var(--dsw-alias-label-primary);flex:1;font-size:13px;font-weight:500;line-height:1.5}.M0pl_badges{align-items:center;gap:8px;display:inline-flex}.M0pl_badge{white-space:nowrap;background:var(--dsw-alias-bg-module-platform);color:var(--dsw-alias-label-secondary);border-radius:999px;padding:1px 8px;font-size:11px;font-weight:500;line-height:17px}.M0pl_reset{font:inherit;color:var(--dsw-alias-label-secondary);cursor:pointer;background:0 0;border:none;padding:0;font-size:12px;line-height:1.5}.M0pl_reset:hover:not(:disabled){color:var(--dsw-alias-label-primary)}.M0pl_reset:disabled{cursor:default}.M0pl_reset:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:2px}.M0pl_input{border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-3);height:34px;font:inherit;color:var(--dsw-alias-label-primary);border-radius:8px;padding:0 12px;font-size:13px;line-height:1.5;width:100%;box-sizing:border-box}.M0pl_input:focus-visible{border-color:var(--dsw-alias-brand-primary);outline:none}.M0pl_input:disabled{color:var(--dsw-alias-label-tertiary);cursor:default}.M0pl_inputInvalid{border:1px solid var(--dsw-alias-label-error);background:var(--dsw-alias-bg-layer-3);height:34px;font:inherit;color:var(--dsw-alias-label-primary);border-radius:8px;padding:0 12px;font-size:13px;line-height:1.5;width:100%;box-sizing:border-box}.M0pl_inputInvalid:focus-visible{outline:2px solid var(--dsw-alias-label-error);outline-offset:1px;border-color:var(--dsw-alias-label-error)}.M0pl_invalid{color:var(--dsw-alias-label-error);margin:0;font-size:12px;line-height:1.5}.M0pl_hint{color:var(--dsw-alias-label-tertiary);margin:0;font-size:12px;line-height:1.5}.M0pl_check{width:16px;height:16px;accent-color:var(--dsw-alias-brand-primary);cursor:pointer}@media (prefers-reduced-motion:reduce){.M0pl_card,.M0pl_header,.M0pl_chevron,.M0pl_chevronOpen,.M0pl_discard,.M0pl_save{transition:none}}";
    const tagId = "dsh-mem0-plugins/settings.css";
    if (typeof document !== "undefined" && document.querySelector("style[data-plugin-css=" + JSON.stringify(tagId) + "]") === null) {
      const tag = document.createElement("style");
      tag.dataset.plugin = "dsh-mem0-plugins";
      tag.dataset.pluginCss = tagId;
      tag.textContent = css;
      document.head.appendChild(tag);
    }

    // ---- 文案 ----
    const zh = {
      "card.title": "Mem0 记忆（dsh-mem0-plugins）",
      "card.description": "自托管 Mem0 持久记忆：自动召回相关记忆、自动合并写入对话事实，并提供 mem0_search/add/update/delete 工具。",
      "card.statusOn": "已启用",
      "card.statusOff": "未启用",
      "group.connection": "连接与身份",
      "group.recall": "自动召回",
      "group.sync": "自动写入",
      "group.reliability": "可靠性与超时",
      "unsaved": "未保存",
      "expand": "展开",
      "collapse": "收起",
      "save": "保存",
      "saving": "保存中…",
      "discard": "放弃",
      "saveFailed": "保存未生效，请检查填写内容",
      "readOnly": "该设置为只读（当前连接不可写）",
      "overridden": "已覆盖",
      "reset": "重置",
      "invalid": "请输入有效数字",
      "field.enabled": "启用插件",
      "hint.enabled": "总开关，默认开启（配置即启用）；关闭后不再召回/写入，工具调用会提示未启用",
      "field.host": "Mem0 服务地址",
      "hint.host": "自托管 server URL，如 http://127.0.0.1:8888 或 http://10.200.0.5:8888",
      "field.apiKey": "API Key（可留空）",
      "hint.apiKey": "以 X-API-Key 头发送；服务端 AUTH_DISABLED 时留空即可",
      "field.userId": "用户标识（user_id）",
      "hint.userId": "记忆归属的用户 ID；跨会话共享同一份记忆",
      "field.agentId": "智能体标识（agent_id）",
      "hint.agentId": "写入时附带的 agent_id，便于按来源过滤",
      "field.forceRecallStep": "第一步强制搜索提醒",
      "hint.forceRecallStep": "每轮第一步注入『必须先调 mem0_search』提醒（UI 消息区显示为「上下文注入·记忆提醒」注记，折叠行直接可见，琐碎轮跳过）；配合 usage 引导让『先搜再答』成为每轮流程，默认开启",
      "field.topK": "搜索条数上限（top_k）",
      "hint.topK": "每次召回返回的最大条数，1–50，默认 10",
      "field.rerank": "搜索重排（rerank）",
      "hint.rerank": "开启后以全深度模式请求重排；服务端需配置 reranker",
      "field.distillEnabled": "长文本查询蒸馏",
      "hint.distillEnabled": "超过阈值的用户消息先由小模型提炼成检索意图再搜索，防止长日志打爆服务端；失败自动回退原文",
      "field.distillMinChars": "蒸馏触发阈值（字符）",
      "hint.distillMinChars": "消息不超过该长度原样直查（零损失零开销），默认 500",
      "field.distillInputMaxChars": "蒸馏输入截断（字符）",
      "hint.distillInputMaxChars": "送入蒸馏模型的原文上限，默认 8000",
      "field.distillBaseUrl": "蒸馏端点（OpenAI 兼容）",
      "hint.distillBaseUrl": "如 http://10.220.0.35:8090/v1；留空则跳过蒸馏直查原文",
      "field.distillApiKey": "蒸馏端点 API Key",
      "hint.distillApiKey": "以 Bearer 发送，与 hermes 默认一致",
      "field.distillModel": "蒸馏模型",
      "hint.distillModel": "默认 Qwen3.5-9B（本地部署）",
      "field.distillTimeoutMs": "蒸馏单次超时（毫秒）",
      "hint.distillTimeoutMs": "默认 90000（对齐 hermes 生产 HERMES_DISTILL_TIMEOUT_S=90）",
      "field.distillRetryAfterMs": "双飞触发阈值（毫秒）",
      "hint.distillRetryAfterMs": "首请求无响应超过该时长即并发第二请求，先完成者胜出，默认 20000",
      "field.syncEnabled": "自动写入对话",
      "hint.syncEnabled": "每轮结束后把「用户消息+助手回复」交给服务端抽取事实",
      "field.coalesceEnabled": "潮浪并忆（合并写入）",
      "hint.coalesceEnabled": "把同一会话的多条短对话合并为一次批量写入，摊薄 LLM 抽取调用",
      "field.coalesceIdleMs": "合并空闲阈值（毫秒）",
      "hint.coalesceIdleMs": "桶内无新消息超过该时长即冲刷，默认 5000",
      "field.coalesceWindowMs": "合并窗口阈值（毫秒）",
      "hint.coalesceWindowMs": "桶从首条起超过该时长即冲刷，默认 15000",
      "field.coalesceMaxTurns": "合并轮数上限",
      "hint.coalesceMaxTurns": "桶内达到该轮数即冲刷，默认 5",
      "field.coalesceMaxChars": "合并字符上限",
      "hint.coalesceMaxChars": "桶内累计字符达到即冲刷，默认 4000",
      "field.fastpathChars": "快速直写阈值（字符）",
      "hint.fastpathChars": "单轮消息超过该长度直接落库不进缓冲，默认 2000",
      "field.feedbackEnabled": "进化反馈上报",
      "hint.feedbackEnabled": "mem0_update/delete 成功后向 /evolve/feedback 上报 correction/useless，参与服务端 salience 进化；失败不影响工具结果",
      "field.queueMaxLen": "待写队列上限",
      "hint.queueMaxLen": "队列满时丢最旧，防止服务端长时间不可用时内存膨胀，默认 50",
      "field.breakerThreshold": "熔断阈值（连续失败次数）",
      "hint.breakerThreshold": "连续失败达该次数暂停调用，默认 5",
      "field.breakerCooldownMs": "熔断冷却（毫秒）",
      "hint.breakerCooldownMs": "熔断后经过该时长自动恢复，默认 120000",
      "field.requestTimeoutMs": "单次请求总超时（毫秒）",
      "hint.requestTimeoutMs": "插件到 mem0 server 的单请求总闸（search/add 共用），与 hermes 一致默认 300000"
    };
    const en = Object.assign({}, zh, {
      "card.title": "Mem0 memory (dsh-mem0-plugins)",
      "card.description": "Self-hosted Mem0 persistent memory: automatic recall injection, coalesced memory writes, and mem0_search/add/update/delete tools.",
      "card.statusOn": "Enabled",
      "card.statusOff": "Disabled",
      "unsaved": "Unsaved",
      "expand": "Expand",
      "collapse": "Collapse",
      "save": "Save",
      "saving": "Saving…",
      "discard": "Discard",
      "saveFailed": "Save did not land; check your input",
      "readOnly": "Read-only in this session",
      "overridden": "Overridden",
      "reset": "Reset",
      "invalid": "Enter a valid number"
    });

    const GROUPS = [
      { titleKey: "group.connection", keys: ["enabled", "host", "apiKey", "userId", "agentId"] },
      { titleKey: "group.recall", keys: ["forceRecallStep", "topK", "rerank", "distillEnabled", "distillMinChars", "distillInputMaxChars", "distillBaseUrl", "distillApiKey", "distillModel", "distillTimeoutMs", "distillRetryAfterMs"] },
      { titleKey: "group.sync", keys: ["syncEnabled", "coalesceEnabled", "coalesceIdleMs", "coalesceWindowMs", "coalesceMaxTurns", "coalesceMaxChars", "fastpathChars", "feedbackEnabled"] },
      { titleKey: "group.reliability", keys: ["queueMaxLen", "breakerThreshold", "breakerCooldownMs", "requestTimeoutMs"] }
    ];

    // ---- 视图组件 ----
    function FieldRow(props) {
      const t = props.t;
      const head = jsxs("div", { className: "M0pl_head", children: [
        jsx("label", { className: "M0pl_label", htmlFor: props.id, children: t(props.labelKey) }),
        props.overridden ? jsxs("span", { style: { display: "inline-flex", gap: "8px", alignItems: "center" }, children: [
          jsx("span", { className: "M0pl_badge", children: t("overridden") }),
          jsx("button", { type: "button", className: "M0pl_reset", disabled: props.disabled, onClick: props.onReset, children: t("reset") })
        ] }) : null
      ] });
      if (props.kind === "bool") {
        return jsxs("div", { className: "M0pl_field", children: [
          jsxs("div", { className: "M0pl_head", children: [
            jsx("label", { className: "M0pl_label", htmlFor: props.id, children: t(props.labelKey) }),
            props.overridden ? jsx("button", { type: "button", className: "M0pl_reset", disabled: props.disabled, onClick: props.onReset, children: t("reset") }) : null,
            jsx("input", { id: props.id, type: "checkbox", className: "M0pl_check", checked: props.checked === true, disabled: props.disabled, onChange: (e) => props.onToggle(e.target.checked) })
          ] }),
          jsx("p", { className: "M0pl_hint", children: t(props.hintKey) })
        ] });
      }
      return jsxs("div", { className: "M0pl_field", children: [
        head,
        jsx("input", {
          id: props.id,
          className: props.invalid ? "M0pl_input M0pl_inputInvalid" : "M0pl_input",
          type: props.secret ? "password" : "text",
          autoComplete: props.secret ? "off" : undefined,
          inputMode: props.numeric ? "numeric" : undefined,
          ...(props.invalid ? { "aria-invalid": true } : {}),
          value: props.text,
          placeholder: props.placeholder || "",
          disabled: props.disabled,
          onChange: (e) => props.onEdit(e.target.value)
        }),
        jsx("p", { className: props.invalid ? "M0pl_invalid" : "M0pl_hint", children: props.invalid ? t("invalid") : t(props.hintKey) })
      ] });
    }

    const FIELD_LABELS = {};
    const FIELD_HINTS = {};
    for (const f of FIELDS) {
      FIELD_LABELS[f.key] = "field." + f.key;
      FIELD_HINTS[f.key] = "hint." + f.key;
    }

    function Mem0Card(props) {
      const pair = useState(false);
      const open = pair[0];
      const setOpen = pair[1];
      const state = props.useMem0((snapshot) => snapshot.shell);
      const status = props.useMem0((snapshot) => snapshot.status);
      const fields = props.useMem0((snapshot) => snapshot);
      const t = props.t;
      if (!state.available) return null;
      const disabled = !state.writable;
      const blocked = !state.dirty || state.invalid || state.saving;
      return jsxs("li", { className: open ? "M0pl_card M0pl_cardOpen" : "M0pl_card", children: [
        jsxs("button", {
          type: "button",
          className: "M0pl_header",
          "aria-expanded": open,
          "aria-label": t(open ? "collapse" : "expand") + ": " + t("card.title"),
          onClick: () => setOpen(!open),
          children: [
            jsxs("span", { className: "M0pl_headText", children: [
              jsx("span", { className: "M0pl_name", children: t("card.title") }),
              jsx("span", { className: "M0pl_description", children: (status && status.enabled ? t("card.statusOn") : t("card.statusOff")) + (status && status.host ? " · " + status.host : "") })
            ] }),
            state.dirty ? jsx("span", { className: "M0pl_pending", children: t("unsaved") }) : null,
            jsx("svg", {
              width: "14",
              height: "14",
              viewBox: "0 0 14 14",
              fill: "none",
              xmlns: "http://www.w3.org/2000/svg",
              className: open ? "M0pl_chevron M0pl_chevronOpen" : "M0pl_chevron",
              children: jsx("path", {
                d: "M11.8486 5.5L11.4238 5.92383L8.69727 8.65137C8.44157 8.90706 8.21562 9.13382 8.01172 9.29785C7.79912 9.46883 7.55595 9.61756 7.25 9.66602C7.08435 9.69222 6.91565 9.69222 6.75 9.66602C6.44405 9.61756 6.20088 9.46883 5.98828 9.29785C5.78438 9.13382 5.55843 9.07248 5.30273 8.65137L2.57617 5.92383L2.15137 5.5L3 4.65137L3.42383 5.07617L6.15137 7.80273C6.42595 8.07732 6.59876 8.24849 6.74023 8.3623C6.87291 8.46904 6.92272 8.47813 6.9375 8.48047C6.97895 8.48703 7.02105 8.48703 7.0625 8.48047C7.07728 8.47813 7.12709 8.46904 7.25977 8.3623C7.40124 8.24849 7.57405 8.07732 7.84863 7.80273L10.5762 5.07617L11 4.65137L11.8486 5.5Z",
                fill: "currentColor"
              })
            })
          ]
        }),
        open ? jsxs("div", { className: "M0pl_body", children: [
          !state.writable ? jsx("p", { className: "M0pl_readOnly", role: "status", children: t("readOnly") }) : null,
          GROUPS.map((group) => jsxs("div", { children: [
            jsx("p", { style: { margin: "14px 0 2px", fontSize: "12px", fontWeight: 600, color: "var(--dsw-alias-label-secondary)" }, children: t(group.titleKey) }),
            group.keys.map((key) => {
              const spec = FIELDS.find((f) => f.key === key);
              const field = fields[key];
              const isBool = spec.type === "bool";
              return jsx(FieldRow, {
                t,
                id: "plugin-config-dsh-mem0-" + key,
                kind: spec.type,
                labelKey: FIELD_LABELS[key],
                hintKey: FIELD_HINTS[key],
                numeric: spec.type === "number",
                secret: spec.secret === true,
                text: field.stagedText,
                checked: field.stagedBool,
                overridden: field.overridden,
                invalid: field.invalid,
                disabled,
                onEdit: (raw) => props.edit(key, raw),
                onToggle: (checked) => props.toggle(key, checked),
                onReset: () => props.resetField(key)
              }, key);
            })
          ] }, group.titleKey)),
          jsxs("div", { className: "M0pl_footer", children: [
            state.failed ? jsx("p", { className: "M0pl_failed", role: "status", children: t("saveFailed") }) : null,
            jsx("button", { type: "button", className: "M0pl_discard", disabled: !state.dirty || state.saving, onClick: props.discard, children: t("discard") }),
            jsx("button", { type: "button", className: "M0pl_save", disabled: blocked, onClick: props.save, children: t(state.saving ? "saving" : "save") })
          ] })
        ] }) : null
      ] });
    }

    const injectServices = ["slots", "locale", "settingsScope"];

    function apply(ctx) {
      ctx.effect(() => ctx.locale.register(NS, { zh, en }), "dsh-mem0-plugins: dictionaries");
      const scope = ctx.settingsScope.bind({ namespace: NS });
      const form = new Mem0Form(scope);
      ctx.effect(() => scope.subscribe(() => form.publish()), "dsh-mem0-plugins: scope-follow");
      ctx.slots.inject("settings.plugin.item", function* () {
        yield ctx.slots.register({
          name: "settings.plugin.item",
          key: NS,
          locale: NS,
          inject: () => ({
            hooks: { mem0: form.store },
            ...form.actions()
          })
        }, Mem0Card);
      });
    }

    exports.apply = apply;
    exports.inject = injectServices;
    return module.exports;
  }
});
