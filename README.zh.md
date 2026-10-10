# Keepsake — Memory Plugin for Hermes Agent

碎片化记忆系统 — 每次对话自动检索相关记忆注入上下文。

```text
用户: "上次那个 React 项目结构怎么搭的？"
                      ↓
         碎片化记忆系统     ← Redis + RediSearch
                      ↓
    ┌─────────────────────────────────────┐
    │  [1] 用户偏好 TypeScript + Vite    │
    │  [2] 之前做过的项目用了 pinia 状态管理 │
    │  [3] 后端建议用 .NET 10 实现       │
    └─────────────────────────────────────┘
                      ↓
         模型直接利用记忆回答
```

## 核心能力

| 特性 | 说明 |
|------|------|
| 📝 **完整条目存储** | 直接存完整文本，不做语义切分 |
| 🔍 **BM25 全文搜索** | RediSearch 全文检索，零成本，同义词扩展 |
| 🧠 **KNN 向量搜索** | 可选 Embedding（OpenAI / DashScope / 本地 Ollama），模型需登记维度（见下文） |
| ⏳ **时间衰减** | 条目按时间降权，旧记忆权重逐步降低（60天半衰期） |
| 🔄 **记忆准入** | `memory(action='add')` 显式写入 + 每轮原文过 v1 写闸门（R1-R8）筛除垃圾/系统注入，v2 两相管线再提纯为事实 |
| 🏷️ **实体提取** | 自动提取人名/地名/项目名/术语存为 entities TAG 字段，搜索时 content 和 entities 双路召回提高命中率 |
| 🔗 **实体共现** | 自动统计实体共现对，搜索时扩展召回关联实体（搜"Python"同时带出"Django"相关条目） |
| 📖 **领域词典** | 从语料+同义词表自动生成 jieba 自定义词典，发 `/new` 时自动加载，分词更准 |
| 🔒 **工作流锁** | 设置 `keepsake:workflow_lock` 全局禁用检索，用于自动化流程 |
| 🚫 **跳过模式** | 配置文件定义跳过词表，简单确认语（好的/嗯/ok）不触发检索 |
| 🏷️ **标签过滤** | 可选按标签范围搜索 |
| 👍 **反馈加权** | 标记有用/没用的条目会影响排序 |
| 🔥 **热门话题** | 自动统计跨会话高频话题 |
| 📖 **同义词表** | 存 Redis Hash，实时加载展开搜索，无需部署 |
| 😡 **情绪烈度** | 检测用户表达激烈程度，烈度高的条目权重更高 |
| 👁️ **注意力追踪** | 用户反复提起的话题自动标记为高关注，相关条目在搜索中排名上升 |
| 🗑️ **选择性遗忘** | 自动清理低价值（旧 + 无反馈 + 低情绪）条目，保持库精简。 |
| 🔁 **多级合并** | jieba 关键词聚类 + LLM 缝合的合并引擎。已从 provider 运行期摘除（2026-09），但**类仍在、Redis/PG 两后端都能跑**，由运维脚本每轮现构造 ⇒ 改阈值下一轮生效、无需重启（见「存储后端」节） |
| 🐘 **双存储后端** | `storage.backend` 一行配置切 `redis`（缺省）或 `postgres`；**读写 / BM25+KNN 检索 / 合并 / 遗忘功能等价**，差异只在实现（见「存储后端」节） |
| ⏰ **定时任务自动注册** | 作为 Hermes 插件使用时，初始化自动注册三条 cron（记忆维护 2h/去重 1h/同义词 8h），零手动配置 |
| 🔀 **RRF 融合排序（v1.3）** | Reciprocal Rank Fusion 将 BM25 全文 + 语义 KNN 结果融合为单一排序，召回更准 |
| 💻 **本地语义检索** | 可选自托管 Ollama 嵌入模型（如 `bge-m3`，1024 维）走 OpenAI 兼容 `/v1/embeddings` 端点，完全本地运行 |
| 🪄 **LLM 查询扩展（2026-09）** | BM25 命中不足 `min_results` 条时，后台用免费档 chat LLM 悄悄生成 2-8 字同义短词写入 Redis 缓存（24h TTL）。**热路径零延迟增加**；未配置 `llm` 通道时自动停用 |
| ⏱️ **时间感知召回（v1.5）** | 实体时间线（`keepsake:entity_timeline`）+ 事实版本化——搜索不仅看「说了什么」还看「什么时候发生」 |
| 🧪 **本地记忆提炼** | `scripts/memory_distill.py` 用本地模型（qwen3:8b）把陈旧条目蒸馏为精简摘要，watermark 增量更新（可开关、ComfyUI 错峰） |
| 📊 **检索质量抽查集** | `scripts/eval_spotcheck.py` 30 条真实查询回归测试持续追踪检索质量（v1.4 BM25-only：60% → 67%；+向量 KNN 融合 2026-09：73%） |
| 🧩 **Hermes 插件壳** | 内含 `hermes-plugin/` 目录（plugin.yaml + __init__.py），即拷即用 |

## 设计哲学：为 LLM 优化的干净记忆

碎片记忆存储的是**完整、自包含的记忆条目**——而不是切碎的对话片段。核心洞察：LLM 需要完整上下文才能有效利用存储的信息。"偏好 TypeScript + Vite" 这种片段没有前后文就是垃圾；完整的 "用户偏好 TypeScript + Vite 做前端项目" 才真正可用。

| 人脑特征 | 实现方式 |
|----------|---------|
| 完整上下文 | 直接存完整文本，不做切分 |
| 遗忘曲线 | 时间衰减（60天半衰期）—— 旧记忆自然淡去 |
| 情绪加深记忆 | 情感权重加权 —— 情绪强烈的经历记得更牢 |
| 反复提及则强化 | 注意力追踪 + 热词统计 |
| 用进废退 | 反馈正强化（keepsake_feedback） |
| 触类旁通、联想回忆 | 同义词自动发现（Jaccard 共现统计）—— "部署" ↔ "上线" |
| 实体关联 | 实体共现追踪 —— "BTC"和"减半"无语义重叠但因共现被关联召回 |
| 实体索引 | 就像人脑给记忆打标签 —— 自动提取实体名，搜索时双路召回 |
| 记忆准入 | 显式 `memory(action='add')` 写入 + 每轮原文过写闸门、由 v2 管线提纯 |
| 睡眠时整理记忆 | 每 2h 选择性遗忘 + 同义发现；v2 两相管线窗口级提纯（合并引擎不从 provider 触发，由运维脚本现构造） |
| 不同场景记忆隔离 | agent_id 标签体系 —— 分身各自记忆不交叉 |
| 模糊但够用 | BM25 全文搜索 —— 不需要精确匹配就能回想起来 |

**默认形态（BM25-only）不需要向量数据库、不需要 Embedding API、不需要 LLM 参与记忆操作**——纯统计方法跑在 Redis + RediSearch 上：频率、时效、情绪烈度、关联性、反馈，跟人脑用的是一套东西。可选层（KNN 语义召回、LLM 写入管线、查询扩展）即插即用且各有显式降级路径——缺配置就是关，绝不悄悄调用付费服务。

## 依赖

- **Python 3.10+**
- **Hermes Agent 0.12+** — 提供 `MemoryProvider` 接口
- **Redis 7+** — 带 RediSearch 模块（v2.6+）
- **jieba** — 中文分词（自动安装）
- **Embedding API**（可选） — OpenAI / DashScope / 任意兼容 `/v1/embeddings` 的服务，含自托管 [Ollama](https://ollama.com)（如 `bge-m3`）

## 安装

```bash
pip install keepsake
```

或者从 GitHub 直装：

```bash
pip install git+https://github.com/j-zly/keepsake.git
```

## 配置

配置优先级（高→低）：**环境变量 > JSON 配置文件 > config.yaml 内联 > 默认值**

### 1. 配置方式

配置碎片化记忆有三种方式，按优先级从高到低排列：

1. **环境变量**（优先级最高）  
   设置如 `KEEPSAKE_REDIS_HOST`、`KEEPSAKE_REDIS_PASSWORD` 等环境变量。

2. **JSON 配置文件**（~/.config/keepsake/config.json）  
   完整的 JSON 配置文件，用于所有设置。

3. **代码默认值**（优先级最低）  
   在代码中定义的默认值。

### 2. 完整配置示例

以下是配置文件 `~/.config/keepsake/config.json` 的完整示例，包含所有可用选项：

```json
{
  // Redis 连接配置
  "redis_host": "127.0.0.1",
  "redis_port": 6379,
  "redis_password": "",

  // 存储后端（可选，缺省即 redis）—— 切后端就改这一行。
  // "postgres" 与 "redis" 功能等价：写入、BM25+KNN 检索、合并、遗忘都能跑。
  // 安装：pip install 'keepsake-memory[postgres]'（数据库侧还需要 pgvector）
  // ⚠️ 不配 embedder 时必须显式给顶层 "embed_dim"（如 1024）—— 见「存储后端」→ embed_dim
  // "storage": {
  //   "backend": "postgres",
  //   "postgres": {"host": "127.0.0.1", "port": 5432, "dbname": "keepsake",
  //                "user": "keepsake", "password": "***", "sslmode": ""}
  // },

  // 搜索相关配置
  "top_k": 5,
  "candidate_k": 10,
  "bm25_limit": 10,
  "tag_filter": "",

  // 跳过检索配置
  "skip_min_length": 2,
  "skip_patterns_file": "~/.config/keepsake/skip_patterns.txt",

  // 时间衰减配置
  "decay_half_days": 60,
  "hot_topic_decay_half_days": 30,
  
  // 排序权重配置
  "sentiment_boost_positive": 1.5,
  "sentiment_boost_negative": 1.3,
  "feedback_positive_boost": 1.3,
  "feedback_negative_penalty": 0.5,
  "hot_topic_boost": 1.2,
  
  // 注意力机制配置
  "attention_boost_max": 1.5,
  "attention_base_increment": 2.0,
  "attention_emotion_factor": 1.5,
  
  // 嵌入配置（可选）— 模型必须已在 embedder.py 的 _MODEL_DIMENSIONS 登记维度，
  // 未登记模型 = 显式降级 BM25-only（绝不静默兜底默认维度）。
  // 例：自托管 Ollama bge-m3（OpenAI 兼容端点，api_key 任意非空值）
  "embedder": {
    "provider": "openai",
    "api_key": "***",
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "bge-m3"
  },

  // LLM 通道（v2 两相管线 + 查询扩展共用）— 无配置 = 无 LLM = v1 规则直存降级
  "llm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash", "key_file": "/path/to/key.pass"},

  // 查询扩展（可选，默认开）— BM25 命中 < min_results 时后台 LLM 生成同义短词入缓存
  "retrieval": {"query_expansion": {"enabled": true, "min_results": 3, "max_terms": 6, "cache_ttl": 86400}},
  
  // 自动维护配置
  "consolidate_min_group": 3,
  "consolidate_min_overlap": 3,
  "consolidate_max_age_hours": 72,
  "forget_max_age_days": 30,
  "forget_dry_run": true,

  // 同义词发现配置
  "synonym_min_word_freq": 10,
  "synonym_jaccard_threshold": 0.5,
  "synonym_min_co_occurrence": 3,

  // 实体共现配置
  "entity_cooc_top_n": 3,
  "entity_cooc_min_count": 2,
  
  // 情感强度因子
  "emotion_intensity_factor": 0.4
}
```

> 注意：Redis 密码兼容性：留空表示无认证，提供密码会自动发送 AUTH 命令。
>
> 注意：`storage.backend` 缺省为 `redis`；缺行 / 空串 / 非法值一律回退 redis，现有部署行为逐字不变。`postgres` 与 `redis` 功能等价 —— 写入、BM25+KNN 检索、合并、遗忘都能跑，差异只在实现。详见「存储后端」节。

### 3. 环境变量对照表

| 环境变量 | 对应配置项 | 说明 |
|----------|------------|------|
| `KEEPSAKE_CONFIG` | — | `config.json` 路径（缺省 `~/.config/keepsake/config.json`） |
| `KEEPSAKE_REDIS_HOST` | `redis_host` | Redis 服务器地址 |
| `KEEPSAKE_REDIS_PORT` | `redis_port` | Redis 服务器端口 |
| `KEEPSAKE_REDIS_PASSWORD` | `redis_password` | Redis 认证密码 |
| `KEEPSAKE_TOP_K` | `top_k` | 最终返回条目数 |
| `KEEPSAKE_CANDIDATE_K` | `candidate_k` | 候选条目数（用于 KNN） |
| `KEEPSAKE_TAG_FILTER` | `tag_filter` | 标签过滤（逗号分隔） |
| `KEEPSAKE_AGENT_ID` | `agent_id` | 身份标签，用于记忆隔离 |
| `KEEPSAKE_IS_PRIMARY` | `is_primary` | `true` = 看得见全部条目；`false` = 只看带自己标签的 |
| `KEEPSAKE_EMBEDDER` | `embedder.provider` | 嵌入模型提供商（`openai`、`dashscope`） |
| `KEEPSAKE_EMBEDDER_URL` | `embedder.base_url` | 嵌入 API 端点 |
| `KEEPSAKE_EMBEDDER_MODEL` | `embedder.model` | 嵌入模型名称 |
| `OPENAI_API_KEY` | `embedder.api_key` | 嵌入 API 密钥 |

> **其余配置项一律只走 `config.json`，没有对应环境变量。** 特别是 `bm25_limit`、`decay_half_days`、`hot_topic_decay_half_days`、`embed_cache_ttl`、`storage.backend`、`embed_dim`、`consolidate_min_group`、`consolidate_min_overlap`、`consolidate_max_age_hours`、`forget_max_age_days`、`forget_dry_run`、`emotion_intensity_factor` 都只从配置文件读。默认值见下方[配置参考](#配置参考)表。

> 注意：Redis 密码兼容空值（无认证）或提供密码进行 AUTH 认证。  
> 注意：修改 config.json 立即生效（只需发送 `/new` 命令，无需重启）。

### 4. 创建 Redis Index（首次使用）

代码会自动创建（`ensure_index()`），也可以手动执行：

```bash
redis-cli FT.CREATE idx:memories ON HASH PREFIX 1 "memory:frag:" SCHEMA \
    content TEXT WEIGHT 1 \
    tags TAG SEPARATOR "," \
    category TAG SEPARATOR "," \
    source TEXT WEIGHT 1 \
    created TEXT WEIGHT 0 \
    entry_type TAG SEPARATOR "," \
    invalid_at TAG SEPARATOR "," \
    entities TAG SEPARATOR "," \
    embed_bin VECTOR FLAT 6 TYPE FLOAT32 DIM 1024 DISTANCE_METRIC COSINE
```

> 维度（DIM）由 embedder 模型的登记维度决定（见「Embedding 模型与维度」表；`bge-m3`=1024，`nomic-embed-text`=768，`text-embedding-3-small`=1536）。换模型/换维度必须：清旧 `embed_bin` 与 `embed_cache:*` → `FT.DROPINDEX`（不带 DD，数据保留）→ 按新维度重建。
> 如果用 Docker：`docker run -d --name redis-stack -p 6379:6379 redis/redis-stack:latest`

### 5. Hermes 配置

在 `~/.hermes/config.yaml` 中开启：

```yaml
memory:
  provider: keepsake
```

如果不配置 `embedder`，则只走 BM25 全文搜索模式。

也支持通过环境变量配置（优先级最高）：

```bash
export KEEPSAKE_REDIS_HOST=127.0.0.1
export KEEPSAKE_REDIS_PORT=6379
export KEEPSAKE_TOP_K=5
export KEEPSAKE_EMBEDDER=dashscope
export KEEPSAKE_EMBEDDER_MODEL=text-embedding-v2
export OPENAI_API_KEY=***        # embedder API key
```

### 6. 工作流锁

在自动化流程（如批量任务）时需要临时禁用检索：

```bash
# 加锁（3600s TTL）
redis-cli SET keepsake:workflow_lock 1 EX 3600

# 解锁
redis-cli DEL keepsake:workflow_lock
```

### 7. 跳过模式文件

创建文本文件，每行一个跳过词，`#` 注释：

```text
# ~/.config/keepsake/skip_patterns.txt
好的
嗯
对
是
哦
可以
没错
ok
okay
yes
yeah
```

在 config.json 中引用：

```json
{
  "skip_min_length": 2,
  "skip_patterns_file": "~/.config/keepsake/skip_patterns.txt"
}
```

### 8. 重启 Gateway

```bash
# CLI 模式重启会话即可
# Gateway 模式需要重启进程
```

## 配置参考

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|---------|--------|------|
| `storage.backend` | — | `"redis"` | `redis` 或 `postgres`。缺行 / 空串 / 非法值一律回退 `redis`，见「存储后端」节 |
| `storage.postgres.host` | — | `127.0.0.1` | PG 主机（仅当 backend=postgres 时读） |
| `storage.postgres.port` | — | `5432` | PG 端口 |
| `storage.postgres.dbname` | — | `keepsake` | PG 库名 |
| `storage.postgres.user` | — | `""` | PG 用户 |
| `storage.postgres.password` | — | `""` | PG 口令 |
| `storage.postgres.sslmode` | — | `""` | PG sslmode |
| `embed_dim` | — | `1536` | PG `embedding vector(N)` 列的维度。**不配 embedder 时必须显式给**，见「存储后端」→ embed_dim |
| `redis_host` | `KEEPSAKE_REDIS_HOST` | `127.0.0.1` | Redis 地址 |
| `redis_port` | `KEEPSAKE_REDIS_PORT` | `6379` | Redis 端口 |
| `top_k` | `KEEPSAKE_TOP_K` | `5` | 最终返回条目数 |
| `candidate_k` | `KEEPSAKE_CANDIDATE_K` | `10` | 候选条目数（KNN 用） |
| `tag_filter` | `KEEPSAKE_TAG_FILTER` | `""` | 标签过滤（逗号分隔） |
| `bm25_limit` | `KEEPSAKE_BM25_LIMIT` | `10` | BM25 搜索候选数 |
| `decay_half_days` | `KEEPSAKE_DECAY_HALF_DAYS` | `60` | 时间衰减半衰期（天） |
| `embed_cache_ttl` | `KEEPSAKE_EMBED_CACHE_TTL` | `3600` | Embedding 缓存时间（秒） |
| `sentiment_boost_positive` | — | `1.5` | 正面条目权重乘数 |
| `sentiment_boost_negative` | — | `1.3` | 负面条目权重乘数 |
| `feedback_positive_boost` | — | `1.3` | 正反馈加分权重 |
| `feedback_negative_penalty` | — | `0.5` | 负反馈降权系数 |
| `hot_topic_boost` | — | `1.2` | 热门话题加权乘数 |
| `embedder.provider` | `KEEPSAKE_EMBEDDER` | `openai` | `openai` / `dashscope` |
| `embedder.api_key` | `OPENAI_API_KEY` | — | Embedding API 密钥 |
| `embedder.base_url` | `KEEPSAKE_EMBEDDER_URL` | `https://api.openai.com/v1/embeddings` | API 端点 |
| `embedder.model` | `KEEPSAKE_EMBEDDER_MODEL` | `text-embedding-3-small` | 嵌入模型名 |
| `consolidate_min_group` | — | `3` | 合并触发最少条目数 |
| `consolidate_min_overlap` | — | `3` | 判为同一主题所需的最少公共关键词数。非法值告警并回落 `3`，见「存储后端」→ 合并阈值 |
| `consolidate_max_age_hours` | — | `72` | 条目最少年龄（小时）后才参与合并。**不从 config.json 读** —— 用 `Consolidator(max_age_hours=…)` 传 |
| `forget_max_age_days` | — | `30` | 条目保留天数后可能被遗忘 |
| `forget_dry_run` | — | `true` | 遗忘安全模式：仅统计不删除 |
| `hot_topic_decay_half_days` | — | `30` | 热门话题时间衰减半衰期（天） |
| `emotion_intensity_factor` | — | `0.4` | 情绪烈度→权重系数（0=不启用，1=max） |
| `skip_min_length` | — | `2` | 触发搜索的最小消息长度 |
| `skip_patterns_file` | — | `""` | 跳过词文件路径（每行一词，# 注释） |
| `attention_boost_max` | — | `1.5` | 注意力加权最大值 |
| `attention_base_increment` | — | `2.0` | 每次提及的基础关注增量 |
| `attention_emotion_factor` | — | `1.5` | 情绪烈度对注意力的放大系数 |
| `synonym_min_word_freq` | — | `10` | 词至少出现在 N 个条目里才考虑 |
| `synonym_jaccard_threshold` | — | `0.5` | 两词 Jaccard 系数 ≥ 此值视为同义 |
| `synonym_min_co_occurrence` | — | `3` | 两词绝对共现次数 ≥ 此值视为同义 |
| `entity_cooc_top_n` | — | `3` | 搜索时扩展多少个关联实体 |
| `entity_cooc_min_count` | — | `2` | 实体共现几次才算有效关联 |

> `sentiment_*`、`feedback_*`、`hot_topic_*` 等排序权重参数目前仅支持 JSON 配置文件设置，暂不支持环境变量。设 `1.0` 即关闭该维度的加权效果。

## 存储后端（Redis / PostgreSQL）

### 切换后端

后端由 `storage.backend` **一行配置**决定：

```json
"storage": {
  "backend": "postgres",
  "postgres": {"host": "127.0.0.1", "port": 5432, "dbname": "keepsake",
               "user": "keepsake", "password": "***", "sslmode": ""}
}
```

`redis`（缺省）与 `postgres` **功能等价**：写入、BM25 + KNN 检索、合并、遗忘在两个后端上都能跑。差异只在实现 —— 同一套 `StorageBase` 接口，两个实现类。

- `backend` 缺行 / 空串 / 非字符串 / 非法值**一律回退 `redis`**，现有部署行为逐字不变。
- 装可选依赖：`pip install 'keepsake-memory[postgres]'`（带 `psycopg[binary]`）。数据库侧还需要 **pgvector** —— KNN 列是 `embedding vector(N)` + HNSW 余弦索引。
- import `storage_pg` **不需要**装 `psycopg`（连接时才 import），所以缺可选依赖不会把纯 Redis 部署拖挂。

### `embed_dim`

顶层键，默认 `1536`，决定 PG `embedding vector(N)` 列的宽度。

**不配 embedder 时必须显式给 `embed_dim`**（例如库是 `bge-m3` 建的 1024 维，就写 `1024`）。不配 embedder 时 provider 会回落默认 `1536`；若线上列是 `vector(1024)`，`ensure_index()` 抛 `_SchemaDimMismatch` ⇒ provider 初始化中止、记忆 0 召回。配了 embedder 时以模型登记维度为准，`embed_dim` 只是兜底。

### schema 自愈

`ensure_index()` 幂等，且能自愈 schema —— 它**绝不无脑重发 DDL**：

1. **先查后补** —— 查 `information_schema` / `pg_indexes`，**缺什么才发什么**；schema 齐备时一条 DDL 都不发。
2. **进程内一次性** —— 按 `(目标库, 维度)` 记忆，重复调用直接返回，不发任何 SQL。
3. **串行 + 有界** —— 固定 `pg_advisory_lock` + `SET LOCAL lock_timeout = 5000`，最多 3 次重试、退避递增。重试超限则记 ERROR 并**返回 `False`**（明确失败，绝不静默成功）。

落到具体场景：**老库缺列**会通过 `ALTER TABLE ... ADD COLUMN` 自动补回维护列，维度已知时再补 `content_tsv` / `embedding`；**空库**不再与 `CREATE TABLE` 撞车（维护列的 `ALTER` 带 `IF NOT EXISTS`，建表路径与补列路径双向幂等）。

### 维护能力与四个后端无关原语

合并与遗忘**只**通过 `StorageBase` 上的四个原语访问存储，这正是两个后端行为一致的原因：

| 原语 | Redis | PostgreSQL |
|------|-------|------------|
| `scan_fragment_keys(cursor, limit, prefix)` | `SCAN` 游标 | keyset 分页 `WHERE key > :cursor ORDER BY key LIMIT n`（**禁用大 OFFSET**） |
| `get_fragments_batch(keys)` | `HMGET` | `WHERE key = ANY(...)` |
| `write_fragments_batch(rows)` | pipeline upsert | upsert，并重算 `content_tsv` / `embedding` |
| `update_fragment_fields(key, fields)` | `HSET` | 局部 `UPDATE`（不动 `content` / `content_tsv` / `embedding`） |
| `delete_fragments_batch(keys)` | pipeline `DEL` | `DELETE ... WHERE key = ANY(...)` + 时间线清理 |

游标是不透明字符串：`""` 表示从头开始，返回的 `next_cursor` 为空表示已扫完。

PG 侧维护列 `level` / `consumed_by` / `consumed_at` 是 `TEXT NOT NULL DEFAULT ''`。**判「未设置」必须用 `= ''`，不得用 `IS NULL`**；默认值保证存量行兼容。

写入语义同样等价：同内容去重时旧版本标 `valid_until` / `is_archived`，新版本另起 `:<epoch>` 后缀 key；R6「命中不覆盖 content」的规则在两个后端上都成立。

### 合并阈值

两个顶层 `config.json` 键（**只有配置文件，没有环境变量**）：

| 键 | 默认值 | 含义 |
|----|--------|------|
| `consolidate_min_overlap` | `3` | 两条碎片判为同主题所需的最少公共关键词数 |
| `consolidate_min_group` | `3` | 一组碎片达到多少条才真合并 |

`consolidate_min_overlap` 此前写死 `2`，生产实测 2819 条里会并 1498 条（53%）；改成 `3` 后约 13%。非法值（非整数 / 布尔 / `< 1`）告警并回落默认值，绝不崩。

合并与遗忘由**运维脚本 / cron 每轮现构造**，provider 不常驻 ⇒ 改这两个键**下一轮生效，无需重启**。为什么 provider 不再常驻合并器，见「Consolidator 退役」一节。

### Redis → PostgreSQL 迁移

```bash
python3 scripts/migrate_redis_to_pg.py --dry-run     # 只读，只打印计划
python3 scripts/migrate_redis_to_pg.py --limit 100   # 先迁 100 条试水
python3 scripts/migrate_redis_to_pg.py                # 全量
python3 scripts/migrate_redis_to_pg.py --skip-aux     # 只搬碎片，不搬辅助结构
python3 scripts/migrate_redis_to_pg.py --config /path/to/config.json
```

该脚本对 Redis **严格只读**（只用 `SCAN` / `HGETALL` / `PING`，启动时用 AST 自检撞写命令黑名单）、**幂等**（`ON CONFLICT (key) DO UPDATE`）、已有的 `embed_bin` 向量**搬运而非重算**（保证两库同源可比），并把辅助结构（话题榜 / 注意力榜 / 实体时间线 / 共现 / 同义词）与碎片一并迁走 —— 迁完逐项打印两侧计数，对不上立即 FAIL。凭据一律来自 keepsake 的 `config.json` / `KEEPSAKE_CONFIG`，不硬编码、不打印、不进日志。

### 已知后端差异

只有语料维护类和两个「死字段」方法有差异，且都是**显式失败而非静默**：

| 方法 | PostgreSQL 行为 | 原因 |
|------|----------------|------|
| `discover_synonyms` | 抛 `NotImplementedError` | 语料维护类，与检索正交 |
| `generate_jieba_dict` | 抛 `NotImplementedError` | 同上 |
| `touch_fragment` | 返回 `False` + 打日志 | `ks_fragment` 无 `touch_count` / `updated_at` 列；这两个字段全仓无读取方 |
| `set_supersedes` | 返回 `False` + 打日志 | 同样无读取方 —— 反向封边走 `supersede_fragment`，两个后端都实现了 |

### 切换后端后的自检清单

把 `storage.backend` 指向 PostgreSQL 后，按序跑下面几步。前四步完全无副作用，后两步是 dry-run（零写入）。

```bash
export PYTHONPATH=src
export KEEPSAKE_CONFIG="$HOME/.config/keepsake/config.json"   # 配置文件不在默认位置就改这里
```

**1. `ensure_index()` → True**（建 / 修 schema，成功时打印 `schema ready on …`）：

```bash
python3 - <<'PY'
from keepsake import _load_json_config
from keepsake.storage import resolve_backend, storage_from_config
cfg = _load_json_config()
print("backend =", resolve_backend(cfg))
st = storage_from_config(config=cfg, agent_id=cfg.get("agent_id", ""), is_primary=cfg.get("is_primary", False))
print("ensure_index =", st.ensure_index())
PY
```

**2. 健康检查**（PG 侧是 `SELECT 1`）：

```bash
python3 - <<'PY'
from keepsake import _load_json_config
from keepsake.storage import storage_from_config
cfg = _load_json_config()
st = storage_from_config(config=cfg, agent_id=cfg.get("agent_id", ""), is_primary=cfg.get("is_primary", False))
print("health_check =", st.health_check())
PY
```

**3. 跑一次检索** —— 期望有命中且分数非零，而不是空列表：

```bash
python3 - <<'PY'
from keepsake import _load_json_config
from keepsake.storage import storage_from_config
cfg = _load_json_config()
st = storage_from_config(config=cfg, agent_id=cfg.get("agent_id", ""), is_primary=cfg.get("is_primary", False))
for hit in st.search("一句你确定在记忆里的原话"):
    print(hit.get("_key"), hit.get("_sim"), (hit.get("content") or "")[:60])
PY
```

**4. 合并 dry-run**（零写入 —— 只扫描 + 聚类 + 报组数）：

```bash
python3 - <<'PY'
from keepsake import _load_json_config
from keepsake.storage import storage_from_config
from keepsake.consolidator import Consolidator
cfg = _load_json_config()
st = storage_from_config(config=cfg, agent_id=cfg.get("agent_id", ""), is_primary=cfg.get("is_primary", False))
print(Consolidator(storage=st, config=cfg).consolidate(dry_run=True))
PY
```

**5. 遗忘 dry-run**（零写入 —— 只统计候选，一条都不删）：

```bash
python3 - <<'PY'
from keepsake import _load_json_config
from keepsake.storage import storage_from_config
from keepsake.forgetter import Forgetter
cfg = _load_json_config()
st = storage_from_config(config=cfg, agent_id=cfg.get("agent_id", ""), is_primary=cfg.get("is_primary", False))
print(Forgetter(storage=st, dry_run=True).forget())
PY
```

两次 dry-run 都应报 `dry_run: True`，且 `deleted: 0` / `would_merge` 有计数。两者都不动数据 —— 比对前后碎片总数即可确认。

> 凭据一律从 `config.json`（或 `KEEPSAKE_CONFIG`）读；上面命令**没有一条**在命令行传口令，**也没有一条**会打印口令。

### Embedding 模型与维度

Embedding 模型必须先在 `embedder.py:_MODEL_DIMENSIONS` **登记维度**——未登记的模型会被显式拒绝并降级为 BM25-only（日志给出登记指引），绝不按猜测的默认维度建索引（防止维度错配导致的静默索引损坏）。

| 模型 | 维度 |
|------|------|
| OpenAI text-embedding-3-small | 1536 |
| OpenAI text-embedding-3-large | 3072 |
| OpenAI text-embedding-ada-002 | 1536 |
| DashScope text-embedding-v2 | 1536 |
| DashScope text-embedding-v3 | 1024 |
| BAAI bge-m3（本地 Ollama） | 1024 |
| BAAI bge-large-zh-v1.5 / bge-large-en-v1.5 | 1024 |
| BAAI bge-base-zh-v1.5 | 768 |
| nomic-embed-text（本地 Ollama） | 768 |
| mxbai-embed-large（本地 Ollama） | 1024 |
| snowflake-arctic-embed（本地 Ollama） | 1024 |

> ⚠️ 换模型 = 换维度，必须走迁移流程：清空存量 `embed_bin` 与 `embed_cache:*` → `FT.DROPINDEX`（不带 DD，数据保留）→ 按新 DIM 重建。旧向量与新索引维度不兼容，不清理会造成 hash_indexing_failures 静默堆积。

### 同义词表

存 Redis Hash `keepsake:synonyms`，搜索时实时展开同义词，提高召回率：

```bash
redis-cli HSET keepsake:synonyms setup '["安装","配置","部署","搭建"]'
redis-cli HSET keepsake:synonyms fix '["修","改","补","修复","解决"]'
```

## 验证

启动后检查日志：

```
Memory provider 'keepsake' registered (0 tools)
keepsake: connected (session=xxx, top_k=5, tag_filter=(none))
keepsake: embedder enabled (openai, dim=1024)      # KNN 已通电
# 或：keepsake: BM25-only mode (no embedder configured)
keepsake: auto-registered cron job 'memory-maintenance'
keepsake: auto-registered cron job 'synonym-discovery-daily'
keepsake: auto-registered cron job '记忆去重'
```

> 生效判据核到子功能：看到 `embedder enabled (dim=N)` 才算向量检索在跑；若配置了模型却看到 `dim=1536` 且日志伴随 `not registered in _MODEL_DIMENSIONS`，说明模型未登记、已显式降级 BM25-only（属预期保护行为，去登记表加一行即可启用）。

## 语义检索升级（2026-09）

可选语义层围绕三条原则重做：

1. **Embedding 侧零静默兜底。** 模型维度必须在 `_MODEL_DIMENSIONS` 登记；未登记模型显式失败（embedder 标记不可用 → `storage._embed_enabled=False` → BM25-only，日志给出登记指引）。线上索引 DIM 与 embedder 不符时**拒写向量**并 ERROR 提示重建，而非静默堆积 `hash_indexing_failures`。换模型迁移流程 = 清 `embed_bin`+`embed_cache:*` → `FT.DROPINDEX`（不带 DD）→ 按新 DIM 重建 → 回填存量（`scripts/backfill_embeddings.py`，`--limit` 默认 500，全量记得放大）。自托管 Ollama 走 OpenAI 兼容端点即插即用（如 `bge-m3`，1024 维）。
2. **LLM 通道纯配置化。** v2 写入管线与查询扩展共用 `config.json` 的 `llm` 节，唯一配置源——硬编码付费模型兜底链已移除（缺配置=功能关闭，绝不产生意外计费调用）；mtime 感知，下一处理窗口热生效免重启。
3. **写侧数据卫生。** R1 闸门拒收系统注入文本（compaction 摘要、网关重启提示 `[System note`）；热门话题链路 jieba 分支只收含 CJK 的 token（英文碎渣绝迹）+ 英文虚词表过滤，技术词（`api`/`ssh`/`log`…）刻意保留防误杀；存量污染用 `scripts/cleanup_hot_topics.py` 清洗（默认 dry-run）。

实测效果：30 条真实查询回归集（`scripts/eval_spotcheck.py`）BM25-only 67% → BM25+KNN（bge-m3，RRF 融合）73%。

## 项目结构

```
keepsake/
├── src/keepsake/         # Python 包 — 记忆提供者核心
├── hermes-plugin/        # Hermes 插件壳（拷到 ~/.hermes/plugins/ 即用）
│   ├── plugin.yaml
│   └── __init__.py
├── cron/                 # 定时任务包装脚本
│   ├── memory-maintenance.py   # 每 2h — 选择性遗忘（合并不在此脚本，见「维护能力」）
│   ├── dedup-memory.sh         # 每 1h — 去重
│   └── discover-synonyms.py    # 每 8h — 同义词自动发现
├── scripts/              # 独立工具脚本（开发/测试）
├── README.md
└── pyproject.toml
```

三条定时任务由插件初始化时**自动注册**（发 `/new` 或重启 gateway），无需手动 `hermes cron create`。
## 工作原理

```
┌────────────────────────────────────────────────────────┐
│                    用户发送消息                          │
└──────────────────┬─────────────────────────────────────┘
                   │
         ┌─────────▼─────────┐
         │   prefetch()       │  ← 自动触发
         │   ↓                │
         │  工作流锁检查       │  ← 检查 keepsake:workflow_lock
         │   ↓                │
         │  跳过模式检查       │  ← 短消息 / 确认词命中 skip list
         │   ↓                │
         │  BM25 全文搜索     │  ← 默认，零成本，搜索完整条目
         │  (KNN 向量 search) │  ← 可选（需 embedder）
         │  实体共现扩展      │  ← 查询实体 → 召回关联实体条目
         │   ↓                │
         │  六维重排序        │  ← 相似度 × 时间衰减
         │                    │    × 情绪 × 反馈 × 热门 × 注意力
         │   ↓                │
         │  Top N 注入上下文   │  ← 完整条目直接返回，不截断
         └─────────┬─────────┘
                   │
         ┌─────────▼─────────┐
         │   模型回复         │  ← 完整内容直接使用
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   on_memory_write()│  ← 仅 memory(action='add') 时触发
         │   存储完整文本      │  ← 不做切分，整段存放
         │   实体提取          │  ← jieba + regex → entities TAG 字段
         │   实体共现记录      │  ← 实体对 ZINCRBY 记共现
         │   注意力追踪        │  ← 提取关键词计入关注度
         │   ↓                │
         │   存入 Redis        │  ← 下次可被检索（完整内容）
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   [cron] 每 2h     │  ← 后台 maintenance
         │   ① 选择性遗忘     │  ← 低价值条目清理
         │   (合并由运维脚本每轮现构造，不在 provider 内触发)
         └───────────────────┘
```

## Consolidator 退役（2026-09-09）

**退役的只是 provider 的运行期接线，不是这个能力。** `Consolidator`（基于 jieba 关键词聚类 + LLM 多级缝合的离线合并引擎）类源码完整保留在 `src/keepsake/consolidator.py`，在 **Redis 与 PostgreSQL 两个后端上都能跑**（它只通过[四个后端无关原语](#维护能力与四个后端无关原语)访问存储）。provider 的 `initialize()` 不再构造它、`maintenance()` 不再触发合并循环；`cron/memory-maintenance.py` 只跑遗忘。

**为什么摘掉接线：** LLM 多级合并产出的「无时态散文」与 v2 封边链（`superseded_by`）互不相认（旧产物会被封边过滤误伤或绕过）；提纯职能已被 v2 两相管线的「提取相 + 更新相」全面覆盖；双管线并存要为同一份碎片库维护两套并查链路，得不偿失。

**怎么用合并：** 运维脚本 / cron **每轮现构造** —— `Consolidator(storage=storage_from_config(...), config=cfg).consolidate(dry_run=False)`。因为每轮现读 config，改 `consolidate_min_overlap` / `consolidate_min_group` **下一轮即生效，无需重启**。阈值见「存储后端」→ 合并阈值，dry-run 见「切换后端后的自检清单」。

**运行时观测：** `maintenance()` 返回的 stats 仍保留 `consolidator` 字段（`{"status": "retired", "reason": "v2 pipeline takeover"}`），便于 cron 探针 / 健康检查观测退役状态而非误报 missing-key。

## 协议

MIT
