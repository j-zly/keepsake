#!/usr/bin/env python3
"""细节修复 v2：把缺失 token 补到**最终存活的条目**上。

v1 的错：只补到 consumed_by 直接指向的那条，而那条自己可能又被 superseded 取代
⇒ token 补到了永远不出现在结果里的条目上（实测：补完仍「未进」）。

v2 目标解析：consumed_by → 反复跟 superseded_by（跳过 __void__ 视为终止）→ 最终条目。
用法: --dry-run（默认）/ --apply
"""
import json, logging, sys, time
sys.path.insert(0, '/opt/fragmented-memory/src')
logging.disable(logging.WARNING)
from keepsake.storage import storage_from_config
from keepsake.consolidator import _missing_concrete_tokens

APPLY = '--apply' in sys.argv
MARK = '关键细节（合并自原条目）'
st = storage_from_config()
cl = st._get_client()


def live_target(key, _seen=None):
    """沿 superseded_by / consumed_by 交替追到最终存活条目。

    只跟一种关系会停在「自己也已经 consumed」的中间条目上（实测踩过），
    ⇒ token 又补到不可见的条目上。
    """
    _seen = _seen or set()
    for _ in range(10):
        if not key or key in _seen:
            return key
        _seen.add(key)
        nxt = None
        for field in ('superseded_by', 'consumed_by'):
            v = cl.hget(key, field)
            v = v.decode() if isinstance(v, bytes) else v
            if v and v != '__void__':
                nxt = v
                break
        if not nxt:
            ft = cl.hget(key, 'fragment_type')
            ft = ft.decode() if isinstance(ft, bytes) else ft
            if ft == 'consumed':      # 没有后继的 consumed：不可见，跳过
                return None
            return key
        key = nxt
    return key


pairs, cur = [], 0
while True:
    cur, keys = cl.scan(cursor=cur, match='memory:frag:*', count=500)
    if keys:
        pipe = cl.pipeline()
        for k in keys:
            pipe.hmget(k, 'fragment_type', 'consumed_by')
        for k, (ft, cb) in zip(keys, pipe.execute()):
            ft = ft.decode() if isinstance(ft, bytes) else ft
            cb = cb.decode() if isinstance(cb, bytes) else cb
            if ft == 'consumed' and cb and cb != '__void__':
                pairs.append((k, cb))
    if cur == 0:
        break
print(f"① consumed 且有后继 = {len(pairs)} 条")

srcs = {}
for i in range(0, len(pairs), 200):
    chunk = pairs[i:i + 200]
    pipe = cl.pipeline()
    for k, _ in chunk:
        pipe.hget(k, 'content')
    for (k, _), c in zip(chunk, pipe.execute()):
        srcs[k] = c.decode('utf-8', 'replace') if c else ''

from collections import defaultdict
groups = defaultdict(list)
for k, cb in pairs:
    tgt = live_target(cb)
    if tgt:
        groups[tgt].append(k)

plan = {}
for tgt, srcs_keys in groups.items():
    summ = cl.hget(tgt, 'content')
    summ = summ.decode('utf-8', 'replace') if summ else ''
    if not summ:
        continue
    missing = []
    for k in srcs_keys:
        for t in _missing_concrete_tokens([srcs.get(k, '')], summ + ' ' + ' '.join(missing)):
            if t not in missing:
                missing.append(t)
    if missing:
        plan[tgt] = missing

print(f"② 需补的【最终存活条目】= {len(plan)} 条，共 {sum(len(v) for v in plan.values())} 个 token")
for tgt, miss in plan.items():
    ft = cl.hget(tgt, 'fragment_type')
    ft = ft.decode() if isinstance(ft, bytes) else ft
    print(f"   {tgt} (ft={ft}): {miss}")
print(f"   模式: {'APPLY' if APPLY else 'DRY-RUN'}")
if not APPLY:
    sys.exit(0)

snap = f'/root/.hermes/backups/ks_detail_repair_v2_{time.strftime("%m%d_%H%M")}.jsonl'
with open(snap, 'w', encoding='utf-8') as f:
    for tgt in plan:
        c = cl.hget(tgt, 'content')
        f.write(json.dumps({'key': tgt,
                            'content': c.decode('utf-8', 'replace') if c else ''},
                           ensure_ascii=False) + '\n')
print(f"③ 快照: {snap}")
n = 0
for tgt, miss in plan.items():
    old = cl.hget(tgt, 'content')
    old = old.decode('utf-8', 'replace') if old else ''
    if MARK in old:
        continue
    cl.hset(tgt, 'content', old.rstrip() + '\n\n' + MARK + '：' + '；'.join(miss))
    n += 1
print(f"④ 已补写 {n} 条")
