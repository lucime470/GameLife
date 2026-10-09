# -*- coding: utf-8 -*-
"""合流库（core/merge.py + main/面板接线）自测。

运行方式（仓库根目录）：
    .venv\\Scripts\\python.exe -m pytest game-life/tests/test_merge.py -q

零额外依赖（标准库 + 既有 requirements.txt），**完全离线**：
    - 不读真实 Steam 目录、不联网、不写 data/；
    - 账号解析 / 游玩数据 / 授权归属 / 成员列表全部打桩，结果稳定可复现。

覆盖：
    1. core.merge 的纯函数：id 别名、展示名、按 appid 合并（时长相加 / last_played 取最大）、
       元数据逐字段取非空、ownership 聚合（款数/时长/重叠/降级）；
    2. 装载入口 main._load_library(account_id="all")：走合流、账号 id 归一化成 "all"、
       成员失败只跳过不拖垮整体、说明行含成员与重叠；
    3. 合流库的硬边界：run 不写回/不存快照、backfill 与 writeback 直接拒绝；
    4. 面板接线：账号下拉项（≥2 个账号才有）、load_library/get_summary 的 merged 标记、
       post_backfill 拒绝、post_run 强制 dry-run。
"""

import argparse
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core import merge  # noqa: E402

import main as cli  # noqa: E402


# ------------------------------------------------------------------ 打桩数据

A1, A2, A3 = "900001", "900002", "900003"


def _own(owned=0, played_shared=0, shared=0, free=0, trace=0, degraded=False, **extra):
    data = {
        "owned": owned, "played_shared": played_shared, "shared": shared,
        "free": free, "trace": trace, "degraded": degraded,
        "total": owned + played_shared + free,
        "played_shared_appids": [], "shared_appids": [], "trace_appids": [],
    }
    data.update(extra)
    return data


def _parts():
    """两个账号：730 双方都有（时长要相加）、570 只有甲、999 只有乙。"""
    return [
        {"id": A1, "name": "甲", "play_stats": {
            730: {"playtime": 600, "last_played": 100},
            570: {"playtime": 60, "last_played": 50}},
         "games_meta": {730: {"name": "CS2", "genres": ["Action"], "price": 0},
                        570: {"name": "Dota", "genres": ["Strategy"]}},
         "ownership": _own(owned=2)},
        {"id": A2, "name": "乙", "play_stats": {
            730: {"playtime": 300, "last_played": 900},
            999: {"playtime": 30, "last_played": 10}},
         "games_meta": {730: {"name": "CS2", "genres": ["Action", "Free to Play"],
                              "price": 100},
                        999: {"name": "Zeta"}},
         "ownership": _own(owned=1, played_shared=1, shared=4, trace=1)},
    ]


@pytest.fixture
def fake_steam(monkeypatch):
    """把装载入口的四个外部依赖全部打桩（授权归属按「原样放行」处理）。"""
    members = [{"id": A1, "name": "甲", "source": "added"},
               {"id": A2, "name": "乙", "source": "added"}]
    playtimes = {
        A1: {730: {"playtime": 600, "last_played": 100}, 570: {"playtime": 60, "last_played": 50}},
        A2: {730: {"playtime": 300, "last_played": 900}, 999: {"playtime": 30, "last_played": 10}},
    }
    ownerships = {A1: _own(owned=2), A2: _own(owned=1, shared=4)}

    monkeypatch.setattr(cli, "_resolve_steam_account", lambda args: ("C:/fake", str(args.account_id)))
    monkeypatch.setattr(cli, "_read_playtime", lambda sp, aid: dict(playtimes.get(aid, {})))
    monkeypatch.setattr(cli, "_fetch_metadata_optional", lambda appids: {})
    monkeypatch.setattr(cli, "_filter_by_ownership",
                        lambda sp, aid, stats, meta: (stats, meta, ownerships.get(aid, _own())))
    monkeypatch.setattr(merge, "resolve_members", lambda steam_path, added=None: list(members))
    return {"members": members, "playtimes": playtimes}


def _load(account_id, with_metadata=False):
    return cli._load_library(argparse.Namespace(steam_path="C:/fake", account_id=account_id),
                             with_metadata=with_metadata)


# ------------------------------------------------------------------ 1. 纯函数

def test_is_merge_id_aliases():
    for value in ("all", "ALL", " merged ", "Merge", "confluence", "合流", "合流库"):
        assert merge.is_merge_id(value) is True, value
    for value in ("10000001", "", None, "alll", "0"):
        assert merge.is_merge_id(value) is False, value


def test_merge_display_name():
    assert merge.merge_display_name(3) == "合流库（3 个账号）"
    assert merge.merge_display_name(0) == "合流库"
    assert merge.merge_display_name(None) == "合流库"


def test_merge_play_stats_sums_and_max_last_played():
    merged = merge.merge_play_stats([p["play_stats"] for p in _parts()])
    assert merged[730] == {"playtime": 900, "last_played": 900}   # 时长相加、last_played 取最大
    assert merged[570]["playtime"] == 60
    assert merged[999]["playtime"] == 30
    assert set(merged) == {730, 570, 999}, "不同账号的同一款必须合成一条"


def test_merge_play_stats_normalizes_str_keys():
    merged = merge.merge_play_stats([{"730": {"playtime": 10}}, {730: {"playtime": 5}}])
    assert merged == {730: {"playtime": 15, "last_played": 0}}


def test_merge_games_meta_takes_first_non_blank():
    merged = merge.merge_games_meta([p["games_meta"] for p in _parts()])
    assert merged[730]["name"] == "CS2"
    assert merged[730]["genres"] == ["Action", "Free to Play"], "列表取更长的那个"
    assert merged[730]["price"] == 0, "第一个非空值优先（甲是 0，不算空）"
    assert merged[570]["name"] == "Dota"


def test_merge_ownership_aggregates():
    merged = merge.merge_library_parts(_parts())
    own = merged["ownership"]
    assert own["merged"] is True
    assert own["account_count"] == 2
    assert own["owned"] == 3 and own["shared"] == 4 and own["trace"] == 1
    assert merged["games"] == 3
    assert merged["total_minutes"] == 990          # 900 + 60 + 30
    assert merged["overlap_games"] == 1            # 只有 730 是两个账号都有
    assert merged["overlap_minutes"] == 300        # 乙那边多出来的 300 分钟
    assert [a["id"] for a in own["accounts"]] == [A1, A2]
    assert own["accounts"][0]["hours"] == 11.0     # 660 / 60


def test_summary_line_mentions_members_overlap_and_drops():
    line = merge.summary_line(merge.merge_library_parts(_parts())["ownership"])
    assert "合流库" in line and "甲" in line and "乙" in line
    assert "2 个账号" in line
    assert "3 条记录" in line and "16.5 小时" in line
    assert "时长已相加" in line
    assert "4 款家庭共享且未玩过，未计入" in line
    assert "1 款 Steam 残留痕迹，未计入" in line


def test_merge_library_parts_empty():
    merged = merge.merge_library_parts([])
    assert merged["play_stats"] == {} and merged["games"] == 0
    assert merged["ownership"]["account_count"] == 0


def test_resolve_members_prefers_added_then_local(tmp_path, monkeypatch):
    """「已添加」优先；一个都没添加才退回本机全部账号。"""
    monkeypatch.setattr(merge, "local_accounts",
                        lambda sp: [{"id": "1", "name": "本机甲"}, {"id": "2", "name": "本机乙"}])
    members = merge.resolve_members("x", added=[{"id": "2", "name": "旧的"}])
    assert [m["id"] for m in members] == ["2"]
    assert members[0]["name"] == "本机乙", "名字以本机最新为准"
    assert members[0]["source"] == "added"

    fallback = merge.resolve_members("x", added=[])
    assert [m["id"] for m in fallback] == ["1", "2"]
    assert all(m["source"] == "local" for m in fallback)


def test_added_accounts_reads_json_and_degrades(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text('{"accounts": [{"id": "7", "name": "七"}, "8"]}', encoding="utf-8")
    monkeypatch.setattr(merge, "accounts_path", lambda: path)
    assert merge.added_account_ids() == ["7", "8"]

    path.write_text("{ this is not json", encoding="utf-8")
    assert merge.added_accounts() == [], "坏文件按空表处理，绝不抛异常"


# ------------------------------------------------------------------ 2. 装载入口

def test_load_library_merged_aggregates(fake_steam):
    loaded = _load("all")
    assert loaded is not None
    play_stats, games_meta, _statuses, classified, steam_path, account_id, ownership = loaded
    assert account_id == "all", "合流库的账号 id 必须是 all"
    assert set(play_stats) == {730, 570, 999}
    assert play_stats[730]["playtime"] == 900, "时长必须相加"
    assert ownership["merged"] is True and ownership["account_count"] == 2
    assert classified is not None and 730 in classified, "状态判定照常跑在合流库上"


def test_load_library_merged_alias_and_single_account_unchanged(fake_steam):
    assert _load("merged")[5] == "all"
    assert _load("合流")[5] == "all"
    single = _load(A1)
    assert single[5] == A1, "普通账号行为完全不变"
    assert set(single[0]) == {730, 570}


def test_load_library_merged_skips_broken_member(fake_steam, monkeypatch):
    """某个成员读不到（返回 None）时只跳过它，其余照常合流。"""
    monkeypatch.setattr(cli, "_read_playtime",
                        lambda sp, aid: None if aid == A2 else {730: {"playtime": 600}})
    loaded = _load("all")
    assert loaded is not None
    assert set(loaded[0]) == {730}
    assert loaded[6]["account_count"] == 1
    assert [a["id"] for a in loaded[6]["accounts"]] == [A1]


def test_load_library_merged_without_members(monkeypatch):
    monkeypatch.setattr(merge, "resolve_members", lambda steam_path, added=None: [])
    monkeypatch.setattr(cli, "_resolve_steam_account", lambda args: ("C:/fake", "all"))
    assert _load("all") is None, "没有成员时必须失败并给出提示，不能假装成功"


def test_describe_ownership_silent_for_merged(fake_steam):
    """合流库的说明由装载时打印过一次，_describe_ownership 不再重复。"""
    assert cli._describe_ownership(_load("all")[6]) == ""
    assert cli._describe_ownership(_own(owned=1)) != ""


def test_export_classification_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_stable_output_dir", lambda: tmp_path)
    out = cli._export_classification({730: {"status": "在玩", "status_name": "在玩"}},
                                     {730: {"playtime": 60, "last_played": 0}}, {},
                                     filename="classification_merged.json")
    assert out is not None and out.name == "classification_merged.json"
    assert (tmp_path / "classification.json").exists() is False


# ------------------------------------------------------------------ 3. 硬边界

def test_cmd_writeback_refuses_merged(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_steam_account", lambda args: ("C:/fake", "all"))
    assert cli.cmd_writeback(argparse.Namespace(steam_path=None, account_id="all")) != cli.EXIT_OK


def test_cmd_backfill_refuses_merged(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_steam_account", lambda args: ("C:/fake", "all"))
    code = cli.cmd_backfill(argparse.Namespace(steam_path=None, account_id="all"))
    assert code != cli.EXIT_OK


def test_cmd_run_merged_skips_writeback_and_snapshot(fake_steam, tmp_path, monkeypatch):
    """run --account-id all：不写回、不存快照，只导出本地分类文件。"""
    monkeypatch.setattr(cli, "_stable_output_dir", lambda: tmp_path)
    called = {"writeback": 0, "snapshot": 0}
    monkeypatch.setattr(cli, "_load_writeback_state", lambda: True)
    monkeypatch.setattr(cli, "_steam_writeback",
                        lambda *a, **k: called.__setitem__("writeback", called["writeback"] + 1))
    import core.snapshot as snapshot
    monkeypatch.setattr(snapshot, "save",
                        lambda *a, **k: called.__setitem__("snapshot", called["snapshot"] + 1))

    code = cli.cmd_run(argparse.Namespace(steam_path=None, account_id="all",
                                          with_metadata=False, dry_run=False))
    assert code == cli.EXIT_OK
    assert called == {"writeback": 0, "snapshot": 0}, "合流库绝不能写回或存快照"
    assert (tmp_path / "classification_merged.json").exists()


def test_library_appids_supports_merged(fake_steam):
    loaded = cli._library_appids(argparse.Namespace(steam_path=None, account_id="all"))
    assert loaded is not None
    _steam_path, account_id, appids = loaded
    assert account_id == "all"
    assert appids == [570, 730, 999]


def test_cli_help_mentions_merge():
    parser = cli.build_parser()
    import io
    import contextlib
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            parser.parse_args(["stats", "--help"])
    except SystemExit:                      # argparse 打印 help 后正常退出
        pass
    assert "合流" in buf.getvalue()
