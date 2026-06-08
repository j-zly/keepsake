# 碎片记忆插件按需检索功能实现总结

## 实现内容

完成了碎片记忆插件的按需检索功能，使得插件在简单确认/问候时不会触发检索，避免噪音碎片污染上下文。

### 主要改动

1. **新增 `_should_search()` 方法**：
   - 判断当前用户消息是否需要检索碎片
   - 跳过条件：
     - 长度 < skip_min_length（默认 2）
     - query 精确匹配外部文件中的 skip pattern（忽略大小写）

2. **修改 `prefetch()` 方法**：
   - 在开头加入门控逻辑：`if not self._should_search(query): return ""`
   - 保持原有逻辑不变

3. **配置加载增强**：
   - `_resolve_config()` 方法中加载 skip patterns 配置
   - 支持 skip_min_length 和 skip_patterns_file 配置项
   - skip_patterns_file 文件不存在时不报错

4. **README.md 更新**：
   - 添加 skip_patterns_file 和 skip_min_length 的配置说明
   - 更新环境变量和配置参考部分

### 功能验证

- ✅ `prefetch()` 在 query="好"、"可以"、"嗯"、"ok" 等确认词时返回空
- ✅ `prefetch()` 在 query="分析BTC"、"Redis密码"、"删了" 等正常召回
- ✅ config.json 配置 skip_patterns_file 后，读取外部文件生效
- ✅ 文件不存在时不报错，不做过滤
- ✅ 已推送到 GitHub

所有变更均已通过语法检查，代码符合项目规范。