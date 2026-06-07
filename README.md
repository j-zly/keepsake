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
- **Auto Sync** — every turn is archived automatically, memory tool writes are synced
- **Auto Maintenance** — consolidation + selective forgetting to keep storage tidy

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

### 1. Create Redis Index (first-time usage)

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

### 2. Hermes Configuration

Enable in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: fragmented
```

Detailed configuration is recommended in a JSON config file (not embedded in config.yaml):

`~/.config/fragmented-memory/config.json`:

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
| `attention_boost_max` | — | `1.5` | Maximum attention weighting value |
| `attention_base_increment` | — | `2.0` | Base attention increment per mention |
| `attention_emotion_factor` | — | `1.5` | Emotion intensity amplification factor for attention |

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
         │   prefetch()       │  ← Automatically triggered
         │   ↓                │
         │  BM25 Full-Text Search │  ← Default, zero cost
         │  (KNN Vector search) │  ← Optional (needs embedder)
         │   ↓                │
         │  Six-dimensional Re-ranking │  ← Similarity × Time decay
         │                    │    × Emotion × Feedback × Hot Topic × Attention
         │   ↓                │
         │  Top N Injected into Context   │
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
