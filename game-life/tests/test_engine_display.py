# -*- coding: utf-8 -*-
"""引擎层中文化（T4）最小自测：显示归显示、判定归判定。

运行方式（在 game-life/ 目录下）：
    python tests/test_engine_display.py
零额外依赖（标准库 + 既有 requirements.txt），**完全离线**：
    - 不读真实 Steam 目录、不联网、不写 data/；
    - 需要索引的用例统一把 display 的懒加载缓存换成测试值（用后还原）。

覆盖：
    1. display_name / display_genres / genre_display / lookup_name 的优先级与降级；
    2. 判定路径（免费标记、高难度标签、单局时长表、价格三态）仍然使用官方英文名；
    3. **核心回归护栏**：同一份数据在「有官方中文列」与「没有中文列」两种情况下，
       统计 / 人格维度 / 性价比榜 / 推荐排序与分数 / 愿望单桶计数 / 族谱结构
       **逐项完全一致**，只有展示名变中文。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import display  # noqa: E402
from engine import (  # noqa: E402
    persona, recommender, series_tree, stats, value_ranking as vrank, wishlist_guard,
)

NOW = int(time.time())


# ------------------------------------------------------------------ 测试样本

def _base_meta():
    """英文侧元数据（与 T4 之前完全一致的那份）。"""
    return {
        1: {"name": "Alpha", "genres": ["Action", "RPG"], "price": 6800},
        2: {"name": "Beta", "genres": ["Casual"], "price": 0, "is_free": True},
        3: {"name": "Gamma", "genres": ["Strategy"], "price": 19900},
        4: {"name": "Delta", "genres": ["Indie", "Action"], "price": 0},          # 价格未知
        5: {"name": "Epsilon", "genres": ["Free to Play", "Casual"], "price": 0},  # 靠英文类型判免费
    }


CN_NAME = {1: "阿尔法", 2: "贝塔", 3: "伽马", 4: "德尔塔", 5: "艾普西龙"}
CN_GENRES = {
    1: ["动作", "角色扮演"], 2: ["休闲"], 3: ["策略"], 4: ["独立", "动作"], 5: ["免费开玩", "休闲"],
}
EN_TO_CN = {
    "Action": "动作", "RPG": "角色扮演", "Casual": "休闲",
    "Strategy": "策略", "Indie": "独立", "Free to Play": "免费开玩",
}


def _play_stats():
    return {
        1: {"playtime": 6000, "last_played": NOW - 3 * 86400},
        2: {"playtime": 30, "last_played": NOW - 100 * 86400},
        3: {"playtime": 3000, "last_played": NOW - 10 * 86400},
        4: {"playtime": 0, "last_played": 0},
        5: {"playtime": 900, "last_played": NOW - 400 * 86400},
    }


def _meta_without_cn():
    return {a: dict(m) for a, m in _base_meta().items()}


def _meta_with_cn():
    out = {}
    for appid, meta in _base_meta().items():
        row = dict(meta)
        row["name_cn"] = CN_NAME[appid]
        row["genres_cn"] = list(CN_GENRES[appid])
        out[appid] = row
    return out


class _DisplayIndex:
    """临时替换 display 的懒加载缓存（测试完还原，绝不污染真实索引）。"""

    def __init__(self, names=None, genre_map=None):
        self.names = names or {}
        self.genre_map = genre_map or {}

    def __enter__(self):
        self.saved = (display._NAME_INDEX, display._GENRE_MAP)
        display._NAME_INDEX = dict(self.names)
        display._GENRE_MAP = dict(self.genre_map)
        return self

    def __exit__(self, *exc):
        display._NAME_INDEX, display._GENRE_MAP = self.saved
        return False


# ------------------------------------------------- 1. 展示名优先级与降级

def test_display_name_priority_and_blank_handling():
    with _DisplayIndex({220: "半条命2"}):
        # 调用方中文名优先
        assert display.display_name(730, {"name": "CS2", "name_cn": "反恐精英：全球攻势"}) == "反恐精英：全球攻势"
        # 空串 / 纯空白 / None 一律当「没有」，回落到本地中文名索引
        for blank in ("", "   ", None, "\t"):
            assert display.display_name(220, {"name": "Half-Life 2", "name_cn": blank}) == "半条命2", repr(blank)
        # 本地索引也没有 -> 官方原名
        assert display.display_name(480, {"name": "Spacewar"}) == "Spacewar"
        # 连英文名都没有 -> appid N（绝不是空白）
        assert display.display_name(292030, {}) == "appid 292030"
        assert display.display_name(292030, None) == "appid 292030"
        # meta 不是 Mapping 也不能崩
        assert display.display_name(292030, "坏数据") == "appid 292030"
        # 索引里没有的 appid 且没有英文名
        assert display.display_name(99999999, {}) == "appid 99999999"
    print("  [1] display_name 中文优先 / 空白算缺失 / appid N 兜底 OK")


def test_display_name_never_returns_blank():
    with _DisplayIndex({}):
        for meta in ({}, None, {"name": ""}, {"name": "  "}, {"name_cn": ""},
                     {"name": "", "name_cn": ""}):
            value = display.display_name(12345, meta)
            assert str(value).strip(), (meta, value)
    print("  [2] display_name 在任何输入下都不返回空白 OK")


def test_lookup_name_only_returns_chinese():
    with _DisplayIndex({730: "反恐精英：全球攻势"}):
        assert display.lookup_name(730) == "反恐精英：全球攻势"
        assert display.lookup_name(730, {"name_cn": "CS2 中文"}) == "CS2 中文"
        # 没有中文名时返回空串（**不**回落英文名，供调用方自行决定下一步）
        assert display.lookup_name(480, {"name": "Spacewar"}) == ""
        assert display.lookup_name(99999999) == ""
        assert display.lookup_name("abc") == ""
    print("  [3] lookup_name 只给中文名、取不到返回空串 OK")


# ------------------------------------------------- 2. 展示类型优先级

def test_display_genres_prefers_chinese():
    assert display.display_genres({"genres": ["Action"], "genres_cn": ["动作"]}) == ["动作"]
    assert display.display_genres({"genres": ["Action", "RPG"], "genres_cn": ["动作", "角色扮演"]}) == ["动作", "角色扮演"]
    # 中文缺失 / 为空 -> 回落英文
    assert display.display_genres({"genres": ["Action"], "genres_cn": []}) == ["Action"]
    assert display.display_genres({"genres": ["Action"], "genres_cn": ["", "  "]}) == ["Action"]
    assert display.display_genres({"genres": ["Action"]}) == ["Action"]
    # 中英同名（如 VRChat 这类官方中文名就是原文）不硬改成中文列表
    assert display.display_genres({"genres": ["Action"], "genres_cn": ["Action"]}) == ["Action"]
    # 都没有 -> 空列表
    assert display.display_genres({}) == []
    assert display.display_genres(None) == []
    print("  [4] display_genres 中文类型优先 / 缺失回落英文 OK")


def test_genre_display_maps_english_to_official_chinese():
    with _DisplayIndex(genre_map=dict(EN_TO_CN)):
        assert display.genre_display("Action") == "动作"
        assert display.genre_display("Free to Play") == "免费开玩"
        # 大小写不敏感
        assert display.genre_display("action") == "动作"
        # 没有对照 -> 原样返回（绝不机翻）
        assert display.genre_display("Souls-like") == "Souls-like"
        assert display.genre_display("") == ""
        assert display.genre_display(None) == ""
    print("  [5] genre_display 英文类型 -> 官方中文 / 无对照不机翻 OK")


# ------------------------------------------------- 3. 判定路径仍用英文名

def test_judgement_paths_still_use_english_names():
    """免费标记 / 高难度标签 / 单局时长表 必须继续吃官方英文名。"""
    free_meta = {"name": "Epsilon", "genres": ["Free to Play"], "genres_cn": ["免费开玩"], "price": 0}
    # 若判定错用了中文类型，这里会变成 unknown
    assert vrank.price_state(free_meta) == "free"
    assert wishlist_guard._price_state(free_meta) == "free"
    # 高难度标签：英文 categories 命中
    hard = {"genres": ["Action"], "genres_cn": ["动作"], "categories": ["Souls-like"]}
    assert persona._is_hard_game(hard) is True
    # 单局时长表：rules.json 里是英文 key，英文类型在 -> 取到 30 分钟
    rules = {"session_minutes": {"casual": 30, "action": 60}, "session_default_minutes": 60}
    assert recommender._session_minutes({"genres": ["Casual"], "genres_cn": ["休闲"]}, rules) == 30
    assert recommender._session_minutes({"genres": ["Action"], "genres_cn": ["动作"]}, rules) == 60
    # 只有中文类型时退回默认值（说明判定**没有**读中文列）
    assert recommender._session_minutes({"genres_cn": ["休闲"]}, rules) == 60
    print("  [6] 判定路径（免费/难度/时长表）仍用官方英文名 OK")


# ------------------------------------------------- 4. 核心回归护栏

def test_verdicts_unchanged_only_display_names_differ():
    """同一份数据，加不加官方中文列：所有判定结果必须逐项一致。"""
    play_stats = _play_stats()
    plain, cn = _meta_without_cn(), _meta_with_cn()
    statuses = {1: "在玩", 2: "搁置", 3: "在玩", 4: "未玩", 5: "搁置"}

    with _DisplayIndex({}, {}):
        s_plain = stats.compute_stats(play_stats, plain, statuses=statuses, today=NOW)
        p_plain = persona.compute_persona(play_stats, plain, statuses=statuses, today=NOW)
        v_plain = vrank.compute_value_ranking(play_stats, plain, statuses=statuses)
        w_plain = wishlist_guard.compute_wishlist_guard(play_stats, plain, statuses=statuses, today=NOW)
        t_plain = series_tree.compute_series_tree(
            play_stats, plain, series_map={}, official={"Saga": [1, 3, 4]}, aliases={"Saga": "传说"}, statuses=statuses)
        r_plain = {}
        for mood, tl, genre in (("随便", "不限", "不限"), ("挑战", "1h", "Action"), ("轻松", "30m", "Casual")):
            r_plain[(mood, tl, genre)] = recommender.recommend(
                play_stats, plain, mood=mood, time_left=tl, genre=genre, statuses=statuses,
                today=NOW, type_index={}, top_k=5)

    with _DisplayIndex(CN_NAME, EN_TO_CN):
        s_cn = stats.compute_stats(play_stats, cn, statuses=statuses, today=NOW)
        p_cn = persona.compute_persona(play_stats, cn, statuses=statuses, today=NOW)
        v_cn = vrank.compute_value_ranking(play_stats, cn, statuses=statuses)
        w_cn = wishlist_guard.compute_wishlist_guard(play_stats, cn, statuses=statuses, today=NOW)
        t_cn = series_tree.compute_series_tree(
            play_stats, cn, series_map={}, official={"Saga": [1, 3, 4]}, aliases={"Saga": "传说"}, statuses=statuses)
        r_cn = {}
        for mood, tl, genre in (("随便", "不限", "不限"), ("挑战", "1h", "Action"), ("轻松", "30m", "Casual")):
            r_cn[(mood, tl, genre)] = recommender.recommend(
                play_stats, cn, mood=mood, time_left=tl, genre=genre, statuses=statuses,
                today=NOW, type_index={}, top_k=5)

    # ---- stats：判定指标逐项一致
    for key in ("total_games", "total_playtime_hours", "avg_playtime_hours", "dust_rate",
                "played_ratio", "played_count", "unplayed_count", "status_counts",
                "status_percent", "playtime_distribution"):
        assert s_plain[key] == s_cn[key], (key, s_plain[key], s_cn[key])
    # 类型计数：只允许键名换中文，计数多重集必须一致
    assert sorted(r["count"] for r in s_plain["top_genres"]) == sorted(r["count"] for r in s_cn["top_genres"])
    # 顺序与 appid 一致
    assert [g["appid"] for g in s_plain["most_forgotten"]] == [g["appid"] for g in s_cn["most_forgotten"]]
    assert [g["appid"] for g in s_plain["recent_played"]] == [g["appid"] for g in s_cn["recent_played"]]

    # ---- persona：五个维度 + 标签必须完全一致
    assert p_plain["dimensions"] == p_cn["dimensions"], (p_plain["dimensions"], p_cn["dimensions"])
    assert p_plain["labels"] == p_cn["labels"]
    assert p_plain["primary_label"] == p_cn["primary_label"]

    # ---- 价格三态 + 性价比榜
    for grp in ("value_top", "waste_top", "zero_cost_top"):
        assert [i["appid"] for i in v_plain[grp]] == [i["appid"] for i in v_cn[grp]], grp
        assert [i["price_state"] for i in v_plain[grp]] == [i["price_state"] for i in v_cn[grp]], grp
    assert [i.get("cost_per_hour") for i in v_plain["value_top"]] == [i.get("cost_per_hour") for i in v_cn["value_top"]]
    assert v_plain["stats"] == v_cn["stats"]

    # ---- 推荐：候选顺序 + 分数 + 状态完全一致
    for combo in r_plain:
        a = [(i["appid"], i["score"], i["status"]) for i in r_plain[combo]["recommendations"]]
        b = [(i["appid"], i["score"], i["status"]) for i in r_cn[combo]["recommendations"]]
        assert a == b, (combo, a, b)
        assert r_plain[combo]["candidate_count"] == r_cn[combo]["candidate_count"]

    # ---- 愿望单：类型桶指标逐项一致（桶名允许变中文）
    def _bucket_sig(result):
        return sorted(
            (b["owned_count"], b["total_playtime_minutes"], b["play_ratio"], b["dust_rate"],
             b["free_count"], b["paid_count"], b["unknown_price_count"], b["avg_price_yuan"])
            for b in result["genres"]
        )
    assert _bucket_sig(w_plain) == _bucket_sig(w_cn)
    assert len(w_plain["genres"]) == len(w_cn["genres"])
    assert [x["owned_count"] for x in w_plain["warnings"]] == [x["owned_count"] for x in w_cn["warnings"]]
    assert len(w_plain["warnings"]) == len(w_cn["warnings"])

    # ---- 族谱：结构（成员 appid / 拥有标记 / 计数）一致
    def shape(tree):
        return [[s["series"], s["owned_count"], s["total_count"], s["completed_count"],
                 [g["appid"] for g in s["games"]], [g["owned"] for g in s["games"]]]
                for s in tree["series"]]
    assert shape(t_plain) == shape(t_cn)
    assert t_plain["series_count"] == t_cn["series_count"]

    # ---- 反过来证明「展示名确实中文化了」（否则上面的一致性可能是假的）
    cn_genre_names = {r["name"] for r in s_cn["top_genres"]}
    en_genre_names = {r["name"] for r in s_plain["top_genres"]}
    assert cn_genre_names == {"动作", "角色扮演", "休闲", "策略", "独立", "免费开玩"}
    assert en_genre_names == {"Action", "RPG", "Casual", "Strategy", "Indie", "Free to Play"}
    assert {i["name"] for i in v_cn["value_top"]} == {CN_NAME[i["appid"]] for i in v_cn["value_top"]}

    # persona 的「最常玩 X」取自 top_genres 第一名。_top_items 的排序键是
    # (-count, name)：并列频次时按**名字**定序，中英文码位不同，并列冠军就可能不同
    # （本例 Action/Casual 各 2 款：英文侧 Action < Casual，所以是 Action；
    #   中文侧 休闲 U+4F11 < 动作 U+52A8，所以是 休闲）。
    # 这是既有的排序规则、与中文化无关，因此不断言「谁并列第一」，只断言
    # 「summary 用的是中文类型名」且「没有夹带英文类型名」。
    assert s_cn["top_genres"][0]["name"] in p_cn["summary"], p_cn["summary"]
    assert not any(name in p_cn["summary"] for name in en_genre_names), p_cn["summary"]
    assert s_plain["top_genres"][0]["name"] in p_plain["summary"], p_plain["summary"]
    print("  [7] 判定逐项一致（统计/维度/榜单/推荐/愿望单/族谱），仅展示名变中文 OK")


def test_engines_localize_names_end_to_end():
    """各引擎产出的展示名确实来自中文索引。"""
    play_stats = _play_stats()
    cn = _meta_with_cn()
    with _DisplayIndex(CN_NAME, EN_TO_CN):
        assert vrank.compute_value_ranking(play_stats, cn)["value_top"][0]["name"] in set(CN_NAME.values())
        rec = recommender.recommend(play_stats, cn, mood="随便", time_left="不限", statuses={},
                                    today=NOW, type_index={}, top_k=5)
        assert all(i["name"] in set(CN_NAME.values()) for i in rec["recommendations"])
        wg = wishlist_guard.compute_wishlist_guard(play_stats, cn, statuses={}, today=NOW)
        assert all(any("\u4e00" <= ch <= "\u9fff" for ch in b["genre"]) for b in wg["genres"])
        st = stats.compute_stats(play_stats, cn, statuses={}, today=NOW)
        assert all(any("\u4e00" <= ch <= "\u9fff" for ch in r["name"]) for r in st["top_genres"])
    print("  [8] stats/price/recommend/wishlist 展示名均中文化 OK")


def test_persona_localizes_precomputed_english_stats():
    """外部直接传英文 stats_result 时，persona 的「最常玩 X」也必须变中文。

    走的是 persona 里 display.genre_display(top_genres[0]["name"]) 这条兜底分支：
    stats_result 是调用方算好的（键还是英文），persona 不再重算 stats，
    只把那个英文类型名查表翻成官方中文。
    """
    play_stats = _play_stats()
    en_stats = {"total_games": 5, "total_playtime_hours": 100.0,
                "top_genres": [{"name": "Action", "count": 2}]}
    with _DisplayIndex(CN_NAME, EN_TO_CN):
        result = persona.compute_persona(play_stats, _meta_with_cn(), stats_result=en_stats, today=NOW)
    assert "动作" in result["summary"], result["summary"]
    assert "Action" not in result["summary"], result["summary"]
    print("  [8b] persona 传入英文 stats_result 时仍输出中文类型 OK")


# ------------------------------------------------- 5. 降级与离线

def test_display_degrades_offline_without_data():
    """数据源缺失时安全降级：不抛异常、不联网、不返回空白。"""
    saved = (display._NAME_INDEX, display._GENRE_MAP)
    try:
        display._NAME_INDEX = None
        display._GENRE_MAP = None
        # 直接触发真实的懒加载（读本地 json / db；取不到就空表）
        name = display.display_name(999999999, {"name": "Nonexistent Game"})
        assert name == "Nonexistent Game", name
        assert display.genre_display("Some Unknown Genre") == "Some Unknown Genre"
        assert isinstance(display.name_cn_of(999999999), str)
    finally:
        display._NAME_INDEX, display._GENRE_MAP = saved
    print("  [9] 索引缺失时安全降级（不抛异常 / 不返回空白）OK")


def test_display_module_is_offline():
    """展示层模块绝不联网：源码里不得出现网络库。"""
    import inspect
    src = inspect.getsource(display)
    for banned in ("requests", "urllib", "http.client", "socket", "urlopen"):
        assert banned not in src, f"展示层不应出现网络相关符号：{banned}"
    assert "from core import" in src  # 只读本地缓存/表
    print("  [10] 展示层零联网（无 requests/urllib/socket）OK")


def main() -> None:
    tests = [
        test_display_name_priority_and_blank_handling,
        test_display_name_never_returns_blank,
        test_lookup_name_only_returns_chinese,
        test_display_genres_prefers_chinese,
        test_genre_display_maps_english_to_official_chinese,
        test_judgement_paths_still_use_english_names,
        test_verdicts_unchanged_only_display_names_differ,
        test_engines_localize_names_end_to_end,
        test_persona_localizes_precomputed_english_stats,
        test_display_degrades_offline_without_data,
        test_display_module_is_offline,
    ]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 项验证通过")


if __name__ == "__main__":
    main()
