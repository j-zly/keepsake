"""文本切分工具 — 按语义完整性（段落 → 句子）切分文本。"""

from __future__ import annotations

import re
from typing import List

NEWLINE = "\n"


def split_text(text: str, max_chars: int = 500) -> List[str]:
    """按语义完整性切分文本。

    策略：
      1. 先按段落（连续空行）切分
      2. 超长段落按句子边界（中英文句号/感叹号/问号）切分
      3. 短碎片（<10 字）丢弃

    参数:
        text: 要切分的文本
        max_chars: 单个碎片最大字符数

    返回:
        切分后的文本列表
    """
    if not text or not text.strip():
        return []

    raw_paras = re.split(r"\n\s*\n", text.strip())
    segments: List[str] = []

    for para in raw_paras:
        para = para.strip()
        if not para:
            continue

        if len(para) <= max_chars:
            segments.append(para)
            continue

        # 超长段落：按句子边界切
        sentences = re.split(r"(?<=[。！？.!?\n])", para)
        chunk = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(chunk) + len(s) > max_chars and chunk:
                segments.append(chunk.strip())
                chunk = s
            else:
                chunk += s + NEWLINE
        if chunk.strip():
            segments.append(chunk.strip())

    return [s for s in segments if len(s) >= 10]
