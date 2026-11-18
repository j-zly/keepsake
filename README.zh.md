# Keepsake — Hermes Agent 的记忆插件

> 最后校准: 2026-11-18（对齐 HEAD 3138441：2.0.0 移除 Redis 后端、storage.backend 必填无缺省、墓碑可查证、定时任务页与进程内班车 2.2.0）

Keepsake 会自动检索相关记忆，并把它们注入每一轮对话的上下文中。

```text
用户: "我们上次是怎么搭那个 React 项目结构的？"
                      ↓
         Keepsake 系统     ← PostgreSQL（pgvector）/ SQLite
                      ↓
    ┌─────────────────────────────────────┐
    │  [1] 用户偏好 TypeScript + Vite       │
    │  [2] 之前的项目用 pinia 做状态管理      │
    │  [3] 后端建议用 .NET 10 实现            │
    └─────────────────────────────────────┘
                      ↓
         模型直接使用记忆作答
```

## 核心能力

- **整条存储** —— 原文整条存下，不做语义切分
- **BM25 全文检索** —— 开箱即用，不需要任何外部 API
- **可选向量检索** —— 经 pgvector 做 KNN（OpenAI / DashScope / 本地 Ollama 嵌入器；模型必须在 `_MODEL_DIMENSIONS` 中注册，见「Embedding 模型与维度」）
- **时间衰减** —— 越新的记忆条目排名越高（半衰期 60 天，可配）
- **情感加权** —— 情绪强烈的记忆条目优先
- **用户反馈** —— 把记忆条目标为有用/无用，反哺排序
- **热点加权** —— 反复被讨论的话题排名更高
- **实体抽取** —— 落库时自动给记忆条目打实体标签（人名、地名、加密货币代码、领域术语），检索时与正文并排匹配，提升召回
- **实体共现** —— 自动记录哪些实体一起出现，检索时向共现实体扩展，做联想式召回（"Python" → 也能召回提到 "Django" 的记忆条目）
- **领域词典** —— 用语料 + 同义词表自动生成 jieba 用户词典，`/new` 时装载，改善中文分词
- **工作流锁** —— 往配置的后端里写一条键为 `keepsake:workflow_lock` 的记录即可全局关闭记忆检索，删掉即释放（见「工作流锁」一节）
- **跳过模式** —— 用文件定义跳过清单，避免对 "ok"、"got it" 这类琐碎查询发起检索
- **记忆摄入** —— 显式的 `memory(action='add')` 写入，加上每轮对话文本经 v1 摄入闸门（R1–R8）筛选；垃圾/系统注入内容在落库前就被拒绝，v2 管线再把对话轮次蒸馏成事实
- **检索期过期** —— 索引里有 `invalid_at` 字段：设个时间戳，该记忆条目在检索时被过滤掉（不丢数据，可回退）
- **自动维护** —— 每 2 小时跑一次选择性遗忘（多维度低价值检测），保持存储整洁。合并（consolidation）自 2026-09 起不再由 provider 接线，但能力仍在——保留为运维手动调用的引擎，见 [存储后端](#存储后端postgresql--sqlite) 与 [Consolidator 退役](#consolidator-退役2026-09-09)。
- **两种存储后端** —— `storage.backend` 选 `postgres` 或 `sqlite`；**没有默认值**（2.0.0 移除了 Redis 后端及其静默兜底）：缺失/非法一律显式报错。两种后端在写入 / BM25 检索 / 合并 / 遗忘上完全等价；只有 PostgreSQL 额外支持 KNN 向量检索，SQLite 的 `search_knn` 是显式抛 `NotImplementedError` 的桩（绝不静默返回空结果）。见 [存储后端](#存储后端postgresql--sqlite)。
- **RRF 融合排序（v1.3）** —— 倒数排名融合把 BM25 全文结果与语义 KNN 结果合成一份排序表，召回更好
- **本地语义检索** —— 可选自托管 Ollama 嵌入器（如 `bge-m3`，1024 维）经 OpenAI 兼容的 `/v1/embeddings` 端点完全本地跑，无需外部嵌入 API
- **时间感知召回（v1.5）** —— 实体时间线（`keepsake:entity_timeline`）+ 带版本的事实，让检索能利用事情「何时发生」，不只是「说了什么」
- **记忆自动蒸馏** —— `cron/memory_distill.py`（部署在 `/root/scripts/`）走 keepsake 的 LLM 通道（配置 `llm` 节，支持热重载）把近期对话蒸馏成一句话记忆条目，带水位线的增量更新；LLM 自创的保留标签由黑名单剔除（可开关）
- **检索质量抽检** —— `scripts/eval_spotcheck.py` 跑 30 条真实查询的回归测试，追踪检索质量（v1.4 纯 BM25：60% → 67%；2026-09 加向量 KNN 融合：73%）
- **自动注册的 cron 任务** —— 以 Hermes 插件方式使用时，三个 cron 任务（每 2 小时记忆维护、每小时去重、每 8 小时同义词发现）在插件初始化时自动注册，零手工配置
- **Hermes 插件外壳** —— 现成的 `hermes-plugin/` 目录，含 `plugin.yaml` 与 `__init__.py`，可直接投放安装
- **两相管线（v2，2026-09）** —— Mem0 风格的提取/更新两相在守护线程上异步运行，用 `superseded_by` 边给旧事实封边（不做物理删除），这样过时快照（例如已完成却标成待办的任务）不会复活。调用量受 `llm_pipeline.max_calls_per_window` 硬顶；任何 LLM/JSON 失败或超预算都回落 v1 规则闸门存储——消息绝不静默丢弃。
- **通用记忆接口与 MCP** —— `MemoryProvider` 是宿主无关的 `Protocol`，带三档固定失败语义（注入 fail-open / 检索抛错 / 写入 fail-closed）；`build_server(provider)` 三行就能把任意实现接成 MCP 服务器。见 [通用记忆接口与 MCP](#通用记忆接口与-mcp)
- **多代理记忆治理** —— 四道闸门把子代理挡在常驻层的写入路径之外，但对其可读：非主代理可读 `core`、写入自动降级为 `core_candidate`、控制台有审核队列、主脑侧有 `retrieval.exclude_tags` 过滤。见 [多代理记忆治理](#多代理记忆治理)
- **控制台记忆 CRUD** —— 控制台可新建、删除（需确认键的最后 8 位，删除前先备份）、JSONL/CSV 导出与试运行导入；常驻层列表支持 `?q=` 自由文本搜索
- **知识库 CRUD 与灌入器** —— 文档/分块存储与记忆库并存，手工域归控制台、`skill` 域归夜间灌入器；分块逻辑单点收在 `keepsake.kb.ingest`。见 [知识库（documents / chunks）与灌入器](#知识库documents--chunks与灌入器)
- **独立的记忆维护页** —— 合并 / 遗忘的试运行与实跑，加上常驻层候选审核队列，从看板里拆成独立控制台页
- **检索结果 LLM 筛选** —— `memory_search` / `kb_search` 带 `screen` 开关：LLM 在注入前先丢掉不相关命中，于是噪声大的查询只回真正答得上来的东西。在控制台 AI 设置页里配置
- **控制台 AI 设置页** —— 一页四节（LLM / 嵌入器 / 筛选 / 分级审核）；凭据值绝不发给浏览器，API 只报告「是否已设置」以及「值存在哪个文件」
- **按代理的注入预算** —— 每个代理都可以带自己的常驻层字符预算；不设则回落全局预算
- **面向代理的知识库** —— 四个工具（`kb_store` / `kb_search` / `kb_get` / `kb_remove`）给代理一个文档库，其归属与可见性规则与记忆库一致
- **待办看板** —— 三个工具（`todo_add` / `todo_done` / `todo_list`）加一块控制台看板，让待办与它依赖的记忆条目摆在相邻位置
- **推送唤醒与通知出口** —— 除私信 SSE 长连接外，另有 HMAC 签名的 webhook 唤醒宿主会话，以及一个 notify URL 推送到聊天频道；两者都 fail-open，出问题的出口绝不阻塞一轮对话
- **共享 PG 连接池** —— 每个连接串一个池，长驻会话不再泄漏连接
- **统一 HTTP 栈** —— 嵌入器与 LLM 通道走同一条 httpx + tenacity 路径，超时与重试行为处处一致

## 设计哲学：为 LLM 优化的干净记忆

Keepsake 存的是**完整、自包含的记忆条目**，而不是切碎的话轮。关键洞察在于：LLM 需要完整上下文才能用上被存下来的信息。像「偏好 TypeScript + Vite」这样缺了上下文条目是没用的；完整条目「用户在前端项目上偏好 TypeScript + Vite」则立刻可用。

| 机制 | 实现 |
|-----------|---------------|
| 完整上下文 | 整条存储，不做切分 |
| 遗忘曲线 | 时间衰减（半衰期 60 天）—— 旧记忆自然淡出 |
| 情绪加深记忆 | 情感权重加成 —— 强烈时刻留痕更深 |
| 重复强化 | 注意力追踪 + 热点打分 |
| 用进废退 | 反馈强化（keepsake_feedback） |
| 联想与类比 | 同义词发现（Jaccard 共现统计）—— "deploy" ↔ "release" |
| 实体关联 | 实体共现追踪 —— 提到 "BTC" 的记忆条目也能召回 "halving"，二者并非同义词 |
| 实体打标 | 像大脑给人/地/物打标一样自动抽实体，检索时与正文并排 |
| 记忆摄入 | 显式 `memory(action='add')` 写入，加上摄入闸门筛选的每轮文本与 v2 管线蒸馏 |
| 睡眠巩固 | 每 2 小时后台维护：选择性遗忘（多维度低价值检测）。合并由运维手动调用，不接在 provider 上。 |
| 上下文隔离 | agent_id 打标 —— 不同身份、彼此隔离的记忆 |
| 模糊但够用 | BM25 全文检索 —— 不必精确匹配也能召回 |

默认（纯 BM25 模式）下 Keepsake 不需要向量库、不需要嵌入 API、不需要 LLM：**纯统计方法**跑在配置的后端上——正是大脑用的那些信号：频次、时近、情绪显著度、联想与反馈。可选层是插上去的，不替换这条基线：自托管或托管的**嵌入 API** 加上经 RRF 融合的 KNN 语义召回，**LLM** 驱动 v2 写入管线。每个可选层都有明确的降级路径——缺配置绝不会悄悄启用某个兜底 provider。

## 依赖

- **Python 3.10+**
- **Hermes Agent 0.12+** —— 提供 `MemoryProvider` 接口
- **PostgreSQL 14+** —— 需要 pgvector 才能做可选的 KNN（不用 KNN 则不需要）
- *或者什么都不装* —— `sqlite` 后端是只用标准库的嵌入式单文件
- **jieba** —— 中文分词（自动安装）
- **嵌入 API**（可选）—— OpenAI / DashScope / 任何兼容的 `/v1/embeddings` 服务，含自托管的 [Ollama](https://ollama.com)（如 `bge-m3`）

## 安装

```bash
# 核心安装 —— BM25 全文检索；sqlite 后端不需要任何服务器
pip install keepsake-memory

# PostgreSQL 后端（生产 / 多代理）
pip install 'keepsake-memory[postgres]'

# 通用 MCP 服务器（keepsake.mcp_server）
pip install 'keepsake-memory[mcp]'
```

也可以直接从 GitHub 安装：

```bash
pip install 'git+https://github.com/j-zly/keepsake.git'
```

### 快速开始

四步，照抄即可。

**1. 安装**

```bash
pip install keepsake-memory                # 具备 sqlite 能力的核心包（纯 BM25，不需要服务器）
pip install 'keepsake-memory[postgres]'    # 或者用 PostgreSQL 后端
```

**2. 配置** —— 创建 `~/.config/keepsake/config.json`（存储后端及其连接细节**只能**写在这里或 `config.yaml` 里；环境变量只能覆写检索参数）：

```bash
mkdir -p ~/.config/keepsake
cat > ~/.config/keepsake/config.json <<'JSON'
{
  "storage": {
    "backend": "sqlite",
    "sqlite": {"path": "~/.keepsake/keepsake.db"}
  },
  "top_k": 5
}
JSON
```

检索参数的环境变量覆写（优先级高于 JSON 文件；自 2.0.0 起 `storage.*` **不可**用环境变量覆写）：

```bash
export KEEPSAKE_TOP_K=5
export KEEPSAKE_TAG_FILTER=work
export KEEPSAKE_AGENT_ID=my-agent
```

**3. 让 Hermes 指过来** —— 在 `~/.hermes/config.yaml` 里：

```yaml
memory:
  provider: keepsake
```

**4. 重启网关**

```bash
# CLI 模式：新开一个会话即可
# Gateway 模式：重启进程
```

没有 `embedder` 节时，provider 保持纯 BM25 模式 —— 不需要嵌入 API、不需要 LLM、不需要向量索引。

## 配置

配置优先级（高到低）：**环境变量 > JSON 配置文件 > config.yaml 内联 > 默认值**

### 1. 配置方式

配置 Keepsake 有三种方式，按优先级排列：

1. **环境变量**（最高优先级）  
   设置 `KEEPSAKE_TOP_K`、`KEEPSAKE_EMBEDDER_MODEL` 等环境变量（存储后端 / 连接细节**只认配置文件** —— 见 [存储后端](#存储后端postgresql--sqlite)）。

2. **JSON 配置文件**（~/.config/keepsake/config.json）  
   覆盖全部设置项的完整 JSON 配置文件。

3. **代码默认值**（最低优先级）  
   代码里定义的默认值。

### 2. 完整配置示例

`~/.config/keepsake/config.json` 的完整配置示例（含全部可用选项）：

```json
{
  // 存储后端 —— 必填，无默认值（2.0.0）。二者取其一：
  //   "postgres"（生产 / 多代理；BM25+KNN；pip install 'keepsake-memory[postgres]'，库需要 pgvector）
  //   "sqlite"   （单文件，零第三方依赖，纯 BM25）
  // ⚠️ 没有 embedder 时必须显式设顶层 "embed_dim"（如 1024）—— 见
  //    「存储后端」→「embed_dim」。
  "storage": {
    "backend": "postgres",
    "postgres": {"host": "127.0.0.1", "port": 5432, "dbname": "keepsake",
                 "user": "keepsake", "password": "***", "sslmode": ""}
  },
  // "storage": { "backend": "sqlite", "sqlite": {"path": "~/.keepsake/keepsake.db", "busy_timeout_ms": 5000} }

  // 检索设置
  "top_k": 5,
  "candidate_k": 10,
  "bm25_limit": 10,
  "tag_filter": "",

  // 跳过模式
  "skip_min_length": 2,
  "skip_patterns_file": "~/.config/keepsake/skip_patterns.txt",

  // 时间衰减
  "decay_half_days": 60,
  "hot_topic_decay_half_days": 30,
  
  // 排序权重
  "sentiment_boost_positive": 1.5,
  "sentiment_boost_negative": 1.3,
  "feedback_positive_boost": 1.3,
  "feedback_negative_penalty": 0.5,
  "hot_topic_boost": 1.2,
  
  // 注意力
  "attention_boost_max": 1.5,
  "attention_base_increment": 2.0,
  "attention_emotion_factor": 1.5,
  
  // 嵌入（可选）—— 模型必须在 embedder.py 的 _MODEL_DIMENSIONS 中注册；
  // 未注册模型 = 显式降级为纯 BM25（绝不静默套用默认维度）。
  // 示例：自托管 Ollama bge-m3（OpenAI 兼容端点，api_key 任意非空）
  "embedder": {
    "provider": "openai",
    "api_key": "***",
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "bge-m3"
  },

  // LLM 通道（v2 两相管线）
  // —— 不配置 = 无 LLM = 回落 v1 规则闸门，绝不静默启用付费 provider
  // base_url: chat completions 端点根（智谱等不需要带 /v1）
  // model: 如 glm-4-flash（必填 —— 缺失 = 通道无效，走 v1 回落）
  // api_key（测试用）或 key_file（生产，支持热轮换）
  "llm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash", "key_file": "/path/to/key.pass"},

  // 检索设置 —— max_content_len = 检索侧长度地板（字符数）。比它长的记忆条目按噪声处理，
  // 不进入检索面。默认 5000（旧的 600 硬上限在生产上藏掉了 103 条合法长记忆条目）。
  // 长度相关性本身由 BM25 的文档长度归一化处理。
  // ⚠️ 旧版的 LLM 查询扩展开关（retrieval.query_expansion）随 Redis 后端在 2.0.0 移除
  //    （它的缓存挂在 Redis hash 上，SQL 侧从未接线）；同义词扩展由后端的 ks_synonym 表承担。
  "retrieval": {"max_content_len": 5000},

  // 私信推送消费者（可选，默认关）—— provider 在 GET {base_url}/events 上挂一条 SSE 流，
  // 让新私信被立刻看到，而不是等到下一轮对话。
  // 🔴 凭据绝不放在这里：本节只写它的来源。
  //     enabled=false ⇒ 零线程、零连接（行为逐字不变）。
  //     ⚠️ 进程启动时读取（线程随网关一起装载）——
  //        改动需要重启网关，没有 mtime 热重载。
  // "push_consumer": {
  //   "enabled": true,
  //   "base_url": "http://127.0.0.1:8210",              // SSE 服务端根；/events 由消费者自己拼
  //   "credential_env": "KEEPSAKE_PUSH_CREDENTIAL",     // 或 "credential_file": "/path/to/secret"
  //   "notify_url": "https://example.invalid/hooks/xxx",// 通知出口（频道 webhook）；留空 = 零外呼
  //   "notify_min_interval_s": 120,                     // 同水位限频窗口（秒）
  //   "notify_format": "generic",                       // generic | feishu
  //   "wake_url": "http://127.0.0.1:8210/hooks/dm-wake",// 唤醒出口（宿主 webhook）；留空 = 零外呼
  //   "wake_secret_env": "KEEPSAKE_WAKE_SECRET"         // 或 "wake_secret_file": "/path/to/secret"
  // },

  // 自动维护
  "consolidate_min_group": 3,
  "consolidate_min_overlap": 3,
  "consolidate_max_age_hours": 72,
  "forget_max_age_days": 30,
  "forget_dry_run": false,

  // 代理隔离
  "agent_id": "main-brain",
  "is_primary": true,

  // 同义词发现
  "synonym_min_word_freq": 10,
  "synonym_jaccard_threshold": 0.5,
  "synonym_min_co_occurrence": 3,

  // 实体共现
  "entity_cooc_top_n": 3,
  "entity_cooc_min_count": 2,
  
  // 情绪强度系数
  "emotion_intensity_factor": 0.4
}
```

>
> 注意：**2.0.0 破坏性变更** —— Redis 后端已移除，`storage.backend` 没有默认值：必须明确写成 `postgres` 或 `sqlite`；缺失 / 留空 / 非法 / `redis` 会在启动时抛显式错误（绝不静默兜底）。从 Redis 装过来的？看 [从 1.x（Redis）迁移](#从-1xredis-迁移)。

### 3. 环境变量对照表

| 环境变量 | 对应配置项 | 说明 |
|----------------------|----------------------------|-------------|
| `KEEPSAKE_CONFIG` | — | `config.json` 路径（默认 `~/.config/keepsake/config.json`） |
| `KEEPSAKE_TOP_K` | `top_k` | 最终返回的记忆条目条数 |
| `KEEPSAKE_CANDIDATE_K` | `candidate_k` | 候选条数（供 KNN 用） |
| `KEEPSAKE_TAG_FILTER` | `tag_filter` | 标签过滤（逗号分隔） |
| `KEEPSAKE_AGENT_ID` | `agent_id` | 代理身份标签，用于隔离 |
| `KEEPSAKE_IS_PRIMARY` | `is_primary` | `true` = 可见全部条目；`false` = 只看带自己标签的 |
| `KEEPSAKE_EMBEDDER` | — | **已退役**：provider 只认配置文件（`embedder.provider`），这个环境变量不再能切换协议 |
| `KEEPSAKE_EMBEDDER_URL` | `embedder.base_url` | 嵌入 API 端点 |
| `KEEPSAKE_EMBEDDER_MODEL` | `embedder.model` | 嵌入模型名 |
| `OPENAI_API_KEY` | `embedder.api_key` | 嵌入 API 密钥 |
| `KEEPSAKE_PUSH_CREDENTIAL` | `push_consumer.credential_env` | 私信推送消费者的凭据（**默认的环境变量名——名字本身可配**） |
| `KEEPSAKE_WAKE_SECRET` | `push_consumer.wake_secret_env` | **唤醒出口** webhook 的订阅密钥（HMAC-SHA256 签名键；**默认的环境变量名——名字本身可配**） |
| `KEEPSAKE_NOTIFY_SECRET` | `push_consumer.notify_secret_env` | 通知出口的签名密钥（**默认的环境变量名——名字本身可配**） |
| `KEEPSAKE_INTERNAL_TOKEN` | — | 跨进程 nudge 投递的共享密钥头 `X-Internal-Token` 的取值；未配置则该端点答 503 |
| `KEEPSAKE_NUDGE_PUBLISH_ENDPOINT` | `nudge.publish_endpoint` | 控制台投递 nudge 帧的端点地址 |
| `KEEPSAKE_PG_DSN` | — | **仅供独立 API/MCP 服务进程**读的知识库 PG 连接串；provider 的存储链不读它（存储连接参数一律来自 `storage.postgres` 配置段） |
| `KEEPSAKE_PG_SCHEMA` | — | 同上，知识库所在 schema（表名固定）；provider 存储链不读它 |

> **其余配置项一律只认 `config.json`——没有对应的环境变量。** 特别是 `bm25_limit`、`decay_half_days`、`hot_topic_decay_half_days`、`embed_cache_ttl`、`storage.backend`、`embed_dim`、`consolidate_min_group`、`consolidate_min_overlap`、`consolidate_max_age_hours`、`forget_max_age_days`、`forget_dry_run` 与 `emotion_intensity_factor` 都只从配置文件读。默认值见下面的[配置参考](#配置参考)表。

> 
> 注意：改完 config.json 立即生效，无需重启（发个 `/new` 就行）。

### 4. 数据库 / Schema（首次使用）

`ensure_index()` 会自己建表并自愈 schema，两种后端都如此 ——
不需要手工 DDL。PostgreSQL 侧你只需要建库和建扩展：

```sql
CREATE EXTENSION IF NOT EXISTS vector;   -- 只有要用 KNN 才需要（可选）
```

> 向量列是 `embedding vector(N)`，配 HNSW 余弦索引；`N` 必须
> 与你的嵌入模型注册维度一致（见下面的 Embedding 模型与维度；
> `bge-m3`=1024，`nomic-embed-text`=768，`text-embedding-3-small`=1536）。换
> 模型需要清掉已有的 `embed_bin` 值，再让 `ensure_index()`
> 按新宽度重建该列（见 `embed_dim` 一节）。

### 5. Hermes 配置

在 `~/.hermes/config.yaml` 里启用：

```yaml
memory:
  provider: keepsake
```

没有配置 `embedder` 时，只会用 BM25 全文检索模式。

同样支持用环境变量覆写检索 / 嵌入参数（优先级最高；`storage` 节本身只认配置文件）：

```bash
export KEEPSAKE_TOP_K=5
export KEEPSAKE_CANDIDATE_K=20
export KEEPSAKE_EMBEDDER_MODEL=text-embedding-v2
export KEEPSAKE_EMBEDDER_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export OPENAI_API_KEY=***           # 嵌入器 API 密钥
```

### 6. 工作流锁

在自动化流程（如批处理）期间临时关掉记忆检索：

锁就是一条键为 `keepsake:workflow_lock` 的记录 —— 存在即禁用
检索，删除即释放（**SQL 后端上没有 TTL，必须显式释放**）：

```sql
-- 加锁
INSERT INTO ks_fragment (key, content, tags) VALUES ('keepsake:workflow_lock', 'lock', '');
-- 解锁
DELETE FROM ks_fragment WHERE key = 'keepsake:workflow_lock';
```

### 7. 跳过模式文件

建一个文件（一行一个模式，`#` 开头是注释）：

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

然后在 config.json 里引用它：

```json
{
  "skip_min_length": 2,
  "skip_patterns_file": "~/.config/keepsake/skip_patterns.txt"
}
```

### 8. 重启网关

```bash
# CLI 模式，重开会话即可
# Gateway 模式，需要重启进程
```

## 配置参考

| 配置项 | 环境变量 | 默认值 | 说明 |
|-------------|---------------------|---------------|-------------|
| `storage.backend` | — | **必填，无默认值** | `postgres` / `sqlite`。缺失 / 留空 / 非法 / `redis` 抛显式错误。见 [存储后端](#存储后端postgresql--sqlite) |
| `storage.postgres.host` | — | `127.0.0.1` | PostgreSQL 主机（仅当 `backend` 为 `postgres` 时读） |
| `storage.postgres.port` | — | `5432` | PostgreSQL 端口 |
| `storage.postgres.dbname` | — | `keepsake` | PostgreSQL 数据库 |
| `storage.postgres.user` | — | `""` | PostgreSQL 用户 |
| `storage.postgres.password` | — | `""` | PostgreSQL 密码 |
| `storage.postgres.sslmode` | — | `""` | PostgreSQL sslmode |
| `storage.sqlite.path` | — | `~/.keepsake/keepsake.db` | SQLite 数据库文件（仅当 `backend` 为 `sqlite` 时读） |
| `storage.sqlite.busy_timeout_ms` | — | `5000` | SQLite `busy_timeout` —— 写锁等待多久才重试 |
| `embed_dim` | — | `1536` | PG `embedding vector(N)` 列的向量宽度。**没有配 embedder 时要显式设它** —— 见 [存储后端](#存储后端postgresql--sqlite) → `embed_dim` |
| `top_k` | `KEEPSAKE_TOP_K` | `5` | 最终返回的记忆条目条数 |
| `candidate_k` | `KEEPSAKE_CANDIDATE_K` | `10` | 候选条数（供 KNN 用） |
| `tag_filter` | `KEEPSAKE_TAG_FILTER` | `""` | 标签过滤（逗号分隔） |
| `bm25_limit` | — | `10` | BM25 检索候选条数（只读配置文件） |
| `decay_half_days` | — | `60` | 时间衰减半衰期（天）（只读配置文件） |
| `embed_cache_ttl` | — | `3600` | 嵌入缓存 TTL（秒）（只读配置文件） |
| `sentiment_boost_positive` | — | `1.5` | 正向情感条目权重系数 |
| `sentiment_boost_negative` | — | `1.3` | 负向情感条目权重系数 |
| `feedback_positive_boost` | — | `1.3` | 正反馈加分权重 |
| `feedback_negative_penalty` | — | `0.5` | 负反馈扣分系数 |
| `hot_topic_boost` | — | `1.2` | 热点加权系数 |
| `embedder.provider` | — | `openai` | `openai` / `dashscope`（只读配置文件，无环境变量覆写） |
| `embedder.api_key` | `OPENAI_API_KEY` | — | 嵌入 API 密钥 |
| `embedder.base_url` | `KEEPSAKE_EMBEDDER_URL` | `https://api.openai.com/v1/embeddings` | API 端点 |
| `embedder.model` | `KEEPSAKE_EMBEDDER_MODEL` | `text-embedding-3-small` | 嵌入模型名 |
| `consolidate_min_group` | — | `3` | 触发合并所需的最少条目数 |
| `consolidate_min_overlap` | — | `3` | 两条记忆条目算同一话题所需的最少共享关键词。非法值打 warning 并回落到 `3`。见 [存储后端](#存储后端postgresql--sqlite) →「合并阈值」 |
| `consolidate_max_age_hours` | — | `72` | 合并前的最小年龄（小时）。**不从 config.json 读** —— 以 `Consolidator(max_age_hours=…)` 传入 |
| `forget_max_age_days` | — | `30` | 删除前的最大年龄（天） |
| `forget_dry_run` | — | `true` | 安全模式：`true` = 只计数，`false` = 真删 |
| `agent_id` | — | `""` | 代理身份标签，用于隔离（如 `"main-brain"`） |
| `is_primary` | — | `false` | `true` = 可见全部条目；`false` = 只看带自己标签的 |
| `hot_topic_decay_half_days` | — | `30` | 热点时间衰减半衰期（天） |
| `emotion_intensity_factor` | — | `0.4` | 情绪强度 → 权重系数 |
| `skip_min_length` | — | `2` | 触发检索的最小查询长度 |
| `skip_patterns_file` | — | `""` | 跳过模式文件路径 |
| `attention_boost_max` | — | `1.5` | 注意力权重上限 |
| `attention_base_increment` | — | `2.0` | 每次提及的基础注意力增量 |
| `attention_emotion_factor` | — | `1.5` | 情绪对注意力的放大系数 |
| `synonym_min_word_freq` | — | `10` | 同义词候选的最低词频 |
| `synonym_jaccard_threshold` | — | `0.5` | 同义词判定的 Jaccard 阈值 |
| `synonym_min_co_occurrence` | — | `3` | 同义词判定的最小共现次数 |
| `entity_cooc_top_n` | — | `3` | 扩展检索时共现实体的数量 |
| `entity_cooc_min_count` | — | `2` | 实体关联所需的最小共现次数 |
| `retrieval.exclude_tags` | — | `core_candidate` | 检索时剔除的标签（逗号分隔）。空串 = 关闸门。在 `initialize()` 里一次性解析 ⇒ **改它要重启**。见 [多代理记忆治理](#多代理记忆治理) |
| `retrieval.max_content_len` | — | `5000` | 检索侧的**长度地板**：超过这个字符数的记忆条目按噪声处理，不进入检索面（两种后端都如此）。旧的 600 硬上限在生产上藏掉了 103 条合法长记忆条目，所以现在是一个可以调高的地板。非法值打 warning 并回落到 `5000`。长度相关性本身交给 BM25 的文档长度归一化（b=0.75）—— 不额外加权 |
| `push_consumer.enabled` | — | `false` | provider 是否挂 SSE 流（私信推送消费者）。关 = 零线程、零连接。进程启动时读取 ⇒ **要重启网关才生效** |
| `push_consumer.base_url` | — | `""` | SSE 服务端根地址（消费者自己拼 `/events`） |
| `push_consumer.credential_env` | `KEEPSAKE_PUSH_CREDENTIAL` | `KEEPSAKE_PUSH_CREDENTIAL` | 存放流凭据的**环境变量名**（凭据值绝不落在 `config.json` 里）。也可用 `push_consumer.credential_file` 指向密钥文件 |
| `push_consumer.notify_url` | — | `""` | 未读私信通知 POST 到的频道 webhook（如飞书群机器人）。**留空 = 通知出口关闭** ⇒ 零外呼 |
| `push_consumer.notify_min_interval_s` | — | `120` | 通知出口的限频窗口（秒）：**同一个**未读数在一个窗口内最多播报一次；未读数上涨则立刻播报 |
| `push_consumer.notify_format` | — | `generic` | 通知出口的载荷形状：`generic`（`{"text": …}`）或 `feishu`（`{"msg_type": "text", "content": {"text": …}}`） |
| `nudge.publish_endpoint` | — | `http://127.0.0.1:8210/internal/publish` | 控制台 **POST** nudge 帧的地址（跨进程投递——流在 MCP 进程里）。环境变量覆写：`KEEPSAKE_NUDGE_PUBLISH_ENDPOINT`。见 [nudge 跨进程投递](#nudge-跨进程投递) |
| `push_consumer.wake_url` | — | `""` | **唤醒出口** POST 的宿主 webhook（Hermes 网关 webhook 订阅）⇒ 宿主起一轮代理对话自己去读信箱。**留空 = 唤醒出口关闭** ⇒ 零外呼 |
| `push_consumer.wake_secret_env` | `KEEPSAKE_WAKE_SECRET` | `KEEPSAKE_WAKE_SECRET` | 存放订阅密钥的**环境变量名**（`X-Hub-Signature-256` 的 HMAC-SHA256 键；值绝不落在 `config.json` 里）。也可用 `push_consumer.wake_secret_file` 指向密钥文件 |


> `sentiment_*`、`feedback_*`、`hot_topic_*` 等排序权重参数目前只支持 JSON 配置文件配置，不支持环境变量。设为 `1.0` 即关掉该维度的影响。

## 存储后端（PostgreSQL / SQLite）

### 切换后端

一行配置选定后端 —— `storage.backend`：

```json
"storage": {
  "backend": "postgres",
  "postgres": {"host": "127.0.0.1", "port": 5432, "dbname": "keepsake",
               "user": "keepsake", "password": "***", "sslmode": ""}
}
```

```json
"storage": {
  "backend": "sqlite",
  "sqlite": {"path": "~/.keepsake/keepsake.db", "busy_timeout_ms": 5000}
}
```

`postgres` 与 `sqlite` 在**写入 / BM25 / 合并 / 遗忘**上完全等价 —— 同一个 `StorageBase` 接口，两份实现。只有 KNN 是 PostgreSQL 独有；`sqlite` 是嵌入式那一档 —— 它能做什么、不能做什么见 [SQLite](#sqlite)。

- **没有默认后端（2.0.0），即 `storage.backend` 必填无缺省。** 缺失、留空、非字符串、无法识别或填 `redis`，一律在启动时抛显式 `ValueError` —— 猜错后端会把记忆静默写到错的地方，比启动失败更糟。
- 装上可选依赖：`pip install 'keepsake-memory[postgres]'`（拉 `psycopg[binary,pool]`）。要用 KNN，数据库还需 **pgvector** —— 列是 `embedding vector(N)`，配 HNSW 余弦索引。
- 导入 `storage_pg` 不要求 `psycopg`；导入发生在连接时，所以 sqlite 档除标准库外什么都不需要。

### `embed_dim`

顶层键，默认 `1536`。它决定 PG `embedding vector(N)` 列的宽度。

**没有配 embedder 时要显式设 `embed_dim`**（比如用 `bge-m3` 构建的库就填 `1024`）。没有 embedder 时 provider 会回落到默认 `1536`；如果实际列是 `vector(1024)`，`ensure_index()` 会抛 `_SchemaDimMismatch`，provider 初始化中止，此后每次召回都是空。没有配 embedder 时的这项显式设置就是为此准备的。配了 embedder 时以它注册的维度为准，`embed_dim` 只是兜底。

### Schema 自愈

`ensure_index()` 是幂等的，会自己修 schema —— 它从不盲目重发 DDL：

1. **先查后改** —— 它查 `information_schema` / `pg_indexes`，只对真正缺的部分发 DDL。schema 完整时一条 DDL 都不发。
2. **每进程一次** —— 以 `(目标库, 维度)` 为键的记忆化短路，重复调用直接返回。
3. **串行且有界** —— 固定 `pg_advisory_lock` 加 `SET LOCAL lock_timeout = 5000`，最多 3 次重试并指数退避。超重试次数则打错误日志并**返回 `False`** —— 显式失败，绝不静默成功。

具体说：**缺列的旧库**会用 `ALTER TABLE ... ADD COLUMN` 补回维护列；维度已知时，`content_tsv` / `embedding` 也一并补上；**空库**不再与 `CREATE TABLE` 冲突（维护列的 `ALTER` 带 `IF NOT EXISTS`，建表路径与改表路径都是幂等的）。

### 维护能力与四个后端无关原语

合并与遗忘只通过 `StorageBase` 接口上的四个原语触达存储，所以它们在所有后端上行为一致：

| 原语 | PostgreSQL | SQLite |
|-----------|------------|--------|
| `scan_fragment_keys(cursor, limit, prefix)` | 键集分页 `WHERE key > :cursor ORDER BY key LIMIT n`（绝不用大 `OFFSET`） | 键集分页 `WHERE key > ? AND key LIKE ? ORDER BY key LIMIT n` |
| `get_fragments_batch(keys)` | `WHERE key = ANY(...)` | `WHERE key IN (...)` |
| `write_fragments_batch(rows)` | upsert，重算 `content_tsv` / `embedding` | 写事务里的 `upsert_fragment`，刷新 FTS5 |
| `update_fragment_fields(key, fields)` | 部分 `UPDATE`（不碰 `content` / `content_tsv` / `embedding`） | 部分 `UPDATE` + 重建 `content_tsv` / FTS5 索引 |
| `delete_fragments_batch(keys)` | `DELETE ... WHERE key = ANY(...)` 加时间线清理 | `DELETE ... WHERE key IN (...)` 加时间线**与 FTS5**清理 |

游标是一个不透明字符串：`""` 表示从头开始，`next_cursor` 为空表示扫完了。

PG 侧的维护列 —— `level`、`consumed_by`、`consumed_at` —— 都是 `TEXT NOT NULL DEFAULT ''`。**测「未设置」要用 `= ''`，绝不用 `IS NULL`**；默认值保证已有行依然合法。

写入同样等价：同内容去重会给旧版本打上 `valid_until` / `is_archived`，新版本以 `:<epoch>` 后缀的键归档；R6 规则「重复命中绝不覆盖 `content`」在所有后端都成立。

### 合并阈值

两个顶层 `config.json` 键（只读配置文件——没有环境变量）：

| 键 | 默认值 | 含义 |
|-----|---------|---------|
| `consolidate_min_overlap` | `3` | 两条记忆条目算同一话题所需的最少共享关键词 |
| `consolidate_min_group` | `3` | 一组记忆条目真正合并前所需的最少条目数 |

`consolidate_min_overlap` 过去写死为 `2`，在一份生产样本上把 2819 条中的 1498 条（53%）合并掉了；改成 `3` 后合并比例约 13%。非法值（非整数、布尔值或 `< 1`）打 warning 并回落到默认值 —— 它不会崩。

合并与遗忘是**每轮由运维脚本 / cron 现构造**的，不挂在 provider 上 —— 所以改这几个键下一轮即生效，**不需要重启**。provider 为什么不再持有 consolidator，见 [Consolidator 退役](#consolidator-退役2026-09-09)。

### 从 1.x（Redis）迁移

Redis 后端已在 **2.0.0（2026-11）移除**：现在受支持的后端面就是两种，
而凭据账本（多凭据 / 作用域）在 Redis 上本来也从未实现。

你的数据仍可迁移：最后一个带 Redis 后端的版本（1.1.x）里附带的
`scripts/migrate_redis_to_pg.py` 对 Redis **严格只读**
（只用 `SCAN` / `HGETALL` / `PING`；有一道 AST 自检拒绝任何写命令），
幂等（`ON CONFLICT (key) DO UPDATE`），直接搬运已有的 `embed_bin` 向量
而不是重算，并把辅助结构（热点、注意力、实体时间线、共现、同义词）
一并带走且带数量核对。

**从 1.1.x 的检出目录运行**（脚本需要旧的 RedisStorage）：

```bash
git clone https://github.com/j-zly/keepsake.git && cd keepsake && git checkout v1.1.1
python3 scripts/migrate_redis_to_pg.py --dry-run     # 只读，打印迁移计划
python3 scripts/migrate_redis_to_pg.py --limit 100   # 先试 100 条
python3 scripts/migrate_redis_to_pg.py               # 全量迁移
```

然后升级到 2.0.0，并把 `storage.backend` 指到 `postgres`。

### 已知后端差异

语料维护在两种后端上实现一致；剩下的差异都是响亮地失败，而不是静默失败：

| 方法 | PostgreSQL | SQLite | 原因 |
|--------|------------|--------|-----|
| `discover_synonyms` | ✅ 支持 | ✅ 支持 | 语料维护，与检索正交 —— 两种后端共用 `storage_shared` 里同一套判定链 |
| `generate_jieba_dict` | ✅ 支持 | ✅ 支持 | 同上。给定同一份语料，两者产出**逐字节一致**的词表与词典 |
| `search_knn` | ✅ 向量检索 | 抛 `NotImplementedError` | 需要 `sqlite-vec` 扩展（可选懒加载，未实现）。故意大声失败 —— **绝不静默返回空结果**；`search` 会降级为纯 BM25 并打 WARNING |
| `touch_fragment` / `set_supersedes` | ✅ 写入（`ks_fragment` 列） | ✅ 写入 | 两个 SQL 后端都保留这些列；单向元数据按设计没有读取方 —— 反向封边用 `supersede_fragment` |

### 切换后端后的自检清单

把 `storage.backend` 指向 PostgreSQL 后按顺序跑这些。最后两步是试运行，其余都是非破坏性的。

```bash
export PYTHONPATH=src
export KEEPSAKE_CONFIG="$HOME/.config/keepsake/config.json"   # 你的路径不同就改这里
```

**1. `ensure_index()` → True**（建/修 schema，打印 `schema ready on …`）：

```bash
python3 - <<'PY'
import os
from keepsake.storage import resolve_backend, storage_from_config
from keepsake import _load_json_config
cfg = _load_json_config()
print("backend =", resolve_backend(cfg))
st = storage_from_config(config=cfg, agent_id=cfg.get("agent_id", ""), is_primary=cfg.get("is_primary", False))
print("ensure_index =", st.ensure_index())
PY
```

**2. 健康检查**（在 PG 上 `SELECT 1`）：

```bash
python3 - <<'PY'
from keepsake import _load_json_config
from keepsake.storage import storage_from_config
cfg = _load_json_config()
st = storage_from_config(config=cfg, agent_id=cfg.get("agent_id", ""), is_primary=cfg.get("is_primary", False))
print("health_check =", st.health_check())
PY
```

**3. 搜一次** —— 期望有分数非零的命中，而不是空列表：

```bash
python3 - <<'PY'
from keepsake import _load_json_config
from keepsake.storage import storage_from_config
cfg = _load_json_config()
st = storage_from_config(config=cfg, agent_id=cfg.get("agent_id", ""), is_primary=cfg.get("is_primary", False))
for hit in st.search("a phrase you know is in memory"):
    print(hit.get("_key"), hit.get("_sim"), (hit.get("content") or "")[:60])
PY
```

**4. 合并试运行**（零写入 —— 只扫描、聚类、报告分组数）：

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

**5. 遗忘试运行**（零写入 —— 只数候选，不删任何东西）：

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

两个试运行都会报 `dry_run: True` 与 `deleted: 0` / `would_merge` 计数。它们都不碰任何行 —— 比较前后条目数即可验证。

> 凭据从 `config.json`（或 `KEEPSAKE_CONFIG`）读；上面的命令都不在命令行上带口令，也都不会打印口令。

### SQLite

第三种后端 —— 面向**单机 / 单代理**那一档。按规模选：

| 规模 | 后端 |
|-------|---------|
| 生产 / 多代理 / KNN | `postgres` |
| 单机、嵌入式、单代理 | `sqlite` |

用户红线依然成立 —— **`storage.backend` 只能选一个后端**；没有混用或并行模式。

（原来的第三行 `redis` 已随该后端本身在 2.0.0 一起移除。）

#### 实现

**纯标准库 `sqlite3`，零第三方依赖。** 切换方式和另外两种一样，就是一行配置：

```json
"storage": {
  "backend": "sqlite",
  "sqlite": {"path": "~/.keepsake/keepsake.db", "busy_timeout_ms": 5000}
}
```

没有默认后端 —— 要明确选一个。导入 `storage_sqlite` 除标准库外什么都不需要。

#### 能力

分三批交付：

- **p1 —— 读写原语、schema 自愈、配置接线**（`e8b0398`）；外加写入路径守卫（`3099609`）：schema 未就绪时拒绝写入（`StorageNotReadyError`，不静默空转），以及覆盖整个事务体的进程内写锁（`0fd1b54`）。
- **p2 —— 检索：FTS5 + jieba 分词 + BM25 + 共用重排器**（`8078c6f`）；随后是契约对齐 —— 结果条数与排序权重与 PG 读同一份配置（`c88070d`）。
- **p3 —— 合并、遗忘、同义词、jieba 词典**（`38d5816`）。同一份语料上，三种后端产出**逐字节一致**的词表与词典（`6a31b17`）。

标签过滤由所有后端共用一个 `tag_match_sql` 辅助函数（`2a9aabd`）—— 数据库里逗号分隔标签，对 `|` 补边标签做子串匹配会静默漏掉所有带第二个标签的行。

#### 边界 —— 选它之前先读这段

**向量检索 `search_knn` 是一个显式抛 `NotImplementedError` 的桩。** `sqlite-vec` 是可选懒加载且未实现。这个约定是刻意的：它**绝不静默返回空结果** —— 静默空意味着「记忆找不到而且没人说为什么」，这是本项目最糟的失败形态。`search` 会捕获该错误、打 WARNING 并降级为纯 BM25，所以降级在日志里永远看得见。

写入**确实**会持久化嵌入向量（`store()` 持久化 float32 小端 blob，与旧 Redis 后端同一套磁盘布局），所以切到 SQLite 不会让历史记忆条目变成没有向量的孤儿 —— 缺的只是向量**检索**。

#### 并发

`PRAGMA journal_mode=WAL` ⇒ **多读一写**；写事务进行中读取照常可用。并发写排在 `busy_timeout`（默认 5000 ms）加写重试（`WRITE_RETRY_ATTEMPTS = 6`，总预算约 34 秒）后面。写事务一律以 `BEGIN IMMEDIATE` 开启 —— 在第一条语句就拿写锁，而不是等到提交时再升级，那正是 `database is locked` 死锁的经典来源。同一进程内所有语句串行在一把 `RLock` 上，因为 `check_same_thread=False` 的连接本身并不线程安全。

#### 中文文本

分词发生在**插入前、Python 侧用 `jieba` 完成**：FTS5 匹配的是空格分隔的词串，绝不匹配原始文本。

> `trigram` 分词器是**刻意不用**的 —— 它有 3 字符下限，于是「证书续签」里「证书」这种 2 字查询词什么都匹配不到。两字词是中文查询的主体，所以在 trigram 下**任何两字查询都返回 0 命中**。jieba 预分词 + 默认分词器才是受支持的路径。

#### 为何不用 DuckDB

DuckDB 评估过并被**否决**：它会拿排他文件锁，所以 `read_only` 连接在别处有写事务进行时也读不了 —— 这正好抵消了选嵌入式后端的意义。

### Embedding 模型与维度

嵌入模型必须在 **embedder.py 的 `_MODEL_DIMENSIONS` 中注册** —— 未注册的模型会被带清晰日志行地拒绝，并降级为纯 BM25，绝不猜一个默认维度（这能防住维度不匹配导致的索引静默损坏）。

| 模型 | 维度 |
|-----|------------|
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

> ⚠️ 换模型要走一遍维度变更流程：清掉已有的 `embed_bin` 值，再让 `ensure_index()` 按新宽度重建 `embedding` 列 / HNSW 索引（见「数据库 / Schema」与 `embed_dim`）。旧向量与新维度不兼容，否则检索会静默失败。

### 同义词表

同义词表存在后端的同义词表里（PostgreSQL / SQLite 上都是 `ks_synonym`），检索时展开以提升召回：

```bash
INSERT INTO ks_synonym(term, synonyms) VALUES ('setup', '["install","configure","deploy","setup"]');
INSERT INTO ks_synonym(term, synonyms) VALUES ('fix', '["fix","modify","correct","repair","solve"]');
```

### 写入闸门 v1（2026-09）

写侧闸门（`src/keepsake/ingest_gate.py`）在每次 `sync_turn()` 落库前拦截。规则按序短路：**R1** 系统注入前缀（`[CONTEXT COMPACTION`、`[System note`）→ 拒绝；**R2** `len > max_len`（默认 2000）→ 拒绝；**R3** 黑名单 / 状态询问 / 去空白后长度 < 8 → 拒绝；**R4** 完全不含字母/汉字/数字 → 拒绝；**R5** `category="memory_tool"` → 拒绝（MEMORY.md 与记忆库解耦）；**R6** 同内容哈希已存在（`turn_memory`/`conversation`）→ 只走 `update_state`（更新 `updated_at`/`touch_count`，绝不覆盖 `content`）；**R7** 其他 → 落库（每次 `storage.store()` 调用点都做密钥擦除 —— 见下面的 R7）。在 `config.json` 的 `"ingest_gate": {"enabled": true, "max_len": 2000}` 里开关。默认开启。Hermes 新增一种系统注入消息文本时，必须把它的前缀加进 `R1 _REJECT_PREFIXES`。

#### R7 —— 密钥擦除（2026-09，写侧源头闸门）

在任何 `storage.store()` 之前，`scrub_secrets(text) -> (cleaned_text, n_masks)` 会遮蔽明显的凭据，让它们永远进不了记忆库。纯函数、幂等（调第二次返回 `n_masks=0`）。所有落库点都接上了它：v1 路径的 `sync_turn`（`__init__.py` 里的 `_v1_store_after_decide`）、v2 管线的 `_do_add` / `_do_update` / `_fallback_to_v1` 原始落库（`pipeline.py`）。R6 的 `update_state` 路径**刻意不做**擦除（它只更新时间戳；原 `content` 保持不变）。

| 类别 | 模式 | 示例 → 遮蔽后 |
|---|---|---|
| 1 | `(密码\|口令\|password\|passwd) [是为:：=] value` | `密码是FAKEpw9` → `密码是***[REDACTED]` |
| 2 | `(api[_-]?key\|token\|secret\|密钥) [是为:：=] value` | `token: FAKEtok+enABCDEF0123456789xyzXYZaaaa==` → `token: ***[REDACTED]` |
| 3 | `Password=value`（大小写不敏感；value 在 `; " ' &` 处截止） | `Password=FAKEpw1;` → `Password=***[REDACTED];` |
| 4 | `Bearer value`（value 为 ≥16 位 base64 字符） | `Bearer abc123def456ghi789jkl0123456789` → `Bearer ***[REDACTED]` |
| 5 | 兜底：连续 12+ 位 `[A-Za-z0-9+=_.-]` 加保守过滤（拒绝纯数字点号串，如 IPv4 / URL 主机名 / 文件路径） | `sk-FAKE0123456789ab` → `***[REDACTED]` |

遮蔽格式：保留匹配到的前缀（标签 + 分隔符），把值换成 `***[REDACTED]`。类别 3（`Password=xxx`）先跑，以保住 `;` 等 DSN 终止符。

对高熵兜底（类别 5）的保守过滤避免了这些误报：

- **IPv4 地址** —— `203.0.113.7` → 不遮蔽
- **URL 主机名 / 路径** —— `https://api.example.com/v1/users`、`/var/log/keepsake/server.log` → 不遮蔽
- **纯英文** —— `thequickbrownfoxjumpsoverthelazydog` → 不遮蔽
- **中文** —— `我的密码本很厚`（无分隔符）→ 不遮蔽
- **常见 `key=value` 对** —— DSN 串里的 `Server=localhost`、`Database=mydb` → 不遮蔽

## 知识库（documents / chunks）与灌入器

记忆库旁边还有一个文档知识库（`kb_documents` + `kb_chunks`），从控制台的知识库页检索。它与 `ks_fragment` 是**不同的存储** —— 同一个 PG 库，不同的表。（表名 `ks_fragment` 是代码内部的历史命名，内部变量名沿用 `fragment`；本文档正文一律称「记忆条目」。）

**按 `source_type` 划分归属域** —— 谁可以写什么：

| `source_type` | 归属方 | 控制台行为 |
|---------------|-------|-------------------|
| `manual` | 控制台 | 可自由新建 / 编辑 / 删除。`source_key` 由内容哈希派生，所以重复导入同样内容是幂等的，不会产生重复。 |
| `skill` / `skill_hk` | 夜间灌入器 | 任何写入都返回 **HTTP 409** —— 编辑会被下一轮灌入覆盖，删除会在技能重新出现时被撤销。UI 把这两类渲染为只读（显示徽标而不是写入按钮）。 |

**分块逻辑单点收口。** 「文档怎么切块、怎么幂等落库」只存在一份，就在 `keepsake.kb.ingest`：`split_chunks`、`upsert_document`、`content_hash`、`replace_chunks`、`update_document`。控制台写入端点与灌入器 CLI 都 import 它，并有测试断言二者是**同一个函数对象** —— 否则控制台写的文档会被灌入器按不同粒度重切，检索命中就会与页面上看到的对不上。改正文会在**同一个事务内**重新分块（先删旧块再插新块），所以文档绝不会带着与正文矛盾的块。`MANUAL_SOURCE_TYPE` 从同一个包导出，所有手工域判定都读这个常量而不是重复写死字面量。

**灌入器** —— `scripts/kb_ingest.py` 读源技能文档库并写入 `documents` / `chunks`。它的行为与它所吸收的版本逐字段等价（在生产库上用两个版本 `--dry` 验证过）。凭据只来自环境变量或口令文件路径 —— 源码里没有明文。先试运行：

```bash
python3 scripts/kb_ingest.py --dry     # 只计数，末尾回滚
python3 scripts/kb_ingest.py           # 实跑
```

它还会清理死文档：`skill` / `skill_hk` 中 `(source_type, meta.node, meta.skill)` 已不在存活集合里的条目。`manual` 绝不被动 —— 那个域归控制台。生产 PG 节点的 cron 已指向这个脚本。

**控制台端点**：文档列表（分页、自由文本与 `source_type` 过滤，且响应里始终带 `source_type`，好让 UI 知道哪些条目可写）、新建 / 更新（重新分块）/ 删除（确认项是文档的完整标题；删除前先写快照与备份）、JSONL 导入（试运行预览，实跑要求输入 `would_add` 条数）与 JSONL 导出。

### 知识库对代理开放（kb_store / kb_search / kb_get / kb_remove）

知识库过去只对控制台开放；现在通过 MCP 对**每个代理**开放，判准与记忆**完全一致**（`keepsake.govern.visibility` —— 一份实现，不是两份）。三条规则：

| 规则 | 行为 |
|------|-----------|
| **归属** | 每篇文档都记录存入它的代理（`documents.agent_id`，取自凭据的调用方 —— 换发凭据也不改变它）。控制台创建的文档保持 `NULL`（= 无主），已有语料不受影响。 |
| **可见性** | 非主代理只能看到**自己的**文档 + 开了 `share_memory` 的代理的文档 + **无主的历史文档**（现存 1844 篇全是无主的）。主代理什么都看得到。 |
| **删除** | 非主代理只能删**自己的**文档（或无主的历史文档）；动别人的 ⇒ 拒绝且**内容一个字都不回显** —— 而且「不可见」与「不是你的」回答完全一致，所以连存在性都不泄露。删除需要 `confirm=true`。主代理可以删任何东西。 |

这四个工具原样复用既有的灌入管线（分块 / 幂等 / 审核状态链）：子代理提交的内容进控制台审核队列（`pending`，批准前不切块），主代理提交的直接落为 `effective`。控制台知识库页给每篇文档显示**来源代理**徽标，并支持按代理过滤（`?agents=a,b`，服务端过滤，在分页之前）。`agent_id` 是新列 —— 已有库的迁移语句是 `ALTER TABLE documents ADD COLUMN IF NOT EXISTS agent_id text NULL;`（在应用之前每篇文档都读作无主，`kb_store` 会带一条人类可读的错误拒绝写入）。

## 待办（todo）—— 一等公民（2026-10）

待办过去是用 `kind:pending` 的记忆条目假装的 —— 12 条里 9 条是过期的账目（做完了没销、逾期了没人知道）。待办需要一套**状态机**和独立的页面，所以有了 `ks_todo` 表。

| 规则 | 行为 |
|------|-----------|
| **三个状态** | `open` / `done` / `obsolete` —— 「做完了」和「这条作废」是两回事；DDL 带 CHECK 约束。 |
| **两个归属列** | `owner`（谁欠着）+ `creator`（谁记下的）。可见 = **owner 是我 ∪ creator 是我**（协作账本：执行人总能看到自己那份）；主代理全看得到。 |
| **一个截止日期裁判** | `due_state`（`none` / `soon` / `overdue`）在**服务端**算（基准日 = UTC 今天）：3 天内到期 = 琥珀色，过了今天 = 红色。前端只做翻译 —— 它自己从不计算日期。 |
| **两种后端一套语义** | PG / SQLite 都是行表（两套 DDL 方言只存在于 `govern/todo.py` 一处）。 |

三个代理侧工具（工具面 15 → 18，全部在凭据闸门之后）：`todo_add(title, detail?, owner?, due?)`（owner 默认是调用方；你也可以派给别人，但那个代理必须**已注册且已批准**）、`todo_list(scope?)`（默认 `open` / `done` / `all`）、`todo_done(id, status?)`（`done` / `obsolete` / `open`；只有 owner、creator 或主代理能改）。

控制台页面「待办」（侧栏，「审核」的前一项）：按状态分组的卡片、owner 徽标、琥珀/红色到期徽标、SelectionBar 批量关闭、新建表单（owner 下拉 = 已注册代理），以及一个真的删除 —— 需要**键入标题**。看板新增待办区（未完 N / 逾期 M，点进去就是该页）；维护页列出「建议作废」提名（**只点名，不改状态** —— 逾期待办是否还有人认领，是人的决定）。

迁移旧条目（脚本**默认试运行**；`--write` 才真写）：

```bash
python3 scripts/migrate_pending_to_todo.py            # 试运行
python3 scripts/migrate_pending_to_todo.py --write    # 实跑
```

映射：title = 正文前 60 字，detail = **原文**（一点不丢），owner = 条目的 `agent:<id>` 标签（已注册且已批准时）否则主代理，status 恒为 `open`；原条目被打上 `migrated-to-todo` 标签并**保留**（删掉就抹掉了「我们当初为什么记这条」）。幂等键就是那个标签，所以第二次跑会报 `migrated=0`。

## 通用记忆接口与 MCP

`keepsake.adapters.generic.protocol.MemoryProvider` 是一个可运行时检查的 `typing.Protocol`，描述一套记忆系统要向宿主暴露什么。该模块不 import 任何宿主相关的东西 —— 它描述形状，不描述调用方 —— 依赖方向严格是 `adapters.hermes → adapters.generic`。

失败语义按档钉死，所以新后端不能自己发明一套：

| 档 | 方法 | 后端失败时 |
|------|---------|--------------------|
| 注入 | `system_prompt_block()`、`prefetch(query)` | **fail-open** —— 吞掉错误，降级成静态文本 / 空字符串，最多一条 `logger.warning`。它们在每一轮对话的关键路径上；这里抛异常会让整轮对话失败，而记忆是增强，不是依赖。 |
| 检索 | `search(query, limit)`、`get(key)` | **抛错。** 静默的 `[]` 是最危险的失败形态：记忆静默找不到、哪儿都没有告警，用户只会得出「AI 忘了」的结论。 |
| 写入 | `add(text, tags)`、`update(key, …)`、`remove(target)` | **fail-closed** —— 明确报失败（假值 / 抛错 / `status=error`），绝不假装成功。吞掉写失败会让调用方继续告诉用户「已保存」，那是撒谎。 |

外加维护档 —— `merge(dry_run=True)` 与 `forget(dry_run=True)`，两者默认试运行 —— 以及用于能力声明的 `capabilities()`。注意 `get(key)` 返回 `None` 意为「确实不存在」，而不是「没查成」。

### 任意宿主三行接入

```python
from keepsake.mcp_server.server import build_server
server = build_server(MyMemoryProvider())   # 实现上面那个协议
server.run("stdio")                         # 任何 MCP 客户端连上就完事
```

`build_server(provider)` 注册 8 个工具 —— `memory_capabilities`、`memory_search`、`memory_get`、`memory_core_list`、`memory_add`、`memory_remove`、`memory_merge`、`memory_forget` —— 以及资源 `memory://core`（常驻层文本，逐字返回，与每轮注入的渲染完全一致）和 `memory://fragment/{key}`。不带参数调用它会装配 keepsake 自己的 provider（`factory.default_provider()`，按 `KEEPSAKE_CONFIG` / `~/.config/keepsake/config.json` 连接）。MCP 写入工具与 provider 共用写入闸门，所以下面的治理规则对 MCP 调用方同样成立。

**归属戳记（2026-10 ks_owner_stamp）。** 通过 MCP 写入的记忆条目属于**凭据推导出的调用方**（`agent:<它的 id>`），**而不是** MCP 进程自身的身份 —— 否则代理检索不到自己刚写的东西（可见性收紧之后），而且它写的一切都落在主脑的库里。它自己在 `tags` 里写的 `agent:xxx` 会被**覆盖**成真实身份（不允许自报归属）；进程内写入（无凭据）仍回落到进程默认值。能力声明里的 `write_owner` 字段就是在讲这件事。

**冲突协议（2026-10 ks_cas_hard）。** `memory_update` 与 `memory_remove` **都要求 `expected_rev`** —— 也就是你正要改的那一行的版本。三步：① 用 `memory_get(key)` 读当前版本（或用 `memory_search` 命中里带的 `rev`）；② 提交更新 / 删除时带上 `expected_rev=<那个 rev>`；③ 收到 `error="conflict"` 时载荷里带 `current_content` / `current_rev`（别人刚写进去的版本）—— **读它、合并、重新提交；绝不强行写入**。

**类型容忍（2026-11）。** `expected_rev` 与 `kb_*` 的文档 `id` 在 MCP schema 上同时接受 `int` 与 `str`（`anyOf`）—— 把 `"2"` 强转成 `2` 的客户端是我们真踩到过的失败形态。容忍**仅限类型**：空/空白 rev 照样拒，错 rev 照样冲突。`kb_*` 的 id 经 `strip() → int()` 归一，垃圾输入报清晰的 `kb_doc_id_invalid` 错误。

为什么强制：记忆条目是核心资产，所以 MCP 面是 **fail-closed** 的 —— 没有「没版本就跳过检查」的逃生口。当多个进程共用一个凭据并写同一个池时，这个版本检查是**唯一**防住彼此静默覆盖的闸门（服务端行锁 + 版本比较是兜底，不是替代）。存储 / provider 层内部保留着自己的空串兼容路径（批处理、合并 cron、控制台直接调 provider），但那条路径**不对 MCP 调用方开放**。

**复活语义（2026-10 ks_remove_sem；rev 部分由 2026-11 ks_tombstone 修订）。** 键**就是内容哈希** ⇒ 重新 `add` 同样的正文在条目还存在时是**空操作**，而在条目已删除后是**对原键的复活**。所以要确认「这条真的没了吗？」只有一个办法：**对那个确切的键做 `memory_get`** —— **绝不**用重新 `add` 同样的正文来验证存在性：成功的 `add` 对存在性什么都没说明（两种情况下都是空操作），而且它会静默复活该条目。

🔴 **`rev` 不再回到 1**（2026-11 ks_tombstone）：复活的条目**从墓碑接续**（以版本 1 删除 ⇒ 复活为版本 2；再删再复活 ⇒ 版本 3，依此类推）—— 版本历史**不再断裂**。没有墓碑（从未删除过）时 `rev` 仍从 1 开始。

**删除留痕（2026-11 ks_tombstone）。** 删除**不再是查无对证的物理删除**：删掉一条会在**同一个事务里**往 `ks_tombstone` 留一行（`key` / owner / **删除时的 `rev`** / 删除时间）。因此 `memory_get` 有两档：

- 行还在 ⇒ 条目照常返回；
- 行没了但**有墓碑** ⇒ 它按本项目惯用措辞回答：*"条目 {key} 已于 {deletion time} 删除（删除时 rev={rev}）。同正文重新 memory_add 会复活原 key（rev 从 {rev+1} 接续）。"* —— 与**「从未存在」**明确区分（后者仍回答 *"记忆条目不存在：{key}"*）。

🔴 **不可见归属 ⇒ 仍回答「不存在」**（墓碑 owner ≠ 调用方且调用方不是主代理时）：**别的代理删了什么一点都不泄露** —— 连存在性本身都受保护。

🔴 **fail-closed**：墓碑写入与删除共用**一个事务** —— 墓碑写失败则整个删除回滚 ⇒ **宁可删不掉，也不删得没痕迹**（两条删除路径 `cas_remove` 与 `delete_fragments_batch` 行为一致）。能力声明里的 `tombstone` 字段就是这个开关：**两种后端都是 true**（旧 Redis 后端是 false，它从不留痕 —— 那一档自 2.0.0 起已消失）。

**四档错误语义（2026-11 ks_err_tiers）。** 写入路径（`memory_update` / `memory_remove`）现在有**四档互不相同的失败**，**不再统统塌缩成「找不到」**：

| 档 | 什么时候 | 你该怎么做 |
|------|------|-------------|
| **conflict** | 链头**还在**，但你提交的版本**不是当前版本**（rev 不匹配） | 从载荷里读 `current_content` / `current_rev`，合并后**重新提交**（绝不强行写入） |
| **not_found** | 条目**从未存在过** | 检查键是不是拼错了 |
| **orphan** | 条目是某条演进链的**历史版本**，而**链头已被删除** ⇒ 链断了 | 该行**还在**（`memory_get` 仍返回它）但它**既不可改写也不可删除** ⇒ 用 `memory_search` 定位那条**活的**条目 |
| **not_owner** | 条目存在，但**调用方不是它的 owner** | 去找主代理（或该条目的 owner） |

⚠️ **orphan 不是 not_found**：链身那一行**还在库里**（`memory_get` 仍返回它）—— 只是它指向的链头没了，演进链已断，改写/删除只会继续分叉一条死链。所以它有**自己的一档**：报「不存在」是**误导**的 —— 客户端会以为这条从未存在，转而去别处重建（实测：历史版本被静默丢成孤儿）。

**超时重试（2026-10 ks_idem）。** 一次 MCP 调用超时 ≠ 服务端没提交。**首选：用同一个 `idempotency_key` 重试**（任意稳定唯一串，如 UUID）—— 服务端按 `(归属代理, 幂等键)` 去重；命中意味着**什么都没被再次写入**，并把首次提交的结果带 `duplicated=true` 返回（未命中就照常写入、登记该键、返回 `duplicated=false`）。这样重试的正文**绝不会在同一话题上落第二条**，即使它略有差异。没有键（存量调用方）时，仍可用 `memory_get` / 一次有辨识度的 `memory_search` 检查是否已落库，此时重试的正文必须**逐字节相同**（同内容哈希 ⇒ 空操作）。

⚠️ **两条限制**（别把它当互斥锁）：① 幂等表**只在成功提交之后**才写 ⇒ 它**不**拦并发同键请求（两个并发同键请求可能都写入）—— 它治的是**串行**重试（「客户端没收到响应，于是又发同一个键」）；② 登记**7 天 TTL** 后过期，之后同一个键被当作新键（会再写一条）。作用域是**归属代理**（A 的键绝不会命中 B 的条目）。（`idempotent_write` 在当前两种后端上都受支持；旧 Redis 档明确拒绝该键，2.0.0 起已消失。）

**传输。** 默认 `stdio`。`http`（streamable-http）只监听 `127.0.0.1` —— 绑非回环地址会以退出码 2 结束而不是启动。记忆库自己没有鉴权，把它暴露到网络上等于发布一个人的长期记忆；请在前面放反向代理。CLI：`python -m keepsake.mcp_server [--transport stdio|http] [--host …] [--port …]`。

## 私信实时推送（SSE 长连接）

2026-10 ks_dm_sse。私信新鲜度有**三层**，每一层都回落到下一层：

| 层 | 何时触发 | 送来什么 | 成本 |
|-------|-----------|------------------|------|
| **SSE 长连接** | `message_send` 提交后服务端立刻推一帧 | `seq` / `from` / `unread`（**只有元数据 —— 绝不含正文**） | 客户端常驻一条连接 |
| **重连时的追帧** | 流刚建立的那一刻 | 当前未读水位（有未读就一帧） | 无 |
| **`unread_dm` 水位** | 每轮对话（提醒行）/ 随时可查 | 未读数 | 只是轮询，延迟一轮 |

SSE 端点在 **MCP 的 http 传输上，同进程同端口**（默认 `127.0.0.1:8210`）：`GET /events`。

### 建流要过三道闸

`Authorization: Bearer <ksk_credential>` → 注册表**按凭据查代理** → `status=approved` **且** `push_enabled=1`（控制台：*Agent Registry → Edit → Private-message push*）。任何一关不过 ⇒ **401 / 403，不建流**。

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
        for line in resp.iter_lines():      # event: / data: 逐行
            print(line)
```

### 事件格式（两类）

帧形状相同：一行 `event:`、一行 `data:`、然后一个空行。

```
event: dm
data: {"seq": 12, "from": "worker-1", "unread": 3}

event: dm-nudge
data: {"seq": 12, "from": "worker-1", "unread": 3}
```

* `dm` —— 有人给你发了私信（提交后推送）；
* `dm-nudge` —— 有人在控制台按了 *Nudge*（Message Center → 按收件人）。

🔴 **帧里绝不带消息正文**：正文从信箱本身取（`message_inbox`）—— 推正文等于把私信抄进日志和代理记忆。

### 边界与保活

* **心跳**：每 25s 一个 `: ping` 注释帧（客户端忽略它）；
* **并发**：每个代理最多 **2 条流**，**新的踢掉旧的**（客户端自己会重连）；
* **挂起上限**：默认 30 分钟，之后**服务端关闭流**（客户端自己会重连）；
* **吊销一个心跳内生效（≤25s）**：每次心跳都重新检查该流的准入 —— 凭据被吊销或 `push_enabled` 被关掉，已开的流在 25s 内结束；
* **总线是进程内**的（`dict[agent_id → list[asyncio.Queue]]`）⇒ 控制台与 MCP 服务器是两个进程 ⇒ 控制台的 *nudge* 走下面的**内部端点**投递（帧由真正持有流的那个进程推；控制台从不本地推）。
  `ponytail:` 跨进程意味着把这一层换成外部消息总线。

### 手动 / 批量触发提醒

控制台 → *Message Center → 按收件人*：

* 每个收件人一行，带当前未读数和一个 *Nudge* 按钮（点前在弹框里确认）；
* 勾选多个 ⇒ 工具栏显示 *Nudge the N selected*（全选 / 清空沿用常驻层与记忆条目页的同一套约定）；
* 按代理反馈：*✓ 已送达 / ⚠ 离线*（离线 = 没人在听 —— 该 nudge **不会**被补投，而且回复绝不撒谎）。

后端端点：`POST /api/v1/messages/nudge`（载荷 `{agents: ["id1", ...]}`，1–50，去重）与 `POST /api/v1/agents/{agent_id}/push`（那个开关）。

### nudge 跨进程投递

SSE 流住在 **MCP http 进程**（8210）；控制台是**另一个进程**（8200），它自己的内存总线上**一个订阅者都没有**。控制台的 *nudge* 过去发在控制台自己的总线上 ⇒ **永远是** `delivered=false`（诚实，但功能是死的）。

缺的那半米现在补上了 —— 控制台把帧 **POST** 给真正持有流的那个进程：

```
控制台 (8200) ──POST /internal/publish──▶ mcp-http (8210) ──▶ 总线 ──▶ SSE 流
```

* **端点**：`POST /internal/publish`，在**与 `/events` 同进程同端口**的 MCP http 传输上（默认 `127.0.0.1:8210`）。载荷 `{agent_id, kind, unread, seq?, from?}` ⇒ 回复 `{"delivered": bool}` —— 这是总线的**真实**答案（有人在听吗？）。帧仍然**只有元数据**（`seq` / `from` / `unread`），绝不含正文。
* **两道闸**：① **只准回环**（`request.client.host ∈ {127.0.0.1, ::1}`；反向代理不转发这条路径 ⇒ 互联网不可达）② **共享密钥头** `X-Internal-Token`，值来自 env `KEEPSAKE_INTERNAL_TOKEN`。
* 🔴 **没配置 ⇒ 该端点答 503**（fail-closed）：nudge 就是送不到，控制台如实这么说。
* **控制台侧**：端点地址 = 配置项 `nudge.publish_endpoint`（env 覆写 `KEEPSAKE_NUDGE_PUBLISH_ENDPOINT`），默认 `http://127.0.0.1:8210/internal/publish`；共享密钥同样来自 `KEEPSAKE_INTERNAL_TOKEN` 这个 env。
* **诚实的回复**：`delivered=false` 且 `reason=no_stream`（没人在听）或 `reason=publish_endpoint_unavailable`（端点不可用 / 503 / 缺密钥）⇒ 页面显示 **⚠ endpoint unavailable** —— 提醒**没有**送达；寄生层（每轮对话的提醒行）与 `unread_dm` 水位照常工作。

**部署**：两个进程需要*同一个*密钥，否则 MCP 侧会一直答 503、每个 nudge 都报「端点不可用」。给两个 unit 各加一行（然后 `systemctl daemon-reload` 并重启两者）：

```ini
# /etc/systemd/system/keepsake-mcp-http.service
# /etc/systemd/system/keepsake-console.service
Environment=KEEPSAKE_INTERNAL_TOKEN=<一段足够长的随机串>
```

### provider 侧挂流消费者（补齐最后一米）

2026-10 ks_push_consumer。服务端本来就在；这是**客户端** —— keepsake 自己的 provider 挂一条 SSE 流并监听，于是私信被**立刻**看到，而不是等到下一轮对话。

**默认关闭。** `push_consumer.enabled=false` ⇒ **零线程、零连接**，行为与之前逐字一致。

```json
"push_consumer": {
  "enabled": true,
  "base_url": "http://127.0.0.1:8210",
  "credential_env": "KEEPSAKE_PUSH_CREDENTIAL",
  "notify_url": "https://example.invalid/hooks/xxxx",
  "notify_min_interval_s": 120,
  "notify_format": "generic",
  "wake_url": "http://127.0.0.1:8210/hooks/dm-wake",
  "wake_secret_env": "KEEPSAKE_WAKE_SECRET"
}
```

* 🔴 **凭据绝不落在 `config.json` 里** —— 本节只写它的*来源*（`credential_env`，或指向密钥文件的 `credential_file`）。值通过环境变量进。
* ⚠️ **进程启动时读取**：线程随网关一起装载，所以改这一节需要重启网关（没有 mtime 热重载 —— 这里写明白，不让人误以为改完就通）；
* `base_url` 是**服务端根地址**；消费者自己拼 `/events`。

收到一帧会做什么 —— 且只做这些：

1. **未读水位缓存失效** ⇒ 下一次 `prefetch` 立刻重查信箱并马上提醒，而不是干等 60 秒去重窗口；
2. **一行 `logger.info`**，带事件摘要（`event` / `seq` / `from` / `unread`）—— **绝不含正文**；
3. **宿主的可选回调** `on_push_event(fn)` 收到同一份摘要字典（`{"event", "seq", "from", "unread"}`）—— **绝不含正文**（正文留在信箱里，用 `message_inbox` 取）。

```python
provider.on_push_event(lambda summary: print(summary["unread"]))
```

回调跑在**消费者线程**上 ⇒ 保持简短，并自己吞掉异常（实现里已经捕获：回调抛异常只会打一条 warning）。

#### 通知出口（空闲期把人叫醒）

上面那套帧处理只是让水位失效 —— 提醒仍要人开口。代理**闲着**的时候，提醒永远不会冒出来。所以收到 `dm` / `dm-nudge` 帧时，会*额外* POST 到你配置的频道 webhook（`push_consumer.notify_url`，如飞书群机器人）：

* 🔴 **只有元数据** —— 载荷带未读数与发送方（`📩 你有 2 条未读私信（来自: worker-1）`），**绝不含消息正文**（正文留在信箱里，用 `message_inbox` 取）；
* ⚠️ **按水位限频** —— 同一个未读数在 `notify_min_interval_s`（默认 120s）内最多播报一次；**上涨**的未读数立即播报，用户不必干等窗口；
* `notify_url` 留空 ⇒ **零外呼**（行为与之前逐字一致）；失败的 POST 只打 warning —— 不重试、不碰水位、不碰主路径。

#### 唤醒出口（代理自己去读信）

通知出口叫醒**用户**；唤醒出口叫醒**宿主会话**。收到 `dm` / `dm-nudge` 帧时，会*额外* POST 到宿主的 webhook 订阅（`push_consumer.wake_url` —— Hermes 网关，本地 `127.0.0.1`）⇒ 网关起一轮代理对话去读 `message_inbox` 并处理消息 —— 不用人转述。🔴 **通知不走 LLM，唤醒才走** —— 两条旁路并存，各司其职，各可独立开关。

* 🔴 **只有元数据** —— 载荷是扁平的，恰好三个键（`from` / `unread` / `seq`），因为订阅的提示词模板要读 `{from}` / `{unread}` / `{seq}`；**绝不含消息正文**；
* 🔴 **带签名** —— `X-Hub-Signature-256: sha256=<hex>` = HMAC-SHA256(原始请求体字节, 订阅密钥)。密钥来自 `wake_secret_env` / `wake_secret_file`（**值绝不落在 `config.json` 里**；无密钥 ⇒ 不唤醒）；
* ⚠️ **边沿触发** —— 只有**新的**消息 `seq` 才唤醒宿主；重连时的追帧摘要（同样的状态再报一次）**不会**再次唤醒 ⇒ 断连/重连风暴不会反复唤醒宿主。`dm-nudge` 帧每帧都唤醒（nudge 的意思就是「现在去看」）；
* `wake_url` 留空 ⇒ **零外呼**；失败的 POST 只打 warning —— 不重试、不碰水位、不碰通知、不碰主路径。

健壮性：连接失败或流断掉 ⇒ **指数退避**（5s 起，翻倍，封顶 5 分钟）后重连。消费者是 **fail-open** 的：它做的任何事都破坏不了 provider —— 连启动失败也不行（`initialize()` 绝不会因它失败），而且线程是守护线程，随进程一起死。

每进程一条流：服务端每个代理最多允许 2 条流（新的踢旧的），而 Hermes 可能每个会话建一个 provider —— 所以消费者与回调表住在**模块级**，不是实例级。

## 多代理记忆治理

四道闸门，全部在生产中生效：

| 闸门 | 行为 | 配置键 | 生效层 |
|------|-----------|------------|-----------------|
| **A —— 非主代理可读 `core`** | 子代理能召回常驻层而不只是自己的私有条目；但仍写不进去 | `is_primary` | 存储过滤 —— 所有后端的 BM25 路径；PG 与 SQLite 共用一个 `_search_filter_sql` 出口，BM25 与 KNN 都覆盖 |
| **B —— 写入 `core` 自动降级** | 非主代理写的 `core` 标签会被改写成 `core_candidate`（其他标签保留）并给出警告 | — | `provider._apply_write_gate`，作用于 `provider.add()`；MCP 的 `memory_add` 路径继承它 |
| **C —— 控制台候选审核队列** | 控制台的记忆维护页分页列出 `core_candidate` 条目。批准 = 先提为 `core` 再摘掉候选标签（顺序不能反，反了会把候选直接弄丢）；驳回 = 只摘候选标签，条目仍留在检索层 | — | 控制台 |
| **D —— 主脑噪音过滤** | `retrieval.exclude_tags` 在检索出口剔除带这些标签的条目。默认把 `core_candidate` 挡在主脑自己的召回之外 —— 一条待审申请不能兼当事实；空串则关掉该闸门 | `retrieval.exclude_tags` | 统一的 `search` / `prefetch` 出口，在**截断之前**生效 |

闸门 D 在 `initialize()` 里一次性解析，所以**改它需要重启**；过滤器本身每次查询都在那个冻结集合上跑。

**排除作用域是主脑的上下文，不是整个检索面**（2026-10-31 ks_exclscope）：按代理的 `exclude_from_primary` 开关（控制台的「进入主脑上下文」开关）只在检索的**主脑侧**剔除条目 —— 它**绝不会**阻止该代理检索自己的条目（子代理调用方照样召回自己的；其可见性仍由共享/归属规则决定，与这个开关无关）。

因此 `core_candidate` 的含义是「一条进入常驻层的待审申请」，而不是 LLM 可以自创的标签。保留标签的判定只有一份实现 —— `core/shared.py` 里的 `sanitize_reserved_tags()` —— 蒸馏 cron（剔除保留标签并记日志）与控制台新建/导入路径（`core` 返回 400）共用它。保留集合：精确标签 `core`、`core_candidate`、`shared`，外加任何以 `agent:` 为前缀的标签。匹配在逗号边界上做精确匹配，所以 `core_x` 不算保留标签。

## 验证

启动后检查日志：

```
Memory provider 'keepsake' registered (0 tools)
keepsake: connected (session=xxx, top_k=5, tag_filter=(none))
keepsake: embedder enabled (openai, dim=1024)      # KNN 生效
# 或：keepsake: BM25-only mode (no embedder configured)
keepsake: auto-registered cron job 'memory-maintenance'
keepsake: auto-registered cron job 'synonym-discovery-daily'
keepsake: auto-registered cron job '记忆去重'
```

## 语义检索升级（2026-09）

可选的语义层围绕三条原则重做了：

1. **嵌入侧没有静默默认值。** 模型维度必须在 `_MODEL_DIMENSIONS` 里注册；未知模型现在*可见地*失败（嵌入器标记为未注册 → `storage._embed_enabled=False` → 纯 BM25，并给出可操作的日志行）。维度与在用嵌入器不一致的活跃索引会停用向量写入，而不是继续堆 `hash_indexing_failures`。换模型 = 清掉 `embed_bin` + `embed_cache:*`、`FT.DROPINDEX`（不要 DD），用新维度重建，再回填已有条目（`scripts/backfill_embeddings.py`；注意 `--limit` 默认 500 —— 全量回填要调大）。自托管 Ollama 经 OpenAI 兼容端点开箱可用（如 `bge-m3`，1024 维）。
2. **LLM 通道只认配置。** `config.json` 的 `llm` 节是 v2 写入管线的唯一真相源 —— 硬编码的 provider 兜底已移除（缺配置 = 功能关闭，绝不突然来一次付费调用）。改动在下一个管线排空窗口靠 mtime 检测热生效。
3. **写侧数据卫生。** R1 闸门在污染召回之前拒掉系统注入文本（压缩摘要、网关重启通知）；热点管线丢掉 jieba 分出的 ASCII 词元（CJK-only 闸门挂在 jieba 分支上）与英文虚词，同时*刻意保留*技术词（`api`、`ssh`、`log`…）；`scripts/cleanup_hot_topics.py` 清扫既有污染（默认试运行）。

在 30 条查询的回归套件（`scripts/eval_spotcheck.py`）上实测：纯 BM25 67% → BM25 + KNN（bge-m3，RRF 融合）73%。

## 项目结构

```
keepsake/
├── src/keepsake/         # Python 包 —— 记忆 provider
│   ├── domain/           # 类型与契约（Fragment / Hit / Capabilities）
│   ├── core/             # 存储后端与共用判定链
│   ├── adapters/
│   │   ├── generic/      # 宿主无关的 MemoryProvider 契约
│   │   └── hermes/       # Hermes 插件 provider
│   ├── kb/               # 知识库写入共用层（分块 / 幂等 upsert）
│   ├── mcp_server/       # 通用 MCP 入口 —— build_server(provider)
│   └── api/              # 控制台后端（路由、鉴权、审计）
├── web/                  # 控制台前端（Vue 3 + Vite）
├── hermes-plugin/        # Hermes 插件外壳（可直接投放到 ~/.hermes/plugins/）
│   ├── plugin.yaml
│   └── __init__.py
├── cron/                 # 定时任务的 cron 包装脚本
│   ├── memory-maintenance.py   # 每 2h —— 选择性遗忘（合并由运维手动调用）
│   ├── memory_distill.py       # 把近期对话蒸馏成一句话记忆条目（剔除保留标签）
│   ├── dedup-memory.sh         # 每 1h —— 去重
│   └── discover-synonyms.py    # 每 8h —— 同义词自动发现
├── scripts/              # 独立工具脚本（开发/测试），含知识库灌入器 kb_ingest.py
├── README.md
└── pyproject.toml
```

`cron/` 里的三个 cron 任务在 keepsake 插件初始化时**自动注册**（在 `/new` 或网关重启时）。不需要手工 `hermes cron create`。

## v2 两相管线（2026-09）

每轮对话走两相 LLM：**提取相**从窗口提炼长期事实（废话/纯确认自然无事实可提而消亡），**更新相**对每条 fact 拿 top-5 相似旧记忆做 ADD / UPDATE / DELETE / NOOP —— UPDATE/DELETE 给旧记忆条目只打 `superseded_by` 封边不物理删，检索侧统一排除；窗口内 LLM 调用硬顶 `llm_pipeline.max_calls_per_window`（默认 8），任一 LLM/JSON 失败或超预算 → 窗口整体回落 v1 规则闸门直存原文，**绝不静默丢消息**。配置键：`llm_pipeline.{enabled, model, window_pairs, window_seconds, max_calls_per_window, update_top_k, recent_context_size}`、`v2_min_score`。

## LLM 通道（2026-09 ks_noqwen 重写）

**唯一配置源：`config.json` 的 `llm` 节**。无 `llm` 节 / 缺字段 = 该通道无效 = v2 管线不启动 + 调用方按既有 v1 规则闸门直存原文，**绝不悄悄用付费模型**。

### 配置字段（全部必填，缺一即通道无效）

| 字段 | 必填 | 说明 |
|------|------|------|
| `base_url` | 是 | chat completions 端点根 URL，rstrip `/`。智谱路径不需 `/v1` |
| `model` | 是 | 模型名（如 `glm-4-flash`）—— 缺该字段 = 通道无效 |
| `api_key` | 二选一 | 直填 key（仅测试用） |
| `key_file` | 二选一 | 文件路径，生产推荐；支持运行时轮换（hot-rotation） |

`api_key` 与 `key_file` 同时配置时，`api_key` 直填优先。

### 无效通道的兜底行为

| 场景 | 行为 |
|------|------|
| 无 `llm` 节 | pipeline 不启动；sync_turn 走 v1 decide() 直存 |
| 缺 `base_url` 或 `model` | 同上 |
| `key_file` 不存在 / 不可读 | api_key="" 但 base_url/model 保留；valid=False |
| `config.json` 中途损坏 | 本窗按 unconfigured；不抛穿 daemon |
| 仅 `OPENAI_API_KEY` env 有值 | 旧调用方兜底（生产禁用；仅 env-only 测试场景） |

### 热生效（hot reload）

`resolve_llm_channel_cached` 按 `(mtime_ns, size)` 缓存解析结果：

- v2 管线每次 `_drain_now` 开头调一次 → **改完 `config.json` 下一处理窗口生效，无需重启网关**
- `key_file` 内容轮换同理窗口级生效
- 中途坏 JSON → 本窗 unconfigured，下窗文件被改回自动恢复

### 完整配置示例

```json
{
  "llm": {
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-4-flash",
    "key_file": "/etc/keepsake/zhipu.pass"
  }
}
```

> 不再支持「零配置=硬编码付费模型兜底」（2026-09 移除）。要跑 v2 管线就必须显式给 `llm` 节；缺它就回到纯 v1 规则闸门路径。

## Consolidator 退役（2026-09-09）

**退役的只是 provider 的运行期接线，不是这个能力。** `Consolidator`（基于 jieba 关键词聚类 + LLM 多级缝合的离线合并引擎）的类源码完整保留在 `src/keepsake/consolidator.py`，在 **PG / SQLite 两个后端上都能跑**（它只通过[四个后端无关原语](#维护能力与四个后端无关原语)访问存储）。provider 的 `initialize()` 不再构造它、`maintenance()` 不再触发合并循环。

**为什么摘掉接线：**
- 输出质量差：LLM 多级合并产生的「无时态散文」与 v2 封边链（`superseded_by`）互不相认 —— 旧合并产物会被 v2 检索侧的封边过滤误伤或绕过。
- 职责重叠：v2 两相管线的「提取相」窗口级事实提炼 +「更新相」supersede 封边，已全面覆盖 Consolidator 的记忆条目提纯职能。
- 维护成本：双管线并存需要为同一份记忆库维护两套并查链路，得不偿失。

**替代者：** `pipeline.py`（v2 两相管线）。每个 turn 窗口触发提取相 → 更新相，原地 supersede 旧记忆条目；运行时无独立合并 cron 任务。

**怎么用合并：** 运维脚本 / cron **每轮现构造** —— `Consolidator(storage=storage_from_config(...), config=cfg).consolidate(dry_run=False)`。因为是每轮现读 config，改 `consolidate_min_overlap` / `consolidate_min_group` **下一轮即生效，无需重启**。阈值说明见 [存储后端](#存储后端postgresql--sqlite) →「合并阈值」，dry-run 用法见[自检清单](#切换后端后的自检清单)。

**运行时观测：** `maintenance()` 返回 stats 仍保留 `consolidator` 字段（`{"status": "retired", "reason": "v2 pipeline takeover"}`），便于 cron 探针 / 健康检查观测退役状态而非误报 missing-key。

## 架构

```
┌────────────────────────────────────────────────────────┐
│                    用户发消息                           │
└──────────────────┬────────────────────────────────────┘
                   │
         ┌─────────▼─────────┐
         │   prefetch()       │  ← 每条用户消息自动触发
         │   ↓                │
         │  工作流锁？          │  ← 查 keepsake:workflow_lock
         │   ↓                │
         │  跳过模式？          │  ← 按长度 / 精确匹配跳过清单
         │   ↓                │
         │  BM25 全文检索       │  ← 默认，零成本，检索整条记忆
         │  （KNN 向量检索）    │  ← 可选（需要嵌入器）
         │  实体共现            │  ← 用共现实体扩展查询
         │   ↓                │
         │  六维重排            │  ← 相似度 × 时间衰减
         │                    │    × 情感 × 反馈 × 热点 × 注意力
         │   ↓                │
         │  Top N 注入上下文    │  ← 整条记忆条目原样返回
         └─────────┬─────────┘
                   │
         ┌─────────▼─────────┐
         │   模型回答          │  ← 直接使用记忆条目（完整文本）
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   on_memory_write()│  ← 只在 memory(action='add') 时
         │   存完整文本        │  ← 整条存储，不做切分
         │   实体抽取          │  ← jieba + 正则 → 实体写进 TAG 字段
         │   实体共现           │  ← 记录实体对到共现表
         │   注意力追踪         │  ← 抽关键词，提升注意力分
         │   ↓                │
         │  存在 PG/SQLite     │  ← 供下一轮检索，完整文本
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   [cron] 每 2h      │  ← 后台维护
         │  ① 选择性遗忘       │  ← 年龄>30d + 无反馈 + 低情感 + 低注意力 → 删除
         │  （合并由运维手动调用 —— 见上文一节）
         └───────────────────┘
```

## 冻结评分时钟

`storage_shared.py` 里的 `scoring_now()` / `scoring_now_ts()` / `frozen_scoring_now()` 给评分路径一个统一读当前时刻的地方。**默认返回真实挂钟 —— 行为与这些函数存在之前逐字节一致，即零行为变更。**

在 `with frozen_scoring_now():` 块里时钟被钉住；退出时恢复真实挂钟，嵌套时继承外层的钉值而不是悄悄跳回真实时间：

```python
from keepsake.storage_shared import frozen_scoring_now

with frozen_scoring_now():
    ...   # 这个块里所有重排与热点衰减都用同一瞬间
```

**只有评分侧读它** —— `rerank_with_decay` 里的时间衰减与 `match_hot_topics` 里的热词衰减（SQLite / PG 都是）。🔴 **写入路径不读**：`store()` 写的 `created` 与时间线时间戳从不读它 —— 冻结写入时间会损坏数据。

**它为什么存在：** 这些衰减是在查询时求值的，所以同一行在连续两次查询里分数不同。两条 `bm25`/`sim` 完全相同、只差一个约 1e-7 衰减项的行**在 1.5 秒内就会交叉**（实测：第 0 轮领先 5.2e-8，第 1 轮被 1.3e-8 反超），这让「同一查询、跑 3 次、键序完全一致」间歇性失败。`scripts/recall_eval.py` 把整轮运行包在一个冻结里，于是给定顺序的给定问题能精确复现（`33727f2`）。

## core 标签 —— 常驻记忆层

Keepsake 用 **`core` 标签**承载常驻记忆，让记忆存储成为记忆的唯一真相源。在 Hermes 侧，`memory.builtin_mode=pointer` 意味着 `MEMORY.md` / `USER.md` 只留一个指针 —— 它们不再读、不再写、不再注入。写入走 `on_memory_write()`，把内容**逐字**存进 `core`（`3a1e43f`）。

## 许可证

MIT

## 2.0.0（2026-11）—— 破坏性变更：移除 Redis 后端

Redis 存储后端**已消失**，连同它那套静默的 `storage.backend` 兜底。存储后端现在恰好两种 —— `postgres`（生产，KNN 走 pgvector）与 `sqlite`（嵌入式，只用标准库）—— 实现为**一套 SQL 语义、两种方言**；不再有第三种键值范式需要同步维护。

实际影响：

- `storage.backend` **必填**。缺失 / 留空 / 非法 / `redis` 现在在启动时抛显式 `ValueError`，不再悄悄兜底。
- 多凭据账本（`ks_credential`）、按凭据作用域，以及 Redis 只能降级处理的 `share_kb` / 幂等 / 墓碑这几档，现在是受支持后端的无条件能力。
- `redis_host` / `redis_port` / `redis_password` 配置键、Redis 相关环境变量覆写、`redis` 硬依赖，以及仅存在于 Redis 侧的 LLM 查询扩展（SQL 侧从未接线）均已移除；控制台 AI 设置页收敛为四节。
- `python -m keepsake.jobs dedup` 及其 Redis 专用辅助脚本已移除 —— SQL 侧的去重能力在维护引擎里。
- 迁移已有 Redis 记忆库：在 **v1.1.x 的检出目录**里跑 `scripts/migrate_redis_to_pg.py`（见[从 1.x（Redis）迁移](#从-1xredis-迁移)），然后升级并把 `storage.backend` 指到 `postgres`。

## 2.2.0（2026-11）—— 记忆班车改为进程内调度（APScheduler + PG jobstore）

维护班车（蒸馏 / 维护 / 分级 / 同义词发现）不再依赖外部 agent 宿主的调度器。`keepsake.core.bus` 在控制台服务进程内跑一个 APScheduler 3.x `BackgroundScheduler`，配 **PostgreSQL jobstore**（同一个库、同一个配置节 —— 不新增要管理的连接）；运行结果落在 `ks_bus_state`，控制台的定时任务面板直接读写这个调度器。依赖：`apscheduler>=3.11,<4` 与 `SQLAlchemy>=2.0`（4.x 是刻意跳过的：它最后一个发布版是 2025-04 的 alpha，这条线此后一直休眠）。

运维要点：
- 班车定义写在代码里（`BUS_JOBS`），这是刻意的 —— 增删一辆班车是一次需要评审的改动，不是一个运行时按钮。面板可以暂停 / 恢复 / 改点。
- 班车以**函数**形式注册（`"keepsake.buses.<name>:run"`，在控制台进程内调用 —— 没有 shell 包装、没有要钉的解释器）。旧的 argv 列表形态已**退役**（2026-11-18 的迭代决议）：API 以显式 400 拒绝并点名这次退役 —— 什么都不被静默丢弃 —— 而且调度器拒绝注册或运行这样的 spec。
- 调度器随控制台服务一起生死：把它放在监管进程下（`Restart=always`）。错过的 tick 会在 1 小时的宽限窗口内合并成一次运行。
- 公开 API 面没有其他改动；旧的宿主 cron 桥接与 provider 侧的自动注册一并退役。
