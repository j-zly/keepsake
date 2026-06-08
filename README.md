# Fragmented Memory Plugin for Hermes Agent

The Fragmented Memory system automatically retrieves relevant memory fragments and injects them into the conversation context for each dialogue.

```text
User: "How did we set up that React project structure last time?"
                      ↓
         Fragmented Memory System     ← Redis + RediSearch
                      ↓
    ┌─────────────────────────────────────┐
    │  [1] User prefers TypeScript + Vite    │
    │  [2] Previous projects used pinia state management │
    │  [3] Backend suggested using .NET 10 implementation       │
    └─────────────────────────────────────┘
                      ↓
         Model directly uses fragments to answer
```

## Features

- **Semantic Splitting** — auto-split conversations into standalone fragments
- **BM25 Full-Text Search** — works out of the box with no external API
- **Optional Vector Search** — KNN via RediSearch (OpenAI / DashScope embedder)
- **Time Decay** — newer fragments rank higher (60-day half-life configurable)
- **Sentiment Weighting** — emotional fragments get priority
- **User Feedback** — mark fragments useful/useless to improve ranking
- **Hot Topic Boost** — frequently discussed topics rank higher
- **Full Memory Recall** — fragments trace back to complete original text, then search again for associative recall
- **Auto Sync** — every turn is archived automatically, memory tool writes are synced
- **Auto Maintenance** — consolidation + selective forgetting to keep storage tidy

## Design Philosophy: Brain-like Memory

Fragmented Memory takes a different approach. It's modeled after **how the human brain actually remembers**:

| Brain Mechanism | Implementation |
|----------------|---------------|
| Forgetting Curve | Time decay (60-day half-life) — old memories fade naturally |
| Emotion Deepens Memory | Emotional weight boost — intense moments stick |
| Repetition Reinforces | Attention tracking + hot topic scoring |
| Correction Works | Correction detection — user says "no", the wrong memory is suppressed |
| Use It or Lose It | Feedback reinforcement (frag_memory_feedback) |
| Association & Analogy | Synonym discovery (Jaccard co-occurrence statistics) — "deploy" ↔ "release" |
| Fragmented Storage | Split conversations into atomic pieces, not full transcripts |
| Fragment Lineage | Each fragment links back to its full original text — "fragment A reminds me of full conversation B" |
| Associative Recall | Search a fragment → trace to full memory → search again for more related fragments |
| Sleep Consolidation | Nightly cron: consolidation + synonym discovery at 3 AM |
| Context Isolation | agent_id tagging — different identities, separate memories |
| Fuzzy but Enough | BM25 full-text search — doesn't need an exact match to recall |

No vector database. No embedding API calls. No LLM inference for memory operations. Just **pure statistical methods** running on Redis + RediSearch — the same techniques the brain uses: frequency, recency, emotional salience, association, and feedback.

## Requirements

- **Python 3.10+**
- **Hermes Agent 0.12+** — provides `MemoryProvider` interface
- **Redis 7+** — with RediSearch module (v2.6+)
- **jieba** — Chinese tokenization (auto-installed)
- **Embedding API** (optional) — OpenAI / DashScope / any compatible `/v1/embeddings` service

## Installation

```bash
pip install fragmented-memory
```

Or install directly from GitHub:

```bash
pip install git+https://github.com/j-zly/fragmented-memory.git
```

## Configuration

Configuration precedence (high to low): **Environment variables > JSON config file > config.yaml inline > defaults**

### 1. Configuration Methods

There are three ways to configure Fragmented Memory, listed in order of priority:

1. **Environment Variables** (Highest precedence)  
   Set environment variables like `FRAGMENTED_REDIS_HOST`, `FRAGMENTED_REDIS_PASSWORD`, etc.

2. **JSON Config File** (~/.config/fragmented-memory/config.json)  
   A complete JSON configuration file for all settings.

3. **Code Defaults** (Lowest precedence)  
   Default values defined in the code.

### 2. Complete Configuration Example

Here's a comprehensive example of the configuration file `~/.config/fragmented-memory/config.json` with all available options:

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

  // 按需检索配置
  "enable_on_demand_search": true,
  "skip_min_length": 2,

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
  
  // 情感强度因子
  "emotion_intensity_factor": 0.4,
  
  // 跳过检索的模式配置
  "enable_on_demand_search": true,
  "skip_min_length": 2
}
```

> Note: Redis password compatibility: leave empty for no authentication, or provide password to automatically send AUTH command.

### 3. Environment Variables Reference

| Environment Variable | Corresponding Config Item | Description |
|----------------------|----------------------------|-------------|
| `FRAGMENTED_REDIS_HOST` | `redis_host` | Redis server host |
| `FRAGMENTED_REDIS_PORT` | `redis_port` | Redis server port |
| `FRAGMENTED_REDIS_PASSWORD` | `redis_password` | Redis password for authentication |
| `FRAGMENTED_TOP_K` | `top_k` | Number of final fragments returned |
| `FRAGMENTED_CANDIDATE_K` | `candidate_k` | Candidate fragments count (for KNN) |
| `FRAGMENTED_BM25_LIMIT` | `bm25_limit` | BM25 search candidate count |
| `FRAGMENTED_TAG_FILTER` | `tag_filter` | Tag filtering (comma-separated) |
| `FRAGMENTED_SKIP_MIN_LENGTH` | `skip_min_length` | Min query length to trigger search (default: 2) |
| `FRAGMENTED_ENABLE_ON_DEMAND_SEARCH` | `enable_on_demand_search` | Enable on-demand search skip patterns |
| `FRAGMENTED_DECAY_HALF_DAYS` | `decay_half_days` | Time decay half-life (days) |
| `FRAGMENTED_HOT_TOPIC_DECAY_HALF_DAYS` | `hot_topic_decay_half_days` | Hot topic time decay half-life (days) |
| `FRAGMENTED_EMBED_CACHE_TTL` | `embed_cache_ttl` | Embedding cache TTL (seconds) |
| `FRAGMENTED_EMBEDDER` | `embedder.provider` | Embedding provider (`openai`, `dashscope`) |
| `FRAGMENTED_EMBEDDER_URL` | `embedder.base_url` | Embedding API endpoint |
| `FRAGMENTED_EMBEDDER_MODEL` | `embedder.model` | Embedding model name |
| `FRAGMENTED_CONSOLIDATE_MIN_GROUP` | `consolidate_min_group` | Minimum fragments to trigger consolidation |
| `FRAGMENTED_CONSOLIDATE_MAX_AGE_HOURS` | `consolidate_max_age_hours` | Minimum age (hours) before fragments can be consolidated |
| `FRAGMENTED_FORGET_MAX_AGE_DAYS` | `forget_max_age_days` | Number of days before fragments might be forgotten |
| `FRAGMENTED_FORGET_DRY_RUN` | `forget_dry_run` | Safe mode for forgetting: only count, don't delete |
| `FRAGMENTED_EMOTION_INTENSITY_FACTOR` | `emotion_intensity_factor` | Emotion intensity → weight coefficient (0=disabled, 1=max) |

> Note: Redis password is compatible with empty value (no auth) or password provided for AUTH command.  

> Note: Changes to config.json take effect immediately without restarting (just send `/new`).

### 4. Create Redis Index (first-time usage)

The code will auto-create (`ensure_index()`), or execute manually:

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

> Dimension (DIM) is dynamically adjusted based on the embedding model used, default 1536.
> For Docker: `docker run -d --name redis-stack -p 6379:6379 redis/redis-stack:latest`

### 5. Hermes Configuration

Enable in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: fragmented
```

If `embedder` is not configured, only BM25 full-text search mode will be used.

Also supports environment variable configuration (highest precedence):

```bash
export FRAGMENTED_REDIS_HOST=127.0.0.1
export FRAGMENTED_REDIS_PORT=6379
export FRAGMENTED_TOP_K=5
export FRAGMENTED_EMBEDDER=dashscope
export FRAGMENTED_EMBEDDER_MODEL=text-embedding-v2
export OPENAI_API_KEY=sk-xxx        # embedder API key
```

### 3. Restart Gateway

```bash
# For CLI mode, restart session is sufficient
# For Gateway mode, restart the process
```

## Configuration Reference

| Config Item | Environment Variable | Default Value | Description |
|-------------|---------------------|---------------|-------------|
| `redis_host` | `FRAGMENTED_REDIS_HOST` | `127.0.0.1` | Redis address |
| `redis_port` | `FRAGMENTED_REDIS_PORT` | `6379` | Redis port |
| `top_k` | `FRAGMENTED_TOP_K` | `5` | Number of final fragments returned |
| `candidate_k` | `FRAGMENTED_CANDIDATE_K` | `10` | Candidate fragments count (for KNN) |
| `tag_filter` | `FRAGMENTED_TAG_FILTER` | `""` | Tag filtering (comma-separated) |
| `bm25_limit` | `FRAGMENTED_BM25_LIMIT` | `10` | BM25 search candidate count |
| `decay_half_days` | `FRAGMENTED_DECAY_HALF_DAYS` | `60` | Time decay half-life (days) |
| `embed_cache_ttl` | `FRAGMENTED_EMBED_CACHE_TTL` | `3600` | Embedding cache TTL (seconds) |
| `sentiment_boost_positive` | — | `1.5` | Positive fragment weight multiplier |
| `sentiment_boost_negative` | — | `1.3` | Negative fragment weight multiplier |
| `feedback_positive_boost` | — | `1.3` | Positive feedback bonus weight |
| `feedback_negative_penalty` | — | `0.5` | Negative feedback penalty coefficient |
| `hot_topic_boost` | — | `1.2` | Hot topic weighting multiplier |
| `embedder.provider` | `FRAGMENTED_EMBEDDER` | `openai` | `openai` / `dashscope` |
| `embedder.api_key` | `OPENAI_API_KEY` | — | Embedding API key |
| `embedder.base_url` | `FRAGMENTED_EMBEDDER_URL` | `https://api.openai.com/v1` | API endpoint |
| `embedder.model` | `FRAGMENTED_EMBEDDER_MODEL` | `text-embedding-3-small` | Embedding model name |
| `consolidate_min_group` | — | `2` | Minimum fragments to trigger consolidation |
| `consolidate_max_age_hours` | — | `72` | Minimum age (hours) before fragments can be consolidated |
| `forget_max_age_days` | — | `30` | Number of days before fragments might be forgotten |
| `forget_dry_run` | — | `true` | Safe mode for forgetting: only count, don't delete |
| `hot_topic_decay_half_days` | — | `30` | Hot topic time decay half-life (days) |
| `emotion_intensity_factor` | — | `0.4` | Emotion intensity → weight coefficient (0=disabled, 1=max) |
| `skip_min_length` | `FRAGMENTED_SKIP_MIN_LENGTH` | `2` | Minimum query length to trigger search |
| `skip_patterns_file` | `FRAGMENTED_SKIP_PATTERNS_FILE` | `""` | Path to file containing patterns to skip search for (one per line, # for comments) |
| `attention_boost_max` | — | `1.5` | Maximum attention weighting value |
| `attention_base_increment` | — | `2.0` | Base attention increment per mention |
| `attention_emotion_factor` | — | `1.5` | Emotion intensity amplification factor for attention |
| `synonym_min_word_freq` | — | `10` | Minimum fragment frequency for word to be considered |
| `synonym_jaccard_threshold` | — | `0.5` | Jaccard similarity threshold for synonym detection |
| `synonym_min_co_occurrence` | — | `3` | Minimum co-occurrence count for synonym detection |

> `sentiment_*`, `feedback_*`, `hot_topic_*` and other ranking weight parameters currently only support configuration through JSON config file, not environment variables. Set to `1.0` to disable the effect of that dimension.

### Embedding Models and Dimensions

| Model | Dimensions |
|-------|------------|
| OpenAI text-embedding-3-small | 1536 |
| OpenAI text-embedding-3-large | 3072 |
| OpenAI text-embedding-ada-002 | 1536 |
| DashScope text-embedding-v2 | 1536 |
| DashScope text-embedding-v3 | 1024 |

Dimensions are automatically detected, switching models doesn't require reconfiguration.

### Synonym Table

Stored in Redis Hash `fragmented:synonyms`, expanded at search time to improve recall:

```bash
redis-cli HSET fragmented:synonyms setup '["install","configure","deploy","setup"]'
redis-cli HSET fragmented:synonyms fix '["fix","modify","correct","repair","solve"]'
```

## Verification

Check logs after startup:

```
Memory provider 'fragmented' registered (0 tools)
fragmented: connected (session=xxx, top_k=5, tag_filter=(none))
fragmented: BM25-only mode (no embedder configured)
```

## Architecture

```
┌────────────────────────────────────────────────────────┐
│                    User sends message                  │
└──────────────────┬─────────────────────────────────────┘
                   │
         ┌─────────▼─────────┐
         │   prefetch()       │  ← Automatically triggered on every user message
         │   ↓                │
         │  BM25 Full-Text Search │  ← Default, zero cost, triggered by user message keywords
         │  (KNN Vector search) │  ← Optional (needs embedder)
         │   ↓                │
         │  Six-dimensional Re-ranking │  ← Similarity × Time decay
         │                    │    × Emotion × Feedback × Hot Topic × Attention
         │   ↓                │
         │  Top N Injected into Context   │  ← Search results injected as code block
         └─────────┬─────────┘
                   │
         ┌─────────▼─────────┐
         │   Model Response   │  ← Fragments can be used as reference
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   sync_turn()      │  ← Automatically archive at end of conversation
         │   Smart Sentence Splitting      │  ← Protect abbreviations/numbers/quotes
         │   Attention Tracking        │  ← Extract keywords and increase attention score
         │   ↓                │
         │   Stored in Redis        │  ← Available for next retrieval
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   [cron] Every 2h     │  ← Background maintenance
         │   ① Multi-level Consolidation       │  ← Same topic→LLM summarization→level+1
         │   ② Selective Forgetting     │  ← Cleanup low-value fragments
         └───────────────────┘
```

## License

MIT
