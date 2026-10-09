"""T22 线：``core.steam_live``（Steam 客户端实时归属源）离线闸门。

背景（用户反馈，实测非推理）
---------------------------
静态痕迹分辨不了「本账号买过、却从没安装过」与「家庭组远端成员共享给我」——
两者在本机留下的东西一模一样（``librarycache`` 里一行库索引）。
``core.steam_live`` 走 Steam 客户端自带的 CEF 调试端口，直接调
``CFamilyGroups_GetSharedLibraryApps`` 拿每个 appid 的 ``owner_steamids``：
这是 Steam 给出的归属，不是推断，也不含任何硬编码 appid。

本文件**完全离线**：不连端口（用 ``ENV_DISABLE`` / 假 ``_evaluate``），
落盘缓存改写进 ``tmp_path``（绝不碰真实的 ``data/steam_live_ownership.json``）。
"""

import json
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))
_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from core import steam_live  # noqa: E402
from test_ownership import MAIN, OWNER  # noqa: E402


def _s64(account) -> str:
    """account_id -> SteamID64 字符串（客户端返回的就是这一套）。"""
    return str(steam_live.STEAM64_BASE + int(account))


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """落盘缓存改到 ``tmp_path``；并打开实时源（会话级 conftest 默认关着）。"""
    path = tmp_path / "steam_live_ownership.json"
    monkeypatch.setattr(steam_live, "_cache_path", lambda: path)
    monkeypatch.delenv(steam_live.ENV_DISABLE, raising=False)
    monkeypatch.delenv(steam_live.ENV_PORT, raising=False)
    steam_live.reset_cache()
    yield path
    steam_live.reset_cache()


# ------------------------------------------------------------------ 视角闸门（_view）

def test_view_splits_owned_and_others():
    """``owner`` 含本账号 -> owned；其余（含 owner 为空的非游戏项）-> others。"""
    apps = {"100": [int(MAIN)], "200": [int(OWNER)], "300": []}
    view = steam_live._view(MAIN, apps, at=5, source="live", group="g1")
    assert view["owned"] == frozenset({100})
    assert view["others"] == frozenset({200, 300})
    assert view["at"] == 5 and view["source"] == "live" and view["group"] == "g1"


def test_view_rejects_account_outside_group():
    """账号不在清单里（不是该组成员 / 客户端答的是别人）-> 一律拒收。"""
    apps = {"200": [int(OWNER)]}
    assert steam_live._view(MAIN, apps, at=0, source="live", group="g1") is None
    assert steam_live._view("all", apps, at=0, source="live", group="g1") is None
    assert steam_live._view("999", apps, at=0, source="live", group="g1") is None


def test_view_returns_none_for_empty_manifest():
    assert steam_live._view(MAIN, {}, at=0, source="live", group="g1") is None


# ------------------------------------------------------------------ 解析（_parse）

def _raw(owner, apps, *, ok=True, group="g1"):
    return json.dumps({"ok": ok, "group": group, "owner": owner, "apps": apps})


def test_parse_normalizes_steamid64_to_account_id():
    raw = _raw(_s64(MAIN), {"100": [_s64(MAIN)], "200": [_s64(OWNER)]})
    view = steam_live._parse(MAIN, _s64(MAIN), raw)
    assert view["owned"] == frozenset({100})
    assert view["others"] == frozenset({200})
    assert view["apps"] == {"100": [int(MAIN)], "200": [int(OWNER)]}
    assert view["source"] == "live" and view["group"] == "g1"


def test_parse_rejects_answer_for_other_account():
    """客户端答的是别人（本账号根本不在清单里）-> 拒收，绝不张冠李戴。"""
    raw = _raw(_s64(OWNER), {"200": [_s64(OWNER)]})
    assert steam_live._parse(MAIN, _s64(MAIN), raw) is None


@pytest.mark.parametrize("raw", [
    "",
    "not json",
    json.dumps({"ok": False, "error": "no service"}),
    json.dumps({"ok": True, "group": None, "owner": "", "apps": {}}),
    json.dumps({"ok": True, "group": "g1", "owner": "", "apps": []}),
    json.dumps({"ok": True, "group": "g1", "owner": "", "apps": {"x": ["1"]}}),
])
def test_parse_degrades_on_bad_payloads(raw):
    assert steam_live._parse(MAIN, _s64(MAIN), raw) is None


def test_account_of_and_clean():
    assert steam_live._account_of(_s64(MAIN)) == int(MAIN)
    assert steam_live._account_of("x") is None
    assert steam_live._account_of(0) is None
    assert steam_live._clean(MAIN) == MAIN
    assert steam_live._clean("all") is None
    assert steam_live._clean(None) is None


# ------------------------------------------------------------------ fetch / ownership_for

@pytest.mark.parametrize("value", ["0", "false", "off", "no", "disable", "disabled"])
def test_fetch_short_circuits_when_disabled(monkeypatch, value):
    """``GAMELIFE_STEAM_LIVE`` 置为关闭取值时，连端口这一步都不能发生。"""
    seen = []
    monkeypatch.setenv(steam_live.ENV_DISABLE, value)
    monkeypatch.setattr(steam_live, "_evaluate", lambda *a, **k: seen.append(1))
    assert steam_live.fetch(MAIN) is None
    assert steam_live.ownership_for(MAIN) is None
    assert seen == [], "关掉实时源时绝不能连端口"


def test_fetch_uses_client_evaluate(monkeypatch):
    monkeypatch.setattr(
        steam_live, "_evaluate",
        lambda port, expr, timeout: _raw(_s64(MAIN), {"100": [_s64(MAIN)]}),
    )
    view = steam_live.fetch(MAIN, port=9999)
    assert view["owned"] == frozenset({100})


def test_fetch_returns_none_when_port_dead(monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("端口不通")

    monkeypatch.setattr(steam_live, "_evaluate", boom)
    assert steam_live.fetch(MAIN) is None
    assert steam_live.ownership_for(MAIN) is None


def test_ownership_for_memoizes_and_falls_back_to_cache(monkeypatch):
    calls = []
    payload = _raw(_s64(MAIN), {"100": [_s64(MAIN)], "200": [_s64(OWNER)]})

    def fake_fetch(account, *, port=None, timeout=None):
        calls.append(account)
        return steam_live._parse(account, _s64(account), payload)

    monkeypatch.setattr(steam_live, "fetch", fake_fetch)
    first = steam_live.ownership_for(MAIN)
    assert first["source"] == "live" and first["owned"] == frozenset({100})
    assert steam_live.ownership_for(MAIN) is first, "进程内只问一次"
    assert calls == [MAIN]

    # 新进程（清掉内存记忆）：端口不通时退回落盘快照，且不再重复连端口
    steam_live.reset_cache()
    monkeypatch.setattr(steam_live, "fetch", lambda *a, **k: None)
    cached = steam_live.ownership_for(MAIN)
    assert cached["source"] == "cache" and cached["owned"] == frozenset({100})
    assert calls == [MAIN]


def test_cache_serves_other_member_of_same_group(monkeypatch):
    """一次实时成功 -> 整份家庭组清单落盘，组里另一个成员离线也能复用。"""
    payload = _raw(_s64(MAIN), {"100": [_s64(MAIN)], "200": [_s64(OWNER)]})
    monkeypatch.setattr(steam_live, "_evaluate", lambda *a, **k: payload)
    steam_live.ownership_for(MAIN)
    steam_live.reset_cache()
    monkeypatch.setattr(steam_live, "fetch", lambda *a, **k: None)
    other = steam_live.ownership_for(OWNER, refresh=False)
    assert other is not None and other["source"] == "cache"
    assert other["owned"] == frozenset({200}) and other["group"] == "g1"


# ------------------------------------------------------------------ 落盘缓存

def test_store_live_records_group_and_accounts(_isolate):
    view = steam_live._view(MAIN, {"100": [int(MAIN)]}, at=7, source="live", group="g7")
    view["apps"] = {"100": [int(MAIN)]}
    steam_live._store_live(MAIN, view)
    data = json.loads(_isolate.read_text(encoding="utf-8"))
    assert data["version"] == steam_live.CACHE_VERSION
    assert data["groups"]["g7"]["apps"] == {"100": [int(MAIN)]}
    assert data["accounts"][MAIN]["group"] == "g7"


def test_cache_never_lends_other_groups_manifest(_isolate):
    """缓存里别的家庭组的清单**绝不能**错套到这个账号头上（成员闸门保护）。"""
    data = {"version": steam_live.CACHE_VERSION,
            "groups": {"g1": {"at": 1, "apps": {"300": [int(OWNER)]}}},
            "accounts": {str(MAIN): {"group": "g1", "at": 1}}}
    _isolate.write_text(json.dumps(data), encoding="utf-8")
    assert steam_live._from_cache(MAIN) is None
    found = steam_live._from_cache(OWNER)
    assert found is not None and found["owned"] == frozenset({300})


def test_cache_version_mismatch_is_ignored(_isolate):
    _isolate.write_text(json.dumps({
        "version": steam_live.CACHE_VERSION + 1,
        "groups": {"g1": {"at": 1, "apps": {"300": [int(MAIN)]}}},
        "accounts": {},
    }), encoding="utf-8")
    assert steam_live._from_cache(MAIN) is None


def test_cache_read_tolerates_missing_or_broken_file(_isolate):
    assert steam_live._from_cache(MAIN) is None
    _isolate.write_text("{not json", encoding="utf-8")
    assert steam_live._from_cache(MAIN) is None


# ------------------------------------------------------------------ 环境变量

@pytest.mark.parametrize("value, expected", [
    ("", steam_live.DEFAULT_PORT),
    ("9222", 9222),
    ("abc", steam_live.DEFAULT_PORT),
    ("0", steam_live.DEFAULT_PORT),
    ("70000", steam_live.DEFAULT_PORT),
])
def test_port_env(monkeypatch, value, expected):
    if value:
        monkeypatch.setenv(steam_live.ENV_PORT, value)
    else:
        monkeypatch.delenv(steam_live.ENV_PORT, raising=False)
    assert steam_live._port() == expected