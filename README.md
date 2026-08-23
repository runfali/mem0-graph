# mem0_falkordb —— 图增强记忆层

> 基于 [mem0ai/mem0](https://github.com/mem0ai/mem0) 的增强版 Fork，聚焦**生产可用**：恢复图数据库支持、补齐记忆衰减/清理/反馈闭环/可观测性等自部署场景必需能力。

---

## 目录

- [项目定位](#项目定位)
- [架构](#架构)
- [快速开始](#快速开始)
- [功能详解](#功能详解)
  - [图存储与检索](#图存储与检索)
  - [记忆质量](#记忆质量)
  - [可观测与进化](#可观测与进化)
- [管理后台（Dashboard）](#管理后台dashboard)
- [DSH 插件（DeepSeek Harness 记忆集成）](#dsh-插件deepseek-harness-记忆集成)
- [配置](#配置)
- [运维](#运维)
- [环境要求](#环境要求)
- [许可证](#许可证)

---

## 项目定位

mem0 是一个为 AI Agent 提供持久记忆的开源库（存/搜/删 + LLM 事实提取）。本 Fork 在 mem0 全部能力的基础上，做了三类增强：

| 类别 | 内容 |
|------|------|
| **图存储恢复** | 完整恢复 `graphs/` 模块，内置 FalkorDB 图数据库后端（真 Cypher 图，可遍历可查询） |
| **生产级能力** | 记忆衰减、过期清理、语义去重、矛盾检测、时间推理、深度路由、rerank、中文全链路 |
| **可观测与进化** | 搜索质量观测、记忆热度体系、反馈闭环、进化循环、统计面板、召回漏斗 trace |

部署与兼容方面的额外亮点：

| 亮点 | 说明 |
|------|------|
| 配置文件驱动 | `MEM0_CONFIG_PATH` 指向 config.json，纯配置部署，无需调 API |
| 自动管理员 | 容器启动自动创建 `admin@mem0.dev` + 随机密码，日志可见 |
| 80+ 个环境变量 | 连接池/超时/批量/并发/衰减/清理/反馈/深度路由/类型权重全覆盖 |
| 中文全链路 | 记忆提取/图实体/图关系/BM25 分词全汉化 |
| Embedder 兼容 | VoyageAI base64 自动适配；pgvector 维度自动检测 |
| Reranker | SiliconFlow 原生支持 + 分数阈值过滤 |
| Docker 就绪 | 预装依赖，`docker compose up -d` 开箱即用 |

---

## 架构

```
┌─────────────────────────────────────────────────┐
│                  AI Agent                        │
│         add() / search() / delete()              │
└────────────────────┬────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│              Mem0 SDK + graphs 模块              │
│  ┌──────────┐  ┌───────────┐  ┌──────────────┐  │
│  │ 向量存储  │  │ 实体存储   │  │ 图存储       │  │
│  │ (pgvector│  │           │  │ (FalkorDB)   │  │
│  └──────────┘  └───────────┘  └──────┬───────┘  │
│                                      │           │
│  ┌─────────── 可观测/进化层 ─────────┐           │
│  │ evolve_queries / evolve_salience │           │
│  │ evolve_feedback / trace 七阶段    │           │
│  └───────────────────────────────────┘          │
└─────────────────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────┐
│  存储层                                         │
│  · PostgreSQL (pgvector) — 向量 + 元数据 + 观测表 │
│  · FalkorDB — 每用户独立图（mem0_{user_id}）    │
└─────────────────────────────────────────────────┘
```

---

## 快速开始

### Server 部署（推荐，自带 Dashboard）

```bash
git clone https://github.com/dlhermes/mem0_falkordb.git
cd mem0_falkordb/server
```

**第一步：创建配置文件**（模型配置）

```bash
cp config.json.example config.json
# 编辑 config.json，填入真实 API key
```

配置文件结构：

```json
{
  "llm":         { "provider": "openai", "config": { "model": "...", "api_key": "sk-...", "openai_base_url": "" } },
  "embedder":    { "provider": "openai", "config": { "model": "...", "api_key": "sk-...", "openai_base_url": "" } },
  "reranker":    { "provider": "siliconflow", "config": { "model": "BAAI/bge-reranker-v2-m3", "api_key": "sk-..." } },
  "graph_store": { "provider": "falkordb", "config": { "host": "falkordb", "port": 6379, "database": "mem0" } }
}
```

> ⚠️ LLM 配置的字段名是 `openai_base_url`，不是 `api_base`，填错会导致容器启动崩溃。
>
> ⚠️ **必须配置 `vector_store`（pgvector）**，否则 mem0 默认用内存向量库，容器重启后记忆全部丢失。

**第二步：创建环境变量**（基础设施配置）

```bash
cp .env.example .env
# 最少设置 POSTGRES_PASSWORD 和 JWT_SECRET
```

```bash
POSTGRES_PASSWORD=改一个强密码
JWT_SECRET=随机字符串至少32位
DASHBOARD_URL=http://你的服务器IP:3002
API_EXTERNAL_URL=http://你的服务器IP:8888
MEM0_CONFIG_PATH=/app/config.json
```

> `DASHBOARD_URL` 必须用 `http://`，用 `https://` 会导致 Dashboard Cookie 被浏览器拒绝。

**第三步：启动**

```bash
docker compose up -d
```

**第四步：登录 Dashboard**

浏览器访问 `http://你的服务器IP:3002`。管理员账号自动创建，凭据在日志中：

```bash
docker compose logs mem0 | grep -E "(admin|密码)"
```

登录后默认进入**仪表盘**（记忆/实体/请求统计总览）；顶栏搜索框可全局检索记忆，侧边栏进入各管理页面。

### 仅使用 Python SDK

```bash
git clone https://github.com/dlhermes/mem0_falkordb.git
cd mem0_falkordb
pip install build
python3 -m build --wheel
pip install dist/mem0_graph-*.whl
pip install falkordb
docker run -d --rm -p 6379:6379 falkordb/falkordb
```

```python
from mem0 import Memory

config = {
    "graph_store": {
        "provider": "falkordb",
        "config": {"host": "localhost", "port": 6379, "database": "mem0"},
    },
    "vector_store": {
        "provider": "pgvector",
        "config": {"host": "localhost", "port": 5432, "user": "postgres", "password": "...", "dbname": "mem0"},
    },
    "llm": {"provider": "openai", "config": {"model": "gpt-4o-mini"}},
    "embedder": {"provider": "openai", "config": {"model": "text-embedding-3-small"}},
}

m = Memory.from_config(config)
m.add("我喜欢披萨", user_id="alice")
results = m.search("alice 喜欢什么？", user_id="alice")
```

---

## 功能详解

### 图存储与检索

#### 图存储（FalkorDB）

- 图存储接口层完整恢复，FalkorDB 直接编译进 `GraphStoreFactory`，配置即用，无需补丁
- 每用户独立图（`mem0_{user_id}`），实体节点 + 关系边 + 引用计数
- 中文关系名（`部署于`、`偏好`）经 backtick 转义直接写入，无需英文映射
- 详见 → **[docs/falkordb-integration.md](docs/falkordb-integration.md)**

**图数据预览**（每用户独立图，实体节点 + 关系边 + 引用计数）：

![图数据预览](docs/screenshots/graph%20data-preview-1.png)

#### 图记忆时效（Temporal Validity）

冲突消解从「物理删除」改为「失效保留」：

- 旧关系不再删除，写入 `invalidated_at` 标记失效
- 检索默认只返回有效事实（`invalidated_at IS NULL`）
- 同事实再次出现自动复活（重置失效标记）
- 存量关系无标记视为有效，向后兼容；失效时间戳由 Cypher 生成，写入零 LLM 成本（冲突判定复用既有 LLM 消解链路）

**价值**：LLM 误判冲突只是「误失效」——可追溯、可恢复，而非永久丢失。

#### 搜索深度路由

| 深度 | 链路 | 降本 |
|------|------|------|
| `minimal` | 跳过全部检索（命中废话白名单） | 100% |
| `standard` | embedding + BM25（跳过图 + rerank） | ~70% |
| `full` | embedding + BM25 + 图 + rerank（默认） | 0% |

深度自动判定在 `Memory.search()` 入口执行；词表存 SQLite `search_keywords` 表（路径 `/app/history/history.db`），增删词即生效，无需重启。每次搜索实际走的深度记录在 `evolve_queries.depth`，可在 Analytics「召回漏斗」观测。

**时间意图自动升档**：查询带时间意图（如"最近部署了什么"）时，即使被路由到 `minimal`/`standard` 档也会强制按 `full` 执行（时间声部与 rerank 依赖 full 链路），保证"最近发生了什么"类查询拿到时间加权结果。可用 `MEM0_TEMPORAL_FORCE_FULL=false` 关闭。

#### 时间声部检索

解决"最近部署了什么""昨天说过什么"这类查询——语义检索对时间维度是盲的，新旧记忆混在一起。时间声部让"时间"成为一路独立检索信号：

- **时间意图检测**：查询含中英文时间词（最近/近 N 天/N 小时前/昨天/上周/今年… 或 ISO 日期）时识别出时间窗口；强信号（最近/最新，w=0.7）与弱信号（昨天/本周，w=0.4）分档；"今天天气怎么样"类语义查询（天气/新闻/汇率/股票/日程/待办）被排除表拦下，不触发
- **时间召回**：按时间窗口从 pgvector 倒序召回（内容发生时间 `temporal_date` 优先、记录时间 `created_at` 回退，全链路 Asia/Shanghai 时区），过期/失效记忆过滤，表达式索引加速
- **融合排序**：时间候选与向量/图候选一起过 rerank 并豁免阈值；时间意图下全部候选按 `w × 时间衰减分 + (1−w) × rerank 分` 融合排序——近期记忆系统性靠前，普通查询零变化
- **观测**：trace 新增 temporal 阶段与 `temporal_triggered` 标记，落 `evolve_queries` 表可在 Analytics「召回漏斗」查看触发情况
- 开关与参数：`MEM0_TEMPORAL_VOICE` / `MEM0_TEMPORAL_WINDOW_DAYS` / `MEM0_TEMPORAL_HALFLIFE_HOURS` / `MEM0_TEMPORAL_TOP_K` / `MEM0_TEMPORAL_FORCE_FULL`

### 记忆质量

#### 记忆衰减

```
score' = score × 0.5 ** (age_days / (half_life × lane_multiplier))
```

| 档位 | 半衰期 | 触发 |
|------|--------|------|
| 永不衰减 | ∞ | LLM 判 importance=5 |
| 慢衰减 | ~100 天 | lane=slow / 关键词含「踩坑/报错/步骤/流程/配置」 |
| 正常衰减 | ~30 天 | 兜底 |
| 快衰减 | ~20 天 | lane=fast / 关键词含「开心/心情/今天/临时」 |

#### 记忆热度体系

- 记忆写入时即注册 salience（`access_count=0`），搜索命中时递增 `access_count` / `last_access_at`——从未被召回的记忆也会进入未召回清单
- 热度分参与排序：在向量/BM25/实体综合分（归一化）基础上叠加热度乘数 `(1 + 权重 × heat_effective)`
  - `heat_effective = min(access_count/100, 1) + (salience_score − 1)`
  - 权重由 `MEM0_EVOLVE_RANK_WEIGHT` 控制，默认 0 时不改变现有排序
- 时间衰减管「时间」，热度管「使用频率」，互不叠加

#### 显式反馈闭环

对话层捕获用户纠正信号（或人工在接口/面板标记），通过 `POST /evolve/feedback` 调整记忆热度分：

| 反馈 | 热度变化 |
|------|---------|
| useful（有用） | +0.1 |
| useless（无用） | −0.15 |
| correction（内容错误） | −0.05 |

只改热度分、不改记忆内容；每条反馈落审计表（evolve_feedback / evolve_salience_adjustments），可追溯、误报可逆。

#### 语义去重

三层判定合并近重复记忆：

1. 向量粗筛：cosine 相似度 > 阈值（无 LLM）
2. 字符 Jaccard 预筛：明显不同措辞直接跳过（无 LLM）
3. LLM 二元判定：剩余候选对「同事实？YES/NO」

只合并近重复、不压缩内容，安全性优先。cron 每日 05:00 执行。

#### 矛盾检测

开启后 LLM 在每次写入时自动对比新消息与已有记忆，发现矛盾自动清理旧记忆，写入即检测。开关：`MEM0_ENABLE_CONTRADICTION=true`。

#### 时间推理

每条记忆自动标注 `temporal`（PAST/PRESENT/FUTURE/TIMELESS），搜索可用 `filters: {"temporal": "FUTURE"}` 过滤；零额外 LLM 调用。

#### 记忆类型（memory_type）

每条记忆写入时自动打上 5 类类型标签——LLM 提取时顺带输出（零额外调用），缺失或非法时按文本关键词规则兜底：

| 类型 | 中文 | 判断关键词（兜底） |
|------|------|-------------------|
| `FACTS` | 客观事实（默认） | 无法判断时输出 |
| `PREFERENCES` | 偏好 | 喜欢 / 偏好 / 讨厌 / 想要 / 希望 / 爱用 |
| `EXPERIENCES` | 经历（含踩坑） | 踩坑 / 报错 / 步骤 / 流程 / 配置 / 修复 |
| `OBSERVATIONS` | 观察 | 观察到 / 发现 / 看到 / 注意到 |
| `DECISIONS` | 决策 | 决定 / 拍板 / 定了 / 选型 / 采用 |

- **写入分类**：提取时 LLM 输出 `metadata.memory_type`；缺失/非法值走关键词兜底（Phase 2.7，sync/async 双路径），都未命中 → `FACTS`
- **存量回填**：`server/scripts/backfill_memory_types.py`（规则优先、`--use-llm` 可选、`PRUNE_DRY_RUN` 支持、幂等，重跑天然断点续跑）
- **检索过滤**：`search` filters 支持 `{"type": "PREFERENCES"}`（别名，自动映射为 `memory_type`）或 `{"memory_type": "EXPERIENCES"}` 直接过滤；带类型过滤的搜索会**跳过图召回合成条目**（无 memory_type 的「src 关系 dst」碎片不混入结果），普通搜索图召回照常
- **类型权重**：排序时对综合分乘类型权重，默认 1.0 = 零行为变化；环境变量 `MEM0_TYPE_WEIGHT_FACTS` / `MEM0_TYPE_WEIGHT_PREFERENCES` / `MEM0_TYPE_WEIGHT_EXPERIENCES` / `MEM0_TYPE_WEIGHT_OBSERVATIONS` / `MEM0_TYPE_WEIGHT_DECISIONS`（非法值回退 1.0）
- **Dashboard**：记忆页「全部类型」筛选下拉；分析面板「记忆构成」类型分布（端点 `GET /memories/types-distribution`）

#### 递归精炼（记忆压缩）

把 N 条碎片化记忆经 LLM 压缩合并为 1-3 条高层抽象，与未召回清单互补（未召回 = 发现，精炼 = 处理）。**铁律**：LLM 只产出「建议稿」，必须人工确认后才写入记忆库；原记忆 soft-superseded（打 `superseded_by` 标记）不物理删除；可随时回滚。

- **候选发现**：按未召回清单（14 天未召回）取记忆文本，embedding cosine ≥ 0.75 贪婪聚类（组代表取均值向量），组内 ≥ 3 条才成为候选组
- **API**：
  - `POST /memory/refine/candidates?user_id=xxx` — 生成候选（注意 `user_id` 是 **query 参数**）
  - `GET /memory/refine/candidates?user_id=xxx` — 候选列表（含主题/组内条数/建议稿）
  - `POST /memory/refine/apply {"candidate_id": N}` — 人工确认应用（写记忆库 + 原记忆 soft-superseded；非 proposed 返回 409）
  - `POST /memory/refine/rollback {"candidate_id": N}` — 回滚（删除新记忆 + 还原原记忆；非 applied 返回 409）
  - `GET /memory/refine/history?user_id=xxx` — 已应用/已回滚记录
  - **Admin 语义**：admin 不传 `user_id` 时，候选/历史返回全部用户的记录（响应每项带 `user_id`），POST 生成候选对全部用户执行；非 admin 只作用自身
- **cron 脚本**：`server/scripts/refine_candidates.py` — 只生成候选，**永不自动 apply**（`REFINE_DRY_RUN` 支持）
- **Dashboard**：分析面板「记忆精炼」（候选建议稿预览 / 确认应用 / 历史回滚）

### 可观测与进化

每次搜索自动落观测日志（查询词/召回数/平均分/耗时/是否零命中，数据存 `evolve_queries` 表），支撑下方进化循环与面板展示。

#### 进化循环（cron 每日 06:00）

- **高频提权**：access_count ≥ 5 的记忆自动加分（`+min(0.05, (acc−4)×0.01)`，上限 1.5），当日幂等
- **零命中统计**：24h 内零命中查询聚合清单
- **未召回清单**：14 天未被召回的观察清单（只提示不自动降权，由人决策）；记忆已删除的 salience 残留不再显示（孤儿过滤）

#### RECALL 召回漏斗

搜索链路每个阶段采集命中数与耗时：候选池 → 阈值过滤 → 时间衰减 → 图召回 → **时间声部** → 重排序 → 最终。用于定位「搜不到」的病灶（哪一阶段丢的）与性能瓶颈（哪一阶段慢）。结果在 [Analytics 面板](#analytics-分析面板) 的「召回漏斗」可视化。

---

## 管理后台（Dashboard）

随 Server 自带 Web 管理后台（`http://<host>:3002`，登录后默认进入仪表盘）。界面为 **Sentry 风格**（紫午夜画布 + 电光青柠 accent，深色/浅色双主题，可在设置中切换），全中文界面。

| 页面 | 能力 |
|------|------|
| 仪表盘（默认首页） | 记忆/实体/请求统计卡 + 成功率/平均延迟 + 最近请求与记忆 |
| 全局搜索 | 顶栏搜索框即时检索全部记忆（SQL 层，不受条数限制），回车直达记忆页搜索结果 |
| 记忆 | 列表/详情/历史演化查看、按用户/类型/时间筛选、单选与批量删除、语义结果页 |
| 请求 | API 请求日志：方法/状态段/时间筛选、统计卡、详情抽屉 |
| 实体 | 实体统计卡（用户/代理/运行分布）、类型筛选、详情抽屉 |
| 分析 | 七个中文数据面板（记忆构成/搜索质量/反馈回路/热度健康/记忆精炼/操作/召回漏斗） |
| API 密钥 | 创建/吊销/列表管理 |
| 配置 | LLM / 嵌入 / 重排序 / 图数据存储独立配置（provider、model、API Key、Base URL）+ 检索参数（深度检索、车道、重排阈值）+ 提取指令编辑，**保存即热生效** |
| 设置 | 深色/浅色主题切换、修改密码、实例信息（当前模型与存储后端）、**深度路由词汇管理**（minimal/standard/full 三级词汇增删，命中即路由，无需重启） |

### 界面预览

管理后台界面预览（记忆内容已脱敏模糊）：

**仪表盘**（默认首页）：记忆/实体/请求统计卡 + 成功率/平均延迟 + 最近请求与记忆

![仪表盘总览](docs/screenshots/dashboard-preview-1.png)

**记忆页**：列表/详情/历史演化查看、按用户/类型/时间筛选、单选与批量删除、语义结果页

![记忆管理](docs/screenshots/dashboard-preview-2.png)

**请求页**：API 请求日志（方法/状态段/时间筛选）、统计卡、详情抽屉

![请求日志](docs/screenshots/dashboard-preview-3.png)

**实体页**：实体统计卡（用户/代理/运行分布）、类型筛选、详情抽屉

![实体管理](docs/screenshots/dashboard-preview-4.png)

### Analytics 分析面板

七个中文数据面板：

| 面板 | 内容 |
|------|------|
| 记忆构成 | memory_type 类型分布（客观事实/偏好/经历/观察/决策 + 未分类） |
| 搜索质量 | 查询量/零命中率/平均分/延迟（7/30 天）+ 每日趋势 + 零命中 Top 查询 |
| 反馈回路 | useful/useless/correction 分布 + 被纠正最多记忆 |
| 热度健康 | 热度分布 + 高频记忆 + 未召回清单（可点「清理/保留」决策 /「生成精炼候选」）+ 提权记录 |
| 记忆精炼 | 精炼候选建议稿预览 / 确认应用 / 历史回滚 |
| 操作 | 请求量/延迟/成功率 |
| 召回漏斗 | 搜索各阶段命中数与耗时（RECALL trace） |

![Analytics 分析面板](docs/screenshots/dashboard-preview-5.png)

---

## DSH 插件（DeepSeek Harness 记忆集成）

仓库附带一个面向 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）的
bundle 插件 [`plugins/dsh-mem0-plugins/`](plugins/dsh-mem0-plugins/README.md)，让 dsh 直接消费
本 server 作为持久记忆层——「自动记忆」开箱即用，无需在 prompt 里手写记忆逻辑：

| 能力 | 说明 |
|------|------|
| 自动召回 | 用户消息进轮即后台语义搜索，命中则注入系统提示；长文本先经本地小模型蒸馏成检索意图再搜索（防打爆服务端），纯问候/确认类输入零网络开销 |
| 自动写入 | 每轮对话交给服务端 LLM 抽取事实；潮浪并忆把同会话短对话合并成批量写入摊薄调用，纯 JSON 工具输出剥除防污染，中断轮次不入记忆 |
| 四个工具 | `mem0_search` / `mem0_add` / `mem0_update` / `mem0_delete`；改错与遗忘自动上报 `/evolve/feedback` 参与 salience 进化 |
| 可靠性 | 300s HTTP 总闸、熔断器（5 连败 120s 冷却）、有界队列丢最旧、连接级重试、失败一律回退原文 |

安装（web profile）：

```bash
dsh plugin --profile web add /data/code/mem0_falkordb/plugins/dsh-mem0-plugins
# 卸载：dsh plugin --profile web remove dsh-mem0-plugins
```

装好后在 dsh「设置 → 插件配置 → Mem0 记忆」填写 server 地址与 API Key 并打开开关；
召回/蒸馏/合并/熔断等全部参数可在设置页热调（即时生效，无需重启）。完整配置项与
排障见插件 [README](plugins/dsh-mem0-plugins/README.md)。


### config.json（模型与图存储）

**配置架构**（优先级从低到高）：
| 层 | 角色 | 生效方式 |
|---|---|---|
| `.env` | 默认配置项 | 修改后需重构容器（`docker compose up -d --force-recreate`） |
| `config.json` | **权威配置源** | 改它 + 重启容器生效 |
| DB `settings.config_overrides` | 调试兜底 | 平时不用；直接改 DB 值则以它为准（重启后仍优先） |

dashboard 配置页是可视化编辑入口：保存 = 原子写 `config.json`（进程内热生效 + 重启后持久），不写 DB。

![Dashboard 配置/设置页](docs/screenshots/dashboard-preview-6.png)

| 块 | 说明 |
|----|------|
| `llm` | 事实提取大模型（OpenAI 兼容任意服务），支持 `fallbacks` 多层兜底 |
| `embedder` | 向量模型（OpenAI / VoyageAI / 本地 bge 等），VoyageAI base64 自动适配、pgvector 维度自动检测 |
| `reranker` | 可选，SiliconFlow 原生支持，配置后搜索自动重排序（分数阈值过滤可调） |
| `graph_store` | `provider: "falkordb"`，见 [docs/falkordb-integration.md](docs/falkordb-integration.md) |

**推理模型适配**：若 LLM 把回复放在 `reasoning_content` 而 `content` 为空（典型：自部署 Qwen3.5 / DeepSeek-R1），记忆提取会全部为空（日志 `results=0`）。在 `llm.config` 加：

```json
"reasoning_effort": "none"
```

**多层兜底**：`llm.fallbacks` 按顺序提供兜底模型（最多 2 个），主模型异常/超时（60s）自动切换下一层；快速失败（连接/5xx/401）层内重试 3 次，超时直接切层。每层超时可通过环境变量 `MEM0_LLM_FALLBACK_TIMEOUT` 配置（默认 60s）。示例：

```json
"llm": {
  "provider": "openai",
  "config": { "model": "主模型", "openai_base_url": "...", "api_key": "..." },
  "fallbacks": [
    { "provider": "openai", "config": { "model": "兜底1", "openai_base_url": "...", "api_key": "..." } },
    { "provider": "openai", "config": { "model": "兜底2", "openai_base_url": "...", "api_key": "...", "reasoning_effort": "none" } }
  ]
}
```

该兜底机制同样覆盖图实体/关系提取链路（graph write），数量自适应：配置 N 个兜底模型即 N 层保障，未配置时仅走主模型、行为不变，无需额外配置。

### .env（基础设施）

| 变量 | 默认 | 说明 |
|------|------|------|
| `POSTGRES_PASSWORD` | — | 必填 |
| `JWT_SECRET` | — | 必填，≥32 位 |
| `DASHBOARD_URL` | http://localhost:3002 | 必须 http |
| `API_EXTERNAL_URL` | http://localhost:8888 | 对外 API 地址 |
| `MEM0_CONFIG_PATH` | /app/config.json | 配置文件路径 |

### 常用功能开关

```bash
MEM0_ENABLE_DECAY=true              # 记忆衰减（默认关）
MEM0_DECAY_HALF_LIFE_DAYS=30        # 衰减半衰期（天）
MEM0_ENABLE_CONTRADICTION=true      # 矛盾检测（默认关）
MEM0_SEARCH_DEPTH_AUTO=true         # 深度路由自动判定
MEM0_SEARCH_DEPTH_DEFAULT=full      # 默认深度（full = 含图+rerank）
MEM0_EVOLVE_RANK_WEIGHT=0.2         # 热度排序加成权重（0 = 不生效）
MEM0_RERANK_SCORE_THRESHOLD=0.4     # rerank 后保留最低分
MEM0_RERANK_QUERY_MAX_CHARS=4000    # rerank query 截断
MEM0_RERANK_DOCS_MAX_CHARS=6000     # rerank 候选文档分批阈值
MEM0_TEMPORAL_VOICE=true              # 时间声部检索（默认开）
MEM0_TEMPORAL_WINDOW_DAYS=7           # 时间意图「最近」默认窗口（天）
MEM0_TEMPORAL_HALFLIFE_HOURS=168      # 时间衰减半衰期（小时，7 天）
MEM0_TEMPORAL_TOP_K=20                # 时间召回条数上限
MEM0_TEMPORAL_FORCE_FULL=true         # 时间意图查询强制 full 档（默认开）
MEM0_TYPE_WEIGHT_PREFERENCES=1.0      # 类型排序权重（默认 1.0 = 零行为变化）
MEM0_TYPE_WEIGHT_FACTS=1.0            # FACTS 类型排序权重
MEM0_TYPE_WEIGHT_EXPERIENCES=1.0      # EXPERIENCES 类型排序权重
MEM0_TYPE_WEIGHT_OBSERVATIONS=1.0     # OBSERVATIONS 类型排序权重
MEM0_TYPE_WEIGHT_DECISIONS=1.0        # DECISIONS 类型排序权重
```

全部 80+ 个变量的完整清单与性能调优说明见 [server/README.md](server/README.md)。

---

## 运维

### cron 任务（Hermes cronjob 调度）

| 任务 | 时间 | 内容 |
|------|------|------|
| mem0-prune-request-logs | 每日 03:00 | API 请求日志清理 |
| mem0-prune-refresh-tokens | 03:30 | 刷新令牌清理 |
| mem0-prune-history-db | 03:45 | history.db 清理 + VACUUM |
| mem0-prune-expired-memories | 04:00 | 过期记忆 + FalkorDB 孤立节点清理 |
| mem0-dedup-memories | 05:00 | 语义去重 |
| mem0-evolve-cycle | 06:00 | 进化循环（高频提权/零命中/未召回清单） |
| mem0-prune-evolve-orphans | 06:20 | evolve 孤儿清理（记忆本体已删的 salience/feedback/adjustments 残留） |
| mem0-refine-candidates | 06:40 | 递归精炼候选生成（只发现，不自动应用） |

所有脚本支持 dry-run 环境变量（`PRUNE_DRY_RUN=true` / `CONSOLIDATION_DRY_RUN=true` / `EVOLVE_DRY_RUN=true`），watchdog 模式：无动作静默，有动作才输出。

### 常用管理命令

```bash
docker compose logs mem0            # 查看日志
docker compose restart mem0         # 重启（config.json 变更生效）
docker compose up -d --force-recreate mem0   # 重建（.env 变更需重建）
```

**重置管理员密码**：容器内执行 `python3 scripts/reset_admin_password.py`。

---

## 环境要求

- Python 3.10-3.12
- Docker（FalkorDB + PostgreSQL）
- FalkorDB ≥ 1.6.0
- PostgreSQL（pgvector/pgvector:pg17 镜像，已预装向量扩展）

---

## 许可证

Apache 2.0 —— 与上游 [mem0ai/mem0](https://github.com/mem0ai/mem0) 一致。
