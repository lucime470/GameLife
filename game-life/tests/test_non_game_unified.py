"""非游戏条目「唯一口径」与六页中文名的回归闸门（G1 线）。

背景（主 agent 实测，非推理）：主账号 174 个条目里有 8 个不是游戏
（``7 Steam Client`` / ``760 Steam Screenshots`` / ``241100 Steam Input Configs`` /
``431960 Wallpaper Engine`` / ``1182480 EALink`` / ``1856540 Mirror News Center`` /
``2371090 Steam Game Notes`` / ``3132990 黑神话：悟空 性能测试工具``）。它们曾经：

  * 让面板顶部显示 **174** 款，而「游戏明细」列表显示 **166** 款 —— 同一个程序里
    两个数字并存；
  * 让「性价比榜」第 5 名躺着 **Wallpaper Engine：壁纸引擎**（``431960``）；
  * 让「反向愿望单」冒出 **实用工具 / 软件培训 / 网络出版 / 游戏开发 /
    动画制作和建模 / 设计和插画 / 照片编辑** 这些**软件类型**；
  * 让年度报告显示 **174 款 / 2174.5h**（含壁纸引擎 2064 分钟）。

根因：每个模块各自维护一份黑名单 —— ``webapp/api.py`` / ``core/render_bridge.py``
各有一份同名同值的 ``NON_GAME_APPIDS`` / ``_NON_GAME_TYPES``，
``engine/value_ranking.py`` / ``engine/series_tree.py`` **只有 3 项黑名单、不查 type**，
``engine/stats.py`` / ``engine/wishlist_guard.py`` 则完全没过滤。

修复：口径收敛到唯一一处 :mod:`core.non_game`，其余模块全部改调它。

本文件断言（离线、可复现、不碰真实 cache.db / Steam 目录）：

  * 判定本身：黑名单命中、非游戏 type 命中、**类型未知一律放行**、非法输入不炸；
  * **口径唯一**：各模块的判定都委托给 ``core.non_game``（换成哨兵即全线上钩），
    且没有任何模块还在自己维护黑名单；
  * ``engine.stats`` / ``engine.value_ranking`` / ``engine.wishlist_guard`` /
    ``engine.series_tree`` 的库范围都不含非游戏条目，且**不误杀**未知类型；
  * 性价比榜不含 ``Application`` 类型的条目；反向愿望单不含软件类型；
  * 六页 payload 与面板 ``stats.total_games`` **相等**；
  * 六页中文名：只有英文名时也能解析出权威中文名；两张表都查不到时**保持原名、不臆造**。
"""

import contextlib
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core import non_game  # noqa: E402
from engine import series_tree, stats, value_ranking, wishlist_guard  # noqa: E402
from webapp import api as panel_api  # noqa: E402

# 本机实测的 8 个非游戏条目（appid -> appinfo type）
NON_GAMES = {
    7: "Config",
    760: "Config",
    241100: "Config",
    431960: "Application",
    1182480: "Config",
    1856540: "Application",
    2371090: "Config",
    3132990: "Tool",
}
# 真游戏（type=Game）+ 索引里查不到的条目（类型未知）
GAMES = {77700021: "Game", 77700022: "Game"}
UNKNOWN = 77700023
GHOST = 77700024            # 只有英文名、任何中文表都查不到


@contextlib.contextmanager
def fake_app_types(mapping):
    """只打桩``core.non_game._app_type``：口径唯一，一处生效全局。"""
    original = non_game._app_type
    saved = dict(non_game._MEMO)
    non_game._app_type = lambda appid: str(mapping.get(int(appid)) or "").strip().lower()
    non_game._MEMO.clear()
    try:
        yield
    finally:
        non_game._app_type = original
        non_game._MEMO.clear()
        non_game._MEMO.update(saved)


@contextlib.contextmanager
def spy_non_game():
    """把 ``core.non_game.is_non_game`` 换成会记录调用的哨兵。

    用来证明上层**确实**在调唯一口径，而不是各留一份实现。
    """
    calls: list = []
    original = non_game.is_non_game

    def _spy(appid):
        calls.append(appid)
        non_game._MEMO.pop(int(appid), None) if str(appid).lstrip("-").isdigit() else None
        return original(appid)

    non_game.is_non_game = _spy
    try:
        yield calls
    finally:
        non_game.is_non_game = original


def _library():
    """8 个非游戏 + 2 款真游戏 + 1 个未知类型条目。"""
    play_stats, games = {}, {}
    for i, appid in enumerate(list(NON_GAMES) + list(GAMES) + [UNKNOWN]):
        play_stats[appid] = {"playtime": (i + 1) * 60, "last_played": 1690000000}
    for i, appid in enumerate(list(NON_GAMES) + list(GAMES) + [UNKNOWN]):
        games[appid] = {
            "name": f"Game {appid}",
            "genres": ["Action"] if i % 2 == 0 else ["Utilities"],
            "price": 6800 if appid not in NON_GAMES else 0,
        }
    return play_stats, games


# ------------------------------------------------------------------ 判定本身

def test_is_non_game_blacklist_types_and_unknown_pass_through():
    with fake_app_types({**NON_GAMES, **GAMES}):
        for appid in NON_GAMES:
            assert non_game.is_non_game(appid) is True, appid
        for appid in GAMES:
            assert non_game.is_non_game(appid) is False, appid
        assert non_game.is_non_game(UNKNOWN) is False, "类型未知必须放行"
        assert non_game.is_non_game("abc") is False
        assert non_game.is_non_game(None) is False
        assert non_game.is_non_game(-1) is False

    # 黑名单不依赖 appinfo：拿不到类型也照样命中
    with fake_app_types({}):
        for appid in (7, 760, 241100):
            assert non_game.is_non_game(appid) is True, appid


def test_real_index_classifies_known_non_games():
    """本机真实 appinfo 在场时，8 个已知非游戏条目必须判为非游戏、真游戏放行。"""
    from core import appinfo

    if not appinfo.load_index():
        pytest.skip("本机没有 appinfo 索引（类型未知 -> 一律放行），跳过真实索引断言")

    non_game.reset_cache()
    for appid in NON_GAMES:
        assert non_game.is_non_game(appid) is True, appid
    for appid in (730, 480, 1245620):          # 真游戏
        assert non_game.is_non_game(appid) is False, appid
    assert non_game.is_non_game(10 ** 9) is False, "索引里没有 -> 未知 -> 放行"


def test_exclude_non_games_is_pure_and_keeps_unknown():
    play_stats = {431960: {"playtime": 2064}, 730: {"playtime": 60}, UNKNOWN: {"playtime": 1}, "abc": {"playtime": 0}}
    with fake_app_types({**NON_GAMES, **GAMES, 730: "Game"}):
        kept = non_game.exclude_non_games(play_stats)
    assert set(kept) == {730, UNKNOWN, "abc"}, kept
    assert 431960 in play_stats, "不得修改入参"
    assert kept is not play_stats


# ------------------------------------------------------------------ 口径唯一

def test_judgement_is_delegated_to_single_source():
    """各模块的判定都委托给 core.non_game：换成哨兵即全线上钩。"""
    play_stats, games = _library()

    with fake_app_types({**NON_GAMES, **GAMES}):
        with spy_non_game() as calls:
            api_modules_hooked = {}
            # 面板：列表过滤
            rows = panel_api._games_rows({"play_stats": play_stats, "games_meta": games,
                                          "classified": {}, "statuses": {},
                                          "steam_path": None})
            api_modules_hooked["panel_games_rows"] = len(calls) > 0
            del rows

            calls.clear()
            stats.compute_stats(play_stats, games)
            api_modules_hooked["stats"] = len(calls) > 0

            calls.clear()
            value_ranking.compute_value_ranking(play_stats, games)
            api_modules_hooked["value_ranking"] = len(calls) > 0

            calls.clear()
            wishlist_guard.compute_wishlist_guard(play_stats, games)
            api_modules_hooked["wishlist_guard"] = len(calls) > 0

            calls.clear()
            series_tree.compute_series_tree(play_stats, games, series_map={}, official={})
            api_modules_hooked["series_tree"] = len(calls) > 0

    assert all(api_modules_hooked.values()), api_modules_hooked


def test_no_module_keeps_its_own_blacklist():
    """没有任何模块还在自己维护黑名单/类型集合（都指向同一对象）。"""
    from core import render_bridge

    # 黑名单/类型集合是同一个对象（不是各自复制的副本）
    for module in (render_bridge, panel_api, value_ranking, series_tree):
        assert module.NON_GAME_APPIDS is non_game.NON_GAME_APPIDS, module.__name__
    for module in (render_bridge, panel_api):
        assert module.NON_GAME_TYPES is non_game.NON_GAME_TYPES, module.__name__

    # is_non_game 都是**转发**：把唯一口径换成哨兵，各模块的结果必须一起变
    # （若谁偷偷留了一份自己的实现，这里就不会跟着变）
    original = non_game.is_non_game
    non_game.is_non_game = lambda appid: True
    try:
        assert render_bridge.is_non_game(730) is True
        assert panel_api.is_non_game(730) is True
        assert render_bridge.exclude_non_games({730: {}}) == {}
        assert panel_api.exclude_non_games({730: {}}) == {}
    finally:
        non_game.is_non_game = original
    assert render_bridge.is_non_game(730) is False
    assert panel_api.is_non_game(730) is False


def test_engine_sources_do_not_redeclare_blacklist():
    """源码级防回归：引擎/渲染源里不得再出现自建的黑名单字面量。"""
    bad_literal = "frozenset({7, 760, 241100})"
    for rel in ("engine/stats.py", "engine/value_ranking.py", "engine/series_tree.py",
                "engine/wishlist_guard.py", "core/render_bridge.py", "webapp/api.py"):
        source = (APP_ROOT / rel).read_text(encoding="utf-8")
        assert bad_literal not in source, f"{rel} 又自己写了一份黑名单"
        assert 'NON_GAME_TYPES = frozenset({' not in source or rel == "core/non_game.py", \
            f"{rel} 又自己写了一份非游戏 type 集合"


# ------------------------------------------------------------------ 各模块库范围

def test_stats_library_scope_excludes_non_games():
    play_stats, games = _library()
    with fake_app_types({**NON_GAMES, **GAMES}):
        result = stats.compute_stats(play_stats, games)
        scope = stats.library_scope(play_stats, games)

    expected = set(GAMES) | {UNKNOWN}
    assert set(scope) == expected, scope
    assert result["total_games"] == len(expected)
    # 非游戏条目的时长不再计入总数
    assert result["total_playtime_hours"] == round(sum(
        (play_stats[a]["playtime"]) for a in expected) / 60, 2)


def test_value_ranking_has_no_non_game_and_clean_library_count():
    play_stats, games = _library()
    with fake_app_types({**NON_GAMES, **GAMES}):
        result = value_ranking.compute_value_ranking(play_stats, games)

    top_ids = {i["appid"] for key in ("value_top", "waste_top", "zero_cost_top")
               for i in result[key]}
    assert not (top_ids & set(NON_GAMES)), top_ids & set(NON_GAMES)
    # 「壁纸引擎」这类 Application 绝不能出现在任何榜单
    assert 431960 not in top_ids
    assert result["stats"]["library_count"] == len(GAMES) + 1


def test_wishlist_guard_has_no_software_genres():
    """反向愿望单不得出现「软件类型」（壁纸引擎 / 资讯类软件的官方类型）。"""
    play_stats = {}
    games = {}
    # 3 款同类型软件条目 + 3 款真游戏（保证类型统计的告警条件能被触发）
    software = {431960: "Application", 1856540: "Application", 3132990: "Tool"}
    for appid in software:
        play_stats[appid] = {"playtime": 0, "last_played": 0}
        games[appid] = {"name": f"Software {appid}", "genres": ["Utilities"], "price": 0}
    for appid in (77700031, 77700032, 77700033):
        play_stats[appid] = {"playtime": 0, "last_played": 0}
        games[appid] = {"name": f"Game {appid}", "genres": ["Action"], "price": 6800}

    with fake_app_types({**software, 77700031: "Game", 77700032: "Game", 77700033: "Game"}):
        result = wishlist_guard.compute_wishlist_guard(play_stats, games)

    genres = {row["genre"] for row in result["genres"]}
    assert "Utilities" not in genres, genres
    assert genres == {"Action"}, genres


def test_series_tree_excludes_non_games():
    play_stats, games = _library()
    official = {"Saga": sorted(list(NON_GAMES) + list(GAMES) + [UNKNOWN])}
    with fake_app_types({**NON_GAMES, **GAMES}):
        result = series_tree.compute_series_tree(
            play_stats, games, series_map={}, official=official, aliases={"Saga": "传说"})

    members = {g["appid"] for item in result["series"] for g in item["games"]}
    assert not (members & set(NON_GAMES)), members & set(NON_GAMES)
    assert UNKNOWN in members, "类型未知必须放行（它在官方目录里）"


# ------------------------------------------------------------------ 六页与面板一致

def test_six_pages_agree_with_panel_total_games():
    from core import render_bridge

    play_stats, games = _library()
    classified = {a: {"status": "搁置", "status_name": "搁置"} for a in play_stats}
    kept = len(GAMES) + 1

    with fake_app_types({**NON_GAMES, **GAMES}):
        totals = {}
        for kind in render_bridge.RENDER_KINDS:
            payload = render_bridge.build_payload(
                kind, play_stats, games, classified=classified,
                snapshots=[], user_name="测试")
            if kind in ("graveyard", "starmap"):
                totals[kind] = len(payload["games"])
            elif kind in ("timeline", "today"):
                totals[kind] = len(payload["play_stats"])
            else:
                totals[kind] = payload["stats"]["total_games"]
        totals["panel"] = panel_api._stats_section(
            {"play_stats": play_stats, "games_meta": games,
             "statuses": {}, "classified": classified})["total_games"]

    assert set(totals.values()) == {kept}, totals


# ------------------------------------------------------------------ 六页中文名

@contextlib.contextmanager
def _no_local_cn():
    """把两张本地权威中文表都打成「查不到」，验证绝不臆造。"""
    import core.appinfo as appinfo
    from core import metadata_cn

    saved_mc = metadata_cn.name_cn_for
    saved_ap = appinfo.localized_name
    metadata_cn.name_cn_for = lambda *a, **k: None
    appinfo.localized_name = lambda *a, **k: None
    try:
        yield
    finally:
        metadata_cn.name_cn_for = saved_mc
        appinfo.localized_name = saved_ap


@contextlib.contextmanager
def sample_chinese_names():
    """把 metadata_cn.name_cn_for 打桩成一张样本表（离线自给，不依赖开发机缓存）。"""
    from core import metadata_cn

    sample = {410110: "12比6好", 2321470: "深岩银河：幸存者", 594650: "猎杀：对决 1896"}
    original = metadata_cn.name_cn_for
    metadata_cn.name_cn_for = lambda appid, *a, **k: sample.get(int(appid)) if str(appid).lstrip("-").isdigit() else None
    try:
        yield
    finally:
        metadata_cn.name_cn_for = original


def test_non_game_module_is_in_render_fingerprint():
    """``core/non_game`` 决定六页内容，必须纳入渲染指纹（改它要触发重画）。"""
    rel = {f.relative_to(panel_api.APP_ROOT).as_posix() for f in panel_api._renderer_source_files()}
    assert "core/non_game.py" in rel, "core/non_game.py 不在渲染指纹里：改了它页面不会重画"
    assert "core/render_bridge.py" in rel


def test_ownership_module_is_in_render_fingerprint():
    """``core/ownership`` 同样决定六页的库范围（T4），必须纳入渲染指纹。

    实测反例（T4 任务书的直接起因）：``ownership`` 剔掉赛博朋克后，六页指纹不变，
    面板继续发旧产物 —— 墓园 / 时光轴里还能看到 ``1091500``。
    """
    rel = {f.relative_to(panel_api.APP_ROOT).as_posix() for f in panel_api._renderer_source_files()}
    assert "core/ownership.py" in rel, "core/ownership.py 不在渲染指纹里：改了它页面不会重画"


def test_page_names_use_authoritative_chinese_tables():
    """六页中文名：本地缓存列没有中文时，继续查 names_cn.json 与本机 appinfo。"""
    from core import render_bridge

    with sample_chinese_names():
        # 1) metadata_cn 有中文 -> 显示中文（410110 为「12比6好」）
        assert render_bridge.game_name(410110, {"name": "12 is Better Than 6"}) == "12比6好"
        # 2) 中文名表里有 -> 显示该中文名
        assert render_bridge.game_name(2321470, {"name": "Deep Rock Galactic: Survivor"}) == "深岩银河：幸存者"
        # 3) 完全缺名字也应拿到中文名，而不是 appid N
        assert render_bridge.game_name(594650, {}) == "猎杀：对决 1896"
        # 4) 本地缓存列仍是最高优先级
        assert render_bridge.game_name(2321470, {"name_cn": "本地列中文"}) == "本地列中文"


def test_page_names_never_fabricate_chinese():
    """绝不臆造：两张表都查不到 -> 保持官方原名；连原名都没有才 appid N。"""
    from core import render_bridge

    with _no_local_cn():
        assert render_bridge.game_name(GHOST, {"name": "Some English Name"}) == "Some English Name"
        assert render_bridge.game_name(GHOST, {}) == f"appid {GHOST}"
        # 英文名冒充中文名的脏数据一律不作数（不得被当成中文名直接显示）
        assert render_bridge.game_name(GHOST, {"name_cn": "Fake English"}) == "appid " + str(GHOST)
        # 有官方原名时，脏中文列被忽略后回落到官方原名
        assert render_bridge.game_name(
            GHOST, {"name_cn": "Fake English", "name": "Real English"}) == "Real English"


def test_page_names_fall_back_to_appinfo_original_name():
    """连英文名都没有的条目：用本机 appinfo 的**官方原名**兜底，最后才是 appid N。

    实测：账号 10000002 的 ``219990``(Grim Dawn) / ``1672970``(Minecraft Dungeons)
    在缓存里没有名字，六页曾显示 ``appid 219990``。appinfo 是权威来源，应当用上。
    """
    from core import appinfo, render_bridge

    if not (appinfo.app_metadata(219990) or {}).get("name"):
        pytest.skip("本机 appinfo 没有 219990，跳过原名兜底断言")

    expected = appinfo.app_metadata(219990)["name"]
    assert render_bridge.game_name(219990, {}) == expected
    assert not render_bridge.game_name(219990, {}).startswith("appid ")
    # 上游已经把名字降级成 "appid N" 占位时，也得继续往下查出真名
    # （engine.stats.most_forgotten -> build_checkup 就是这样把占位符传回来的）
    assert render_bridge.game_name(219990, {"name": f"appid {219990}"}) == expected
    # 索引里查不到的真·无名条目仍然回落 appid N（保留历史行为）
    assert render_bridge.game_name(GHOST, {}) == f"appid {GHOST}"
    assert render_bridge.game_name(GHOST, {"name": f"appid {GHOST}"}) == f"appid {GHOST}"


def test_page_names_are_visible_in_rendered_html():
    """真跑六页渲染：中文名出现在 HTML 里，且不出现空白名字段。"""
    import re
    import tempfile
    from core import render_bridge
    from render import (annual_report, archaeologist, checkup, graveyard,
                        starmap, timemachine)

    from datetime import datetime, timezone

    # today（时光机 · 去年今日）只收录「target_date 的**一年前**」当天的记录
    then = int(datetime(2022, 7, 24, 12, tzinfo=timezone.utc).timestamp())
    play_stats = {410110: {"playtime": 600, "last_played": then},
                  2321470: {"playtime": 300, "last_played": then}}
    games = {410110: {"name": "12 is Better Than 6", "genres": ["Action"]},
             2321470: {"name": "Deep Rock Galactic: Survivor", "genres": ["Action"]}}
    classified = {a: {"status": "搁置", "status_name": "搁置"} for a in play_stats}

    generators = {
        "report": (annual_report.generate_report, lambda: render_bridge.build_report(
            play_stats, games, classified, user_name="玩家")),
        "graveyard": (graveyard.generate_graveyard, lambda: render_bridge.build_library(
            play_stats, games, classified, user_name="玩家")),
        "starmap": (starmap.generate_starmap, lambda: render_bridge.build_library(
            play_stats, games, classified, user_name="玩家")),
        "timeline": (archaeologist.generate_timeline, lambda: render_bridge.build_timeline(
            play_stats, games, [], user_name="玩家")),
        "today": (timemachine.generate_timemachine, lambda: render_bridge.build_today(
            play_stats, games, [], user_name="玩家", target_date="2023-07-24")),
        "checkup": (checkup.generate_checkup, lambda: render_bridge.build_checkup(
            play_stats, games, classified, user_name="玩家")),
    }

    with sample_chinese_names(), tempfile.TemporaryDirectory(prefix="gl_g1_cn_") as tmp:
        out_dir = Path(tmp)
        for kind, (generator, builder) in generators.items():
            out = out_dir / f"{kind}.html"
            generator(builder(), output_path=out, open_browser=False)
            html = out.read_text(encoding="utf-8")
            assert "12比6好" in html, f"{kind}: 页面里没有中文游戏名"
            assert "appid 410110" not in html, f"{kind}: 中文名被回落成 appid N"
            for container in ("game-name", "tomb-name", "unburied-name",
                              "sc-name", "chk-forgot-name", "tm-card-line"):
                blanks = re.findall(r'class="%s">\s*</' % container, html)
                assert not blanks, f"{kind}: 出现空白名字段 {blanks}"
