-- =============================================================================
-- keepsake 记忆契约 v2 —— 演化链 + 适用条件（2026-10 keepsake_contract_v2）
-- =============================================================================
--
-- 🔴 **执行者：主脑。本文件只写不跑**（任务书红线）。
--    代码里**不**擅自 ALTER 生产表 —— 与 `api/routers/kb.py` 的
--    `_CTYPE_MIGRATION_HINT` 同一纪律：迁移是运维的动作，不是库代码的。
--
-- 幂等：四条全是 `ADD COLUMN IF NOT EXISTS`，重复跑是 no-op。
--
-- ## 为什么这四列
--
--   原来「顶掉」只有**单向**的 `superseded_by`：能说「这条被谁顶了」，
--   不能说「这条顶了谁」，检索侧也只能逐条剔除、排不出链身。
--   上游 Hy-Memory 的模型对应这四根列（models/memory.py 的
--   supersedes/superseded_by/is_latest 双向链 + coding/extractor.py 的
--   applicability），本单移植的就是这层**契约**（调和提示词协议是下一单）。
--
--   is_latest         链头标记：'1' = 链头（当前有效）/ '0' = 链身（已被顶掉）。
--                     **老行为 NULL/未知一律按 '1'（链头）** —— 猜成链身会把
--                     存量记忆整批从检索里抹掉，方向只能宁多不少。
--   supersedes        反向链：JSON 数组，存「本条顶掉了哪些旧 key」。
--                     ⚠️ 契约 v2 之前这一列存的是**裸 key 串**（不是数组）。
--                     存储层 `append_supersedes` 读时容错（解不开当单元素列表），
--                     下次写回即自愈，**本脚本不需要也不该去改老行的值**。
--   supersede_reason  作废原因（调用方给的，没有就是空串）。
--   applicability     适用条件，自由文本多行 "key: value"。环境无关的记忆留空。
--
-- ## 与自动补列的关系
--
-- `PgStorage.ensure_index()` 也会自动补这四列（`_contract_v2_column_ddl`，
-- 与 level/consumed_by 同款「先查后补」）。本脚本存在的理由是：
-- ensure_index 只在 provider 启动 / 显式调用时跑，且它连三次重试都失败时
-- 返回 False（不抛错）。**显式跑一次这个脚本 = 把「补列」从启动期的运气
-- 变成一条确定会发生的事**，也顺手绕开 ACCESS EXCLUSIVE 与并发 DDL 互等的窗口。
--
-- ## 执行顺序
--
--   1. 先跑本文件（纯 ADD COLUMN，秒级；四个 DDL 放一个事务，全成或全不成）
--   2. 再确认 SELECT 出来是 '1'：
--        SELECT is_latest, count(*) FROM ks_fragment GROUP BY 1;
--      期望只有 '1'（存量行被 DEFAULT 回填成链头）。
--   3. 之后才发版应用 —— 应用侧检索过滤是「读 is_latest」，
--      列没在就已经在按「列不存在 ⇒ 不加该过滤」降级跑着（不会 500），
--      但先补列再发版可以完全避开这个中间态。
--   4. Redis 后端**无需迁移**（hash 加字段，天然向后兼容）。
--      但要注意：已在跑的进程索引里没有 applicability/is_latest 两根字段，
--      重启后由 `ensure_index` 的 FT.ALTER 补上（失败不致命，只少两个可选字段）。
--
-- ## 刻意不建索引
--
-- `is_latest` 的判据写在检索 WHERE 里，与 `invalid_at` / `valid_until` 同款 ——
-- 那是**过滤**不是**点查**，给它建 GIN/BTREE 索引是本项目最典型的
-- 「造没人用的数据」：过滤掉的行本来就少，索引选择性极低，
-- 而每次写入都要多维护一棵 B 树。真要等它成为瓶颈（先有 EXPLAIN 证据）再加。

BEGIN;

ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS is_latest        text NOT NULL DEFAULT '1';
ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS supersedes       text NOT NULL DEFAULT '';
ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS supersede_reason text NOT NULL DEFAULT '';
ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS applicability    text NOT NULL DEFAULT '';

COMMIT;

-- 事后核对（只读，不改数据）：
--   SELECT is_latest, count(*) FROM ks_fragment GROUP BY 1;
--   期望：只有 ('1', <总行数>) 一行。
