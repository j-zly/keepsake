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
| ✂️ **语义切分** | 对话内容按段落/句子边界自动拆分为独立碎片 |
| 🔍 **向量搜索** | RediSearch KNN 语义检索，不止关键词匹配 |
| ⏳ **时间衰减** | 碎片按时间降权，旧记忆权重逐步降低（60天半衰期） |
| 🔄 **自动写入** | `memory()` 操作和对话轮次自动存档，无需手动管理 |
| ☁️ **零外部依赖** | 除嵌入模型 API 外无其他外部服务 |

## 依赖

- **Python 3.10+**
- **Hermes Agent 0.12+** — 提供 `MemoryProvider` 接口
- **Redis 7+** — 带 RediSearch 模块（v2.6+）
- **Embedding API** — OpenAI / DashScope / 任意兼容 `/v1/embeddings` 的服务

## 安装

```bash
pip install fragmented-memory
```

或者从 GitHub 直装：

```bash
pip install git+https://github.com/j-zly/fragmented-memory.git
```

## 配置

### 1. 创建 Redis Index（首次使用）

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

> 如果你用 Docker：`docker run -d --name redis-stack -p 6379:6379 redis/redis-stack:latest`

### 2. Hermes 配置

在 `~/.hermes/config.yaml` 中：

```yaml
memory:
  provider: fragmented
  fragmented:
    redis_host: 127.0.0.1
    redis_port: 6379
    embedder:
      provider: openai            # openai | dashscope
      api_key: sk-xxx             # 或设环境变量 OPENAI_API_KEY
      model: text-embedding-3-small
```

也可以通过环境变量配置：

```bash
export OPENAI_API_KEY=*** FRAGMENTED_REDIS_HOST=127.0.0.1
export FRAGMENTED_REDIS_PORT=6379
export FRAGMENTED_EMBEDDER=openai
```

### 3. 重启 Gateway

```bash
# CLI 模式重启会话即可
# Gateway 模式需要重启进程
```

## 验证

启动后检查日志：

```
Memory provider 'fragmented' registered (0 tools)
fragmented: connected (session=xxx)
Memory provider 'fragmented' activated
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
         │  text → Embedding  │  ← 调 API 转向量
         │   ↓                │
         │  FT.SEARCH KNN     │  ← RediSearch 语义检索
         │   ↓                │
         │  时间衰减重排序     │  ← 新碎片优先
         │   ↓                │
         │  Top 5 注入上下文   │
         └─────────┬─────────┘
                   │
         ┌─────────▼─────────┐
         │   模型回复         │  ← 碎片可用作参考
         └───────────────────┘
                   │
         ┌─────────▼─────────┐
         │   sync_turn()      │  ← 对话结束自动存档
         │   段落切分          │
         │   ↓                │
         │   存入 Redis        │  ← 下次可被检索
         └───────────────────┘
```

## 配置参考

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|---------|--------|------|
| `redis_host` | `FRAGMENTED_REDIS_HOST` | `127.0.0.1` | Redis 地址 |
| `redis_port` | `FRAGMENTED_REDIS_PORT` | `6379` | Redis 端口 |
| `embedder.provider` | `FRAGMENTED_EMBEDDER` | `openai` | 嵌入 API 提供商 |
| `embedder.api_key` | `OPENAI_API_KEY` | — | API 密钥 |
| `embedder.base_url` | `FRAGMENTED_EMBEDDER_URL` | `https://api.openai.com/v1` | API 端点 |
| `embedder.model` | `FRAGMENTED_EMBEDDER_MODEL` | `text-embedding-3-small` | 嵌入模型名 |

### DashScope 用户

```yaml
embedder:
  provider: dashscope
  api_key: sk-xxx          # DashScope API Key
  # base_url 和 model 可省略，自动适配
```

## 协议

MIT
