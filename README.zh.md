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
| 🏷️ **标签过滤** | 可选按标签范围搜索 |
| 👍 **反馈加权** | 标记有用/没用的碎片会影响排序 |
| 🔥 **热门话题** | 自动统计跨会话高频话题 |
| 📖 **同义词表** | 存 Redis Hash，实时加载展开搜索，无需部署 |
| 😡 **情绪烈度** | 检测用户表达激烈程度（反复问号/感叹号/程度副词），烈度高的碎片权重更高 |
| 👁️ **注意力追踪** | 用户反复提起的话题自动标记为高关注，相关碎片在搜索中排名上升 |
| 🧬 **多级合并** | 每 2 小时自动将同主题碎片合并提炼为高层记忆，支持 level 1→2→3 多级蒸馏 |
| 🗑️ **选择性遗忘** | 自动清理低价值（旧 + 无反馈 + 低情绪）碎片，保持库精简 |

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

### 1. 创建 Redis Index（首次使用）

代码会自动创建（`ensure_index()`），也可以手动执行：

```bash
redis-cli FT.CREATE idx:memories ON HASH PREFIX 1 "memory:frag:" SCHEMA \
    content TEXT WEIGHT 1 \
    tags TAG SEPARATOR "," \
    category TAG SEPARATOR "," \
    source TEXT WEIGHT 1 \
    created TEXT WEIGHT 0 \
    fragment_type TAG SEPARATOR "," \
    embed_bin VECTOR FLAT 6 TYPE FLOAT32 DIM 1536 DISTANCE_METRIC COSINE
```

> 维度（DIM）根据实际使用的 Embedding 模型动态调整，默认 1536。
> 如果用 Docker：`docker run -d --name redis-stack -p 6379:6379 redis/redis-stack:latest`

### 2. Hermes 配置

在 `~/.hermes/config.yaml` 中开启：

```yaml
memory:
  provider: fragmented
```

详细配置推荐写到 JSON 配置文件（不需要嵌在 config.yaml 里）：

`~/.config/fragmented-memory/config.json`：

```json
{
  "redis_host": "127.0.0.1",
  "redis_port": 6379,
  "top_k": 5,
  "candidate_k": 10,
  "embedder": {
    "provider": "dashscope",
    "api_key": "sk-xxx",
    "model": "text-embedding-v2"
  }
}
```

如果不配置 `embedder`，则只走 BM25 全文搜索模式。

也支持通过环境变量配置（优先级最高）：

```bash
export FRAGMENTED_REDIS_HOST=127.0.0.1
export FRAGMENTED_REDIS_PORT=6379
export FRAGMENTED_TOP_K=5
export FRAGMENTED_EMBEDDER=dashscope
export FRAGMENTED_EMBEDDER_MODEL=text-embedding-v2
export OPENAI_API_KEY=sk-xxx        # embedder API key
```

### 3. 重启 Gateway

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
| `attention_boost_max` | — | `1.5` | 注意力加权最大值 |
| `attention_base_increment` | — | `2.0` | 每次提及的基础关注增量 |
| `attention_emotion_factor` | — | `1.5` | 情绪烈度对注意力的放大系数 |

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
         │  BM25 全文搜索     │  ← 默认，零成本
         │  (KNN 向量 search) │  ← 可选（需 embedder）
         │   ↓                │
         │  六维重排序        │  ← 相似度 × 时间衰减
         │                    │    × 情绪 × 反馈 × 热门 × 注意力
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
         │   智能句子切分      │  ← 保护缩写/数字/引号
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