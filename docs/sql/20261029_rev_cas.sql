-- =============================================================================
-- keepsake 记忆乐观并发版本号 rev（2026-10 ks_memory_rev_cas）
-- =============================================================================
--
-- 🔴 **执行者：主脑。本文件只写不跑**（任务书红线：不在代码里 ALTER 生产表）。
--    与 `20261018_contract_v2.sql` 同一纪律：迁移是运维的动作，不是库代码的。
--
-- 幂等：一条 `ADD COLUMN IF NOT EXISTS`，重复跑是 no-op。
--
-- ## 为什么加这一列
--
--   共享记忆被多个代理读写，此前**没有并发控制**：两个代理基于同一版各自改写
--   ⇒ key = 内容哈希 ⇒ 各自存一条 ⇒ **互相看不见对方**（内容分叉：检索时两条
--   都命中，代理却以为自己改成功了）。
--
--   rev = 乐观并发（HTTP ETag / If-Match、CouchDB _rev 的思路）：
--     * 读：返回值里带 rev；
--     * 写：声明 expected_rev，对不上 ⇒ **拒绝并回最新内容**（不覆盖）。
--
--   为什么不用悲观锁：共享持有锁会引出「孤儿锁 / TTL / 续期 / 崩溃释放」一整套
--   子系统，而代理随时可能被杀掉 ⇒ 锁会跟着进坟墓。乐观并发只多存一列。
--
-- ## 列语义
--
--   rev  text NOT NULL DEFAULT '1'
--     文本而不是 int：版本号只做**相等**比对（不做大小比较），文本形态允许
--     未来扩展（如 '2.1'），也让「存量行 = '1'」这件事一眼可读。
--     存量行 ALTER 补列时统一回填 '1' ⇒ **迁移对调用方零感知**（=「没改过」）。
--
-- ## 与自动补列的关系
--
--   `PgStorage.ensure_index()` 也会自动补这一列（`_rev_column_ddl`，与
--   level/consumed_by、契约 v2 四列同款「先查后补」）。本脚本存在的理由与
--   contract_v2 那份完全一样：ensure_index 只在 provider 启动 / 显式调用时跑，
--   且连三次重试都失败时返回 False（不抛错）。**显式跑一次 = 把「补列」从
--   启动期的运气变成一条确定会发生的事**，也顺手绕开 ACCESS EXCLUSIVE 与
--   并发 DDL 互等的窗口。
--
--   SQLite 后端**无需本脚本**：`SqliteStorage.ensure_index()` 自己会补列
--   （`PRAGMA table_info` 先查后补），且读写路径有 `_require_ready` 守卫。
--   Redis 后端**天然兼容**（hash 加字段；且 Redis 侧明确**不支持** rev CAS，
--   `cas_update` / `cas_remove` 直接抛 `backend_unsupported`，不静默降级）。
--
-- ## 执行顺序
--
--   1. 先跑本文件（纯 ADD COLUMN，秒级）
--   2. 再确认存量行都是 '1'：
--        SELECT rev, count(*) FROM ks_fragment GROUP BY 1;
--      期望只有 ('1', <总行数>) 一行。
--   3. 之后才发版应用 —— 应用侧读路径对**缺列**是容错的
--      （`PgStorage._read_columns` 会剔掉 rev，出参按 '1' 兜），
--      但 CAS 写路径会明确报「缺列 + 迁移 SQL」，先补列再发版更干净。
--
-- ## 刻意不建索引
--
--   rev 的判据是「按主键点查后比对」⇒ 主键索引已经覆盖。专门给 rev 建索引是
--   本项目最典型的「造没人用的数据」。

BEGIN;

ALTER TABLE ks_fragment ADD COLUMN IF NOT EXISTS rev text NOT NULL DEFAULT '1';

COMMIT;

-- 事后核对（只读，不改数据）：
--   SELECT rev, count(*) FROM ks_fragment GROUP BY 1;
--   期望：只有 ('1', <总行数>) 一行。
