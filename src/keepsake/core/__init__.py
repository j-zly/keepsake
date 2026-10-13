"""core 层：存储与检索实现（Redis / PG / SQLite 三后端 + 共用重排 + 维护算子）。

依赖方向单向：core → domain；core 不许反向依赖 adapters。
"""
