#!/bin/bash
# 记忆去重 — 每 1h 执行
cd /opt/fragmented-memory && python3 scripts/dedup_memory.py --threshold 0.65 --batch-size 500
