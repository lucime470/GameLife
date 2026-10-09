"""族谱「拥有 / 玩过（家庭库）/ 未拥有」三态回归闸门。

背景（用户反馈，实测非推理）
---------------------------
``engine/series_tree.compute_series_tree`` 历史上把「在库里」当成「已拥有」，而库是
**家庭共享过滤后**的集合，于是出现两种自相矛盾的错标：

  * ``553850 绝地潜兵2`` / ``628070 三国志11``：家庭共享、本账号真玩过 —— 被标成
    「已拥有」（其实不属于自己）；
  * 真拥有、但从没启动过（``playtime == 0``，例如 ``49520 无主之地2``）：不在
    ``playtime`` 记录里 —— 反被标成「未拥有」。

现在的口径（唯一来源 :mod:`core.ownership`）：
  * ``owned``  **确实拥有** —— 授权票 ``flag == 0``（与玩没玩过无关）；
  * ``played`` **未拥有但本账号玩过** —— 家庭共享 / 免费体验，``playtime > 0``；
  * 其余为「未拥有」。读不到授权票时**降级**为历史行为「在库里即算拥有」。

本文件全部离线、在 ``tmp_path`` 里造假的 steam 目录树（复用 ``test_ownership`` 的
``make_steam``），换台机器也能跑。
"""

import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from core import ownership  # noqa: E402
from engine import series_tree as st  # noqa: E402
from test_ownership import (  # noqa: E402
    MAIN,
    OWNER,
    make_library_cache,
    make_sharing_log,
    make_steam,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    ownership.reset_cache()
    yield
    ownership.reset_cache()


# 一部真拥有但没启动过（1001）/ 一部家庭共享且玩过（1002）/ 一部家庭共享没玩过（1003）
# 另有一整个系列只有「家庭共享且玩过」的成员（2011 玩过、2012 没玩过）
_OFFICIAL = {"Saga": [1001, 1002, 1003], "Only Shared": [2011, 2012]}
_PLAY_STATS = {
    1002: {"playtime": 60, "last_played": 0},    # 1001 不在 play_stats（没启动过）
    2011: {"playtime": 30, "last_played": 0},
}

# 免费游戏（``core.ownership.FREE``）：没有授权票，但确确实实在自己库里玩过。
# 3001 = 玩过的免费游戏（该标「已拥有」）/ 3002 = 没玩过的免费游戏 /
# 3003 = 家庭共享且玩过（该标「玩过（家庭库）」，不能被免费分支误收）
_FREE_OFFICIAL = {"Freebies": [3001, 3002, 3003, 3004]}
_FREE_PLAY_STATS = {
    3001: {"playtime": 37, "last_played": 0},
    3002: {"playtime": 0, "last_played": 0},     # 在库里（免费入库）但从没启动
    3003: {"playtime": 30, "last_played": 0},
    3004: {"playtime": 0, "last_played": 0},     # 家庭共享且没玩过（grayed）
}


def _steam(root: Path) -> Path:
    return make_steam(
        root,
        accounts={MAIN: {1001: 0, 1002: 2, 1003: 2, 2011: 2, 2012: 2},
                  OWNER: {1002: 0, 1001: 0, 9001: 0}},
    )


def _tree(root: Path, *, account_id: str = MAIN, **kwargs) -> dict:
    return st.compute_series_tree(
        _PLAY_STATS, {}, series_map={}, official=_OFFICIAL, aliases={},
        steam_path=str(root), account_id=account_id, **kwargs,
    )


def _saga(tree: dict) -> dict:
    return next(s for s in tree["series"] if s["series"] == "Saga")


def test_owned_is_flag_zero_not_played(tmp_path):
    """① 确实拥有（flag == 0）就标「已拥有」——哪怕它从没被启动过。"""
    root = _steam(tmp_path)
    saga = _saga(_tree(root))
    row = next(g for g in saga["games"] if g["appid"] == 1001)
    assert row["owned"] is True, "真拥有（flag==0）必须标「已拥有」，与玩没玩过无关"
    assert row["played"] is False
    assert saga["owned_count"] == 1 and saga["played_count"] == 1


def test_shared_and_played_is_played_not_owned(tmp_path):
    """② 家庭共享但玩过 -> 标「玩过」，**不是**「已拥有」（绝地潜兵2 的根因）。"""
    root = _steam(tmp_path)
    saga = _saga(_tree(root))
    row = next(g for g in saga["games"] if g["appid"] == 1002)
    assert row["owned"] is False, "家庭共享的游戏不属于自己，绝不能标「已拥有」"
    assert row["played"] is True and row["playtime_hours"] == 1.0
    # 时长要算进系列总时长（玩过就参与统计），但仍不计入 owned_count
    assert saga["playtime_hours"] == 1.0


def test_shared_never_played_is_neither(tmp_path):
    """③ 家庭共享且没玩过 -> 两态都不占（也没有时长）。"""
    root = _steam(tmp_path)
    row = next(g for g in _saga(_tree(root))["games"] if g["appid"] == 1003)
    assert row["owned"] is False and row["played"] is False
    assert row["playtime_hours"] == 0 and row["status"] == ""


def test_played_only_series_is_still_shown(tmp_path):
    """④ 一部都没真拥有、只有「玩过」的系列照常展示（进榜靠 played_count）。"""
    root = _steam(tmp_path)
    tree = _tree(root)
    item = next(s for s in tree["series"] if s["series"] == "Only Shared")
    assert item["owned_count"] == 0 and item["played_count"] == 1
    assert item["total_count"] == 2
    assert tree["unowned_series"] >= 1


def test_unowned_series_is_hidden(tmp_path):
    """⑤ 既没拥有、也没玩过的系列不展示。"""
    root = _steam(tmp_path)
    official = {"Nothing": [7001, 7002]}
    tree = st.compute_series_tree(
        _PLAY_STATS, {}, series_map={}, official=official, aliases={},
        steam_path=str(root), account_id=MAIN,
    )
    assert tree["series_count"] == 0


def test_degrades_to_legacy_when_no_ticket(tmp_path):
    """⑥ 读不到授权票（没给 steam_path）-> 退回「在库里即拥有」，绝不判空。"""
    tree = st.compute_series_tree(
        _PLAY_STATS, {}, series_map={}, official=_OFFICIAL, aliases={},
    )
    saga = _saga(tree)
    # 降级时 1002（在 play_stats 里）按历史行为算「已拥有」
    assert next(g for g in saga["games"] if g["appid"] == 1002)["owned"] is True
    assert saga["owned_count"] == 1 and saga["played_count"] == 0
    assert saga["games"][0]["played"] is False


def test_ownership_map_injection_overrides(tmp_path):
    """⑦ 可注入 ``ownership_map``（离线/测试）：不再读盘，三态直接生效。"""
    tree = st.compute_series_tree(
        _PLAY_STATS, {}, series_map={}, official=_OFFICIAL, aliases={},
        account_id=MAIN,
        ownership_map={1001: "owned", 1002: "played_shared", 1003: "shared"},
    )
    saga = _saga(tree)
    by_id = {g["appid"]: g for g in saga["games"]}
    assert by_id[1001]["owned"] is True and by_id[1001]["played"] is False
    assert by_id[1002]["owned"] is False and by_id[1002]["played"] is True
    assert by_id[1003]["owned"] is False and by_id[1003]["played"] is False


def test_merged_account_unions_member_ownership(tmp_path, monkeypatch):
    """⑧ 合流库：一款游戏只要任一成员真拥有，在合流库里就算「拥有」。"""
    from core import merge

    root = _steam(tmp_path)
    monkeypatch.setattr(
        merge, "resolve_member_ids", lambda steam_path, added=None: [MAIN, OWNER]
    )
    kinds = st._resolve_ownership_kinds(str(root), merge.MERGE_ID, [1001, 1002], _PLAY_STATS)
    assert kinds is not None
    assert kinds[1002] == ownership.OWNED, "OWNER 真拥有 1002 -> 合流库里应为 owned"
    assert kinds[1001] == ownership.OWNED


def test_cli_run_prints_three_states(tmp_path, monkeypatch, capsys):
    """⑨ CLI 文案三态：已拥有 / 玩过（家庭库）/ 未拥有。"""
    from types import SimpleNamespace

    root = _steam(tmp_path)
    monkeypatch.setattr(st, "official_series", lambda *a, **k: dict(_OFFICIAL))
    args = SimpleNamespace(
        play_stats=_PLAY_STATS, games={}, series_map={}, statuses={},
        steam_path=str(root), account_id=MAIN,
    )
    assert st.run(args) == 0
    out = capsys.readouterr().out
    assert "[已拥有" in out
    assert "[玩过（家庭库）" in out
    assert "[未拥有]" in out
    assert "另有 1 个系列你未拥有、但在家庭库里玩过" in out


def test_free_game_in_library_is_owned_not_played(tmp_path):
    """⑫ 免费游戏（``FREE`` 类）**在库里就算「已拥有」**，不能标成「玩过（家庭库）」。

    用户反馈：神之浩劫 / 饥荒 / Dota 霸业这类免费游戏被标成「玩过（家庭库）」——
    它们没有授权票（免费游戏本来就没有），但确实是自己库里的一份。
    ``3001`` 玩过、``3002`` 没玩过，都应算拥有；``3003`` 是家庭共享（``SHARED``/
    ``PLAYED_SHARED``）不能被免费分支误收。
    """
    tree = st.compute_series_tree(
        _FREE_PLAY_STATS, {}, series_map={}, official=_FREE_OFFICIAL, aliases={},
        account_id=MAIN,
        ownership_map={
            3001: "free", 3002: "free", 3003: "played_shared", 3004: "shared",
        },
    )
    item = next(s for s in tree["series"] if s["series"] == "Freebies")
    by_id = {g["appid"]: g for g in item["games"]}
    assert by_id[3001]["owned"] is True and by_id[3001]["played"] is False
    assert by_id[3001]["playtime_hours"] == 0.62      # 37 分钟，四舍五入到两位
    assert by_id[3002]["owned"] is True, "免费游戏没玩过也是自己的，必须标「已拥有」"
    assert by_id[3003]["owned"] is False and by_id[3003]["played"] is True
    assert by_id[3004]["owned"] is False and by_id[3004]["played"] is False
    assert item["owned_count"] == 2 and item["played_count"] == 1


def test_free_kind_outside_library_is_not_owned(tmp_path):
    """⑬ 类别是 ``free``，但游戏**根本不在库里**（play_stats / games 都没有）-> 不算拥有。

    免费分支的判据是「类别 == free **且** 在库里」，两者同时成立才算拥有；
    只靠类别会把它人的条目 / 目录里的在售作品误判成本账号自有。
    """
    official = {"Ghosts": [5001, 5002]}
    tree = st.compute_series_tree(
        {5002: {"playtime": 5, "last_played": 0}}, {}, series_map={},
        official=official, aliases={}, account_id=MAIN,
        ownership_map={5001: "free", 5002: "free"},
    )
    item = next(s for s in tree["series"] if s["series"] == "Ghosts")
    by_id = {g["appid"]: g for g in item["games"]}
    assert by_id[5001]["owned"] is False and by_id[5001]["played"] is False
    assert by_id[5002]["owned"] is True


# ------------------------------------------------------------------ 面板（webapp）

def _panel_lib(root: Path) -> dict:
    """面板 ``load_library`` 的返回形状（这里手工拼，避免真去读盘）。"""
    return {
        "steam_path": str(root),
        "account_id": MAIN,
        "play_stats": dict(_PLAY_STATS),
        "games_meta": {},
        "statuses": {},
        "classified": {},
    }


def _inject_catalog(monkeypatch) -> None:
    """让 webapp 走注入的系列目录（否则它会去读本机真实的离线缓存）。"""
    monkeypatch.setattr(st, "official_series", lambda *a, **k: dict(_OFFICIAL))
    monkeypatch.setattr(st, "load_series_map", lambda *a, **k: {})
    monkeypatch.setattr(st, "load_official_aliases", lambda *a, **k: {})


def test_series_detail_rows_three_states(tmp_path, monkeypatch):
    """⑩ 详情页卡片行：确实拥有 / 玩过（家庭库）/ 未拥有 三态齐全且排序正确。"""
    from webapp import api as panel_api

    root = _steam(tmp_path)
    monkeypatch.setattr(panel_api, "_require_library", lambda *a, **k: _panel_lib(root))
    _inject_catalog(monkeypatch)
    data = panel_api.get_series_detail("Saga", account_id=MAIN, fetch_covers=False)
    assert data["owned_count"] == 1 and data["played_count"] == 1
    assert data["playtime_hours"] == 1.0            # 玩过的时长计入总时长
    by_id = {r["appid"]: r for r in data["games"]}
    assert by_id[1001]["owned"] is True and by_id[1001]["played"] is False
    assert by_id[1002]["owned"] is False and by_id[1002]["played"] is True
    assert by_id[1003]["owned"] is False and by_id[1003]["played"] is False
    # 排序：已拥有 -> 玩过 -> 未拥有
    assert [r["appid"] for r in data["games"]] == [1001, 1002, 1003]


def test_series_section_marks_played_thumbs(tmp_path, monkeypatch):
    """⑪ 面板系列框：thumbs_played 与 thumbs 一一对应，玩过的排在未拥有之前。"""
    from webapp import api as panel_api

    root = _steam(tmp_path)
    lib = _panel_lib(root)
    _inject_catalog(monkeypatch)
    monkeypatch.setattr(
        panel_api, "_cover_url",
        lambda appid, steam_path=None: f"/covers/{int(appid)}.jpg",
    )
    section = panel_api._series_section(lib)
    saga = next(s for s in section["series"] if s["series"] == "Saga")
    assert saga["owned_count"] == 1 and saga["played_count"] == 1
    assert saga["thumbs_owned"] == [True, False, False]
    assert saga["thumbs_played"] == [False, True, False]
    assert len(saga["thumbs"]) == len(saga["thumbs_played"]) == len(saga["thumbs_owned"])


# ------------------------------------------------------------------ 家庭组远端共享 / 退款残留（T14）

def test_series_borrowed_remote_is_never_owned(tmp_path):
    """借自家庭组**远端成员**的游戏：没玩过只显示「未拥有」，玩过才是「玩过（家庭库）」。

    用户点名的 ``24960 战地：叛逆连队2`` 属于前者 —— 本机没有任何账号有它的票，
    只有共享日志记着 owner 是远端成员（本机无 userdata）。
    """
    from test_ownership import _changed

    official = {"Squad": [6101, 6102]}
    steam = make_steam(
        tmp_path / "steam",
        accounts={MAIN: {}, OWNER: {}},
        apps={MAIN: {6101: True, 6102: True}},
    )
    make_sharing_log(steam, [_changed(6101, "1531614643"), _changed(6102, "1531614643")])
    ownership.reset_cache()

    tree = st.compute_series_tree(
        {6102: {"playtime": 45, "last_played": 0}}, {},
        series_map={}, official=official, aliases={},
        steam_path=str(steam), account_id=MAIN,
    )
    item = next(s for s in tree["series"] if s["series"] == "Squad")
    by_id = {g["appid"]: g for g in item["games"]}
    assert by_id[6101]["owned"] is False and by_id[6101]["played"] is False
    assert by_id[6102]["owned"] is False and by_id[6102]["played"] is True
    assert item["owned_count"] == 0 and item["played_count"] == 1


def test_series_refund_residue_is_not_owned(tmp_path, monkeypatch):
    """库索引里的**退款残留**：权威自有清单没有它 -> 显示「未拥有」。

    用户点名的 ``413410 Danganronpa: Trigger Happy Havoc`` 买过又退款，只剩
    ``librarycache`` 里一份残留（``play_stats`` 里已经没有它了）。库索引不是权威，
    必须与客户端自有清单取交集，否则残留会被升级成「已拥有」。
    """
    from test_ownership import make_client_cache

    official = {"Refunded": [6201, 6202]}
    monkeypatch.setattr(ownership, "_LOCALAPPDATA", str(tmp_path / "local"))
    steam = make_steam(tmp_path / "steam", accounts={MAIN: {}, OWNER: {}})
    make_library_cache(steam, MAIN, [6201, 6202])
    make_client_cache(tmp_path / "local", MAIN, [6202] + [4000 + i for i in range(30)])
    ownership.reset_cache()

    tree = st.compute_series_tree(
        {}, {}, series_map={}, official=official, aliases={},
        steam_path=str(steam), account_id=MAIN,
    )
    item = next(s for s in tree["series"] if s["series"] == "Refunded")
    by_id = {g["appid"]: g for g in item["games"]}
    assert by_id[6201]["owned"] is False, "退款残留不在自有清单里 -> 不算拥有"
    assert by_id[6201]["played"] is False
    assert by_id[6202]["owned"] is True, "清单里有 -> 已拥有"
    assert item["owned_count"] == 1
    ownership.reset_cache()


# ------------------------------------------------------------------ Steam 客户端实时归属（T22 线）

def _fake_live_snapshot(monkeypatch, owned, others, *, team=(MAIN,), group="g-test"):
    """把一个假的权威归属快照接到 ``core.steam_live`` 上（完全离线）。"""
    from core import steam_live

    snapshot = {"owned": frozenset(int(x) for x in owned),
                "others": frozenset(int(x) for x in others),
                "at": 1, "source": "live", "group": group}
    monkeypatch.setattr(
        steam_live, "ownership_for",
        lambda account_id, **kwargs: (snapshot if str(account_id) in {str(m) for m in team}
                                      else None),
    )
    ownership.reset_cache()


def test_authoritative_snapshot_marks_true_owned_and_family_played(tmp_path, monkeypatch):
    """权威清单在手：真拥有（哪怕没玩过）标「已拥有」；家庭库玩过标「玩过（家庭库）」。"""
    official = {"T22": [8001, 8002, 8003]}
    steam = make_steam(tmp_path / "steam", {MAIN: {8002: 2}})
    _fake_live_snapshot(monkeypatch, owned={8001}, others={8002, 8003})

    tree = st.compute_series_tree(
        {8003: {"playtime": 30, "last_played": 0}}, {}, series_map={},
        official=official, aliases={}, steam_path=str(steam), account_id=MAIN,
    )
    item = next(s for s in tree["series"] if s["series"] == "T22")
    by_id = {g["appid"]: g for g in item["games"]}
    assert by_id[8001]["owned"] is True and by_id[8001]["played"] is False
    assert by_id[8002]["owned"] is False and by_id[8002]["played"] is False
    assert by_id[8003]["owned"] is False and by_id[8003]["played"] is True
    assert item["owned_count"] == 1 and item["played_count"] == 1


def test_authoritative_snapshot_hides_pure_family_series(tmp_path, monkeypatch):
    """整个系列都属于家庭组别人、且一条都没玩过 -> 不展示（用户点名的命令与征服 / 杀手）。"""
    official = {"Family": [8101, 8102]}
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    _fake_live_snapshot(monkeypatch, owned=set(), others={8101, 8102})

    tree = st.compute_series_tree(
        {}, {}, series_map={}, official=official, aliases={},
        steam_path=str(steam), account_id=MAIN,
    )
    assert tree["series_count"] == 0


def test_authoritative_snapshot_unions_merge_members(tmp_path, monkeypatch):
    """合流库：任一成员真拥有即算拥有（与 :func:`_classify_union` 同一口径）。"""
    from core import merge
    from core import steam_live

    official = {"Merged": [8201, 8202]}
    steam = make_steam(tmp_path / "steam", {MAIN: {}, OWNER: {}})
    monkeypatch.setattr(
        merge, "resolve_member_ids", lambda steam_path, added=None: [MAIN, OWNER]
    )
    snapshots = {
        MAIN: {"owned": frozenset({8201}), "others": frozenset({8202}),
               "at": 1, "source": "live", "group": "g"},
        OWNER: {"owned": frozenset({8202}), "others": frozenset({8201}),
                "at": 1, "source": "live", "group": "g"},
    }
    monkeypatch.setattr(steam_live, "ownership_for",
                        lambda account_id, **kwargs: snapshots.get(str(account_id)))
    ownership.reset_cache()

    tree = st.compute_series_tree(
        {}, {}, series_map={}, official=official, aliases={},
        steam_path=str(steam), account_id=merge.MERGE_ID,
    )
    item = next(s for s in tree["series"] if s["series"] == "Merged")
    assert item["owned_count"] == 2
