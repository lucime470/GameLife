"""分析引擎 P3 模块的最小自测（value_ranking / series_tree / wishlist_guard）。

运行方式（在 game-life/ 目录下）：
    python tests/test_analysis.py
零依赖，纯标准库。
"""

import sys
import time
from datetime import date
from pathlib import Path

# 让 game-life/ 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import value_ranking as v   # noqa: E402
from engine import series_tree as st    # noqa: E402
from engine import wishlist_guard as w  # noqa: E402
from engine import achievement_radar as ar  # noqa: E402
from engine import challenge as ch  # noqa: E402

NOW = int(time.time())


def _base_data():
    """通用样本：价格单位=分。"""
    play_stats = {
        1: {"playtime": 6000, "last_played": NOW - 3 * 86400},   # 100h 在玩
        2: {"playtime": 30, "last_played": NOW - 100 * 86400},   # 0.5h 搁置
        3: {"playtime": 5, "last_played": NOW - 100 * 86400},    # 5 分钟 几乎没玩
        4: {"playtime": 0, "last_played": 0},                    # 未玩
        5: {"playtime": 480, "last_played": NOW - 86400},        # 免费游戏 8h
    }
    games = {
        1: {"name": "A1", "genres": ["Action"], "price": 6800},      # ¥68
        2: {"name": "A2", "genres": ["Action"], "price": 9900},      # ¥99
        3: {"name": "A3", "genres": ["Strategy"], "price": 19800},   # ¥198
        4: {"name": "A4", "genres": ["Strategy"], "price": 3000},    # ¥30
        # 免费必须由 is_free 明确标注；price=0 而无标记一律视为「价格未知」
        5: {"name": "A5", "genres": ["Strategy"], "price": 0, "is_free": True},
    }
    return play_stats, games


def test_value_ranking():
    play_stats, games = _base_data()
    r = v.compute_value_ranking(play_stats, games)
    assert r["stats"]["paid_count"] == 4
    assert r["stats"]["free_count"] == 1
    assert r["stats"]["unknown_price_count"] == 0
    assert r["stats"]["barely_played_count"] == 2  # appid 3(5分钟)、4(0分钟)

    # 性价比榜：不含 <10 分钟与免费游戏；A1(68元/100.5h=0.68) 最便宜
    assert [i["appid"] for i in r["value_top"]] == [1, 2]
    assert r["value_top"][0]["cost_per_hour"] == 0.68

    # 最浪费榜：含全部付费游戏，单价降序，几乎没玩的在顶部
    assert len(r["waste_top"]) == 4
    assert r["waste_top"][0]["appid"] == 3 and r["waste_top"][0]["barely_played"] is True
    assert r["waste_top"][0]["cost_per_hour"] > r["waste_top"][1]["cost_per_hour"]

    # 零成本榜：免费游戏按游玩时长降序
    assert [i["appid"] for i in r["zero_cost_top"]] == [5]
    assert r["zero_cost_top"][0]["price_state"] == "free"
    print("PASS value_ranking 单价/最浪费/零成本榜")


def test_value_ranking_custom():
    play_stats, games = _base_data()
    r = v.compute_value_ranking(play_stats, games, top_n=1, price_divisor=1)
    assert len(r["value_top"]) == 1
    # price_divisor=1 时 A1 单价 = 6800 / (100 + 0.5) = 67.66
    assert r["value_top"][0]["cost_per_hour"] == 67.66
    # 空库
    empty = v.compute_value_ranking({}, {})
    assert empty["value_top"] == [] and empty["stats"]["paid_count"] == 0
    print("PASS value_ranking 自定义参数与空库")


def test_value_ranking_unknown_price_not_free():
    """核心修正：价格未知 ≠ 免费，未知名不参与任何榜单。"""
    play_stats = {
        1: {"playtime": 6000, "last_played": NOW - 86400},   # 有价
        2: {"playtime": 6000, "last_played": NOW - 86400},   # 未知价（price=0 且无 is_free）
        3: {"playtime": 6000, "last_played": NOW - 86400},   # 明确免费
        4: {"playtime": 6000, "last_played": NOW - 86400},   # 官方 Free to Play 类型 -> 免费
    }
    games = {
        1: {"name": "Paid", "genres": ["Action"], "price": 6800},
        2: {"name": "Unknown", "genres": ["Action"], "price": 0},
        3: {"name": "FreeFlag", "genres": ["Action"], "price": 0, "is_free": True},
        4: {"name": "FreeGenre", "genres": ["Free to Play"], "price": 0},
    }
    r = v.compute_value_ranking(play_stats, games)
    assert r["stats"]["paid_count"] == 1, r["stats"]
    assert r["stats"]["free_count"] == 2, r["stats"]
    assert r["stats"]["unknown_price_count"] == 1, r["stats"]

    # 未知价：不进性价比榜、不进最浪费榜、也不进零成本榜
    assert [i["appid"] for i in r["value_top"]] == [1]
    assert [i["appid"] for i in r["waste_top"]] == [1]
    assert sorted(i["appid"] for i in r["zero_cost_top"]) == [3, 4]
    assert all(i["appid"] != 2 for i in r["value_top"] + r["waste_top"] + r["zero_cost_top"])

    # 非游戏条目（Steam Client 等）不参与任何榜单
    noisy = v.compute_value_ranking({7: {"playtime": 9999}}, {7: {"name": "Steam Client"}})
    assert noisy["value_top"] == [] and noisy["waste_top"] == []
    assert noisy["zero_cost_top"] == [] and noisy["stats"]["unknown_price_count"] == 0
    print("PASS value_ranking 价格未知≠免费 / 非游戏条目排除")


def test_series_tree():
    # 自定义小映射：文明 6 拥有并通关；文明 5 未拥有；CS2 独立系列
    series_map = {
        "289070": {"series": "文明", "name": "文明6"},
        "8930": {"series": "文明", "name": "文明5"},
        "730": {"series": "反恐精英", "name": "反恐精英2"},
    }
    own = {289070: {"playtime": 6000, "last_played": NOW - 86400},
           730: {"playtime": 300, "last_played": NOW - 86400}}
    own_games = {
        289070: {"name": "文明6", "genres": ["Strategy"], "price": 19900},
        730: {"name": "CS2", "genres": ["Action"], "price": 0},
    }
    r = st.compute_series_tree(own, own_games, series_map=series_map,
                               statuses={289070: "通关", 730: "在玩"},
                               official={}, aliases={})
    names = {s["series"] for s in r["series"]}
    # 文明有 2 部 -> 是真系列；反恐精英只有 1 部（CS2）-> 属于「独立作品」而不是系列，
    # 按用户反馈「1/1 就是没有系列」默认不展示。
    assert r["series_count"] == 1 and r["total_titles"] == 2
    assert "文明" in names and "反恐精英" not in names
    assert r["singletons_hidden"] == 1
    assert r["single_title_series"] == 0 and r["unowned_series"] == 0

    civ = next(s for s in r["series"] if s["series"] == "文明")
    assert civ["owned_count"] == 1 and civ["total_count"] == 2
    assert civ["completed_count"] == 1
    assert civ["playtime_hours"] == 100.0
    assert civ["games"][0]["owned"] is False  # 8930 文明5 排在前面（appid 升序）
    assert civ["games"][1]["owned"] is True and civ["games"][1]["status"] == "通关"  # 289070 文明6

    # min_titles 可放宽：设成 1 时又允许单作品条目出现（便于展示层按需切换）
    relaxed = st.compute_series_tree(own, own_games, series_map=series_map,
                                     statuses={289070: "通关", 730: "在玩"},
                                     official={}, aliases={}, min_titles=1)
    assert relaxed["series_count"] == 2 and relaxed["singletons_hidden"] == 0
    cs = next(s for s in relaxed["series"] if s["series"] == "反恐精英")
    assert cs["owned_count"] == 1 and cs["completed_count"] == 0

    # 原始 dict（字符串 appid 键）与加载后的 int 键均可用
    loaded = st.load_series_map()  # 默认 data/series_map.json
    assert len(loaded) > 50 and 289070 in loaded
    # 空库：不再展示任何系列（只输出 owned_count > 0）
    empty_tree = st.compute_series_tree({}, {}, official={}, aliases={})
    assert empty_tree["series_count"] == 0
    assert sum(s["owned_count"] for s in empty_tree["series"]) == 0
    print("PASS series_tree 聚合/未拥有/单一作品不算系列/默认映射文件")


def test_series_tree_official_and_owned_filter():
    """官方 franchise 为主源 + 只输出 owned>0 + 不截断 + 非游戏排除。"""
    official = {
        "Monster Hunter": [582010, 1277400, 1446780],   # 用户拥有其中 2 部
        "Total War": [34030, 779340],
        "Nobody Owns This": [111, 222],                  # 一部都没有 -> 不展示
    }
    manual = {"1277400": {"series": "怪物猎人", "name": "怪物猎人物语2"}}
    aliases = {"Monster Hunter": "怪物猎人", "Total War": "全面战争"}
    play_stats = {
        582010: {"playtime": 12000, "last_played": NOW - 86400},
        1446780: {"playtime": 600, "last_played": NOW - 86400},
        779340: {"playtime": 60, "last_played": NOW - 86400},
        7: {"playtime": 0, "last_played": 0},           # Steam Client：非游戏
    }
    games = {582010: {"name": "Monster Hunter: World"},
             1446780: {"name": "MONSTER HUNTER RISE"},
             779340: {"name": "Total War: THREE KINGDOMS"},
             7: {"name": "Steam Client"}}

    r = st.compute_series_tree(play_stats, games, series_map=manual,
                               statuses={582010: "通关"}, official=official,
                               aliases=aliases)
    names = {s["series"] for s in r["series"]}
    assert "怪物猎人" in names and "全面战争" in names
    assert "Monster Hunter" not in names, "官方英文名应被中文展示名取代"
    assert "Nobody Owns This" not in names, "一部都没有的系列不能展示"

    mh = next(s for s in r["series"] if s["series"] == "怪物猎人")
    # 官方目录的 3 部全部列出（未拥有的 1277400 也在，展示"还差哪几部"）
    assert mh["owned_count"] == 2 and mh["total_count"] == 3
    assert mh["completed_count"] == 1
    assert mh["playtime_hours"] == round(12600 / 60, 2)
    assert [g["appid"] for g in mh["games"]] == [582010, 1277400, 1446780]
    assert any(not g["owned"] for g in mh["games"])
    # 官方英文系列名保留在 official_name（展示名是中文，不再有 alias 字段）
    assert mh.get("official_name") == "Monster Hunter"
    assert "alias" not in mh

    # 非游戏条目（appid 7）绝不出现在任何系列里
    assert all(g["appid"] != 7 for s in r["series"] for g in s["games"])
    print("PASS series_tree 官方为主/只含owned>0/不截断/排除非游戏")


def test_series_tree_merges_duplicate_series():
    """同系列不得拆成两条：英文官方 franchise 与中文手工表按展示名归并。"""
    official = {
        "Devil May Cry": [6550],                          # 官方只有 DMC3
        "Kingdom": [701160],                              # 官方：系列名
        "Kingdom Two Crowns": [701160],                   # 官方：同一 appid 的作品名
        "The Elder Scrolls": [489830],
        "Skyrim": [489830],                               # 同上，另一组同集合 franchise
    }
    manual = {
        220440: {"series": "鬼泣", "name": "DmC：鬼泣"},
        329050: {"series": "鬼泣", "name": "鬼泣4特别版"},
        601150: {"series": "鬼泣", "name": "鬼泣5"},
    }
    aliases = {
        "Devil May Cry": "鬼泣",
        "Kingdom": "王国",
        "Kingdom Two Crowns": "王国：两位君主",
        "The Elder Scrolls": "上古卷轴",
        "Skyrim": "上古卷轴",
    }
    play_stats = {6550: {"playtime": 120, "last_played": NOW - 86400},
                  601150: {"playtime": 60, "last_played": NOW - 86400},
                  701160: {"playtime": 300, "last_played": NOW - 86400},
                  489830: {"playtime": 30, "last_played": NOW - 86400}}
    games = {6550: {"name": "Devil May Cry 3: Special Edition"},
             601150: {"name": "Devil May Cry 5"},
             701160: {"name": "Kingdom Two Crowns"},
             489830: {"name": "The Elder Scrolls V: Skyrim"}}

    r = st.compute_series_tree(play_stats, games, series_map=manual,
                               official=official, aliases=aliases)
    names = [s["series"] for s in r["series"]]
    assert len(names) == len(set(names)), f"展示名必须唯一：{names}"
    assert "鬼泣" in names and "Kingdom" not in names and "Devil May Cry" not in names
    assert "王国：两位君主" not in names and "Skyrim" not in names

    dmc = next(s for s in r["series"] if s["series"] == "鬼泣")
    # 官方 1 部 + 手工 3 部合并为一条，同一 appid 不重复
    ids = [g["appid"] for g in dmc["games"]]
    assert ids == sorted(set(ids)) == [6550, 220440, 329050, 601150]
    assert dmc["total_count"] == 4 and dmc["owned_count"] == 2

    # 「王国」与「上古卷轴」本节样本里都只有 1 部（作品名当系列名的那两条），
    # 按「单一作品不算系列」规则不展示——归并本身用 groups 直接验，避免被过滤掩盖。
    assert "王国" not in names and "上古卷轴" not in names
    flat_names = {a: str((v or {}).get("name") or "") for a, v in games.items()}
    groups, offnames = st._compose_groups(official, {
        220440: {"series": "鬼泣", "name": "DmC：鬼泣"},
        329050: {"series": "鬼泣", "name": "鬼泣4特别版"},
        601150: {"series": "鬼泣", "name": "鬼泣5"},
    }, aliases, flat_names)
    kingdom_set = set(groups["王国"])
    assert kingdom_set == {701160}, f"Kingdom 的两条 franchise 应归并到一组：{groups.get('王国')}"
    assert offnames.get("王国") == "Kingdom"
    assert set(groups["上古卷轴"]) == {489830}
    assert offnames.get("上古卷轴") == "The Elder Scrolls"
    print("PASS series_tree 同系列归并/展示名唯一/appid 不重复")


def test_merge_series_catalogs():
    """权威缓存 vs 本机 appinfo 的并集合并：同一系列取成员并集，非游戏剔除。"""
    local = {"Monster Hunter": [582010, 1446780], "Total War": [779340]}
    cache = {
        "Monster Hunter": [582010, 1277400, 1446780, 2246340, 2356560, 2852190],
        "Total War": [4700, 10500, 34030, 201270, 214950, 325610, 364360, 594570,
                      779340, 1142710],
        "Nobody Owns This": [111, 222],
    }
    merged = st.merge_series_catalogs(local, cache)
    assert merged["Monster Hunter"] == [582010, 1277400, 1446780, 2246340, 2356560, 2852190]
    assert merged["Total War"] == sorted(cache["Total War"])
    assert merged["Nobody Owns This"] == [111, 222]
    # 空/损坏输入安全降级
    assert st.merge_series_catalogs(None, {}, {"X": []}) == {}
    # 非游戏 appid（7 / 760 / 241100）绝不出现在任何系列
    dirty = st.merge_series_catalogs({"A": [7, 760, 241100, 570]})
    assert dirty == {"A": [570]}


def test_series_tree_authoritative_coverage():
    """用权威缓存喂 compute_series_tree：系列成员远多于本机 appinfo 的覆盖。"""
    local = {"Monster Hunter": [582010, 1446780], "Total War": [779340]}
    cache = {
        "Monster Hunter": [582010, 1277400, 1446780, 2246340, 2356560, 2852190],
        "Total War": [4700, 10500, 34030, 201270, 214950, 325610, 364360, 594570,
                      779340, 1142710],
    }
    manual = {"1277400": {"series": "怪物猎人", "name": "怪物猎人物语2"}}
    aliases = {"Monster Hunter": "怪物猎人", "Total War": "全面战争"}
    play_stats = {582010: {"playtime": 12000, "last_played": NOW - 86400},
                  1446780: {"playtime": 600, "last_played": NOW - 86400},
                  779340: {"playtime": 60, "last_played": NOW - 86400}}

    r = st.compute_series_tree(play_stats, {}, series_map=manual,
                               official=st.merge_series_catalogs(local, cache),
                               aliases=aliases)
    mh = next(s for s in r["series"] if s["series"] == "怪物猎人")
    assert [g["appid"] for g in mh["games"]] == [
        582010, 1277400, 1446780, 2246340, 2356560, 2852190]
    assert mh["total_count"] == 6 and mh["owned_count"] == 2
    assert mh.get("official_name") == "Monster Hunter"
    # 未拥有但属于该系列的作品也列出（表达"还差哪几部"）
    assert sum(1 for g in mh["games"] if not g["owned"]) == 4

    tw = next(s for s in r["series"] if s["series"] == "全面战争")
    assert tw["total_count"] == 10 and tw["owned_count"] == 1

    # 只给本机 appinfo（模拟没有权威缓存）：仍可用，但覆盖明显更少
    offline = st.compute_series_tree(play_stats, {}, series_map=manual, official=local,
                                     aliases=aliases)
    mh_off = next(s for s in offline["series"] if s["series"] == "怪物猎人")
    assert [g["appid"] for g in mh_off["games"]] == [582010, 1277400, 1446780]
    print("PASS series_tree 权威缓存覆盖 / 并集合并 / 离线兜底")


def test_franchise_html_parsing():
    """官方检索结果解析：只认 App_ 游戏本体，剔除 Sub_ 组合包与试玩/原声/性能测试。"""
    html = (
        '<a href="https://store.steampowered.com/app/582010/x/" data-ds-appid="582010"'
        ' data-ds-itemkey="App_582010"><span class="title">Monster Hunter: World</span></a>'
        '<a href="https://store.steampowered.com/sub/1120457/y/" data-ds-appid="1277400,1461850"'
        ' data-ds-itemkey="Sub_1120457">'
        '<span class="title">Monster Hunter Stories 2: Wings of Ruin Deluxe Edition</span></a>'
        '<a href="https://store.steampowered.com/app/1836450/z/" data-ds-appid="1836450"'
        ' data-ds-itemkey="App_1836450">'
        '<span class="title">MONSTER HUNTER RISE: SUNBREAK DEMO</span></a>'
        '<a href="https://store.steampowered.com/app/999001/s/" data-ds-appid="999001"'
        ' data-ds-itemkey="App_999001">'
        '<span class="title">Monster Hunter Soundtrack</span></a>'
        '<a href="https://store.steampowered.com/app/2246340/w/" data-ds-appid="2246340"'
        ' data-ds-itemkey="App_2246340"><span class="title">Monster Hunter Wilds</span></a>'
    )
    rows = st.parse_franchise_html(html)
    assert [appid for appid, _ in rows] == [582010, 2246340], rows
    assert rows[0][1] == "Monster Hunter: World"
    print("PASS 官方检索解析：App_ 才认，Sub_/试玩/原声已剔除")


def test_series_cache_roundtrip(tmpdir=None):
    """缓存读写/过期判定：往返一致、日期可解析、损坏文件安全降级。"""
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as work:
        target = _Path(work) / "series_official.json"
        st._write_cache_file(target, {
            "Monster Hunter": {"updated": date.today().isoformat(), "total_count": 2,
                               "members": [[582010, "Monster Hunter: World"],
                                           [1446780, "MONSTER HUNTER RISE"]]},
            "Empty": {"updated": date.today().isoformat(), "total_count": 0, "members": []},
        })
        loaded = st.load_series_cache(target)
        assert loaded == {"Monster Hunter": [582010, 1446780]}, loaded
        titles = st.load_series_cache_titles(target)
        assert titles[582010] == "Monster Hunter: World"
        assert st.series_cache_age_days(target) == 0.0
        assert st.series_cache_is_stale(target) is False
        # 缺失文件、损坏文件都必须安全降级，不抛异常
        assert st.load_series_cache(_Path(work) / "nope.json") == {}
        broken = _Path(work) / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        assert st.load_series_cache(broken) == {}
        assert st.series_cache_is_stale(broken) is True
    print("PASS 系列缓存 往返/过期/损坏降级")


def test_wishlist_guard():
    play_stats, games = _base_data()
    statuses = {1: "在玩", 2: "搁置", 3: "搁置", 4: "未玩", 5: "在玩"}
    r = w.compute_wishlist_guard(play_stats, games, statuses=statuses)
    by_genre = {item["genre"]: item for item in r["genres"]}

    action = by_genre["Action"]
    assert action["owned_count"] == 2
    assert action["avg_playtime_hours"] == 50.25  # (6000+30)/60/2
    assert action["play_ratio"] == 100.0 and action["dust_rate"] == 50.0

    strategy = by_genre["Strategy"]
    assert strategy["owned_count"] == 3
    assert strategy["avg_playtime_hours"] == round(485 / 60 / 3, 2)  # 2.69h ≥ 1h 不告警
    # 免费款（is_free）计入平均价且分子为 0；未知价不计入分母
    assert strategy["avg_price_yuan"] == round((19800 + 3000 + 0) / 3 / 100, 2)
    assert strategy["paid_count"] == 2 and strategy["free_count"] == 1
    assert strategy["unknown_price_count"] == 0
    assert all(x["genre"] != "Strategy" for x in r["warnings"])

    # 买了 3 款、平均 < 1h 的肉鸽类型 → 触发告警
    ps = {10: {"playtime": 10, "last_played": NOW - 86400},
          11: {"playtime": 5, "last_played": NOW - 86400},
          12: {"playtime": 0, "last_played": 0}}
    gs = {10: {"name": "R1", "genres": ["Roguelike"], "price": 5000},
          11: {"name": "R2", "genres": ["Roguelike"], "price": 8000},
          12: {"name": "R3", "genres": ["Roguelike"], "price": 10000}}
    rw = w.compute_wishlist_guard(ps, gs, statuses={10: "在玩", 11: "搁置", 12: "未玩"})
    assert len(rw["warnings"]) == 1
    warn = rw["warnings"][0]
    assert warn["genre"] == "Roguelike" and warn["owned_count"] == 3
    assert warn["avg_playtime_hours"] == 0.08  # 15 分钟 / 60 / 3
    assert warn["dust_rate"] == 66.7 and warn["play_ratio"] == 66.7
    assert "冷静三秒" in warn["warning_message"]

    # 全免费类型不告警（必须由 is_free 明确标注）
    free_only = {10: {"playtime": 0, "last_played": 0}, 11: {"playtime": 0, "last_played": 0},
                 12: {"playtime": 0, "last_played": 0}}
    free_games = {i: {"name": f"F{i}", "genres": ["Free Game"], "price": 0, "is_free": True}
                  for i in (10, 11, 12)}
    rf = w.compute_wishlist_guard(free_only, free_games)
    assert all(x["genre"] != "Free Game" for x in rf["warnings"])
    assert all(item["unknown_price_count"] == 0 for item in rf["genres"])

    # 价格未知的类型仍要告警（未知 ≠ 免费，不能因为"看起来是 0 元"就豁免）
    unknown_games = {i: {"name": f"U{i}", "genres": ["Unknown Price"], "price": 0}
                     for i in (20, 21, 22)}
    ru = w.compute_wishlist_guard(
        {20: {"playtime": 0, "last_played": 0}, 21: {"playtime": 0, "last_played": 0},
         22: {"playtime": 0, "last_played": 0}},
        unknown_games,
    )
    assert any(x["genre"] == "Unknown Price" for x in ru["warnings"])
    bucket = next(item for item in ru["genres"] if item["genre"] == "Unknown Price")
    assert bucket["unknown_price_count"] == 3 and bucket["free_count"] == 0
    assert bucket["avg_price_yuan"] == 0.0
    print("PASS wishlist_guard 统计/警告条件/全免费豁免/未知价仍告警")


def test_achievement_radar_excludes_completed():
    """已 100%（剩余 0 个）的游戏不进候选榜；其余条目仍按原规则排序。"""
    data = {
        # 已 100% 全成就：不该出现在候选榜（用户口径：无需冲刺就不显示）
        10: {"achieved": 20, "total": 20, "completion_rate": 100.0},
        11: {"achieved": 5, "total": 5, "completion_rate": 100.0},
        # 剩余 0 但 achieved 只给整数、rate 缺失：仍应按「已 100%」剔除
        12: {"achieved": 5, "total": 5},
        # 差 1 个：应保留，且排序在最前
        13: {"achieved": 9, "total": 10, "completion_rate": 90.0},
        # 差 3 个：保留
        14: {"achieved": 7, "total": 10, "completion_rate": 70.0},
    }
    games = {i: {"name": f"G{i}"} for i in data}
    pt = {13: 600, 14: 1200}
    r = ar.compute_achievement_radar(data, games, pt)
    ids = [c["appid"] for c in r["candidates"]]
    assert ids == [13, 14], ids
    assert all(c["remaining"] > 0 for c in r["candidates"])
    assert not any(c["appid"] in (10, 11, 12) for c in r["candidates"])
    summary = r["summary"]
    assert summary["completed"] == 3, summary
    assert summary["incomplete"] == 2, summary        # == 候选榜条数
    assert summary["with_data"] == 5, summary          # completed + incomplete
    assert summary["total"] == 5, summary
    print("PASS 成就雷达：已 100% 不进候选榜，summary 如实计数")

def test_rules_json_still_valid():
    """rules.json 未被本轮破坏，stats 仍可加载。"""
    from engine.stats import load_rules
    rules = load_rules()
    assert rules["playtime"]["addicted_min"] == 3000
    assert rules["recommender"]["top_k"] == 5
    print("PASS rules.json 可加载（回归）")


def main():
    tests = [
        test_value_ranking,
        test_value_ranking_custom,
        test_value_ranking_unknown_price_not_free,
        test_series_tree,
        test_series_tree_official_and_owned_filter,
        test_series_tree_merges_duplicate_series,
        test_merge_series_catalogs,
        test_series_tree_authoritative_coverage,
        test_franchise_html_parsing,
        test_series_cache_roundtrip,
        test_wishlist_guard,
        test_achievement_radar_excludes_completed,
        test_rules_json_still_valid,
    ]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 组验证通过")


if __name__ == "__main__":
    main()