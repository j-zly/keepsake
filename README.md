# Keepsake — Memory Plugin for Hermes Agent

> 最后校准: 2026-10-15（对齐 HEAD 2ca18c5：多代理治理四闸 + 记忆/知识库 CRUD + 通用 MCP）

The Keepsake system automatically retrieves relevant memories and injects them into the conversation context for each dialogue.

```text
User: "How did we set up that React project structure last time?"
                      ↓
         Keepsake System     ← Redis + RediSearch
                      ↓
    ┌─────────────────────────────────────┐
    │  [1] User prefers TypeScript + Vite    │
    │  [2] Previous projects used pinia state management │
    │  [3] Backend suggested using .NET 10 implementation       │
    └─────────────────────────────────────┘
                      ↓
         Model directly uses memories to answer
```

## Features

- **Full Entry Storage** — stores complete text as-is, no semantic splitting
- **BM25 Full-Text Search** — works out of the box with no external API
- **Optional Vector Search** — KNN via RediSearch (OpenAI / DashScope / local Ollama embedder; model must be registered in `_MODEL_DIMENSIONS`, see Embedding Models)
- **Time Decay** — newer entries rank higher (60-day half-life configurable)
- **Sentiment Weighting** — emotional entries get priority
- **User Feedback** — mark entries useful/useless to improve ranking
- **Hot Topic Boost** — frequently discussed topics rank higher
- **Entity Extraction** — auto-tags entries with entities (people, places, crypto tickers, domain terms) at store time; searched alongside content text for higher recall
- **Entity Co-occurrence** — auto-track which entities appear together, expand search to co-occurring entities for associative recall ("Python" → also finds entries mentioning "Django")
- **Domain Dictionary** — jieba user dictionary auto-generated from corpus + synonym table, loaded on `/new` for better Chinese tokenization
- **Workflow Lock** — set `keepsake:workflow_lock` in Redis to globally disable memory retrieval (e.g. during automated workflows)
- **Skip Patterns** — define skip lists (via file) to avoid searching on trivial queries like "ok", "got it"
- **Memory Intake** — explicit `memory(action='add')` entries plus per-turn text screened by the v1 ingest gate (R1–R8); junk/system-injected content is rejected before storage, and the v2 pipeline distills turns into facts
- **Search-Time Expiry** — `invalid_at` field in index: set a timestamp and the entry is filtered out at search time (no data loss, can be reverted)
- **Auto Maintenance** — selective forgetting (multi-dimension low-value detection) run every 2h to keep storage tidy. Consolidation is no longer wired into the provider (2026-09) but stays available as an ops-invoked engine — see [Storage Backends](#storage-backends-redis--postgresql--sqlite) and [Consolidation Retirement](#consolidation-retirement-2026-09-09).
- **Three Storage Backends** — `storage.backend` picks `redis` (default) / `postgres` / `sqlite`; all three are equivalent for write / BM25 search / consolidation / forgetting. Redis and PostgreSQL additionally do KNN vector search; SQLite's `search_knn` is an explicit `NotImplementedError` stub (never a silent empty result). See [Storage Backends](#storage-backends-redis--postgresql--sqlite).
- **RRF Fusion Ranking (v1.3)** — Reciprocal Rank Fusion combines BM25 full-text and semantic KNN results into a single ranked list for better recall
- **Local Semantic Search** — optional self-hosted Ollama embedder (e.g. `bge-m3`, 1024-dim) runs fully on-premise via the OpenAI-compatible `/v1/embeddings` endpoint — no external embedding API needed
- **LLM Query Expansion (2026-09)** — when BM25 recalls fewer than `min_results` hits, a free-tier chat LLM quietly generates synonymous short phrases (2–8 chars) into a Redis cache (24h TTL). Runs on a background thread: **zero added latency on the hot path**; disabled automatically when no `llm` channel is configured
- **Time-Aware Recall (v1.5)** — entity timelines (`keepsake:entity_timeline`) + versioned facts let searches leverage *when* things happened, not just what was said
- **Auto Memory Distillation** — `cron/memory_distill.py` (deployed at `/root/scripts/`) uses the keepsake LLM channel (config `llm` section, hot-reload) to distill recent conversations into one-line memories with watermark incremental updates; reserved governance tags self-invented by the LLM are stripped by a blacklist (toggleable)
- **Retrieval Quality Spot Checks** — `scripts/eval_spotcheck.py` runs 30 real-query regression tests to track search quality (v1.4 BM25-only: 60% → 67%; +vector KNN fusion 2026-09: 73%)
- **Auto-Registered Cron Jobs** — when used as a Hermes plugin, three cron jobs (memory maintenance every 2h, deduplication every 1h, synonym discovery every 8h) are automatically registered on plugin initialization — zero manual setup
- **Hermes Plugin Wrapper** — ready-to-use `hermes-plugin/` directory with `plugin.yaml` and `__init__.py` for drop-in installation
- **Two-Phase Pipeline (v2, 2026-09)** — Mem0-style extract/update phases run async on a daemon thread, sealing old facts via `superseded_by` edges (not physical delete) so stale snapshots (e.g. completed tasks marked pending) never reappear. Cost-capped at `llm_pipeline.max_calls_per_window`; any LLM/JSON failure or budget breach falls back to v1 rule-gate storage — messages are never silently dropped.
- **Generic Memory Interface & MCP** — `MemoryProvider` is a host-agnostic `Protocol` with three fixed failure tiers (injection fail-open / retrieval raises / write fail-closed); `build_server(provider)` wires any implementation into an MCP server in three lines. See [Generic Memory Interface & MCP](#generic-memory-interface--mcp)
- **Multi-Agent Memory Governance** — four gates keep sub-agents out of the resident layer's write path while leaving it readable: non-primary read-open on `core`, automatic `core_candidate` downgrade on write, a console review queue, and the main brain's `retrieval.exclude_tags` filter. See [Multi-Agent Memory Governance](#multi-agent-memory-governance)
- **Console Memory CRUD** — create, delete (confirm the last 8 characters of the key, backed up first), JSONL/CSV export and dry-run import from the console; the resident-layer list takes `?q=` free-text search
- **Knowledge Base CRUD and Ingestor** — document/chunk storage beside the memory store, with a manual domain owned by the console and a `skill` domain owned by the nightly ingestor; chunking is single-sourced in `keepsake.kb.ingest`. See [Knowledge Base (documents / chunks) and the Ingestor](#knowledge-base-documents--chunks-and-the-ingestor)
- **Standalone Memory Maintenance Page** — merge / forget dry-run and real run plus the resident-layer candidate review queue, split out of the dashboard into its own console page

## Design Philosophy: Clean Memory for LLMs

Keepsake stores **full, self-contained entries** — not split conversation snippets. The key insight is that LLMs need complete context to make use of stored information. An entry like "prefers TypeScript + Vite" without its surrounding context is useless; the full entry "User prefers TypeScript + Vite for frontend projects" is immediately actionable.

| Mechanism | Implementation |
|-----------|---------------|
| Complete Context | Stores full text entries, no splitting |
| Forgetting Curve | Time decay (60-day half-life) — old memories fade naturally |
| Emotion Deepens Memory | Emotional weight boost — intense moments stick |
| Repetition Reinforces | Attention tracking + hot topic scoring |
| Use It or Lose It | Feedback reinforcement (keepsake_feedback) |
| Association & Analogy | Synonym discovery (Jaccard co-occurrence statistics) — "deploy" ↔ "release" |
| Entity Association | Entity co-occurrence tracking — entries mentioning "BTC" also recall "halving" without being synonyms |
| Entity Tagging | Like the brain tagging memories with people/places/things — auto-extracted entities searched alongside content |
| Memory Intake | Explicit `memory(action='add')` writes, plus per-turn text screened by the ingest gate and distilled by the v2 pipeline |
| Sleep Consolidation | Background maintenance every 2h: selective forgetting (multi-dimension low-value detection). Consolidation is ops-invoked, not wired into the provider. |
| Context Isolation | agent_id tagging — different identities, separate memories |
| Fuzzy but Enough | BM25 full-text search — doesn't need an exact match to recall |

By default (BM25-only mode) Keepsake needs no vector database, no embedding API and no LLM: **pure statistical methods** run on Redis + RediSearch — the same signals the brain uses: frequency, recency, emotional salience, association, and feedback. Optional layers plug in without replacing this baseline: a self-hosted or hosted **embedding API** adds KNN semantic recall fused via RRF, and an **LLM** powers the v2 write pipeline and query expansion. Every optional layer has an explicit degrade path — missing config never silently enables a fallback provider.

## Requirements

- **Python 3.10+**
- **Hermes Agent 0.12+** — provides `MemoryProvider` interface
- **Redis 7+** — with RediSearch module (v2.6+)
- **jieba** — Chinese tokenization (auto-installed)
- **Embedding API** (optional) — OpenAI / DashScope / any compatible `/v1/embeddings` service, including self-hosted [Ollama](https://ollama.com) (e.g. `bge-m3`)

## Installation

```bash
pip install keepsake
```

Or install directly from GitHub:

```bash
pip install git+https://github.com/j-zly/keepsake.git
```

## Configuration

Configuration precedence (high to low): **Environment variables > JSON config file > config.yaml inline > defaults**

### 1. Configuration Methods

There are three ways to configure Keepsake, listed in order of priority:

1. **Environment Variables** (Highest precedence)  
   Set environment variables like `KEEPSAKE_REDIS_HOST`, `KEEPSAKE_REDIS_PASSWORD`, etc.

2. **JSON Config File** (~/.config/keepsake/config.json)  
   A complete JSON configuration file for all settings.

3. **Code Defaults** (Lowest precedence)  
   Default values defined in the code.

### 2. Complete Configuration Example

Here's a comprehensive example of the configuration file `~/.config/keepsake/config.json` with all available options:

```json
{
  // Redis connection
  "redis_host": "127.0.0.1",
  "redis_port": 6379,
  "redis_password": "",

  // Storage backend (optional, defaults to "redis") — this ONE line switches backends.
  // "postgres" is functionally equivalent to "redis": write, BM25+KNN search,
  // consolidation and forgetting all work. See "Storage Backends" below.
  // Install: pip install 'keepsake-memory[postgres]'  (needs pgvector in the database)
  // ⚠️ Without an embedder you MUST set top-level "embed_dim" (e.g. 1024) — see
  //    "Storage Backends" → "embed_dim".
  // "storage": {
  //   "backend": "postgres",
  //   "postgres": {"host": "127.0.0.1", "port": 5432, "dbname": "keepsake",
  //                "user": "keepsake", "password": "***", "sslmode": ""}
  // },
  //
  // "sqlite" is the third backend: single file, zero third-party deps, no server.
  //   "backend": "sqlite",
  //   "sqlite": {"path": "~/.keepsake/keepsake.db", "busy_timeout_ms": 5000}

  // Search settings
  "top_k": 5,
  "candidate_k": 10,
  "bm25_limit": 10,
  "tag_filter": "",

  // Skip patterns
  "skip_min_length": 2,
  "skip_patterns_file": "~/.config/keepsake/skip_patterns.txt",

  // Time decay
  "decay_half_days": 60,
  "hot_topic_decay_half_days": 30,
  
  // Ranking weights
  "sentiment_boost_positive": 1.5,
  "sentiment_boost_negative": 1.3,
  "feedback_positive_boost": 1.3,
  "feedback_negative_penalty": 0.5,
  "hot_topic_boost": 1.2,
  
  // Attention
  "attention_boost_max": 1.5,
  "attention_base_increment": 2.0,
  "attention_emotion_factor": 1.5,
  
  // Embedding (optional) — model MUST be registered in embedder.py _MODEL_DIMENSIONS;
  // unregistered model = explicit degrade to BM25-only (never a silent default dim).
  // Example: self-hosted Ollama bge-m3 (OpenAI-compatible endpoint, api_key any non-empty)
  "embedder": {
    "provider": "openai",
    "api_key": "***",
    "base_url": "http://127.0.0.1:11434/v1",
    "model": "bge-m3"
  },

  // LLM channel (v2 two-phase pipeline + query expansion share this)
  // — unconfigured = no LLM = v1 rule-gate fallback, never a silent paid provider
  // base_url: chat completions endpoint root (no trailing /v1 for zhipu etc.)
  // model: e.g. glm-4-flash (REQUIRED — missing = channel invalid, v1 fallback)
  // api_key (testing) OR key_file (prod, supports hot-rotation)
  "llm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash", "key_file": "/path/to/key.pass"},

  // Query expansion (optional, on by default) — background LLM synonyms when BM25 hits < min_results
  "retrieval": {"query_expansion": {"enabled": true, "min_results": 3, "max_terms": 6, "cache_ttl": 86400}},

  // Auto maintenance
  "consolidate_min_group": 3,
  "consolidate_min_overlap": 3,
  "consolidate_max_age_hours": 72,
  "forget_max_age_days": 30,
  "forget_dry_run": false,

  // Agent isolation
  "agent_id": "main-brain",
  "is_primary": true,

  // Synonym discovery
  "synonym_min_word_freq": 10,
  "synonym_jaccard_threshold": 0.5,
  "synonym_min_co_occurrence": 3,

  // Entity co-occurrence
  "entity_cooc_top_n": 3,
  "entity_cooc_min_count": 2,
  
  // Emotion intensity factor
  "emotion_intensity_factor": 0.4
}
```

> Note: Redis password compatibility: leave empty for no authentication, or provide password to automatically send AUTH command.
>
> Note: `storage.backend` defaults to `redis`. A missing, empty or unrecognised value always falls back to `redis`, so existing deployments are unaffected. `postgres` is functionally equivalent to `redis` — write, BM25+KNN search, consolidation and forgetting all work; only the implementation differs. `sqlite` is the embedded single-file backend for single-machine / single-agent setups — BM25 search, consolidation and forgetting all work, but `search_knn` raises `NotImplementedError`. See [Storage Backends](#storage-backends-redis--postgresql--sqlite).

### 3. Environment Variables Reference

| Environment Variable | Corresponding Config Item | Description |
|----------------------|----------------------------|-------------|
| `KEEPSAKE_CONFIG` | — | Path to `config.json` (default `~/.config/keepsake/config.json`) |
| `KEEPSAKE_REDIS_HOST` | `redis_host` | Redis server host |
| `KEEPSAKE_REDIS_PORT` | `redis_port` | Redis server port |
| `KEEPSAKE_REDIS_PASSWORD` | `redis_password` | Redis password for authentication |
| `KEEPSAKE_TOP_K` | `top_k` | Number of final entries returned |
| `KEEPSAKE_CANDIDATE_K` | `candidate_k` | Candidate entries count (for KNN) |
| `KEEPSAKE_TAG_FILTER` | `tag_filter` | Tag filtering (comma-separated) |
| `KEEPSAKE_AGENT_ID` | `agent_id` | Agent identity tag for isolation |
| `KEEPSAKE_IS_PRIMARY` | `is_primary` | `true` = sees all entries; `false` = only tagged ones |
| `KEEPSAKE_EMBEDDER` | `embedder.provider` | Embedding provider (`openai`, `dashscope`) |
| `KEEPSAKE_EMBEDDER_URL` | `embedder.base_url` | Embedding API endpoint |
| `KEEPSAKE_EMBEDDER_MODEL` | `embedder.model` | Embedding model name |
| `OPENAI_API_KEY` | `embedder.api_key` | Embedding API key |

> **Every other config item is `config.json`-only — there is no environment variable for it.** In particular `bm25_limit`, `decay_half_days`, `hot_topic_decay_half_days`, `embed_cache_ttl`, `storage.backend`, `embed_dim`, `consolidate_min_group`, `consolidate_min_overlap`, `consolidate_max_age_hours`, `forget_max_age_days`, `forget_dry_run` and `emotion_intensity_factor` are read from the config file only. See the [Configuration Reference](#configuration-reference) table below for defaults.

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
    entry_type TAG SEPARATOR "," \
    invalid_at TAG SEPARATOR "," \
    entities TAG SEPARATOR "," \
    embed_bin VECTOR FLAT 6 TYPE FLOAT32 DIM 1024 DISTANCE_METRIC COSINE
```

> The vector DIM must match the registered dimension of your embedding model (see Embedding Models below; `bge-m3`=1024, `nomic-embed-text`=768, `text-embedding-3-small`=1536). `ensure_index()` handles this automatically from the configured embedder. Changing models requires: purge old `embed_bin` + `embed_cache:*` → `FT.DROPINDEX` (without DD, data survives) → recreate with the new DIM.
> For Docker: `docker run -d --name redis-stack -p 6379:6379 redis/redis-stack:latest`

### 5. Hermes Configuration

Enable in `~/.hermes/config.yaml`:

```yaml
memory:
  provider: keepsake
```

If `embedder` is not configured, only BM25 full-text search mode will be used.

Also supports environment variable configuration (highest precedence):

```bash
export KEEPSAKE_REDIS_HOST=127.0.0.1
export KEEPSAKE_REDIS_PORT=6379
export KEEPSAKE_TOP_K=5
export KEEPSAKE_EMBEDDER=dashscope
export KEEPSAKE_EMBEDDER_MODEL=text-embedding-v2
export OPENAI_API_KEY=sk-xxx        # embedder API key
```

### 6. Workflow Lock

Temporarily disable memory retrieval during automated workflows (like batch processing):

```bash
# Lock (3600s TTL)
redis-cli SET keepsake:workflow_lock 1 EX 3600

# Unlock
redis-cli DEL keepsake:workflow_lock
```

### 7. Skip Patterns File

Create a file (one pattern per line, `#` for comments):

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

Then reference it in config.json:

```json
{
  "skip_min_length": 2,
  "skip_patterns_file": "~/.config/keepsake/skip_patterns.txt"
}
```

### 8. Restart Gateway

```bash
# For CLI mode, restart session is sufficient
# For Gateway mode, restart the process
```

## Configuration Reference

| Config Item | Environment Variable | Default Value | Description |
|-------------|---------------------|---------------|-------------|
| `storage.backend` | — | `"redis"` | `redis` / `postgres` / `sqlite`. Missing / empty / invalid falls back to `redis`. See [Storage Backends](#storage-backends-redis--postgresql--sqlite) |
| `storage.postgres.host` | — | `127.0.0.1` | PostgreSQL host (only read when `backend` is `postgres`) |
| `storage.postgres.port` | — | `5432` | PostgreSQL port |
| `storage.postgres.dbname` | — | `keepsake` | PostgreSQL database |
| `storage.postgres.user` | — | `""` | PostgreSQL user |
| `storage.postgres.password` | — | `""` | PostgreSQL password |
| `storage.postgres.sslmode` | — | `""` | PostgreSQL sslmode |
| `storage.sqlite.path` | — | `~/.keepsake/keepsake.db` | SQLite database file (only read when `backend` is `sqlite`) |
| `storage.sqlite.busy_timeout_ms` | — | `5000` | SQLite `busy_timeout` — how long a writer waits for the write lock before a retry |
| `embed_dim` | — | `1536` | Vector width for the PG `embedding vector(N)` column. **Set this explicitly when no embedder is configured** — see [Storage Backends](#storage-backends-redis--postgresql--sqlite) → `embed_dim` |
| `redis_host` | `KEEPSAKE_REDIS_HOST` | `127.0.0.1` | Redis address |
| `redis_port` | `KEEPSAKE_REDIS_PORT` | `6379` | Redis port |
| `top_k` | `KEEPSAKE_TOP_K` | `5` | Number of final entries returned |
| `candidate_k` | `KEEPSAKE_CANDIDATE_K` | `10` | Candidate entries count (for KNN) |
| `tag_filter` | `KEEPSAKE_TAG_FILTER` | `""` | Tag filtering (comma-separated) |
| `bm25_limit` | `KEEPSAKE_BM25_LIMIT` | `10` | BM25 search candidate count |
| `decay_half_days` | `KEEPSAKE_DECAY_HALF_DAYS` | `60` | Time decay half-life (days) |
| `embed_cache_ttl` | `KEEPSAKE_EMBED_CACHE_TTL` | `3600` | Embedding cache TTL (seconds) |
| `sentiment_boost_positive` | — | `1.5` | Positive entry weight multiplier |
| `sentiment_boost_negative` | — | `1.3` | Negative entry weight multiplier |
| `feedback_positive_boost` | — | `1.3` | Positive feedback bonus weight |
| `feedback_negative_penalty` | — | `0.5` | Negative feedback penalty coefficient |
| `hot_topic_boost` | — | `1.2` | Hot topic weighting multiplier |
| `embedder.provider` | `KEEPSAKE_EMBEDDER` | `openai` | `openai` / `dashscope` |
| `embedder.api_key` | `OPENAI_API_KEY` | — | Embedding API key |
| `embedder.base_url` | `KEEPSAKE_EMBEDDER_URL` | `https://api.openai.com/v1/embeddings` | API endpoint |
| `embedder.model` | `KEEPSAKE_EMBEDDER_MODEL` | `text-embedding-3-small` | Embedding model name |
| `consolidate_min_group` | — | `3` | Minimum entries to trigger consolidation |
| `consolidate_min_overlap` | — | `3` | Minimum shared keywords for two entries to count as the same topic. Invalid values log a warning and fall back to `3`. See [Storage Backends](#storage-backends-redis--postgresql--sqlite) → "Consolidation thresholds" |
| `consolidate_max_age_hours` | — | `72` | Minimum age (hours) before consolidation. **Not read from config.json** — pass it as `Consolidator(max_age_hours=…)` |
| `forget_max_age_days` | — | `30` | Max age (days) before deletion |
| `forget_dry_run` | — | `true` | Safe mode: `true` = count only, `false` = delete |
| `agent_id` | — | `""` | Agent identity tag for isolation (e.g. `"main-brain"`) |
| `is_primary` | — | `false` | `true` = sees all entries; `false` = only tagged ones |
| `hot_topic_decay_half_days` | — | `30` | Hot topic time decay half-life (days) |
| `emotion_intensity_factor` | — | `0.4` | Emotion intensity → weight coefficient |
| `skip_min_length` | — | `2` | Minimum query length to trigger search |
| `skip_patterns_file` | — | `""` | Path to skip patterns file |
| `attention_boost_max` | — | `1.5` | Max attention weighting value |
| `attention_base_increment` | — | `2.0` | Base attention increment per mention |
| `attention_emotion_factor` | — | `1.5` | Emotion amplification for attention |
| `synonym_min_word_freq` | — | `10` | Min frequency for synonym candidate |
| `synonym_jaccard_threshold` | — | `0.5` | Jaccard threshold for synonym detection |
| `synonym_min_co_occurrence` | — | `3` | Min co-occurrence for synonym detection |
| `entity_cooc_top_n` | — | `3` | Number of co-occurring entities to expand search |
| `entity_cooc_min_count` | — | `2` | Min co-occurrence for entity association |
| `retrieval.exclude_tags` | — | `core_candidate` | Tags (comma-separated) excluded from retrieval. Empty string = gate off. Resolved once in `initialize()` ⇒ **restart to apply**. See [Multi-Agent Memory Governance](#multi-agent-memory-governance) |

> `sentiment_*`, `feedback_*`, `hot_topic_*` and other ranking weight parameters currently only support configuration through JSON config file, not environment variables. Set to `1.0` to disable the effect of that dimension.

## Storage Backends (Redis / PostgreSQL / SQLite)

### Switching backends

One config line selects the backend — `storage.backend`:

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

`redis` (the default) and `postgres` are **functionally equivalent**: write, BM25 + KNN search, consolidation and forgetting all work on both. Only the implementation differs — same `StorageBase` interface, three classes. `sqlite` covers the third tier — see [SQLite](#sqlite) for what it does and does not do.

- A missing, empty, non-string or unrecognised `backend` value **always falls back to `redis`**, so existing deployments are unaffected.
- Install the optional dependency: `pip install 'keepsake-memory[postgres]'` (pulls `psycopg[binary]`). The database also needs **pgvector** — the KNN column is `embedding vector(N)` with an HNSW cosine index.
- Importing `storage_pg` does not require `psycopg`; the import happens at connect time, so a missing optional dependency never breaks Redis-only installs.

### `embed_dim`

Top-level key, default `1536`. It sets the width of the PG `embedding vector(N)` column.

**If no embedder is configured, set `embed_dim` explicitly** (e.g. `1024` for a library built with `bge-m3`). Without an embedder the provider falls back to the default `1536`; if the live column is `vector(1024)`, `ensure_index()` raises `_SchemaDimMismatch`, provider initialisation aborts, and every recall comes back empty. With an embedder configured, its registered dimension wins and `embed_dim` is only a fallback.

### Schema self-healing

`ensure_index()` is idempotent and repairs the schema on its own — it never blindly re-sends DDL:

1. **Check before applying** — it queries `information_schema` / `pg_indexes` and issues DDL only for what is actually missing. A complete schema gets no DDL at all.
2. **Once per process** — a memo keyed by `(target database, dimension)` short-circuits repeat calls.
3. **Serialised and bounded** — a fixed `pg_advisory_lock` plus `SET LOCAL lock_timeout = 5000`, with up to 3 retries and exponential backoff. Exceeding the retries logs an error and **returns `False`** — an explicit failure, never a silent success.

Concretely: an **old database missing columns** gets them back via `ALTER TABLE ... ADD COLUMN` for the maintenance columns and, when a dimension is known, for `content_tsv` / `embedding`; an **empty database** no longer collides with `CREATE TABLE` (the maintenance-column `ALTER`s carry `IF NOT EXISTS`, so the create path and the alter path are both idempotent).

### Maintenance capabilities and the four backend-agnostic primitives

Consolidation and forgetting reach storage **only** through four primitives on the `StorageBase` interface, which is why they behave identically on all backends:

| Primitive | Redis | PostgreSQL | SQLite |
|-----------|-------|------------|--------|
| `scan_fragment_keys(cursor, limit, prefix)` | `SCAN` cursor | keyset pagination `WHERE key > :cursor ORDER BY key LIMIT n` (never a large `OFFSET`) | keyset pagination `WHERE key > ? AND key LIKE ? ORDER BY key LIMIT n` |
| `get_fragments_batch(keys)` | `HMGET` | `WHERE key = ANY(...)` | `WHERE key IN (...)` |
| `write_fragments_batch(rows)` | pipeline upsert | upsert, recomputing `content_tsv` / `embedding` | `upsert_fragment` in a write transaction, refreshing FTS5 |
| `update_fragment_fields(key, fields)` | `HSET` | partial `UPDATE` (does not touch `content` / `content_tsv` / `embedding`) | partial `UPDATE` + reindex `content_tsv` / FTS5 |
| `delete_fragments_batch(keys)` | pipeline `DEL` | `DELETE ... WHERE key = ANY(...)` plus timeline cleanup | `DELETE ... WHERE key IN (...)` plus timeline **and FTS5** cleanup |

The cursor is an opaque string: `""` starts from the beginning, and an empty `next_cursor` means the scan is done.

PG-side maintenance columns — `level`, `consumed_by`, `consumed_at` — are `TEXT NOT NULL DEFAULT ''`. **Test "not set" with `= ''`, never `IS NULL`**; the default keeps existing rows valid.

Writing is equivalent too: same-content deduplication marks the old version `valid_until` / `is_archived` and files the new one under a `:<epoch>` key suffix, and the R6 rule "a duplicate hit never overwrites `content`" holds on all backends.

### Consolidation thresholds

Two top-level `config.json` keys (config file only — no environment variable):

| Key | Default | Meaning |
|-----|---------|---------|
| `consolidate_min_overlap` | `3` | Minimum shared keywords for two entries to count as the same topic |
| `consolidate_min_group` | `3` | Minimum entries in a group before it is actually merged |

`consolidate_min_overlap` used to be hard-coded at `2`, which on a production sample merged 1498 of 2819 entries (53%); at `3` it merges roughly 13%. An invalid value (non-integer, boolean, or `< 1`) logs a warning and falls back to the default — it never crashes.

Consolidation and forgetting are **constructed fresh each round by the ops script / cron**, not held by the provider — so changing these keys takes effect on the next round, **no restart required**. See [Consolidation Retirement](#consolidation-retirement-2026-09-09) for why the provider no longer keeps a consolidator.

### Migrating Redis → PostgreSQL

```bash
python3 scripts/migrate_redis_to_pg.py --dry-run     # read-only, prints the plan
python3 scripts/migrate_redis_to_pg.py --limit 100   # try 100 fragments first
python3 scripts/migrate_redis_to_pg.py                # full migration
python3 scripts/migrate_redis_to_pg.py --skip-aux     # fragments only, skip auxiliary structures
python3 scripts/migrate_redis_to_pg.py --config /path/to/config.json
```

The script is **strictly read-only against Redis** (only `SCAN` / `HGETALL` / `PING`; an AST self-check rejects any write command at startup), is idempotent (`ON CONFLICT (key) DO UPDATE`), copies existing `embed_bin` vectors rather than recomputing them, and migrates the auxiliary structures (hot topics, attention, entity timelines, co-occurrence, synonyms) alongside the fragments — it prints both counts and fails if they disagree. Credentials come from keepsake's `config.json` / `KEEPSAKE_CONFIG`; nothing is hardcoded or logged.

### Known backend differences

Corpus maintenance is now implemented on **all three backends**, and the differences that remain fail loudly rather than silently:

| Method | Redis / PostgreSQL | SQLite | Why |
|--------|--------------------|--------|-----|
| `discover_synonyms` | ✅ supported | ✅ supported | Corpus maintenance, orthogonal to search — all three backends share the same judgement chain in `storage_shared` |
| `generate_jieba_dict` | ✅ supported | ✅ supported | Same. Given the same corpus, all three produce a **byte-identical** vocabulary and dictionary (`6a31b17`) |
| `search_knn` | ✅ vector search | raises `NotImplementedError` | Needs the `sqlite-vec` extension (optional lazy-load, not implemented). Fails loudly on purpose — **never a silent empty result**; `search` degrades to BM25-only with a WARNING |
| `touch_fragment` | PG returns `False` + logs; Redis writes | ✅ writes | `ks_fragment` has no `touch_count` / `updated_at` columns; the fields have no reader anywhere in the repo |
| `set_supersedes` | PG returns `False` + logs; Redis writes | ✅ writes | No reader either — reverse sealing uses `supersede_fragment`, which works on all backends |

### Post-switch self-check checklist

Run these in order after pointing `storage.backend` at PostgreSQL. Each step is non-destructive until the last two, which are dry-runs.

```bash
export PYTHONPATH=src
export KEEPSAKE_CONFIG="$HOME/.config/keepsake/config.json"   # adjust if yours lives elsewhere
```

**1. `ensure_index()` → True** (builds / repairs the schema; prints `schema ready on …`):

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

**2. Health check** (`SELECT 1` on PG):

```bash
python3 - <<'PY'
from keepsake import _load_json_config
from keepsake.storage import storage_from_config
cfg = _load_json_config()
st = storage_from_config(config=cfg, agent_id=cfg.get("agent_id", ""), is_primary=cfg.get("is_primary", False))
print("health_check =", st.health_check())
PY
```

**3. One search** — expect hits with non-zero scores, not an empty list:

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

**4. Consolidation dry-run** (zero writes — scans, clusters, reports group counts only):

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

**5. Forgetting dry-run** (zero writes — counts candidates, deletes nothing):

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

Both dry-runs report `dry_run: True` with `deleted: 0` / `would_merge` counts. Neither touches a row — verify by comparing fragment counts before and after.

> Credentials are read from `config.json` (or `KEEPSAKE_CONFIG`); none of the commands above take a password on the command line, and none of them print one.

### SQLite

The third backend, for the **single-machine / single-agent** tier. Pick by scale:

| Scale | Backend |
|-------|---------|
| Large / multi-agent | `postgres` |
| General | `redis` (default) |
| Single machine, embedded, single agent | `sqlite` |

The user red line still holds — **`storage.backend` selects exactly one backend**; Redis and PostgreSQL are never run in parallel.

#### Implementation

**Pure standard-library `sqlite3`, zero third-party dependencies.** Switching is the same one config line as the other two:

```json
"storage": {
  "backend": "sqlite",
  "sqlite": {"path": "~/.keepsake/keepsake.db", "busy_timeout_ms": 5000}
}
```

The default is still `redis`, and production currently runs `postgres`. Importing `storage_sqlite` needs nothing beyond the standard library.

#### Capabilities

Shipped in three batches:

- **p1 — read/write primitives, schema self-healing, config wiring** (`e8b0398`); plus a write-path guard (`3099609`) that refuses writes when the schema is not ready (`StorageNotReadyError`, no silent no-op) and an in-process write lock covering the whole transaction body (`0fd1b54`).
- **p2 — search: FTS5 + jieba tokenisation + BM25 + the shared re-ranker** (`8078c6f`); then contract alignment — result limits and ranking weights read from the same config as PG (`c88070d`).
- **p3 — consolidation, forgetting, synonyms, jieba dictionary** (`38d5816`). On the same corpus, all three backends produce a **byte-identical vocabulary and dictionary** (`6a31b17`).

Tag filtering is shared by all three backends via one `tag_match_sql` helper (`2a9aabd`) — commas delimit tags in the database, so a substring match on `|`-padded tags silently missed every row with a second tag.

#### Boundaries — read this before choosing it

**Vector search `search_knn` is a stub that raises `NotImplementedError` explicitly.** `sqlite-vec` is an optional lazy-load and is not implemented. The convention is deliberate: it **never returns an empty result silently** — a silent empty means "memory can't be found and nothing says why", the worst failure mode in this project. `search` catches the error, logs a WARNING, and degrades to BM25-only, so the degradation is always visible in the log.

Writes *do* persist embeddings (`store()` stores a float32 blob in the same `struct.pack` layout as Redis), so switching to SQLite does not strand history without vectors — only vector *retrieval* is missing.

#### Concurrency

`PRAGMA journal_mode=WAL` ⇒ **many readers, one writer**; reads stay available while a write transaction is in progress. Concurrent writes queue on `busy_timeout` (default 5000 ms) plus write retries (`WRITE_RETRY_ATTEMPTS = 6`, ≈ 34 s total budget). Write transactions always open with `BEGIN IMMEDIATE` — taking the write lock at statement one rather than upgrading at commit time, which is the classic source of `database is locked` deadlocks. Within one process all statements serialise on a single `RLock`, because a `check_same_thread=False` connection is not thread-safe on its own.

#### Chinese text

Tokenisation happens **Python-side with `jieba`** before insert: FTS5 matches on a space-separated token string, never on raw text.

> The `trigram` tokenizer is deliberately **not** used — it has a 3-character minimum, so a 2-character query term such as `证书` inside `证书续签` matches nothing. Two-character words are the bulk of Chinese queries, so **any 2-character query returns 0 hits** under trigram. jieba pre-tokenisation plus the default tokenizer is the supported path.

#### Why not DuckDB

DuckDB was evaluated and **rejected**: it takes an exclusive file lock, so a `read_only` connection cannot read even while a write transaction is in progress elsewhere — which defeats the point of choosing an embedded backend.

### Embedding Models and Dimensions

Embedding models must be **registered** in `embedder.py:_MODEL_DIMENSIONS` — an unregistered model is rejected with a clear log line and degrades to BM25-only, never a guessed default dimension (this prevents silent index corruption from dim mismatch).

| Model | Dimensions |
|-------|------------|
| OpenAI text-embedding-3-small | 1536 |
| OpenAI text-embedding-3-large | 3072 |
| OpenAI text-embedding-ada-002 | 1536 |
| DashScope text-embedding-v2 | 1536 |
| DashScope text-embedding-v3 | 1024 |
| BAAI bge-m3 (local Ollama) | 1024 |
| BAAI bge-large-zh-v1.5 / bge-large-en-v1.5 | 1024 |
| BAAI bge-base-zh-v1.5 | 768 |
| nomic-embed-text (local Ollama) | 768 |
| mxbai-embed-large (local Ollama) | 1024 |
| snowflake-arctic-embed (local Ollama) | 1024 |

> ⚠️ Switching models requires a dimension change procedure: purge existing `embed_bin` values and `embed_cache:*` keys, then `FT.DROPINDEX` + recreate the index with the new DIM (see Redis Index section). Old vectors are incompatible with a new index DIM and will fail indexing silently otherwise.

### Synonym Table

Stored in Redis Hash `keepsake:synonyms`, expanded at search time to improve recall:

```bash
redis-cli HSET keepsake:synonyms setup '["install","configure","deploy","setup"]'
redis-cli HSET keepsake:synonyms fix '["fix","modify","correct","repair","solve"]'
```

### Ingest Gate v1 (2026-09)

Write-side gate (`src/keepsake/ingest_gate.py`) intercepts every `sync_turn()` call before storage. Rules short-circuit in order: **R1** system-injection prefixes (`[CONTEXT COMPACTION`, `[System note`) → reject; **R2** `len > max_len` (default 2000) → reject; **R3** blacklist / status questions / stripped length < 8 → reject; **R4** no letter/Chinese/digit at all → reject; **R5** `category="memory_tool"` → reject (MEMORY.md ↔ fragment library decoupled); **R6** same content hash already exists for `turn_memory`/`conversation` → `update_state` only (bump `updated_at`/`touch_count`, never overwrite `content`); **R7** else → store (with secrets scrubbed at every `storage.store()` call site — see R7 below). Toggle in `config.json` under `"ingest_gate": {"enabled": true, "max_len": 2000}`. Defaults: enabled. When Hermes adds a new kind of system-injected message text, its prefix must be added to `R1 _REJECT_PREFIXES`.

#### R7 — Secret Scrubbing (2026-09, write-side source gate)

Before any `storage.store()` call, `scrub_secrets(text) -> (cleaned_text, n_masks)` masks obvious credentials so they never enter the fragment library. Pure function, idempotent (calling twice returns `n_masks=0` on the second call). Wired at every store point: `sync_turn` v1 path (`_v1_store_after_decide` in `__init__.py`), v2 pipeline `_do_add` / `_do_update` / `_fallback_to_v1` raw-store in `pipeline.py`. The R6 `update_state` path is intentionally **not** scrubbed (only timestamps are bumped; original `content` is preserved).

| Category | Pattern | Example → Masked |
|---|---|---|
| 1 | `(密码\|口令\|password\|passwd) [是为:：=] value` | `密码是FAKEpw9` → `密码是***[REDACTED]` |
| 2 | `(api[_-]?key\|token\|secret\|密钥) [是为:：=] value` | `token: FAKEtok+enABCDEF0123456789xyzXYZaaaa==` → `token: ***[REDACTED]` |
| 3 | `Password=value` (case-insensitive; value stops at `; " ' &`) | `Password=FAKEpw1;` → `Password=***[REDACTED];` |
| 4 | `Bearer value` (value ≥ 16 base64 chars) | `Bearer abc123def456ghi789jkl0123456789` → `Bearer ***[REDACTED]` |
| 5 | Fallback: 12+ consecutive `[A-Za-z0-9+=_.-]` with conservative filter (reject pure-digit-dot strings like IPv4 / URL host parts / file paths) | `sk-FAKE0123456789ab` → `***[REDACTED]` |

Mask format: keep the matching prefix (label + separator), replace the value with `***[REDACTED]`. Categories 3 (Password=xxx) runs first to preserve `;` and other DSN terminators.

Conservative filtering of the high-entropy fallback (Category 5) avoids false positives on:

- **IPv4 addresses** — `154.219.96.202` → no mask
- **URL host / paths** — `https://api.example.com/v1/users`, `/var/log/keepsake/server.log` → no mask
- **Plain English** — `thequickbrownfoxjumpsoverthelazydog` → no mask
- **Chinese text** — `我的密码本很厚` (no separator) → no mask
- **Common `key=value` pairs** — `Server=localhost`, `Database=mydb` in DSN strings → no mask

## Knowledge Base (documents / chunks) and the Ingestor

Beside the memory store sits a document knowledge base (`kb_documents` + `kb_chunks`), searched from the console's knowledge base page. It is a separate store from `ks_fragment` — same PG database, different tables.

**Domain split by `source_type`** — who may write what:

| `source_type` | Owner | Console behaviour |
|---------------|-------|-------------------|
| `manual` | the console | Create / edit / delete freely. `source_key` is derived from the content hash, so re-importing identical content is idempotent rather than duplicating. |
| `skill` / `skill_hk` | the nightly ingestor | **HTTP 409** on any write — an edit would be overwritten by the next ingest run, and a delete would be undone when the skill reappears. The UI renders these read-only (a badge instead of the write buttons). |

**Chunking is single-sourced.** "How a document is cut into chunks, and how it lands idempotently" exists exactly once, in `keepsake.kb.ingest`: `split_chunks`, `upsert_document`, `content_hash`, `replace_chunks`, `update_document`. The console write endpoints and the ingestor CLI both import it, and a test asserts they are the *same* function object — otherwise the console would write documents the ingestor re-cuts at a different granularity, and search hits would disagree with what the page shows. Editing a body re-chunks **inside the same transaction** (delete old + insert new), so a document can never be left holding chunks that contradict its body. `MANUAL_SOURCE_TYPE` is exported from that same package, and every manual-domain check reads the constant rather than repeating the literal.

**The ingestor** — `scripts/kb_ingest.py` reads the source skill-document store and writes `documents` / `chunks`. Its behaviour is field-by-field equivalent to the version it absorbed (verified on a production database with both versions run in `--dry`). Credentials come only from environment variables or a password-file path — no plaintext in the source. Dry run first:

```bash
python3 scripts/kb_ingest.py --dry     # counts only, rolls back at the end
python3 scripts/kb_ingest.py           # real run
```

It also prunes dead documents: `skill` / `skill_hk` entries whose `(source_type, meta.node, meta.skill)` is no longer in the live set. `manual` is never touched — that domain belongs to the console. The production PG node's cron already points at this script.

**Console endpoints**: document list (paged, free-text and `source_type` filters, and `source_type` always present in the response so the UI knows which entries are writable), create / update (re-chunking) / delete (confirm = the document's full title; snapshot + backup written before the delete), JSONL import (dry-run preview, real run requires typing the `would_add` count), and JSONL export.

## Generic Memory Interface & MCP

`keepsake.adapters.generic.protocol.MemoryProvider` is a runtime-checkable `typing.Protocol` describing what a memory system exposes to its host. The module imports nothing host-specific — it describes the shape, not the caller — and the dependency direction is strictly `adapters.hermes → adapters.generic`.

Failure semantics are pinned per tier, so a new backend cannot invent its own:

| Tier | Methods | On backend failure |
|------|---------|--------------------|
| Injection | `system_prompt_block()`, `prefetch(query)` | **fail-open** — swallow the error, degrade to static text / empty string, at most a `logger.warning`. These sit on the critical path of every turn; an exception here fails the whole conversation, and memory is an enhancement, not a dependency. |
| Retrieval | `search(query, limit)`, `get(key)` | **raise.** A silent `[]` is the most dangerous failure shape: memories silently unfindable, no warning anywhere, and the user just concludes "the AI forgot". |
| Write | `add(text, tags)`, `update(key, …)`, `remove(target)` | **fail-closed** — report failure explicitly (falsy / raise / `status=error`), never pretend to have succeeded. A swallowed write failure has the caller go on to tell the user "saved", which is a lie. |

Plus the maintenance tier — `merge(dry_run=True)` and `forget(dry_run=True)`, both dry-run by default — and `capabilities()` for capability declaration. Note `get(key)` returning `None` means "genuinely absent", not "could not be checked".

### Three lines to any host

```python
from keepsake.mcp_server.server import build_server
server = build_server(MyMemoryProvider())   # implements the protocol above
server.run("stdio")                         # any MCP client connects and is done
```

`build_server(provider)` registers 8 tools — `memory_capabilities`, `memory_search`, `memory_get`, `memory_core_list`, `memory_add`, `memory_remove`, `memory_merge`, `memory_forget` — plus the resources `memory://core` (the resident-layer text, returned verbatim as the same rendering injected every turn) and `memory://fragment/{key}`. Called with no argument it assembles keepsake's own provider (`factory.default_provider()`, connecting per `KEEPSAKE_CONFIG` / `~/.config/keepsake/config.json`). The MCP write tools share the provider's write gate, so the governance rules below apply to MCP callers too.

**Transport.** `stdio` is the default. `http` (streamable-http) listens on `127.0.0.1` only — a non-loopback bind address exits with code 2 rather than starting. The memory store has no authentication of its own, so exposing it on the network is equivalent to publishing a person's long-term memory; put a reverse proxy in front of it instead. CLI: `python -m keepsake.mcp_server [--transport stdio|http] [--host …] [--port …]`.

## Multi-Agent Memory Governance

Four gates, all live in production:

| Gate | Behaviour | Config key | Effective layer |
|------|-----------|------------|-----------------|
| **A — non-primary may read `core`** | A sub-agent recalls the resident layer instead of only its own private entries; it still cannot write there | `is_primary` | storage filter — the BM25 path on every backend; PG and SQLite share a single `_search_filter_sql` exit covering both BM25 and KNN |
| **B — writes to `core` are downgraded** | A non-primary agent that writes the `core` tag has it rewritten to `core_candidate` (its other tags are kept) and gets a warning | — | `provider._apply_write_gate`, on `provider.add()`; the MCP `memory_add` path inherits it |
| **C — console candidate review queue** | The console's memory maintenance page lists `core_candidate` entries with paging. Approve = promote into `core` and drop the candidate tag (in that order — reversing it can lose the candidate outright); reject = drop the candidate tag only, keeping the entry in the retrieval layer | — | console |
| **D — main-brain noise filter** | `retrieval.exclude_tags` drops tagged entries at the retrieval exit. The default keeps `core_candidate` out of the main brain's own recall — a pending application must not double as a fact; an empty string turns the gate off | `retrieval.exclude_tags` | the single `search` / `prefetch` exit, applied **before** truncation |

Gate D is resolved once in `initialize()`, so **changing it requires a restart**; the filter itself runs per query over that frozen set.

`core_candidate` therefore means "a pending application to enter the resident layer", not a tag an LLM may invent. The reserved-tag judgement has exactly one implementation — `sanitize_reserved_tags()` in `core/shared.py` — shared by the distill cron (which strips reserved tags and logs them) and the console create/import paths (which return 400 on `core`). Reserved set: the exact tags `core`, `core_candidate`, `shared`, plus anything prefixed `agent:`. Matching is exact on the comma boundary, so `core_x` is not reserved.

## Verification

Check logs after startup:

```
Memory provider 'keepsake' registered (0 tools)
keepsake: connected (session=xxx, top_k=5, tag_filter=(none))
keepsake: embedder enabled (openai, dim=1024)      # KNN active
# OR: keepsake: BM25-only mode (no embedder configured)
keepsake: auto-registered cron job 'memory-maintenance'
keepsake: auto-registered cron job 'synonym-discovery-daily'
keepsake: auto-registered cron job '记忆去重'
```

## Semantic Search Upgrade (2026-09)

The optional semantic layer was reworked around three principles:

1. **No silent defaults on the embedding side.** Model dimensions must be registered in `_MODEL_DIMENSIONS`; unknown models now fail *visibly* (embedder marked unregistered → `storage._embed_enabled=False` → BM25-only, with an actionable log line). A live index whose DIM differs from the configured embedder disables vector writes instead of accumulating `hash_indexing_failures`. Switching to a new model = purge `embed_bin` + `embed_cache:*`, `FT.DROPINDEX` (no DD), recreate with the new DIM, backfill existing entries (`scripts/backfill_embeddings.py`; note `--limit` defaults to 500 — raise it for full backfills). Self-hosted Ollama works out of the box via the OpenAI-compatible endpoint (e.g. `bge-m3`, 1024-dim).
2. **LLM channels are config-only.** The `llm` section in `config.json` is the single source of truth for both the v2 write pipeline and query expansion — hard-coded provider fallbacks were removed (missing config = feature off, never a surprise paid call). Changes hot-reload at the next pipeline drain window via mtime detection.
3. **Write-side data hygiene.** The R1 gate rejects system-injected texts (compaction summaries, gateway-restart notes) before they pollute recall; the hot-topic pipeline drops jieba ASCII fragments (CJK-only gate on the jieba branch) and English function words while deliberately *keeping* technical tokens (`api`, `ssh`, `log`…); `scripts/cleanup_hot_topics.py` scrubs existing pollution (dry-run by default).

Measured effect on the 30-query regression suite (`scripts/eval_spotcheck.py`): BM25-only 67% → BM25 + KNN (bge-m3, RRF fusion) 73%.

## Project Structure

```
keepsake/
├── src/keepsake/         # Python package — the memory provider
│   ├── domain/           # Types and contracts (Fragment / Hit / Capabilities)
│   ├── core/             # Storage backends and the shared judgement chains
│   ├── adapters/
│   │   ├── generic/      # Host-agnostic MemoryProvider contract
│   │   └── hermes/       # Hermes plugin provider
│   ├── kb/               # Knowledge-base write shared layer (chunking / idempotent upsert)
│   ├── mcp_server/       # Generic MCP entrypoint — build_server(provider)
│   └── api/              # Console backend (routers, auth, audit)
├── web/                  # Console frontend (Vue 3 + Vite)
├── hermes-plugin/        # Hermes plugin wrapper (drop-in for ~/.hermes/plugins/)
│   ├── plugin.yaml
│   └── __init__.py
├── cron/                 # Cron wrapper scripts for scheduled tasks
│   ├── memory-maintenance.py   # Every 2h — selective forgetting (consolidation is ops-invoked)
│   ├── memory_distill.py       # Distill recent turns into one-line entries (strips reserved tags)
│   ├── dedup-memory.sh         # Every 1h — deduplication
│   └── discover-synonyms.py    # Every 8h — synonym auto-discovery
├── scripts/              # Standalone utility scripts (dev/test), incl. the knowledge-base ingestor kb_ingest.py
├── README.md
└── pyproject.toml
```

The three cron jobs in `cron/` are **auto-registered** when the keepsake plugin initializes (on `/new` or gateway restart). No manual `hermes cron create` needed.

## v2 Two-Phase Pipeline (2026-09)

每轮对话走两相 LLM：**提取相**从窗口提炼长期事实（废话/纯确认自然无事实可提而消亡），**更新相**对每条 fact 拿 top-5 相似旧记忆做 ADD / UPDATE / DELETE / NOOP —— UPDATE/DELETE 给旧碎片只打 `superseded_by` 封边不物理删，检索侧统一排除；窗口内 LLM 调用硬顶 `llm_pipeline.max_calls_per_window`（默认 8），任一 LLM/JSON 失败或超预算 → 窗口整体回落 v1 规则闸门直存原文，**绝不静默丢消息**。配置键：`llm_pipeline.{enabled, model, window_pairs, window_seconds, max_calls_per_window, update_top_k, recent_context_size}`、`v2_min_score`。

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

## Consolidator Retirement (2026-09-09)

**退役的只是 provider 的运行期接线，不是这个能力。** `Consolidator`（基于 jieba 关键词聚类 + LLM 多级缝合的离线合并引擎）的类源码完整保留在 `src/keepsake/consolidator.py`，在 **Redis 与 PostgreSQL 两个后端上都能跑**（它只通过[四个后端无关原语](#maintenance-capabilities-and-the-four-backend-agnostic-primitives)访问存储）。provider 的 `initialize()` 不再构造它、`maintenance()` 不再触发合并循环。

**为什么摘掉接线：**
- 输出质量差：LLM 多级合并产生的「无时态散文」与 v2 封边链（`superseded_by`）互不相认 —— 旧合并产物会被 v2 检索侧的封边过滤误伤或绕过。
- 职责重叠：v2 两相管线的「提取相」窗口级事实提炼 +「更新相」supersede 封边，已全面覆盖 Consolidator 的碎片提纯职能。
- 维护成本：双管线并存需要为同一份碎片库维护两套并查链路，得不偿失。

**替代者：** `pipeline.py`（v2 两相管线）。每个 turn 窗口触发提取相 → 更新相，原地 supersede 旧碎片；运行时无独立合并 cron 任务。

**怎么用合并：** 运维脚本 / cron **每轮现构造** —— `Consolidator(storage=storage_from_config(...), config=cfg).consolidate(dry_run=False)`。因为是每轮现读 config，改 `consolidate_min_overlap` / `consolidate_min_group` **下一轮即生效，无需重启**。阈值说明见 [Storage Backends](#storage-backends-redis--postgresql--sqlite) → "Consolidation thresholds"，dry-run 用法见 [自检清单](#post-switch-self-check-checklist)。

**运行时观测：** `maintenance()` 返回 stats 仍保留 `consolidator` 字段（`{"status": "retired", "reason": "v2 pipeline takeover"}`），便于 cron 探针 / 健康检查观测退役状态而非误报 missing-key。

## Architecture

```
┌────────────────────────────────────────────────────────┐
│                    User sends message                  │
└──────────────────┬─────────────────────────────────────┘
                   │
         ┌─────────▼─────────┐
         │   prefetch()       │  ← Automatically triggered on every user message
         │   ↓                │
         │  Workflow Lock?    │  ← Checks keepsake:workflow_lock
         │   ↓                │
         │  Skip patterns?    │  ← Length / exact match against skip list
         │   ↓                │
         │  BM25 Full-Text Search │  ← Default, zero cost, searches full entries
         │  (KNN Vector search) │  ← Optional (needs embedder)
         │  Entity co-occurrence │  ← Expand query with co-occurring entities
         │   ↓                │
         │  Six-dimensional Re-ranking │  ← Similarity × Time decay
         │                    │    × Emotion × Feedback × Hot Topic × Attention
         │   ↓                │
         │  Top N Injected into Context   │  ← Full entries returned as-is
         └─────────┬─────────┘
                   │
         ┌─────────▼─────────┐
         │   Model Response   │  ← Entries used directly (complete text)
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   on_memory_write()│  ← Only on memory(action='add')
         │   Stores Full Text │  ← Complete entry, no splitting
         │   Entity Extraction│  ← jieba + regex → entities TAG field
         │   Entity Co-occur. │  ← Track entity pairs in ZSET
         │   Attention Track  │  ← Extract keywords, increase attention score
         │   ↓                │
         │   Stored in Redis  │  ← Available for next retrieval as full text
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   [cron] Every 2h     │  ← Background maintenance
         │   ① Selective Forgetting  │  ← Age>30d + no feedback + low emotion + low attention → delete
         │   (consolidation is ops-invoked — see section above)
         └───────────────────┘
```

## Frozen Scoring Clock

`scoring_now()` / `scoring_now_ts()` / `frozen_scoring_now()` in `storage_shared.py` give the scoring path one place to read the current time from. **By default they return the real wall clock — behaviour is byte-for-byte identical to before the functions existed, i.e. zero behaviour change.**

Inside a `with frozen_scoring_now():` block the clock is pinned; it restores the real wall clock on exit, and nesting inherits the outer pin rather than silently jumping back:

```python
from keepsake.storage_shared import frozen_scoring_now

with frozen_scoring_now():
    ...   # every rerank and hot-topic decay in this block uses the same instant
```

**Only the scoring side reads it** — the time decay in `rerank_with_decay` and the hot-word decay in `match_hot_topics` (SQLite / PG). 🔴 The **write path does not**: `store()` writing `created`, and timeline timestamps, never read it — freezing write time would corrupt the data.

**Why it exists:** those decays are evaluated at query time, so the same row scores differently on two consecutive queries. Two rows with identical `bm25`/`sim` differing only by a ~1e-7 decay term **cross over within 1.5 seconds** (measured: round 0 led by 5.2e-8, round 1 flipped by 1.3e-8), which made "same query, 3 runs, identical key sequence" fail intermittently. `scripts/recall_eval.py` wraps a whole run in one freeze, so a given question in a given order reproduces exactly (`33727f2`).

## Core Tag — the Resident Memory Tier

Keepsake carries resident memory under the **`core` tag**, making the fragment store the single source of truth for memory. On the Hermes side, `memory.builtin_mode=pointer` means `MEMORY.md` / `USER.md` keep only a pointer — they no longer read, write or inject. Writes go through `on_memory_write()`, which stores the content **verbatim** into `core` (`3a1e43f`).

## License

MIT
