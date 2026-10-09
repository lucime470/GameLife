"""非游戏条目「唯一口径」收尾回归（H 线）。

背景（主 agent 实测，非推理）：G1 把判定收敛到 :mod:`core.non_game` 后，还剩两处
各自留着一份黑名单 —— ``engine/recommender.py``（自带 ``NON_GAME_TYPES`` + appinfo
类型索引加载 + 条目 meta 的 ``type`` 字段）与 ``webapp/static/app.js``（硬编码
``[7, 760, 241100]``）。两份黑名单意味着「哪个条目算游戏」有两个答案，改一处漏一处。

H 线处理后：``recommender`` 的判定改调唯一口径，前端不再自己过滤（服务端
``webapp/api.py`` 返回的 ``games`` 与 ``recommendations`` 已经干净）。

本文件断言：

  * **唯一口径**：把 ``core.non_game.is_non_game`` 换成哨兵，六个入口
    （``engine.recommender`` / ``engine.value_ranking`` / ``engine.wishlist_guard`` /
    ``engine.series_tree`` / ``engine.stats`` / ``webapp.api``）全都真的调它；
  * **``recommender`` 不再自带黑名单**：源码里没有 ``NON_GAME_TYPES`` 字面量、
    没有 ``load_app_type_index`` / ``_type_index_path`` / ``_stable_data_dir`` 定义；
  * **推荐候选池干净**：``Application`` / ``Tool`` / ``Config`` 类条目不进候选池，
    而**类型未知的条目必须留在池里**（铁律：绝不误杀真游戏）；
  * **前端无硬编码黑名单**：``app.js`` 里不再出现 ``NON_GAME_APPIDS`` 或
    ``7, 760, 241100``，过滤职责归服务端；
  * **``rules.json:exclude_appids`` 仍然生效**：它是**用户可配置**的额外排除项，
    与「是不是游戏」语义不同，这次重构不得弄丢它。
"""

import contextlib
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core import non_game  # noqa: E402
from engine import recommender, series_tree, stats, value_ranking, wishlist_guard  # noqa: E402
from webapp import api as panel_api  # noqa: E402

# 本机 appinfo 里真实存在的非游戏条目（都**不在**默认 exclude_appids 里，
# 好让「type 判定」这条路径被单独验证，而不是被用户配置的排除项掩盖）
APPLICATION = 431960          # Application —— 壁纸引擎
TOOL = 3132990                # Tool —— 性能测试工具
CONFIG_APPID = 1182480        # Config
MIRROR_NEWS = 1856540         # Application
NON_GAME_BY_TYPE = (APPLICATION, TOOL, CONFIG_APPID, MIRROR_NEWS)

# 合成条目：类型未知（索引查不到）与明确的真游戏
UNKNOWN = 900001
REAL_GAME = 900002


@contextlib.contextmanager
def fake_app_types(mapping):
    """只打桩 ``core.non_game._app_type``：口径唯一，一处生效全局。"""
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
    """把唯一口径换成会记录调用的哨兵，证明上层**确实**在调它。"""
    calls: list = []
    original = non_game.is_non_game

    def _spy(appid):
        calls.append(appid)
        return original(appid)

    non_game.is_non_game = _spy
    try:
        yield calls
    finally:
        non_game.is_non_game = original


def _library(appids):
    play_stats, games = {}, {}
    for i, appid in enumerate(appids):
        play_stats[appid] = {"playtime": (i + 1) * 60, "last_played": 1690000000}
        games[appid] = {"name": f"Game {appid}", "genres": ["Action"], "price": 6800}
    return play_stats, games


def _lib_for_panel(appids):
    play_stats, games = _library(appids)
    return {
        "play_stats": play_stats,
        "games_meta": games,
        "classified": {},
        "statuses": {},
        "steam_path": None,
    }


# ------------------------------------------------------------------ 唯一口径

def test_all_entry_points_delegate_to_single_source():
    """六个入口的判定都委托 core.non_game：换成哨兵即全部上钩。"""
    play_stats, games = _library(list(NON_GAME_BY_TYPE) + [UNKNOWN, REAL_GAME])
    hook = {}

    with fake_app_types({a: "Application" for a in NON_GAME_BY_TYPE}):
        with spy_non_game() as calls:
            del calls[:]
            recommender.recommend(play_stats, games, mood="轻松", time_left="不限",
                                  statuses={}, top_k=50)
            hook["recommender"] = len(calls)

            del calls[:]
            stats.compute_stats(play_stats, games)
            hook["stats"] = len(calls)

            del calls[:]
            value_ranking.compute_value_ranking(play_stats, games)
            hook["value_ranking"] = len(calls)

            del calls[:]
            wishlist_guard.compute_wishlist_guard(play_stats, games)
            hook["wishlist_guard"] = len(calls)

            del calls[:]
            series_tree.compute_series_tree(play_stats, games, series_map={}, official={})
            hook["series_tree"] = len(calls)

            del calls[:]
            panel_api._games_rows(_lib_for_panel(list(play_stats)))
            hook["webapp_api"] = len(calls)

    missing = {name: n for name, n in hook.items() if n <= 0}
    assert not missing, f"这些入口没有调唯一口径：{missing}（全部计数：{hook}）"


def test_recommender_no_longer_owns_a_blacklist():
    """源码级防回归：recommender 不得再自带黑名单 / 类型集合 / 索引加载。"""
    source = (APP_ROOT / "engine" / "recommender.py").read_text(encoding="utf-8")
    for banned in (
        "NON_GAME_TYPES = frozenset({",
        "frozenset({7, 760, 241100})",
        "def load_app_type_index",
        "def _type_index_path",
        "def _stable_data_dir",
        "_TYPE_INDEX_CACHE",
    ):
        assert banned not in source, f"recommender.py 又自己写了一份：{banned}"
    # 反过来：它必须真的引用唯一口径
    assert "non_game.is_non_game" in source, "recommender.py 没有调用 core.non_game"
    # 已删除的内部符号不得再被本模块导出
    for symbol in ("load_app_type_index", "_type_index_path", "_stable_data_dir"):
        assert not hasattr(recommender, symbol), f"recommender 仍导出 {symbol}"


# ------------------------------------------------------------------ 候选池

def test_candidate_pool_excludes_non_games_but_keeps_unknown():
    """合成库：Application / Tool / Config 不进池；**类型未知必须留在池里**。"""
    appids = list(NON_GAME_BY_TYPE) + [UNKNOWN, REAL_GAME]
    play_stats, games = _library(appids)
    types = {APPLICATION: "Application", TOOL: "Tool", CONFIG_APPID: "Config",
             MIRROR_NEWS: "application", UNKNOWN: "", REAL_GAME: "Game"}

    with fake_app_types(types):
        result = recommender.recommend(play_stats, games, mood="随便", time_left="不限",
                                       statuses={}, top_k=50)

    kept = {item["appid"] for item in result["recommendations"]}
    assert result["candidate_count"] == 2, (result["candidate_count"], sorted(kept))
    assert kept == {UNKNOWN, REAL_GAME}, kept
    for appid in NON_GAME_BY_TYPE:
        assert appid not in kept, f"非游戏条目漏进候选池：{appid}"


def test_candidate_pool_clean_against_real_appinfo_index():
    """同一件事用本机真实索引再验一次（不依赖任何打桩）。"""
    from core import appinfo

    index = appinfo.load_index()
    if not index:
        pytest.skip("本机没有 appinfo 索引，跳过真实索引断言")

    missing = [a for a in NON_GAME_BY_TYPE if str(a) not in index]
    if missing:
        pytest.skip(f"本机索引缺少 {missing}，跳过真实索引断言")
    if any(str(index[str(a)].get("type") or "").strip().lower() == "game"
           for a in NON_GAME_BY_TYPE):
        pytest.skip("本机索引把这几个 appid 记成了 Game，语义已变，跳过")

    non_game.reset_cache()
    play_stats, games = _library(list(NON_GAME_BY_TYPE) + [UNKNOWN, 730])
    result = recommender.recommend(play_stats, games, mood="随便", time_left="不限",
                                   statuses={}, top_k=50)
    kept = {item["appid"] for item in result["recommendations"]}
    assert kept == {UNKNOWN, 730}, kept
    assert result["candidate_count"] == 2, result["candidate_count"]


def test_unknown_type_never_killed_even_with_empty_index():
    """铁律守卫：索引完全读不到时（类型一律未知）不得误杀任何条目。"""
    appids = [1, 2, 3, 4, 5, 6]
    play_stats, games = _library(appids)
    with fake_app_types({}):
        result = recommender.recommend(play_stats, games, mood="随便", time_left="不限",
                                       statuses={}, top_k=50)
    assert result["candidate_count"] == len(appids), result["candidate_count"]


# ------------------------------------------------- 用户可配置排除项仍然生效

def test_user_configured_exclude_appids_still_effective():
    """``rules.json:exclude_appids`` 是用户可配置项，语义不同于「非游戏」，不得弄丢。"""
    appids = [UNKNOWN, REAL_GAME]
    play_stats, games = _library(appids)
    types = {UNKNOWN: "", REAL_GAME: "Game"}

    # 在内存里合成一份「用户把 exclude_appids 加了一条」的规则（只读 rules.json，不写它）
    base_rules = recommender.load_rules()
    custom_rules = {
        **base_rules,
        "recommender": {**base_rules["recommender"], "exclude_appids": [REAL_GAME]},
    }

    with fake_app_types(types):
        baseline = recommender.recommend(play_stats, games, mood="随便", time_left="不限",
                                         statuses={}, top_k=50)
        excluded = recommender.recommend(
            play_stats, games, mood="随便", time_left="不限", statuses={}, top_k=50,
            rules=custom_rules,
        )

    assert {i["appid"] for i in baseline["recommendations"]} == {UNKNOWN, REAL_GAME}
    kept = {i["appid"] for i in excluded["recommendations"]}
    assert kept == {UNKNOWN}, kept
    assert excluded["candidate_count"] == 1, excluded["candidate_count"]

    # 内置默认值本身也要还在（用户没配时靠它兜底）
    assert list(recommender.RECOMMENDER_DEFAULTS["exclude_appids"]) == [7, 760, 241100]


# ------------------------------------------------------------------ 前端

def test_frontend_has_no_hardcoded_non_game_blacklist():
    """app.js 不得再硬编码黑名单：判定只有服务端一份。"""
    js = (APP_ROOT / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    assert "NON_GAME_APPIDS" not in js, "前端又出现了自己的非游戏黑名单常量"
    assert "7, 760, 241100" not in js, "前端又硬编码了那三个 appid"
    assert "241100" not in js, "前端仍硬编码 appid 241100"


def test_server_side_is_the_single_filter_for_frontend():
    """前端放弃过滤的前提：服务端确实干净，且判定就是唯一口径。"""
    # 面板模块的判定与集合都指向唯一口径（不是各自复制一份）
    assert panel_api.NON_GAME_APPIDS is non_game.NON_GAME_APPIDS
    assert panel_api.NON_GAME_TYPES is non_game.NON_GAME_TYPES

    non_game.reset_cache()
    for appid in NON_GAME_BY_TYPE:
        assert non_game.is_non_game(appid) is True, appid
    assert non_game.is_non_game(730) is False
    assert non_game.is_non_game(UNKNOWN) is False, "类型未知必须放行"

    # 端到端：服务端返回的游戏明细里没有非游戏条目
    lib = _lib_for_panel(list(NON_GAME_BY_TYPE) + [UNKNOWN, 730])
    rows = panel_api._games_rows(lib)
    returned = {r["appid"] for r in rows}
    assert not (returned & set(NON_GAME_BY_TYPE)), returned & set(NON_GAME_BY_TYPE)
    assert returned == {UNKNOWN, 730}, returned