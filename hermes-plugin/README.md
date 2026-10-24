# Keepsake — memory plugin for Hermes Agent

Keepsake stores **complete memories** (never fragments) and automatically
retrieves the relevant ones into your conversation context on every turn.
It works out of the box with pure keyword search and grows smarter from your
feedback.

```text
User: "How did we set up that project structure last time?"
        ↓  Keepsake (Redis / PostgreSQL / SQLite)
  [1] User prefers TypeScript + Vite
  [2] Previous projects used pinia state management
        ↓  model answers using recalled memories
```

## What it does

- **Full-entry storage** — every memory is stored as-is; nothing is chopped into meaningless fragments.
- **BM25 full-text recall** — works immediately, no external API required; Chinese and English tokenized symmetrically.
- **Optional vector search** — KNN via RediSearch with any OpenAI-compatible embedding endpoint or a fully local Ollama model; results fused with BM25 (RRF).
- **Time decay** — newer memories rank higher (configurable half-life).
- **Feedback ranking** — mark entries useful / useless and retrieval reorders accordingly.
- **Three storage backends** — Redis (default), PostgreSQL (pgvector-style KNN), SQLite (zero-infrastructure); identical read/write semantics.
- **Auto maintenance** — periodic low-value detection prunes noise; a review pipeline (with an optional LLM screener) distills raw turns into durable facts.
- **Multi-agent governance** — per-agent memory ownership, approval-based agent registry, tiered visibility (private / shared), and a persistent "core" layer with a budget per agent.
- **Knowledge base** — document store with agent-scoped CRUD tools alongside memories.
- **Secret scrubbing** — high-confidence credential shapes are redacted at write time.

## Install

```bash
pip install keepsake-memory            # or let `hermes plugins install keepsake` do it
```

Activate in Hermes `config.yaml`:

```yaml
memory:
  provider: keepsake
```

Restart the gateway.

## Configure

Main config file: `~/.config/keepsake/config.json` (override path with
`KEEPSAKE_CONFIG`; individual values via `KEEPSAKE_*` environment variables):

```json
{
  "redis_host": "127.0.0.1",
  "redis_port": 6379,
  "storage": { "backend": "redis" },
  "top_k": 8,
  "half_life_days": 60
}
```

Every field, the embedding-model registry, and the multi-agent setup are
documented in the full reference:
[README.md (English)](https://github.com/j-zly/keepsake#readme) ·
[README.zh.md (中文)](https://github.com/j-zly/keepsake/README.zh.md).

## Requirements

- Python ≥ 3.10, Hermes ≥ 0.21.
- A storage backend you run yourself: Redis with the RediSearch module
  (default), PostgreSQL ≥ 14, or SQLite (nothing to install).
- Vector search is optional — BM25 recall needs no external service at all.

## Privacy

Keepsake talks only to the endpoints **you** configure (your Redis/PostgreSQL,
your embedding API). The plugin has no hard-coded telemetry or third-party
service.
