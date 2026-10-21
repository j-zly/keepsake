# Keepsake — Memory Plugin for Hermes Agent

> 最后校准: 2026-10-19（对齐 HEAD 11e0ef0：多代理治理四闸 + 记忆/知识库 CRUD + 通用 MCP + Hy-Memory 借鉴线三批（契约v2 演化链四列 / 调和协议五裁决 / 检索层批3）全部上线，班车 3000/5000 准入改制生效）

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
| 🧪 **自动记忆提炼** | `cron/memory_distill.py`（线上部署在 `/root/scripts/`）走 keepsake LLM 通道（config `llm` 节热生效）把近期对话提炼为一句结论入库，watermark 增量更新，LLM 自造治理标签由保留字黑名单剥除（可开关） |
| 📊 **检索质量抽查集** | `scripts/eval_spotcheck.py` 30 条真实查询回归测试持续追踪检索质量（v1.4 BM25-only：60% → 67%；+向量 KNN 融合 2026-09：73%） |
| 🧩 **Hermes 插件壳** | 内含 `hermes-plugin/` 目录（plugin.yaml + __init__.py），即拷即用 |
| 🔌 **通用记忆接口与 MCP** | `MemoryProvider` 是不绑宿主的 `Protocol`，三段失败语义写死（注入 fail-open / 检索显式抛错 / 写 fail-closed）；`build_server(provider)` 三行把任意实现接成 MCP 服务。见「通用记忆接口与 MCP」节 |
| 🛡️ **多代理记忆治理** | 四道闸：非 primary 检索放开 core、写 core 自动降 `core_candidate`、控制台候选审核队列、主脑 `retrieval.exclude_tags` 挡候选。见「多代理记忆治理」节 |
| 🖥️ **控制台记忆条目 CRUD** | 新增 / 真删（confirm=key 末 8 位，删前备份）/ JSONL·CSV 导出 / 导入（dry-run 预演+手输实跑）；常驻层列表带 `?q=` 搜索 |
| 📚 **知识库 CRUD 与灌入器** | 记忆库旁的 documents/chunks 文档库：manual 域归控制台、skill 域归夜间灌入器，切块判据单点收在 `keepsake.kb.ingest`。见「知识库（documents / chunks）与灌入器」节 |
| 🧰 **记忆维护独立页** | 合并/遗忘 dry-run 与实跑 + 常驻候选审核区，从看板摘出为控制台独立页 |

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

  // 私信推送消费者（可选，**缺省关**）— provider 侧挂一条 SSE 流
  // （GET {base_url}/events），新私信**立刻**被看见，不必等下一轮对话。
  // 🔴 凭据**绝不落这里**：本节只写凭据的**来源**。
  //     enabled=false ⇒ 零线程零连接（行为逐字不变）。
  //     ⚠️ **进程启动时读**（线程随网关装载）— 改了要重启网关，没有 mtime 热生效。
  // "push_consumer": {
  //   "enabled": true,
  //   "base_url": "http://127.0.0.1:8210",              // SSE 服务端根地址；/events 由消费者自己拼
  //   "credential_env": "KEEPSAKE_PUSH_CREDENTIAL",     // 或 "credential_file": "/path/to/secret"
  //   "notify_url": "https://example.invalid/hooks/xxx",// 通知出口（渠道 webhook）；空 = 零外呼
  //   "notify_min_interval_s": 120,                     // 同水位限频窗口（秒）
  //   "notify_format": "generic"                        // generic | feishu
  // },
  
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
| `KEEPSAKE_PUSH_CREDENTIAL` | `push_consumer.credential_env` | 私信推送消费者的建流凭据（**默认环境变量名 —— 名字本身可配**） |

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
| `retrieval.exclude_tags` | — | `core_candidate` | 检索时要排除的标签（逗号分隔）。写空串 = 关闸。在 `initialize()` 里解析一次 ⇒ **改它要重启**。见「多代理记忆治理」节 |
| `push_consumer.enabled` | — | `false` | provider 侧是否挂 SSE 流（私信推送消费者）。关 = 零线程零连接。**进程启动时读** ⇒ 改它要重启网关 |
| `push_consumer.base_url` | — | `""` | SSE 服务端根地址（消费者自己拼 `/events`） |
| `push_consumer.credential_env` | `KEEPSAKE_PUSH_CREDENTIAL` | `KEEPSAKE_PUSH_CREDENTIAL` | 建流凭据的**环境变量名**（**凭据真值不落 config.json**）。另有 `push_consumer.credential_file` 指 secrets 文件 |
| `push_consumer.notify_url` | — | `""` | 通知出口的渠道 webhook 地址（例：飞书群机器人）。**空 = 功能关** ⇒ 零外呼 |
| `push_consumer.notify_min_interval_s` | — | `120` | 通知出口的限频窗口（秒）：**同一未读数**在窗口内只喊一次；水位升高立刻再喊 |
| `push_consumer.notify_format` | — | `generic` | 通知载荷形状：`generic`（`{"text": …}`）或 `feishu`（`{"msg_type": "text", "content": {"text": …}}`） |
| `nudge.publish_endpoint` | — | `http://127.0.0.1:8210/internal/publish` | 控制台把提醒帧 **POST** 到哪儿（跨进程投递 —— 流挂在 MCP 进程里）。env 覆盖：`KEEPSAKE_NUDGE_PUBLISH_ENDPOINT`。见「nudge 跨进程投递」节 |

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

写入语义同样等价：同内容去重**只在链头之间进行**（批3 起，`is_latest='0'` 的链身是被顶掉的历史、由当前链头代表，新写入不与链身判重，而是直接另起 `:<epoch>` key 且不动链身）；命中链头复本时旧版标 `valid_until` / `is_archived`，新版本另起 `:<epoch>` 后缀 key，复本保留确定性 = 最早 created 者为链头代表。链头判据全仓单点：Python 侧 `shared.is_chain_head()` + SQL 侧 `CHAIN_HEAD_SQL`，字段缺失/空串/`'1'` 一律按链头处理（老库未迁移不炸）。R6「命中不覆盖 content」的规则在两个后端上都成立。

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

## 知识库（documents / chunks）与灌入器

记忆库之外还有一套文档知识库（`kb_documents` + `kb_chunks`），从控制台的知识库页检索。与 `ks_fragment` 同库不同表，互不干扰。

**按 `source_type` 分域 —— 谁的地盘谁能写：**

| `source_type` | 归属 | 控制台行为 |
|---------------|------|-----------|
| `manual` | 控制台 | 可自由增/改/删。`source_key` 由内容 hash 派生，同样的内容再导一次是幂等而不是重复入库。 |
| `skill` / `skill_hk` | 夜间灌入器 | 任何写操作一律 **HTTP 409** —— 改了下当晚那轮会被覆盖，删了技能再现就被清理回来。UI 上这两类文档只渲染只读徽标，不给写按钮。 |

**切块判据单点收口。**「一篇文档怎么切块、怎么幂等落库」在仓库里只有一份实现：`keepsake.kb.ingest` 的 `split_chunks` / `upsert_document` / `content_hash` / `replace_chunks` / `update_document`。控制台写端点与灌入器 CLI 都从这里 import，测试还用「是同一个函数对象」把这条钉死 —— 否则控制台写进去的文档会被灌入器切成另一种粒度，检索命中与页面显示对不上。改正文时 **同事务重切 chunks**（删旧 + 插新），文档永远不会留下一堆与正文矛盾的切片。`MANUAL_SOURCE_TYPE` 也从这个包导出，所有 manual 域判据都读常量而不写死字面量。

**灌入器** —— `scripts/kb_ingest.py` 从源 skill 文档库读、往 `documents` / `chunks` 写，行为与它收编的旧版**逐字段等价**（在生产库上把两版都跑 `--dry` 对过账）。凭据只走环境变量或口令文件路径，源码零明文。先预演：

```bash
python3 scripts/kb_ingest.py --dry     # 只统计，结束时回滚
python3 scripts/kb_ingest.py           # 实跑
```

它顺带清理死文档：`skill` / `skill_hk` 中 `(source_type, meta.node, meta.skill)` 已不在 live 集合里的那些。`manual` 是控制台的地盘，本脚本永不动它。生产 PG 节点的 cron 已指向本脚本。

**控制台端点**：文档列表（分页、关键词与 `source_type` 双过滤，且响应里**必带** `source_type`，前端据此判断哪些可写）、增 / 改（改正文连带重切）/ 删（confirm = 文档标题全文，删前先写快照备份）、JSONL 导入（dry-run 预演，实跑须手输 `would_add` 计数）、JSONL 导出。

## 通用记忆接口与 MCP

`keepsake.adapters.generic.protocol.MemoryProvider` 是一个 `runtime_checkable` 的 `typing.Protocol`，描述「一个记忆系统对宿主暴露什么」。该模块不 import 任何宿主的东西 —— 描述的是形状，与调用方是谁无关；依赖方向严格是 `adapters.hermes → adapters.generic`。

失败语义按层写死，新后端不能自己发明一套：

| 层 | 方法 | 后端出故障时 |
|------|------|-------------|
| 注入层 | `system_prompt_block()`、`prefetch(query)` | **fail-open** —— 吞掉异常，降级成静态文案 / 空串，至多一条 `logger.warning`。这两个方法在每轮组装上下文的**关键路径**上，这里抛异常等于整轮对话失败 —— 记忆是增强项，不该有能力让对话瘫掉。 |
| 检索层 | `search(query, limit)`、`get(key)` | **显式抛错。** 静默返回 `[]` 是最危险的失败形态：记忆静默搜不到、没有任何告警，用户只会以为「AI 忘了」。 |
| 写层 | `add(text, tags)`、`update(key, …)`、`remove(target)` | **fail-closed** —— 明确报告失败（返回 falsy / 抛错 / `status=error`），绝不假装成功。写失败被当成成功，调用方就会继续往下走并向用户确认「已保存」—— 那是在撒谎。 |

外加维护层 —— `merge(dry_run=True)` 与 `forget(dry_run=True)`，**两者缺省即 dry-run** —— 以及能力声明 `capabilities()`。注意 `get(key)` 返回 `None` 的语义是「确实没有这条」，不是「查不出来」。

### 任意宿主三行接入

```python
from keepsake.mcp_server.server import build_server
server = build_server(MyMemoryProvider())   # 实现上面那套协议
server.run("stdio")                         # 任何 MCP 客户端连上就能用
```

`build_server(provider)` 挂 8 个 tool —— `memory_capabilities`、`memory_search`、`memory_get`、`memory_core_list`、`memory_add`、`memory_remove`、`memory_merge`、`memory_forget` —— 外加两个 resource：`memory://core`（常驻层全文，取的就是每轮注入上下文那份渲染的逐字原件）与 `memory://fragment/{key}`。不传参则用 keepsake 自己的默认装配（`factory.default_provider()`，按 `KEEPSAKE_CONFIG` / `~/.config/keepsake/config.json` 连库）。MCP 的写 tool 与 `provider.add()` 共用同一道写闸，下面的治理规则对 MCP 调用方同样成立。

**传输。** `stdio` 是缺省。`http`（streamable-http）**只听 `127.0.0.1`** —— 绑非回环地址直接以退出码 2 终止，不启动。记忆库没有自带认证，把它暴露到网络上等于把一个人的长期记忆公开读；要对外请走反向代理。CLI：`python -m keepsake.mcp_server [--transport stdio|http] [--host …] [--port …]`。

## 私信实时推送（SSE 长连接）

2026-10 ks_dm_sse。私信实时性分**三层**，逐层递降、互为兜底：

| 层 | 什么时候动 | 拿到什么 | 代价 |
|----|-----------|---------|------|
| **SSE 长连接** | 服务端 `message_send` 落库 COMMIT 成功后**顺原流**推一帧 | `seq` / `from` / `unread`（**只有元数据，没有正文**） | 客户端要常驻一条长连接 |
| **重连追帧** | 建流成功的那一刻 | 当前未读水位（有未读就立刻推一帧） | 无 |
| **`unread_dm` 水位** | 每轮对话（寄生提醒行）/ 主动查 | 未读条数 | 只能轮询，延迟一轮 |

SSE 端点挂在 **MCP 的 http 传输同进程同端口**（默认 `127.0.0.1:8210`）：`GET /events`。

### 建流要过三道闸

`Authorization: Bearer <ksk_凭据>` → 注册表**按凭据反查**代理 → `status=approved` **且** `push_enabled=1`（控制台「代理登记 → 编辑 → 私信实时推送」里开）。任一不满足 ⇒ **401 / 403，不建流**。

```bash
# curl（-N = 不缓冲，逐帧看）
curl -N -H "Authorization: Bearer ksk_..." http://127.0.0.1:8210/events
```

```python
import httpx

with httpx.Client(timeout=None) as client:
    with client.stream(
        "GET",
        "http://127.0.0.1:8210/events",
        headers={"Authorization": "Bearer ksk_..."},
    ) as resp:
        for line in resp.iter_lines():      # event: / data: 逐行到
            print(line)
```

### 事件格式（两类）

帧的形状一样：`event:` 行 + `data:` 行 + 一个空行。

```
event: dm
data: {"seq": 12, "from": "worker-1", "unread": 3}

event: dm-nudge
data: {"seq": 12, "from": "worker-1", "unread": 3}
```

* `dm` —— 有人给你投了私信（`message_send` 落库后推的那一帧）；
* `dm-nudge` —— 人工在控制台点「触发提醒」推的（消息中心 → 按收件代理 → 触发提醒 / 批量提醒）。

🔴 **帧里没有正文**：正文永远走信箱本体（`message_inbox` 拿）—— 推正文等于把私信复制进日志与代理内存。

### 边界与保活

* **心跳**：每 25s 一行 `: ping` 注释帧（客户端忽略即可）；
* **并发**：单代理最多 **2** 条流，**新的踢旧的**（客户端自己会重连）；
* **挂起上限**：默认 30 分钟到点**服务端主动断**（客户端自会重连）；
* **吊销生效边界 = 一个心跳间隔（≤25s）**：每个心跳复核一次建流资格 ——
  凭据被吊销或 `push_enabled` 被关之后，已挂着的那条流**最多再活 25s**；
* **进程内总线**：事件走**进程内**总线（`dict[agent_id → list[asyncio.Queue]]`）⇒
  控制台进程与 MCP 进程**分开跑**时，控制台的「触发提醒」改走下面的
  **内部端点**投递（由**真正挂着流的那台进程**去推；控制台自己不再在本进程推帧）。
  `ponytail:` 真要更通用的跨进程，把这一层换成 Redis pub/sub 即可。

### 手动 / 批量触发提醒

控制台「消息中心 → 按收件代理」：

* 每个收件代理一行，显示它当前未读数 + 「触发提醒」按钮（弹框确认）；
* 勾选多个 → 工具条「批量提醒已选 N 个」（全选 / 清空与常驻层页、记忆条目页同一口径）；
* 逐代理回报 **✓送达 / ⚠离线**（离线 = 对方没挂着流，**提醒不补发**，不谎报送达）。

对应后端端点：`POST /api/v1/messages/nudge`（body `{agents: ["id1", ...]}`，1~50 个去重）、
`POST /api/v1/agents/{agent_id}/push`（开关）。

### nudge 跨进程投递

SSE 流挂在 **MCP 进程**（8210）的内存总线上；控制台是**另一个进程**（8200），
它自己那台总线**一个订阅者都没有** ⇒ 控制台点「触发提醒」若在本进程推帧，
**恒** `delivered=false`（如实，但功能是坏的）。

现在补上了那缺的一米 —— 控制台把帧 **POST** 给**真正挂着流的那个进程**：

```
控制台 (8200) ──POST /internal/publish──▶ mcp-http (8210) ──▶ BUS ──▶ SSE 流
```

* **端点**：`POST /internal/publish`，挂在 **MCP 的 http 传输同进程同端口**
  （与 `/events` 一起，默认 `127.0.0.1:8210`）。body
  `{agent_id, kind, unread, seq?, from?}` ⇒ 回包 `{"delivered": bool}` ——
  **BUS 的真实答案**（到底有没有人挂着流）。帧里**仍然只有元数据**
  （`seq` / `from` / `unread`），**永远没有正文**。
* **两道闸**：① **只接受回环来源**（`request.client.host ∈ {127.0.0.1, ::1}`；
  反向代理不转发本路径 ⇒ 公网不可达）② **共享密钥头** `X-Internal-Token`，
  值从 env `KEEPSAKE_INTERNAL_TOKEN` 读。
* 🔴 **未配置该 env ⇒ 端点一律 503**（fail-closed）：提醒**不投递**，控制台
  **如实**这么说，不谎报已送达。
* **控制台侧**：端点地址 = 配置项 `nudge.publish_endpoint`（env 覆盖
  `KEEPSAKE_NUDGE_PUBLISH_ENDPOINT`），缺省
  `http://127.0.0.1:8210/internal/publish`；共享密钥读**同一个**
  `KEEPSAKE_INTERNAL_TOKEN` env。
* **如实回报**：`delivered=false` + `reason=no_stream`（没人挂着流）或
  `reason=publish_endpoint_unavailable`（端点没起来 / 503 / 没配密钥）⇒
  页面显示 **⚠ 端点不可用** —— 提醒**没送到**；寄生层（对话轮提醒）与
  `unread_dm` 水位仍在。

**部署**：两个进程要配**同一个**密钥，否则 MCP 侧一直 503，每次提醒都报
「端点不可用」。给两个 unit 各加一行（之后 `systemctl daemon-reload` + 重启）：

```ini
# /etc/systemd/system/keepsake-mcp-http.service
# /etc/systemd/system/keepsake-console.service
Environment=KEEPSAKE_INTERNAL_TOKEN=<一串足够长的随机串>
```

### provider 侧挂流消费者（补齐最后一米）

2026-10 ks_push_consumer。服务端早就有了，缺的是**客户端** —— keepsake 自己的
provider 挂一条 SSE 流听着，于是新私信**立刻**被看见，不必等下一轮对话。

**缺省关**：`push_consumer.enabled=false` ⇒ **零线程零连接**，行为与上线前逐字相同。

```json
"push_consumer": {
  "enabled": true,
  "base_url": "http://127.0.0.1:8210",
  "credential_env": "KEEPSAKE_PUSH_CREDENTIAL",
  "notify_url": "https://example.invalid/hooks/xxxx",
  "notify_min_interval_s": 120,
  "notify_format": "generic"
}
```

* 🔴 **凭据绝不落 `config.json`** —— 本节只写凭据的**来源**（`credential_env`，
  或 `credential_file` 指 secrets 文件）；真值经环境变量注入；
* ⚠️ **进程启动时读**：线程随网关装载 ⇒ 改这一节要**重启网关**（没有 mtime 热生效，
  这里如实写明，不让人以为改完就通）；
* `base_url` 是**服务端根地址**，`/events` 由消费者自己拼。

收到一帧只做三件事（**仅此而已**）：

1. **未读水位缓存失效** ⇒ 下一轮 `prefetch` 必重新查库并提醒，不用干等那个
   60s 去重窗口；
2. `logger.info` 记**一行**事件摘要（`event` / `seq` / `from` / `unread`）——
   **永远没有正文**；
3. 宿主可选注册的回调 `on_push_event(fn)` 收到同一个摘要 dict
   （`{"event", "seq", "from", "unread"}`）—— **永远没有正文**
   （正文仍在信箱里，用 `message_inbox` 取）。

```python
provider.on_push_event(lambda summary: print(summary["unread"]))
```

回调在**消费者线程**里被调用 ⇒ 要短、要自己吞异常（实现已经兜了一层：
回调抛异常只记 warning，不影响其余回调与主路径）。

#### 通知出口（空闲期把人叫醒）

上面那套只置水位 —— 提醒还得等用户开口；代理**空闲**时提醒就一直浮不出来。
所以收到 `dm` / `dm-nudge` 帧时，**另外**往你配的渠道 webhook POST 一次
（`push_consumer.notify_url`，例：飞书群机器人）：

* 🔴 **只含元数据** —— 载荷只有未读条数与发件人
  （`📩 你有 2 条未读私信（来自: worker-1）`），**永远没有正文**
  （正文仍在信箱里，用 `message_inbox` 取）；
* ⚠️ **同水位限频** —— 同一未读数在 `notify_min_interval_s`（缺省 120s）窗口内
  只喊一次；**水位升高立刻再喊**，不让人干等窗口；
* `notify_url` 为空 ⇒ **零外呼**（行为与上线前逐字相同）；POST 失败只记 warning
  —— 绝不重试轰炸、绝不碰水位与主路径。

健壮性：连不上 / 断流 ⇒ **指数退避**重连（5s 起、翻倍、封顶 5min）；消费者整体
**fail-open**：起不来也不影响 provider（`initialize()` 绝不因它失败），线程是
daemon，随进程亡。

**一个进程一条流**：服务端每代理最多 2 条并发流（新的踢旧的），而 Hermes 可能按
session 造多个 provider —— 所以消费者与回调表挂在**模块级**，不是实例级。

## 多代理记忆治理

四道闸，均已在生产生效：

| 闸 | 行为 | 配置键 | 生效层 |
|----|------|--------|--------|
| **A — 非 primary 检索放开 `core`** | 子代理也能召回常驻层，而不只是自己的私有条目；但仍写不进去 | `is_primary` | 存储过滤 —— 三个后端的 BM25 路径均生效；PG 与 SQLite 共用同一个 `_search_filter_sql` 出口，BM25 与 KNN 一并覆盖 |
| **B — 写 `core` 自动降级** | 非 primary 代理写 `core` 标签时，该标签被改写成 `core_candidate`（其余标签保留），并打一条告警 | — | `provider._apply_write_gate`，挂在 `provider.add()` 上；MCP 的 `memory_add` 走同一条 |
| **C — 控制台候选审核队列** | 控制台「记忆维护」页分页列出 `core_candidate` 条目。批准 = 先 promote 进 `core` 再摘掉候选标（顺序不能反，反了可能两头空）；驳回 = 只摘候选标，条目仍留在检索层 | — | 控制台 |
| **D — 主脑噪音过滤** | `retrieval.exclude_tags` 在检索出口按标签丢弃条目。缺省挡掉 `core_candidate`，不让「待审申请」在主脑自己的召回里当成事实重复出现；写空串即关闸 | `retrieval.exclude_tags` | `search` / `prefetch` 的同一出口，**先过滤再截断** |

第 D 闸的值在 `initialize()` 里解析一次，所以**改它要重启**；过滤本身每次查询都跑，但跑的是那份冻结下来的集合。

`core_candidate` 的语义因此是「进入常驻层的待审申请」，而不是 LLM 可以自己发明的标签。保留字判据全仓只有一份实现 —— `core/shared.py` 的 `sanitize_reserved_tags()`，提炼 cron（剥掉保留字并记日志）与控制台新增/导入（打 `core` 直接 400）共用。保留集合：精确标签 `core` / `core_candidate` / `shared`，外加 `agent:` 前缀族；按逗号边界精确匹配，所以 `core_x` 不算保留字。

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

## Hy-Memory 借鉴线（2026-10，三批收官）

从 Hy-Memory 开源实现移植的三段能力，全部按「读路径顺手做、零新增定时任务、fail-open」纪律落地：

1. **批1 · 契约v2 演化链**：条目四列 `is_latest` / `supersedes` / `supersede_reason` / `applicability` 进三后端（缺列降级照 content_type 先例）；supersede 双向链维护，检索默认只召回链头。
2. **批2 · 调和协议五裁决**：写入管线的更新相裁决扩为 ADD / UPDATE / SUPERSEDE / VOID / NOOP——UPDATE=同条下一版（封旧+落新、reason 恒空），SUPERSEDE=两条独立事实新顶旧（带 reason 走双向链），VOID=逻辑删（正文保留、`supersede_reason='voided'`，旧 DELETE 动作名向后兼容）；subject 归一化等值闸防「张三的咖啡顶掉用户的」；链头禁抄旧细节 + 同事件另次发生独立 ADD 两条规则进提示词。
3. **批3 · 检索层惰性过期 + 链头判重**：`valid_until` 三态语义——空串=普通事实、过去=归档/版本化（检索滤掉）、**未来=意图有效期**（进候选；被召回时若已过期，`apply_v2_filters` 第五道当场清 `valid_until` + 打 `expired_fact` 标签并从本次结果剔除，下次当普通事实召回，不新增 cron）；store 判重只在链头之间进行（见「维护能力」节）。

配套纪律：链头/裁决类判据一律单点出口（`is_chain_head()` / `CHAIN_HEAD_SQL`），防回归测试类 `TestSingleSourceOfTruth` 钉死"不许出现第三份实现"。

## 项目结构

```
keepsake/
├── src/keepsake/         # Python 包 — 记忆提供者核心
│   ├── domain/           # 类型与契约（Fragment / Hit / Capabilities）
│   ├── core/             # 存储后端与共用判据链
│   ├── adapters/
│   │   ├── generic/      # 不绑宿主的 MemoryProvider 契约
│   │   └── hermes/       # Hermes 插件 provider
│   ├── kb/               # 知识库写侧共用层（切块 / 幂等 upsert）
│   ├── mcp_server/       # 通用 MCP 入口 —— build_server(provider)
│   └── api/              # 控制台后端（路由、鉴权、审计）
├── web/                  # 控制台前端（Vue 3 + Vite）
├── hermes-plugin/        # Hermes 插件壳（拷到 ~/.hermes/plugins/ 即用）
│   ├── plugin.yaml
│   └── __init__.py
├── cron/                 # 定时任务包装脚本
│   ├── memory-maintenance.py   # 每 2h — 选择性遗忘（合并不在此脚本，见「维护能力」）
│   ├── memory_distill.py       # 近期对话提炼为一句结论（剥除保留字）
│   ├── dedup-memory.sh         # 每 1h — 去重
│   └── discover-synonyms.py    # 每 8h — 同义词自动发现
├── scripts/              # 独立工具脚本（开发/测试），含知识库灌入器 kb_ingest.py
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
