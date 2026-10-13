"""adapters 层：对外适配器（Hermes 插件适配 + 通用记忆契约）。

依赖方向单向：adapters → core → domain；adapters 之间只允许
`hermes → generic`（hermes 侧实现 generic 契约，反向不许）。
"""
