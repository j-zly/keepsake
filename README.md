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
| `embedder.provider` | `FRAGMENTED_EMBEDDER` | `openai` | `openai` / `dashscope` |
| `embedder.api_key` | `OPENAI_API_KEY` | — | Embedding API 密钥 |
| `embedder.base_url` | `FRAGMENTED_EMBEDDER_URL` | `https://api.openai.com/v1` | API 端点 |
| `embedder.model` | `FRAGMENTED_EMBEDDER_MODEL` | `text-embedding-3-small` | 嵌入模型名 |

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
         │  五维重排序        │  ← 相似度 × 时间衰减
         │                    │    × 情感 × 反馈 × 热门话题
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
         │   ↓                │
         │   存入 Redis        │  ← 下次可被检索
         └───────────────────┘
```

## 协议

MIT
