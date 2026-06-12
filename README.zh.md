# Fragmented Memory Plugin for Hermes Agent

碎片化记忆系统 — 每次对话自动检索相关记忆碎片注入上下文。

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
         模型直接利用碎片回答
```

## 核心能力

| 特性 | 说明 |
|------|------|
| ✂️ **语义切分** | 对话内容按段落/句子边界自动拆分为独立碎片，保护缩写/数字/省略号 |
| 🔍 **BM25 全文搜索** | RediSearch 全文检索，零成本，同义词扩展 |
| 🧠 **KNN 向量搜索** | 可选 Embedding（OpenAI / DashScope），动态维度适配 |
| ⏳ **时间衰减** | 碎片按时间降权，旧记忆权重逐步降低（60天半衰期） |
| 🔄 **自动写入** | `memory()` 操作和对话轮次自动存档，无需手动管理 |
| 📖 **完整记忆注入** | 每个碎片回溯其完整原文，在上下文行内展示 `(完整记忆: ...)` |
| 🔗 **联想回忆** | 碎片完整原文再搜一轮，自动追加更多关联碎片 |
| 🏷️ **实体提取** | 自动提取人名/地名/项目名/术语存为 entities TAG 字段，搜索时 content 和 entities 双路召回提高命中率 |
| 🔗 **实体共现** | 自动统计实体共现对，搜索时扩展召回关联实体（搜"BTC"同时带出"缠论"相关碎片） |
| 📖 **领域词典** | 从碎片语料+同义词表自动生成 jieba 自定义词典，发 `/new` 时自动加载，分词更准 |
| 🔒 **工作流锁** | 设置 `fragmented:workflow_lock` 全局禁用碎片检索，用于自动化流程 |
| 🚫 **跳过模式** | 配置文件定义跳过词表，简单确认语（好的/嗯/ok）不触发检索 |
| 🏷️ **标签过滤** | 可选按标签范围搜索 |
| 👍 **反馈加权** | 标记有用/没用的碎片会影响排序 |
| 🔥 **热门话题** | 自动统计跨会话高频话题 |
| 📖 **同义词表** | 存 Redis Hash，实时加载展开搜索，无需部署 |
| 😡 **情绪烈度** | 检测用户表达激烈程度（反复问号/感叹号/程度副词），烈度高的碎片权重更高 |
| 👁️ **注意力追踪** | 用户反复提起的话题自动标记为高关注，相关碎片在搜索中排名上升 |
| 🧬 **多级合并** | 每 2 小时自动将同主题碎片合并提炼为高层记忆，支持 level 1→2→3 多级蒸馏 |
| 🗑️ **选择性遗忘** | 自动清理低价值（旧 + 无反馈 + 低情绪）碎片，保持库精简 |

## 设计哲学：类脑记忆

碎片记忆走了另一条路。它模仿的是**人脑真实的记忆机制**：

| 人脑特征 | 实现方式 |
|----------|---------|
| 遗忘曲线 | 时间衰减（60天半衰期）—— 旧记忆自然淡去 |
| 情绪加深记忆 | 情感权重加权 —— 情绪强烈的经历记得更牢 |
| 反复提及则强化 | 注意力追踪 + 热词统计 |
| 被纠正后不再记错 | 纠正检测 —— 用户说「不对」，错误记忆自动降权 |
| 用进废退 | 反馈正强化（frag_memory_feedback） |
| 触类旁通、联想回忆 | 同义词自动发现（Jaccard 共现统计）—— "部署" ↔ "上线" |
| 实体关联 | 实体共现追踪 —— "BTC"和"减半"无语义重叠但因共现被关联召回 |
| 碎片化存储 | 对话按段落切分成原子碎片，不存完整 transcript |
| 碎片溯源 | 每个碎片指向原始完整文本 —— "碎片A让我想起完整对话B" |
| 联想回忆 | 搜碎片 → 回溯完整原文 → 再搜关联碎片（去重追加） |
| 实体索引 | 就像人脑给记忆打标签 —— 自动提取实体名，搜索时双路召回提高命中率 |
| 睡眠时整理记忆 | 每天凌晨 consolidation + 同义发现（03:00 cron）|
| 不同场景记忆隔离 | agent_id 标签体系 —— 分身各自记忆不交叉 |
| 模糊但够用 | BM25 全文搜索 —— 不需要精确匹配就能回想起来 |

没有向量数据库。没有 Embedding API 调用。没有 LLM 参与记忆操作。**纯统计方法**跑在 Redis + RediSearch 上——频率、时效、情绪烈度、关联性、反馈——跟人脑用的是一套东西。

## 依赖

- **Python 3.10+**
- **Hermes Agent 0.12+** — 提供 `MemoryProvider` 接口
- **Redis 7+** — 带 RediSearch 模块（v2.6+）
- **jieba** — 中文分词（自动安装）
- **Embedding API**（可选） — OpenAI / DashScope / 任意兼容 `/v1/embeddings` 的服务

## 安装

```bash
pip install fragmented-memory
```

或者从 GitHub 直装：

```bash
pip install git+https://github.com/j-zly/fragmented-memory.git
```

## 配置

配置优先级（高→低）：**环境变量 > JSON 配置文件 > config.yaml 内联 > 默认值**

### 1. 配置方式

配置碎片化记忆有三种方式，按优先级从高到低排列：

1. **环境变量**（优先级最高）  
   设置如 `FRAGMENTED_REDIS_HOST`、`FRAGMENTED_REDIS_PASSWORD` 等环境变量。

2. **JSON 配置文件**（~/.config/fragmented-memory/config.json）  
   完整的 JSON 配置文件，用于所有设置。

3. **代码默认值**（优先级最低）  
   在代码中定义的默认值。

### 2. 完整配置示例

以下是配置文件 `~/.config/fragmented-memory/config.json` 的完整示例，包含所有可用选项：

```json
{
  // Redis 连接配置
  "redis_host": "127.0.0.1",
  "redis_port": 6379,
  "redis_password": "",
  
  // 搜索相关配置
  "top_k": 5,
  "candidate_k": 10,
  "bm25_limit": 10,
  "tag_filter": "",
  
  // 跳过检索配置
  "skip_min_length": 2,
  "skip_patterns_file": "~/.config/fragmented-memory/skip_patterns.txt",

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
  
  // 嵌入配置（可选）
  "embedder": {
    "provider": "dashscope",
    "api_key": "sk-xxx",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "text-embedding-v2"
  },
  
  // 自动维护配置
  "consolidate_min_group": 2,
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

### 3. 环境变量对照表

| 环境变量 | 对应配置项 | 说明 |
|----------|------------|------|
| `FRAGMENTED_REDIS_HOST` | `redis_host` | Redis 服务器地址 |
| `FRAGMENTED_REDIS_PORT` | `redis_port` | Redis 服务器端口 |
| `FRAGMENTED_REDIS_PASSWORD` | `redis_password` | Redis 认证密码 |
| `FRAGMENTED_TOP_K` | `top_k` | 最终返回碎片数 |
| `FRAGMENTED_CANDIDATE_K` | `candidate_k` | 候选碎片数（用于 KNN） |
| `FRAGMENTED_BM25_LIMIT` | `bm25_limit` | BM25 搜索候选数 |
| `FRAGMENTED_TAG_FILTER` | `tag_filter` | 标签过滤（逗号分隔） |
| `FRAGMENTED_DECAY_HALF_DAYS` | `decay_half_days` | 时间衰减半衰期（天） |
| `FRAGMENTED_HOT_TOPIC_DECAY_HALF_DAYS` | `hot_topic_decay_half_days` | 热门话题时间衰减半衰期（天） |
| `FRAGMENTED_EMBED_CACHE_TTL` | `embed_cache_ttl` | Embedding 缓存时间（秒） |
| `FRAGMENTED_EMBEDDER` | `embedder.provider` | 嵌入模型提供商（`openai`、`dashscope`） |
| `FRAGMENTED_EMBEDDER_URL` | `embedder.base_url` | 嵌入 API 端点 |
| `FRAGMENTED_EMBEDDER_MODEL` | `embedder.model` | 嵌入模型名称 |
| `FRAGMENTED_CONSOLIDATE_MIN_GROUP` | `consolidate_min_group` | 触发合并的最小碎片数 |
| `FRAGMENTED_CONSOLIDATE_MAX_AGE_HOURS` | `consolidate_max_age_hours` | 碎片参与合并的最小年龄（小时） |
| `FRAGMENTED_FORGET_MAX_AGE_DAYS` | `forget_max_age_days` | 碎片保留天数后可能被遗忘 |
| `FRAGMENTED_FORGET_DRY_RUN` | `forget_dry_run` | 遗忘安全模式：仅统计不删除 |
| `FRAGMENTED_EMOTION_INTENSITY_FACTOR` | `emotion_intensity_factor` | 情绪烈度→权重系数（0=禁用，1=最大） |

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
    fragment_type TAG SEPARATOR "," \
    invalid_at TAG SEPARATOR "," \
    entities TAG SEPARATOR "," \
    embed_bin VECTOR FLAT 6 TYPE FLOAT32 DIM 1536 DISTANCE_METRIC COSINE
```

> 维度（DIM）根据实际使用的 Embedding 模型动态调整，默认 1536。
> 如果用 Docker：`docker run -d --name redis-stack -p 6379:6379 redis/redis-stack:latest`

### 5. Hermes 配置

在 `~/.hermes/config.yaml` 中开启：

```yaml
memory:
  provider: fragmented
```

如果不配置 `embedder`，则只走 BM25 全文搜索模式。

也支持通过环境变量配置（优先级最高）：

```bash
export FRAGMENTED_REDIS_HOST=127.0.0.1
export FRAGMENTED_REDIS_PORT=6379
export FRAGMENTED_TOP_K=5
export FRAGMENTED_EMBEDDER=dashscope
export FRAGMENTED_EMBEDDER_MODEL=text-embedding-v2
export OPENAI_API_KEY=***        # embedder API key
```

### 6. 工作流锁

在自动化流程（如批量任务）时需要临时禁用碎片检索：

```bash
# 加锁（3600s TTL）
redis-cli SET fragmented:workflow_lock 1 EX 3600

# 解锁
redis-cli DEL fragmented:workflow_lock
```

### 7. 跳过模式文件

创建文本文件，每行一个跳过词，`#` 注释：

```text
# ~/.config/fragmented-memory/skip_patterns.txt
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
  "skip_patterns_file": "~/.config/fragmented-memory/skip_patterns.txt"
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
| `redis_host` | `FRAGMENTED_REDIS_HOST` | `127.0.0.1` | Redis 地址 |
| `redis_port` | `FRAGMENTED_REDIS_PORT` | `6379` | Redis 端口 |
| `top_k` | `FRAGMENTED_TOP_K` | `5` | 最终返回碎片数 |
| `candidate_k` | `FRAGMENTED_CANDIDATE_K` | `10` | 候选碎片数（KNN 用） |
| `tag_filter` | `FRAGMENTED_TAG_FILTER` | `""` | 标签过滤（逗号分隔） |
| `bm25_limit` | `FRAGMENTED_BM25_LIMIT` | `10` | BM25 搜索候选数 |
| `decay_half_days` | `FRAGMENTED_DECAY_HALF_DAYS` | `60` | 时间衰减半衰期（天） |
| `embed_cache_ttl` | `FRAGMENTED_EMBED_CACHE_TTL` | `3600` | Embedding 缓存时间（秒） |
| `sentiment_boost_positive` | — | `1.5` | 正面碎片权重乘数 |
| `sentiment_boost_negative` | — | `1.3` | 负面碎片权重乘数 |
| `feedback_positive_boost` | — | `1.3` | 正反馈加分权重 |
| `feedback_negative_penalty` | — | `0.5` | 负反馈降权系数 |
| `hot_topic_boost` | — | `1.2` | 热门话题加权乘数 |
| `embedder.provider` | `FRAGMENTED_EMBEDDER` | `openai` | `openai` / `dashscope` |
| `embedder.api_key` | `OPENAI_API_KEY` | — | Embedding API 密钥 |
| `embedder.base_url` | `FRAGMENTED_EMBEDDER_URL` | `https://api.openai.com/v1` | API 端点 |
| `embedder.model` | `FRAGMENTED_EMBEDDER_MODEL` | `text-embedding-3-small` | 嵌入模型名 |
| `consolidate_min_group` | — | `2` | 合并触发最少碎片数 |
| `consolidate_max_age_hours` | — | `72` | 碎片最少年龄（小时）后才参与合并 |
| `forget_max_age_days` | — | `30` | 碎片保留天数后可能被遗忘 |
| `forget_dry_run` | — | `true` | 遗忘安全模式：仅统计不删除 |
| `hot_topic_decay_half_days` | — | `30` | 热门话题时间衰减半衰期（天） |
| `emotion_intensity_factor` | — | `0.4` | 情绪烈度→权重系数（0=不启用，1=max） |
| `skip_min_length` | — | `2` | 触发搜索的最小消息长度 |
| `skip_patterns_file` | — | `""` | 跳过词文件路径（每行一词，# 注释） |
| `attention_boost_max` | — | `1.5` | 注意力加权最大值 |
| `attention_base_increment` | — | `2.0` | 每次提及的基础关注增量 |
| `attention_emotion_factor` | — | `1.5` | 情绪烈度对注意力的放大系数 |
| `synonym_min_word_freq` | — | `10` | 词至少出现在 N 条碎片里才考虑 |
| `synonym_jaccard_threshold` | — | `0.5` | 两词 Jaccard 系数 ≥ 此值视为同义 |
| `synonym_min_co_occurrence` | — | `3` | 两词绝对共现次数 ≥ 此值视为同义 |
| `entity_cooc_top_n` | — | `3` | 搜索时扩展多少个关联实体 |
| `entity_cooc_min_count` | — | `2` | 实体共现几次才算有效关联 |

> `sentiment_*`、`feedback_*`、`hot_topic_*` 等排序权重参数目前仅支持 JSON 配置文件设置，暂不支持环境变量。设 `1.0` 即关闭该维度的加权效果。

### Embedding 模型与维度

| 模型 | 维度 |
|------|------|
| OpenAI text-embedding-3-small | 1536 |
| OpenAI text-embedding-3-large | 3072 |
| OpenAI text-embedding-ada-002 | 1536 |
| DashScope text-embedding-v2 | 1536 |
| DashScope text-embedding-v3 | 1024 |

维度自动检测，切换模型无需重建配置。

### 同义词表

存 Redis Hash `fragmented:synonyms`，搜索时实时展开同义词，提高召回率：

```bash
redis-cli HSET fragmented:synonyms setup '["安装","配置","部署","搭建"]'
redis-cli HSET fragmented:synonyms fix '["修","改","补","修复","解决"]'
```

## 验证

启动后检查日志：

```
Memory provider 'fragmented' registered (0 tools)
fragmented: connected (session=xxx, top_k=5, tag_filter=(none))
fragmented: BM25-only mode (no embedder configured)
```

## 工作原理

```
┌────────────────────────────────────────────────────────┐
│                    用户发送消息                          │
└──────────────────┬─────────────────────────────────────┘
                   │
         ┌─────────▼─────────┐
         │   prefetch()       │  ← 自动触发
         │   ↓                │
         │  工作流锁检查       │  ← 检查 fragmented:workflow_lock
         │   ↓                │
         │  跳过模式检查       │  ← 短消息 / 确认词命中 skip list
         │   ↓                │
         │  BM25 全文搜索     │  ← 默认，零成本
         │  (KNN 向量 search) │  ← 可选（需 embedder）
         │  实体共现扩展      │  ← 查询实体 → 召回关联实体碎片
         │   ↓                │
         │  六维重排序        │  ← 相似度 × 时间衰减
         │                    │    × 情绪 × 反馈 × 热门 × 注意力
         │   ↓                │
         │  完整记忆注入       │  ← top 3 碎片 → 回溯完整原文 → 行内展示
         │  联想回忆           │  ← 完整原文再搜 → 去重追加碎片
         │   ↓                │
         │  Top N 注入上下文   │
         └─────────┬─────────┘
                   │
         ┌─────────▼─────────┐
         │   模型回复         │  ← 碎片可用作参考
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   sync_turn()      │  ← 对话结束自动存档
         │   存储完整原文      │  ← memory:full:{hash} 供碎片溯源
         │   智能句子切分      │  ← 保护缩写/数字/引号
         │   实体提取          │  ← jieba + regex → entities TAG 字段
         │   实体共现记录      │  ← 实体对 ZINCRBY 记共现
         │   注意力追踪        │  ← 提取关键词计入关注度
         │   ↓                │
         │   存入 Redis        │  ← 下次可被检索
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   [cron] 每 2h     │  ← 后台 maintenance
         │   ① 多级合并       │  ← 同主题→LLM提炼→level+1
         │   ② 选择性遗忘     │  ← 低价值碎片清理
         └───────────────────┘
```

## 协议

MIT
