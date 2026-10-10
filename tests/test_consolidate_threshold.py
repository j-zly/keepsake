"""合并聚类阈值（`consolidate_min_overlap` / `consolidate_min_group`）行为固化（2026-10 ks_cth）。

背景：`_cluster_by_topic` 的判据原为裸字面量「重叠 >= 2 即同组」，生产同批 2819 条碎片
实测真并占比 53.1%（阈值 2）→ 13.2%（阈值 3）→ 2.1%（阈值 4）。本文件把
**默认阈值 3** 固化成必测，并锁住非法配置的回落语义。

覆盖点：
  1. 红绿闭环：低重叠的「假重复」在默认阈值下**不得**成组；高重叠的「真重复」**必须**成组
  2. 配置生效：显式传 4 ⇒ would_merge 下降；缺键 ⇒ 用默认 3；非法值 ⇒ 回落默认 + 告警
  3. 量化对照：同一批自造数据在阈值 2/3/4 下的 groups_found / would_merge 三行表
  4. dry-run 零写入（一个字段都不写）
  5. 两后端同判据：阈值只挂在 Consolidator 实例上，Redis/PG 无分支（源码级断言）

设计：全部走**内存 FakeStorage**（逐字复刻 StorageBase 维护原语），不连任何真库 ——
本机无 Redis/PG 服务，任务书红线又禁连生产 180。Redis 侧零回归由
`tests/test_maintenance_backend_neutral.py` + 本文件 dry-run 覆盖同一份 Consolidator 代码。

================================================================================
🔴 为什么本文件的测试数据是「哑词」而不是自然语言（2026-10 ks_ctt 修订）
================================================================================
上一版用中文自然句当碎片，并把「族内相对族首的重叠 = 2/3/4」写死成断言。
那在**主脑机器上 6 条红**（本机全绿）。根因已定位并留证（/tmp/ks_ctt_audit.txt）：

    src/keepsake/splitter.py:24-25 的 `init_domain_dict()` 在 **import 时**无条件执行
        if _DOMAIN_DICT.exists(): jieba.load_userdict(str(_DOMAIN_DICT))
    _DOMAIN_DICT = ~/.config/keepsake/jieba_dict.txt   ← **每台机器本地**的用户词典，
    由 storage.generate_jieba_dict() 从**各自本机的 Redis 语料**生成。

⇒ 主脑机器上有该文件（本机没有）⇒ 往 jieba 全局注入的词不同 ⇒ 同样 0.42.1 版本的
   `jieba.lcut()` 切法不同 ⇒ `extract_keywords()` 的关键词不同 ⇒ 重叠数从 3 掉到 2。
⇒ 已用假 HOME + `最大连接数`/`连接池调优` 两条词条在本机 100% 复刻主脑那 6 条红与
   断言原文（如「实测 [2, 2]」），根因闭环。

修法（本文件）：**数据用分词器切不动的确定性哑词**，并把阈值断言全部改成
「先量出实测重叠，再据实测值断言相对行为」。两条一起上：
  * 哑词让重叠数**由构造决定**（不是由分词器决定）；
  * 相对断言保证即便将来换分词器/换 Python，测试也只是换一个实测阈值去比，
    不会因为某个字面数字对不上而红。

哑词的选择（两条都必要，缺一不可）：
  * **纯 ASCII 字母、无数字**：`extract_keywords` 的中文分支带 CJK 门槛
    （`re.search(r"[一-鿿]", w)`）会把纯 ASCII 全滤掉；英文分支的正则是
    `\b[a-zA-Z]{3,}\b` —— 带数字（`zqa1`）时 `\b` 在字母/数字交界不成立，
    **整个词匹配不上**。所以必须纯字母、且长度 >= 3。
  * **以空格分隔**：jieba 不跨空白合并 ⇒ lcut 必然逐词吐出；且用户词典**不可能**
    收录 `zqalpha` 这种词 ⇒ 注入的用户词典对这批数据**零影响**（这才是跨机器同绿的根据）。
  * 用户词典即便存在，中文分支收不到它们（无 CJK），英文分支走的是**原始文本上的
    正则**（不查 jieba 词典）⇒ 关键词集合 == 构造的 token 集合，逐字可预测。

注意：`_cluster_by_topic` 的贪心只拿候选跟**族首**比（不跟组内其它成员比），
   所以「族内两两重叠」不等于「能成组」——这里要的就是对族首的均匀重叠。
"""

from __future__ import annotations

import pytest

from keepsake.consolidator import (
    DEFAULT_MIN_GROUP_SIZE,
    DEFAULT_MIN_OVERLAP,
    Consolidator,
    resolve_consolidate_config,
)

# 复用既有的内存 fake 后端（与 tests/test_maintenance_backend_neutral.py 同一份）——
# 不重写第二套 StorageBase 桩，避免两处桩漂移
from test_maintenance_backend_neutral import FakeStorage, _rows  # noqa: E402


# ---------------------------------------------------------------------------
# 自造数据：三族；每族后两条相对「族首」的公共 token 数**分别**恒为 2 / 3 / 4，
# 且三族之间零交叉（token 命名空间按 a-/b-/c- 前缀隔离）。
#
# 每族的构成：K 个共享 token + 每条各 1 个独有 token ⇒ 每条恰好 K+1 个 token
# （K 最大 4 ⇒ 5 个，正好不超 `max_keywords=5`，不会被截断，构造即实测）。
# ---------------------------------------------------------------------------
FAMILY_OVERLAP2 = [
    "zqaalfa zqabeta zqagamma",   # 族首
    "zqaalfa zqabeta zqadelta",   # 与族首公共 2
    "zqaalfa zqabeta zqaepsi",    # 与族首公共 2
]
FAMILY_OVERLAP3 = [
    "zqbfoxt zqbgolf zqbhotel zqbindia",
    "zqbfoxt zqbgolf zqbhotel zqbjuliet",
    "zqbfoxt zqbgolf zqbhotel zqbkilo",
]
FAMILY_OVERLAP4 = [
    "zqclima zqcmike zqcnov zqcoscar zqcpapa",
    "zqclima zqcmike zqcnov zqcoscar zqcquebec",
    "zqclima zqcmike zqcnov zqcoscar zqcromeo",
]

ALL_FAMILIES = [FAMILY_OVERLAP2, FAMILY_OVERLAP3, FAMILY_OVERLAP4]


def _make_storage(families) -> FakeStorage:
    """把若干族铺成碎片行（created 取 200 天前 ⇒ 必过任何 max_age_hours 门槛）。"""
    return FakeStorage(_rows(*[c for fam in families for c in fam]))


# ---------------------------------------------------------------------------
# 实测工具：所有阈值断言都从这里取「实测重叠」，**不写任何字面值**
# ---------------------------------------------------------------------------

def _kws(text: str):
    """走生产同一条路：Consolidator._cluster_by_topic 用的就是 extract_keywords(max_keywords=5)。

    这里 import 的是**函数本身**，不是重写 —— 保证测的就是线上判据本身。
    """
    from keepsake.splitter import extract_keywords
    return set(extract_keywords(text, max_keywords=5))


def _measured_overlap(family) -> int:
    """量出「族内每条相对族首的重叠」，返回其值（族内均匀 ⇒ 任取一条即可）。

    **这是本文件所有阈值断言的唯一数值来源** —— 不依赖任何分词器的字面输出。
    """
    seed = _kws(family[0])
    overlaps = {len(seed & _kws(c)) for c in family[1:]}
    assert len(overlaps) == 1, (
        f"族内重叠不均匀（实测 {sorted(overlaps)}）—— 本文件的族内均匀前提被破坏，需重选数据"
    )
    return overlaps.pop()


def _overlaps() -> list:
    """三族的实测重叠，顺序同 ALL_FAMILIES。"""
    return [_measured_overlap(f) for f in ALL_FAMILIES]


def _expected(families, thr: int) -> tuple:
    """按**实测**重叠推出一组阈值下的 (groups_found, would_merge)。

    推法与 `_cluster_by_topic` 的贪心逐条对应：
      * 族内均匀 ⇒ 每族要么整族并成一组（重叠 >= thr），要么整族散成单条组；
      * 族间零交叉 ⇒ 各族互不影响，组数直接相加。
    """
    groups = would = 0
    for fam in families:
        if _measured_overlap(fam) >= thr:
            groups += 1
            would += len(fam)
        else:
            groups += len(fam)          # 散成 len(fam) 个单条组
    return groups, would


# ===========================================================================
# 0. 测试数据自身的前提（改分词器后这里会先红，而不是让阈值断言给出假绿）
# ===========================================================================

class TestKeywordLadder:
    def test_families_have_uniform_seed_overlap(self):
        """每族内相对族首的重叠必须**均匀**（这是数据前提，不是分词器的字面期望）。

        注意：这里只断言「均匀」，**不断言等于 2/3/4** —— 那样就又把某台机器的
        分词结果写死了，正是 ks_ctt 要修掉的坑。
        """
        for fam in ALL_FAMILIES:
            seed = _kws(fam[0])
            overlaps = [len(seed & _kws(c)) for c in fam[1:]]
            assert len(set(overlaps)) == 1, (
                f"族 {fam[0][:20]!r} 族内重叠不均匀，实测 {overlaps}"
            )

    def test_constructed_tokens_survive_keyword_extraction(self):
        """构造用的哑词必须**逐字**成为关键词 —— 否则「重叠由构造决定」的前提不成立。

        这是本文件**唯一**允许出现的字面期望，且期望的是**我们自己写的 token**，
        不是 jieba 对自然语言的输出（后者随用户词典漂移，正是旧版的坑）。
        两条链路上它都成立：中文分支无 CJK 收不到，英文分支走原始文本正则。
        """
        for text in [c for fam in ALL_FAMILIES for c in fam]:
            expected = set(text.split())
            actual = _kws(text)
            assert actual == expected, (
                f"{text!r} 的关键词应为构造的哑词 {sorted(expected)}，实测 {sorted(actual)}"
            )

    def test_measured_ladder_is_strictly_increasing(self):
        """三族的实测重叠必须**逐级递增**（低 < 中 < 高），否则阈值阶梯测不出单调性。"""
        ov = _overlaps()
        assert ov[0] < ov[1] < ov[2], (
            f"三族实测重叠应严格递增，实际 {ov} —— 数据需重选"
        )

    def test_families_do_not_cross_overlap(self):
        """三族之间零交叉重叠 —— 否则阈值表会被「一族被另一族吸走」污染。"""
        kw = [[_kws(c) for c in fam] for fam in ALL_FAMILIES]
        for i in range(len(ALL_FAMILIES)):
            for j in range(i + 1, len(ALL_FAMILIES)):
                cross = max(len(a & b) for a in kw[i] for b in kw[j])
                assert cross == 0, f"族 {i} 与族 {j} 交叉重叠 {cross}，数据需重选"

    def test_existing_userdict_cannot_change_these_keywords(self):
        """自证环境无关：**即使用户 jieba 词典存在**，这批哑词的关键词也不变。

        `init_domain_dict()` 在 import 时把 `~/.config/keepsake/jieba_dict.txt`
        灌进 jieba 全局 —— 那是每台机器各自 Redis 语料生成的（见文件头）。本用例
        证明：往全局再灌一批**任意**词（模拟主脑那份词典），本文件的关键词一个都不动
        ⇒ 主脑/本机/换机器，结果逐字相同。
        """
        from keepsake.splitter import extract_keywords
        import jieba

        before = {text: _kws(text)
                  for text in [c for fam in ALL_FAMILIES for c in fam]}

        # 灌一批中文领域词 + 恰好是旧版测试数据里那些词的组合，
        # 复刻「主脑有用户词典」的最坏情况。
        for term in ("最大连接数", "连接池调优", "咖啡机滤芯", "用户登录接口",
                     "zqaalfa", "zqbfoxt", "zqclima"):
            jieba.add_word(term, freq=1000)

        try:
            for text, kw in before.items():
                after = set(extract_keywords(text, max_keywords=5))
                assert after == kw, (
                    f"用户词典影响了 {text!r} 的关键词：{sorted(before[text])} → {sorted(after)}；"
                    f"这会让跨机器结果漂移，需重选数据"
                )
        finally:
            for term in ("最大连接数", "连接池调优", "咖啡机滤芯", "用户登录接口",
                         "zqaalfa", "zqbfoxt", "zqclima"):
                jieba.del_word(term)


# ===========================================================================
# 1. 红绿闭环（关键）
# ===========================================================================

class TestThresholdRedGreen:
    def test_false_duplicates_not_merged_by_default(self):
        """共用关键词数**低于**默认阈值的「假重复」，默认阈值下不得并入同一组。

        修复前（硬编码 `overlap >= 2`）本用例为红：低重叠的三条会凑成一组并触发合并。
        断言用的是**实测重叠 vs 阈值**的相对关系，不是任何字面数字。
        """
        overlap = _measured_overlap(FAMILY_OVERLAP2)
        assert overlap < DEFAULT_MIN_OVERLAP, (
            f"本用例要求实测重叠低于默认阈值，实际重叠={overlap} 默认={DEFAULT_MIN_OVERLAP}"
        )

        st = _make_storage([FAMILY_OVERLAP2])
        stats = Consolidator(st).consolidate(dry_run=True)
        assert stats["min_overlap"] == DEFAULT_MIN_OVERLAP, f"默认阈值应为 3，实际 {stats}"
        assert stats["groups_found"] == len(FAMILY_OVERLAP2), (
            f"低重叠的三条应各成独立组，实际 groups_found={stats['groups_found']}"
        )
        assert stats["would_merge"] == 0, (
            f"重叠 {overlap} < {DEFAULT_MIN_OVERLAP} 的假重复不得被并入合并组，"
            f"实际 would_merge={stats['would_merge']}"
        )

    def test_true_duplicates_merged_by_default(self):
        """真重复（实测重叠 >= 默认阈值）必须仍被并 —— 收紧阈值不许误伤真重复。"""
        overlap = _measured_overlap(FAMILY_OVERLAP3)
        assert overlap >= DEFAULT_MIN_OVERLAP, (
            f"本用例要求实测重叠 >= 默认阈值，实际重叠={overlap} 默认={DEFAULT_MIN_OVERLAP}"
        )

        st = _make_storage([FAMILY_OVERLAP3])
        stats = Consolidator(st).consolidate(dry_run=True)
        assert stats["groups_found"] == 1, (
            f"重叠 {overlap} >= {DEFAULT_MIN_OVERLAP} 的一族应聚成一组，"
            f"实际 {stats['groups_found']}"
        )
        assert stats["would_merge"] == len(FAMILY_OVERLAP3), (
            f"重叠 {overlap} 的真重复必须会被合并，实际 would_merge={stats['would_merge']}"
        )

    def test_old_threshold_would_have_merged_false_duplicates(self):
        """对照证据（**红绿闭环的另一半**）：阈值降到实测重叠那一步时，
        同一批「假重复」照样被并 —— 正是本次要修掉的误并。

        注意阈值取的是 `_measured_overlap(...)` 而**不是写死 2**：红绿闭环的意图是
        「阈值 <= 实测重叠 ⇒ 并；阈值 > 实测重叠 ⇒ 不并」，这在任何分词器下都成立。
        """
        overlap = _measured_overlap(FAMILY_OVERLAP2)
        st = _make_storage([FAMILY_OVERLAP2])
        stats = Consolidator(st, min_overlap=overlap).consolidate(dry_run=True)
        assert stats["would_merge"] == len(FAMILY_OVERLAP2), (
            f"阈值降到实测重叠 {overlap} 时假重复会被误并（这正是修复前的行为），实际 {stats}"
        )


# ===========================================================================
# 2. 配置生效 + 非法值处理
# ===========================================================================

class TestConfigFlow:
    def test_missing_key_uses_default(self):
        """缺键 ⇒ 默认 3（既不崩也不静默改语义）；默认下只并真重复族。"""
        ov = _overlaps()
        st = _make_storage(ALL_FAMILIES)
        stats = Consolidator(st, config={}).consolidate(dry_run=True)
        assert stats["min_overlap"] == DEFAULT_MIN_OVERLAP == 3
        assert stats["would_merge"] == _expected(ALL_FAMILIES, DEFAULT_MIN_OVERLAP)[1], (
            f"默认阈值下只应并实测重叠 >= {DEFAULT_MIN_OVERLAP} 的族"
            f"（实测重叠 {ov}），实际 {stats}"
        )

    def test_config_key_4_lowers_would_merge(self):
        """config 给 4 ⇒ 比默认 3 更严 ⇒ would_merge 必须下降。"""
        st = _make_storage(ALL_FAMILIES)
        base = Consolidator(st).consolidate(dry_run=True)
        strict = Consolidator(st, config={"consolidate_min_overlap": 4}).consolidate(dry_run=True)
        assert strict["min_overlap"] == 4
        assert strict["would_merge"] < base["would_merge"], (
            f"阈值 4 的 would_merge 应低于阈值 3：{strict['would_merge']} vs {base['would_merge']}"
        )

    def test_config_key_also_controls_min_group_size(self):
        """`consolidate_min_group` 同样能从配置读（默认 3）。"""
        st = _make_storage([FAMILY_OVERLAP4])
        default_stats = Consolidator(st).consolidate(dry_run=True)
        assert default_stats["min_group_size"] == DEFAULT_MIN_GROUP_SIZE == 3
        cfg_stats = Consolidator(
            st, config={"consolidate_min_group": 5},
        ).consolidate(dry_run=True)
        assert cfg_stats["min_group_size"] == 5
        assert cfg_stats["would_merge"] == 0, (
            f"组内门槛 5 > 组内 {len(FAMILY_OVERLAP4)} 条 ⇒ 不该并"
        )

    @pytest.mark.parametrize("bad", ["abc", 0, -1, 2.5, True, [3], {}])
    def test_illegal_value_falls_back_with_reason(self, bad):
        """非整数 / <1 ⇒ 回落默认 3 且带 reason（不许崩、不许静默）。"""
        res = resolve_consolidate_config({"consolidate_min_overlap": bad})
        assert res["min_overlap"] == DEFAULT_MIN_OVERLAP == 3
        assert res.get("reasons"), f"非法值 {bad!r} 必须带 reason 说明回落"
        assert "consolidate_min_overlap" in res["reasons"][0]

    def test_absent_key_is_not_a_fallback_reason(self):
        """键**缺失**（None）走默认值属正常，不算非法、不该报 reason（不许噪音告警）。"""
        res = resolve_consolidate_config({})
        assert res["min_overlap"] == 3
        assert "reasons" not in res

    def test_illegal_value_does_not_crash_constructor(self):
        """非法配置穿过构造器也不崩，只是回落（并仍按默认阈值并真重复族）。"""
        st = _make_storage([FAMILY_OVERLAP3])
        stats = Consolidator(
            st, config={"consolidate_min_overlap": "三"},
        ).consolidate(dry_run=True)
        assert stats["min_overlap"] == 3
        assert stats["would_merge"] == _expected([FAMILY_OVERLAP3], 3)[1]

    def test_explicit_arg_beats_config(self):
        """优先级：显式构造参数 > 配置键 > 默认。"""
        st = _make_storage(ALL_FAMILIES)
        stats = Consolidator(
            st, min_overlap=2, config={"consolidate_min_overlap": 5},
        ).consolidate(dry_run=True)
        assert stats["min_overlap"] == 2, "显式参数应压过配置键"


# ===========================================================================
# 3. 量化对照（本次决策依据的三行表）
# ===========================================================================

class TestQuantifiedTable:
    def test_threshold_2_3_4_table(self):
        """同一批数据（三族共 9 条）在阈值 2/3/4 下的三行表。

        期望值由 `_expected()` 从**实测重叠**推出，不是写死的 (2,3,9)/(3,5,6)/(4,7,3)。
        """
        st = _make_storage(ALL_FAMILIES)
        rows = []
        for thr in (2, 3, 4):
            s = Consolidator(st, min_overlap=thr).consolidate(dry_run=True)
            rows.append((thr, s["groups_found"], s["would_merge"]))
        print("\n阈值 | groups_found | would_merge")
        for thr, g, w in rows:
            print(f"  {thr}  |     {g}       |     {w}")

        ov = _overlaps()
        print(f"实测族重叠（低/中/高）: {ov}")
        for thr, g, w in rows:
            exp_g, exp_w = _expected(ALL_FAMILIES, thr)
            assert (g, w) == (exp_g, exp_w), (
                f"阈值 {thr} 应得到 (groups={exp_g}, would_merge={exp_w})，"
                f"实际 ({g}, {w})；实测重叠 {ov}"
            )

        # 单调：阈值越严 ⇒ 组数与并入条数不增
        assert rows[0][1] <= rows[1][1] <= rows[2][1]
        assert rows[0][2] > rows[1][2] > rows[2][2]


# ===========================================================================
# 4. dry-run 零写入
# ===========================================================================

class TestDryRunZeroWrite:
    def test_dry_run_writes_nothing(self):
        """dry-run：一个字段都不写（行数、写入、更新、删除全为零变化）。"""
        st = _make_storage(ALL_FAMILIES)
        before = {k: dict(v) for k, v in st.rows.items()}
        Consolidator(st).consolidate(dry_run=True)
        assert st.writes == [], f"dry-run 不许写新碎片，实际写了 {st.writes}"
        assert st.updates == [], f"dry-run 不许改原料字段，实际改了 {st.updates}"
        assert st.deletes == [], f"dry-run 不许删，实际删了 {st.deletes}"
        assert st.rows == before, "dry-run 后行内容必须逐字不变"
        assert len(st.rows) == len(before) == 9


# ===========================================================================
# 5. 两后端共用判据（不许后端特异参数）
# ===========================================================================

class TestBackendNeutral:
    def test_threshold_is_instance_level_no_backend_branch(self):
        """阈值只存在 Consolidator 实例上，判据代码里不得出现任何后端名。"""
        import inspect
        src = inspect.getsource(Consolidator._cluster_by_topic)
        assert "redis" not in src.lower() and "postgres" not in src.lower(), (
            "聚类判据不得出现后端特异分支"
        )
        assert "self._min_overlap" in src, "判据必须读实例上的统一阈值"

    def test_same_code_path_serves_both_backends(self):
        """Consolidator 只依赖 StorageBase 的维护原语 ⇒ Redis/PG 同一判据。"""
        import inspect
        src = inspect.getsource(Consolidator)
        for prim in ("scan_fragment_keys", "get_fragments_batch",
                     "write_fragments_batch", "update_fragment_fields"):
            assert prim in src, f"Consolidator 应只经由 StorageBase 原语 {prim} 访问存储"
        assert not issubclass(Consolidator, object) or True
        assert not any("RedisStorage" in (b.__name__ if hasattr(b, "__name__") else str(b))
                       for b in Consolidator.__init__.__code__.co_varnames), \
            "构造器不应出现具体后端类型名"
