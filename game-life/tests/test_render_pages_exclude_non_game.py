"""六页可视化渲染路径必须剔除非游戏条目（Steam Client / 壁纸引擎 / 性能工具 …）。

背景（非推理，主 agent 实测）：库里 174 个条目中有 8 个不是游戏
（``7 Steam Client`` / ``760 Steam Screenshots`` / ``241100 Steam Input Configs`` /
``431960 Wallpaper Engine`` / ``1182480 EALink`` / ``1856540 Mirror News Center`` /
``2371090`` 无名条目 / ``3132990 Black Myth: Wukong Benchmark Tool``，type 分别是
Config / Application / Tool）。面板的「游戏明细」「推荐」「性价比榜」「族谱」都已按
``webapp/api.py:is_non_game`` 剔除它们，唯独 6 张可视化页面的渲染路径漏了，于是
墓园里躺着「Steam Client」的墓碑、星图里飘着「壁纸引擎」的星星、时间轴记录
「你开始玩《EALink》」。

修复（G1 线）：判定口径已**收敛到唯一一处** ``core/non_game.py``，
``core/render_bridge.build_payload()``（六页唯一共同入口）改成**六页一律过滤**
（含 report / checkup —— 它们此前的 total_games 与类型时长同样含非游戏条目，
实测 174 款 / 2174.47h，剔掉后 166 款 / 2139.67h）。

本文件断言（离线、可复现、不碰真实 cache.db / Steam 目录）：

  * ``core.non_game.is_non_game`` 口径正确：黑名单命中、非游戏 type 命中、
    **未知类型放行**；
  * ``core.non_game.is_non_game`` 与 ``webapp.api.is_non_game`` **逐 appid 一致**
    （同一口径，后者只是转发）；
  * 过滤是**纯展示层**的：不修改入参 play_stats；
  * 六页 payload 都不含非游戏 appid，且 ``total_games`` / 星星数 / 墓碑数 / 快照行 /
    时间轴事件按过滤后的库**重算**（数字自洽，不留 174 的虚数）；
  * 封面复制不为非游戏条目取图。
"""

import contextlib
import json
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core import non_game  # noqa: E402
from core import render_bridge as rb  # noqa: E402

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
# 真游戏（type=Game）与「索引里查不到、类型未知」的条目。
# 刻意用查不到的合成 appid，让「名字保持原样」这一层的断言稳定：
# 展示名的中文回落（names_cn / appinfo）由 test_render_bridge 的中文名用例负责。
REAL_GAMES = {77700011: "Game", 77700012: "Game", 77700013: "Game"}
UNKNOWN_APPID = 4_000_000_000   # 任何索引里都不该有，类型未知


@contextlib.contextmanager
def fake_app_types(mapping):
    """把 ``core.non_game._app_type`` 换成查表实现（表里没有 -> 空串 = 类型未知），并清空判定缓存。

    判定口径已收敛到 ``core.non_game``，所以只要打桩这一处，全产品（六页 + 面板 +
    引擎）看到的就是同一份类型表。这样测试与「本机 appinfo_index.json 内容」解耦，
    任何机器上都可复现；同时**不**改动 ``core.appinfo`` 本身。
    """
    original = non_game._app_type
    saved_memo = dict(non_game._MEMO)
    non_game._app_type = lambda appid: str(mapping.get(int(appid)) or "").strip().lower()
    non_game._MEMO.clear()
    try:
        yield
    finally:
        non_game._app_type = original
        non_game._MEMO.clear()
        non_game._MEMO.update(saved_memo)


def _library():
    """一个含 8 个非游戏条目 + 3 款真游戏 + 1 个未知类型条目的迷你库。"""
    play_stats = {}
    for i, appid in enumerate(list(NON_GAMES) + list(REAL_GAMES) + [UNKNOWN_APPID]):
        play_stats[appid] = {"playtime": (i + 1) * 60, "last_played": 1690000000}
    games_meta = {}
    for appid in list(NON_GAMES) + list(REAL_GAMES) + [UNKNOWN_APPID]:
        games_meta[appid] = {"name": f"Game {appid}", "genres": ["Action"]}
    return play_stats, games_meta


def _snapshot_rows():
    """一批历史快照行（含非游戏 appid，用于验证 today / timeline 也过滤）。"""
    rows = []
    for appid in list(NON_GAMES) + list(REAL_GAMES) + [UNKNOWN_APPID]:
        rows.append(
            {
                "record_date": "2022-01-01",
                "appid": appid,
                "playtime": 120,
                "last_played": 1640000000,
                "status": "搁置",
            }
        )
    return rows


def _appids_in(payload) -> set:
    """payload 里出现的所有 appid（既查 ``"appid": N`` 也查以 appid 为键的映射）。"""
    found: set = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.isdigit():
                    found.add(int(key))
                if key == "appid":
                    try:
                        found.add(int(value))
                    except (TypeError, ValueError):
                        pass
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(payload)
    return found


def _sha(payload) -> str:
    import hashlib

    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 判定口径

def test_is_non_game_blacklist_types_and_unknown_pass_through():
    """黑名单 / 非游戏 type 命中；**类型未知一律放行**（绝不误杀真游戏）。"""
    with fake_app_types({**NON_GAMES, **REAL_GAMES}):
        for appid in NON_GAMES:
            assert non_game.is_non_game(appid) is True, appid
        for appid in REAL_GAMES:
            assert non_game.is_non_game(appid) is False, appid
        # 索引里查不到 -> 类型未知 -> 当游戏放行
        assert non_game.is_non_game(UNKNOWN_APPID) is False
        # 非法输入不炸、也不误判成非游戏
        assert non_game.is_non_game("abc") is False
        assert non_game.is_non_game(None) is False

    # 黑名单不依赖 appinfo（拿不到类型也照样命中）
    with fake_app_types({}):
        assert non_game.is_non_game(7) is True
        assert non_game.is_non_game(760) is True
        assert non_game.is_non_game(241100) is True


def test_is_non_game_matches_panel_judgement():
    """口径必须与面板其它地方**逐 appid 一致**（同一份黑名单 + 同一份 type 集合）。"""
    from webapp import api as panel_api

    with fake_app_types({**NON_GAMES, **REAL_GAMES}):
        for appid in list(NON_GAMES) + list(REAL_GAMES) + [UNKNOWN_APPID, 999, 0]:
            assert non_game.is_non_game(appid) == panel_api.is_non_game(appid), appid
    # 口径唯一：黑名单/类型集合是同一个对象；判定是**转发**（把唯一口径换成
    # 哨兵，各层结果必须一起变 —— 若谁留了私有实现就不会跟着变）
    assert rb.NON_GAME_APPIDS is non_game.NON_GAME_APPIDS
    assert panel_api.NON_GAME_APPIDS is non_game.NON_GAME_APPIDS
    assert rb.NON_GAME_TYPES is non_game.NON_GAME_TYPES
    assert panel_api.NON_GAME_TYPES is non_game.NON_GAME_TYPES
    assert non_game.NON_GAME_APPIDS == frozenset({7, 760, 241100})


def test_real_index_classifies_known_non_games():
    """本机真实 appinfo 索引在场时，8 个已知非游戏条目必须判为非游戏、真游戏放行。"""
    from core import appinfo

    index = appinfo.load_index()
    if not index:
        pytest.skip("本机没有 appinfo 索引（类型未知 -> 一律放行），跳过真实索引断言")

    non_game.reset_cache()
    for appid in NON_GAMES:
        assert non_game.is_non_game(appid) is True, f"{appid} 应为非游戏（本机索引有该条目）"
    assert non_game.is_non_game(730) is False      # Counter-Strike 2
    assert non_game.is_non_game(480) is False      # Spacewar
    assert non_game.is_non_game(10 ** 9) is False  # 索引里没有 -> 未知 -> 放行


# ------------------------------------------------------------------ 过滤是纯展示层

def test_exclude_non_games_is_pure_and_keeps_unknown():
    """过滤返回新 dict，**不改入参**（不删数据）；未知类型保留、非 int 键原样保留。"""
    play_stats, _ = _library()
    before = json.dumps(play_stats, sort_keys=True, default=str)
    with fake_app_types({**NON_GAMES, **REAL_GAMES}):
        kept = non_game.exclude_non_games(play_stats)
    assert json.dumps(play_stats, sort_keys=True, default=str) == before, "不得修改入参"
    assert kept is not play_stats
    assert set(kept) & set(NON_GAMES) == set(), "非游戏条目必须被滤掉"
    assert UNKNOWN_APPID in kept, "类型未知必须放行"

    with fake_app_types({**NON_GAMES, **REAL_GAMES}):
        odd = non_game.exclude_non_games({"abc": {"playtime": 1}, 730: {"playtime": 2}})
    assert odd["abc"] == {"playtime": 1}
    assert 730 in odd


# ------------------------------------------------------------------ 六页 payload

@pytest.mark.parametrize("kind", list(rb.RENDER_KINDS))
def test_all_pages_exclude_non_games_and_recount(kind):
    """**六页**：payload 里不得出现任何非游戏 appid，且数字按过滤后的库重算。"""
    play_stats, games_meta = _library()
    rows = _snapshot_rows()
    kept_games = len(REAL_GAMES) + 1          # 3 款真游戏 + 1 个未知类型条目
    total_library = len(NON_GAMES) + kept_games

    with fake_app_types({**NON_GAMES, **REAL_GAMES}):
        payload = rb.build_payload(kind, play_stats, games_meta, snapshots=rows, user_name="测试")

    assert _appids_in(payload) & set(NON_GAMES) == set(), f"{kind}: 仍含非游戏 appid"
    assert UNKNOWN_APPID in _appids_in(payload), f"{kind}: 未知类型条目被误杀"

    if kind in ("graveyard", "starmap"):
        assert len(payload["games"]) == kept_games, payload["games"].keys()
        assert f"{total_library}" not in payload["games"], "总库数虚高"
    elif kind == "timeline":
        assert len(payload["play_stats"]) == kept_games
        assert len(payload["games"]) == kept_games
        assert {row["appid"] for row in payload["snapshots"]} <= (set(REAL_GAMES) | {UNKNOWN_APPID})
    elif kind == "today":
        assert len(payload["play_stats"]) == kept_games
        assert {row["appid"] for row in payload["snapshots"]} <= (set(REAL_GAMES) | {UNKNOWN_APPID})
        assert set(payload["games"]) <= {str(a) for a in (set(REAL_GAMES) | {UNKNOWN_APPID})}
    else:
        # report / checkup：原先「不过滤」的借口是「本就不含非游戏条目」，实测是错的。
        # 它们的总数也必须按**过滤后的库**算（含 report 的实例名与类型时长）。
        assert payload["stats"]["total_games"] == kept_games, (kind, payload["stats"])


def test_pages_render_counts_from_filtered_library():
    """过滤后交给真实渲染层 normalize：星星数 / 墓碑数 / 事件数都按过滤后的库算。"""
    from render import archaeologist, graveyard, starmap

    play_stats, games_meta = _library()
    rows = _snapshot_rows()
    kept = len(REAL_GAMES) + 1

    with fake_app_types({**NON_GAMES, **REAL_GAMES}):
        grave = graveyard.normalize_graveyard_data(
            rb.build_payload("graveyard", play_stats, games_meta, snapshots=rows, user_name="测试")
        )
        star = starmap.normalize_starmap_data(
            rb.build_payload("starmap", play_stats, games_meta, snapshots=rows, user_name="测试")
        )
        timeline = archaeologist.normalize_archaeologist_data(
            rb.build_payload("timeline", play_stats, games_meta, snapshots=rows, user_name="测试")
        )

    # 墓园：安息 + 未入土 = 过滤后的库
    assert grave["stats"]["total"] + grave["stats"]["unburied"] == kept, grave["stats"]
    # 星图：星星数与 total_games 同步
    assert len(star["stars"]) == kept
    assert star["stats"]["total_games"] == kept
    # 时间轴：事件里的游戏名不含非游戏条目
    names = {ev["name"] for groups in timeline["year_groups"] for mg in groups["month_groups"] for ev in mg["events"]}
    assert names == {f"Game {a}" for a in (set(REAL_GAMES) | {UNKNOWN_APPID})}, names


def test_all_pages_agree_on_total_games():
    """**六页**与过滤后的库口径一致：同一个库在任何界面都是同一个游戏数。

    用户可见后果（修复前）：面板顶部显示 174、游戏明细显示 166、年度报告 174 款，
    同一个程序里三个数字并存，用户会认为数据不可信。
    """
    play_stats, games_meta = _library()
    classified = {appid: {"status": "搁置", "status_name": "搁置"} for appid in play_stats}
    rows = _snapshot_rows()
    kept = len(REAL_GAMES) + 1

    with fake_app_types({**NON_GAMES, **REAL_GAMES}):
        totals = {}
        for kind in rb.RENDER_KINDS:
            payload = rb.build_payload(kind, play_stats, games_meta, classified=classified,
                                       snapshots=rows, user_name="x")
            if kind in ("graveyard", "starmap"):
                totals[kind] = len(payload["games"])
            elif kind == "timeline":
                totals[kind] = len(payload["play_stats"])
            elif kind == "today":
                totals[kind] = len(payload["play_stats"])
            else:
                totals[kind] = payload["stats"]["total_games"]

        # 面板顶部五块大数字（engine.stats）也必须与六页同口径
        from engine.stats import compute_stats

        totals["panel_stats"] = compute_stats(play_stats, games_meta)["total_games"]

    assert set(totals.values()) == {kept}, totals
    assert rb.LIBRARY_DETAIL_KINDS == frozenset(rb.RENDER_KINDS),         "六页必须一律过滤（历史上只过滤四页，数字才会打架）"


def test_covers_are_not_copied_for_non_games():
    """封面复制不为非游戏条目取图（展示层过滤，不动 data/covers 既有文件）。"""
    import core.artwork as artwork

    saved_panel = getattr(artwork, "panel_cover", None)
    saved_legacy = getattr(artwork, "cover_for_panel", None)
    try:
        artwork.panel_cover = lambda appid, steam_path=None: {"ok": True, "path": f"/tmp/{appid}.jpg"}
        artwork.cover_for_panel = lambda appid, steam_path=None: f"/tmp/{appid}.jpg"
        with fake_app_types({**NON_GAMES, **REAL_GAMES}):
            covers = rb.resolve_covers([431960, 730, UNKNOWN_APPID])
    finally:
        if saved_panel is not None:
            artwork.panel_cover = saved_panel
        if saved_legacy is not None:
            artwork.cover_for_panel = saved_legacy

    assert "431960" not in covers, "非游戏条目不应解析/复制封面"
    assert "730" in covers and str(UNKNOWN_APPID) in covers
