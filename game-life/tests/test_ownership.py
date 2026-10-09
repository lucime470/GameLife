"""授权归属（家庭共享剔除）回归闸门 —— 线别 A3，规则于 A4 修正。

背景（主 agent 实测，非推理）：几个账号在同一个 Steam 家庭库里，家庭库会把**别人拥有**
的游戏也写进本账号的 ``localconfig.vdf``，于是同一个游戏在多个账号下都出现、时长各算一遍。
主账号 ``10000001`` 里有 42 款其实是家庭共享来的（``374320`` / ``1245620`` / ``1623730`` /
``294100`` …），虚增 102.8h。判定口径收敛到唯一一处 :mod:`core.ownership`。

A4 规则修正（用户新需求「保留虽然是家庭库的但是当前账号确实玩过的游戏」）：
家庭共享里再分两类 —— ``playtime > 0``（与 ``engine.classifier`` / ``engine.stats``
同一口径）→ ``PLAYED_SHARED`` **保留**；``playtime == 0`` → ``SHARED`` **剔除**。
只有 ``SHARED`` 会被剔除。

本文件断言（**离线、可复现、绝不读写真实 ``<Steam 安装目录>``**）：
所有用例都在 ``tmp_path`` 里造假的 steam 目录树（``userdata/<id>/config/localconfig.vdf``
+ ``steamapps/appmanifest_*.acf``），换台机器也能跑。

  * ``license_flag``：偏移 28 的 32 位小端整数；**长度不足一律返回 None**；
  * 四条规则：拥有保留 / 标 2（玩过保留、没玩过剔除）/ 无票但别人拥有（玩过保留、
    没玩过剔除）/ 无票且无人拥有保留；
  * **降级**：vdf 读不到 / 账号不存在 / 文件被写坏 → 全部保留、不抛异常；
  * 全产品唯一口径：装载入口（``main._load_library``）与 ``core.library`` 都走这里，
    且过滤后不留孤儿 meta；
  * ``summarize`` 给的数字与 ``LastOwner`` 交叉验证口径自洽。
"""

import struct
import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core import ownership  # noqa: E402

MAIN = "10000001"          # 主账号
OWNER = "10000002"         # 另一个账号（也是家庭组成员）
EMPTY = "10000005"         # 一个什么都没有的账号


# ------------------------------------------------------------------ 造假 steam 目录树

def _ticket(flag: int) -> str:
    """造一张授权票：偏移 28 起 4 字节小端整数 = flag，前面垫无关字节。"""
    return (b"\x00" * ownership.LICENSE_FLAG_OFFSET
            + struct.pack("<I", flag)
            + b"\x00" * 8).hex()


def make_steam(root: Path, accounts: dict[str, dict[int, int]],
               apps: dict[str, dict[int, bool]] | None = None) -> Path:
    """造假的 steam 目录树。

    ``accounts``：``{account_id: {appid: flag}}`` 写 ``apptickets``（规则 1/2/3 用）。
    ``apps``    ：``{account_id: {appid: has_playtime}}`` 写
    ``Software.Valve.Steam.Apps``（规则 4 的**第二条依据**用）：
    ``True`` = 该条目带 ``Playtime`` 字段（真拥有过）；``False`` = 只有痕迹、**没有**
    ``Playtime``（只有 ``LastPlayed`` + ``cloud``）。

    ⚠️ 不传 ``apps`` 时该账号**整段 Apps 都不写** —— 此时 ``untimed_appids`` 读不到节点会
    返回 ``None``（= 信息不足），规则 4 一律保留。所以**要测 trace 就必须显式给 apps**；
    老用例（规则 1/2/3 / 降级）不传 apps 也能照常跑。

    用 ``vdf.dumps`` 生成，保证与真实 ``localconfig.vdf`` 的结构（同层 apptickets +
    Software.Valve.Steam.Apps）一致；两个节点写在**同一份**文件里。
    """
    import vdf

    for account_id, tickets in accounts.items():
        config = root / "userdata" / account_id / "config"
        config.mkdir(parents=True, exist_ok=True)
        store: dict = {
            "apptickets": {str(appid): _ticket(flag) for appid, flag in tickets.items()},
        }
        if apps and account_id in apps:
            entries: dict = {}
            for appid, has_playtime in apps[account_id].items():
                if has_playtime:
                    entries[str(appid)] = {"Playtime": "1", "LastPlayed": "1"}
                else:
                    # 真实痕迹的样子：有条目、有 LastPlayed / cloud，唯独没有 Playtime
                    entries[str(appid)] = {
                        "LastPlayed": "1",
                        "cloud": {"last_sync_state": "synchronized"},
                    }
            store["Software"] = {"Valve": {"Steam": {"Apps": entries}}}
        (config / "localconfig.vdf").write_text(
            vdf.dumps({"UserLocalConfigStore": store}, pretty=True),
            encoding="utf-8",
        )
    return root


def make_manifest(root: Path, appid: int, owner_account: str) -> None:
    """写一份 appmanifest：LastOwner 用 SteamID64（独立于 apptickets 的第二数据源）。"""
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)
    steam64 = int(owner_account) + ownership.STEAM64_BASE
    (steamapps / f"appmanifest_{appid}.acf").write_text(
        '"AppState"\n{\n\t"appid"\t\t"%d"\n\t"LastOwner"\t\t"%d"\n}\n' % (appid, steam64),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例前后都清缓存，避免跨用例互相污染。"""
    ownership.reset_cache()
    yield
    ownership.reset_cache()


# ------------------------------------------------------------------ license_flag

def test_license_flag_reads_offset_28():
    assert ownership.license_flag(_ticket(0)) == 0
    assert ownership.license_flag(_ticket(2)) == 2
    assert ownership.license_flag(_ticket(1)) == 1          # 未来若有别的值，如实读出来
    # 长度刚好够（32 字节）也能读
    assert ownership.license_flag(("00" * 28 + "02000000").ljust(64, "0")) == 2


def test_license_flag_len_too_short_returns_none():
    """长度不足 **必须** 返回 None（调用方据此放行，绝不误杀）。"""
    assert ownership.license_flag("") is None
    assert ownership.license_flag("00" * 27) is None        # 27 字节 < 28+4
    assert ownership.license_flag("00" * 31) is None        # 31 字节 < 32
    assert ownership.license_flag("00" * 32) == 0           # 32 字节，刚好够


def test_license_flag_bad_input_returns_none():
    assert ownership.license_flag(None) is None
    assert ownership.license_flag("zz") is None             # 非法 hex
    assert ownership.license_flag(12345) is None            # 位数不对的十进制串
    assert ownership.license_flag(object()) is None


def test_account_id_from_steam64():
    assert ownership.account_id_from_steam64(76561197960265728 + 10000001) == 10000001
    assert ownership.account_id_from_steam64("76561197970265729") == 10000001
    assert ownership.account_id_from_steam64(0) is None
    assert ownership.account_id_from_steam64("abc") is None


# ------------------------------------------------------------------ 四条规则

def test_rule1_owned_kept(tmp_path):
    root = make_steam(tmp_path, {MAIN: {1001: 0}})
    assert ownership.classify(root, MAIN, [1001]) == {1001: ownership.OWNED}
    assert ownership.filter_owned(root, MAIN, [1001]) == {1001}


def test_rule2_flagged_two_and_never_played_dropped(tmp_path):
    """规格变更（A4）：票面标 2 **且从未玩过**（playtime == 0）才剔除。"""
    root = make_steam(tmp_path, {MAIN: {2002: 2}})
    assert ownership.classify(root, MAIN, {2002: {"playtime": 0}}) == {2002: ownership.SHARED}
    assert ownership.filter_owned(root, MAIN, {2002: {"playtime": 0}}) == set()


def test_rule2_flagged_two_but_played_kept(tmp_path):
    """规格变更（A4）：票面标 2 **但本账号玩过**（playtime > 0）→ 保留（played_shared）。"""
    root = make_steam(tmp_path, {MAIN: {2102: 2}})
    kinds = ownership.classify(root, MAIN, {2102: {"playtime": 30}})
    assert kinds == {2102: ownership.PLAYED_SHARED}
    assert ownership.filter_owned(root, MAIN, {2102: {"playtime": 30}}) == {2102}


def test_rule2_played_shared_via_explicit_playtimes(tmp_path):
    """显式传 playtimes（只给 appid 列表 + 单独时长表）也能分出 played_shared / shared。"""
    root = make_steam(tmp_path, {MAIN: {2202: 2, 2203: 2}})
    kinds = ownership.classify(root, MAIN, [2202, 2203], playtimes={2202: 12, 2203: 0})
    assert kinds == {2202: ownership.PLAYED_SHARED, 2203: ownership.SHARED}
    assert ownership.filter_owned(root, MAIN, [2202, 2203], playtimes={2202: 12, 2203: 0}) == {2202}


def test_rule3_no_ticket_but_someone_else_owns_dropped(tmp_path):
    """主账号没这张票、但另一个账号 flag==0 → 家庭共享；**且从未玩过** → 剔除。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {3003: 0}})
    assert ownership.classify(root, MAIN, {3003: {"playtime": 0}}) == {3003: ownership.SHARED}
    assert ownership.filter_owned(root, MAIN, {3003: {"playtime": 0}}) == set()


def test_rule3_no_ticket_but_someone_else_owns_and_played_kept(tmp_path):
    """规则 3 来源（无票但本机别人拥有）**且本账号玩过** → 保留（played_shared）。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {3103: 0}})
    kinds = ownership.classify(root, MAIN, {3103: {"playtime": 45}})
    assert kinds == {3103: ownership.PLAYED_SHARED}
    assert ownership.filter_owned(root, MAIN, {3103: {"playtime": 45}}) == {3103}


def test_rule3_no_ticket_and_nobody_owns_kept(tmp_path):
    """谁都没有票（免费 / 试玩 / 未知）→ **保留**（类型未知一律放行的既有铁律）。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}})
    assert ownership.classify(root, MAIN, [4004]) == {4004: ownership.FREE}
    assert ownership.filter_owned(root, MAIN, [4004]) == {4004}


def test_rule4_unparsable_ticket_kept(tmp_path):
    """票在、但 hex 坏了 → 保留（不猜、不当家庭共享）。"""
    root = make_steam(tmp_path, {MAIN: {}})
    config = root / "userdata" / MAIN / "config" / "localconfig.vdf"
    config.write_text(
        '"UserLocalConfigStore"\n{\n\t"apptickets"\n\t{\n\t\t"5005"\t\t"deadbeef"\n\t}\n}\n',
        encoding="utf-8",
    )
    assert ownership.classify(root, MAIN, [5005]) == {5005: ownership.FREE}


def test_rule4_beats_rule3(tmp_path):
    """票解不出时**不退回**规则 3：即使别人拥有它，也按「未知」保留。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {6006: 0}})
    config = root / "userdata" / MAIN / "config" / "localconfig.vdf"
    config.write_text(
        '"UserLocalConfigStore"\n{\n\t"apptickets"\n\t{\n\t\t"6006"\t\t"00"\n\t}\n}\n',
        encoding="utf-8",
    )
    assert ownership.classify(root, MAIN, [6006]) == {6006: ownership.FREE}


def test_all_rules_together(tmp_path):
    """一次覆盖四类（含「共享但玩过」），并检查 summarize 的四类计数与 total。"""
    root = make_steam(tmp_path, {MAIN: {1: 0, 2: 2, 5: 2}, OWNER: {3: 0, 6: 0}})
    library = {
        1: {"playtime": 100},     # owned
        2: {"playtime": 0},       # 标 2 且没玩过 -> shared（剔除）
        3: {"playtime": 0},       # 无票但别人拥有且没玩过 -> shared（剔除）
        4: {"playtime": 0},       # 无票且无人拥有 -> free（保留）
        5: {"playtime": 7},       # 标 2 但玩过 -> played_shared（保留）
        6: {"playtime": 9},       # 无票但别人拥有且玩过 -> played_shared（保留）
    }
    kinds = ownership.classify(root, MAIN, library)
    assert kinds == {
        1: ownership.OWNED,
        2: ownership.SHARED,
        3: ownership.SHARED,
        4: ownership.FREE,
        5: ownership.PLAYED_SHARED,
        6: ownership.PLAYED_SHARED,
    }
    summary = ownership.summarize(root, MAIN, library)
    assert (summary["owned"], summary["played_shared"], summary["shared"],
            summary["free"], summary["total"]) == (1, 2, 2, 1, 4)
    assert summary["shared_appids"] == [2, 3]
    assert summary["played_shared_appids"] == [5, 6]
    assert summary["degraded"] is False
    # 保留集合 = owned + played_shared + free；剔除集合 = shared
    assert ownership.filter_owned(root, MAIN, library) == {1, 4, 5, 6}
    assert ownership.excluded_appids(root, MAIN, library) == {2, 3}


# ------------------------------------------------------------------ 残留痕迹（T1）

def make_fake_db(path: Path, rows) -> Path:
    """造一个假的 ``cache.db``（只含 ``paid_appids`` 真正会读的 5 列）。"""
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE games (appid INTEGER PRIMARY KEY, price INTEGER, is_free INTEGER,"
            " genres TEXT, genres_cn TEXT)"
        )
        conn.executemany("INSERT INTO games VALUES (?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    return path


def test_paid_appids_counts_only_priced_rows(tmp_path):
    """只有「有明确售价且没标免费」的才进付费集合；``price=0`` / ``NULL`` 一律不进。"""
    db = make_fake_db(tmp_path / "cache.db", [
        (90001, 29800, None, '["RPG"]', '["角色扮演"]'),                      # 付费
        (90002, 0, 1, '["Action","Free to Play"]', '["动作","免费开玩"]'),     # is_free 标记
        (90003, 0, None, '[]', '[]'),                                        # price == 0 -> 免费
        (90004, None, None, '["Action"]', '["动作"]'),                        # 价格未知 -> 不进
        (90005, 19900, None, '["Free to Play"]', '["免费开玩"]'),              # 有价但标免费
        (90006, 7600, None, '["Adventure"]', '["冒险"]'),                     # 付费
    ])
    assert ownership.paid_appids(db) == {90001, 90006}


def test_paid_appids_degrades_when_db_unreadable(tmp_path):
    """整个库读不到 -> ``None``（调用方据此**全部保留**，绝不误杀）。"""
    assert ownership.paid_appids(tmp_path / "nope.db") is None
    # 文件在、但不是合法 sqlite -> 同样降级为 None（不是空集！）
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a sqlite file")
    assert ownership.paid_appids(broken) is None


def test_rule4_paid_trace_dropped(tmp_path):
    """规则 4（T1 + T4）：没票 + 本机没人拥有 + 没玩过 + **有明确售价** + **Apps 段只有痕迹**
    -> ``trace`` 剔除。T4 起两个依据都要成立（``untimed`` 显式注入，完全离线）。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}},
                      apps={MAIN: {1091500: False}})          # 有条目、没有 Playtime = 痕迹
    paid = frozenset({1091500})
    untimed = frozenset({1091500})
    assert ownership.classify(root, MAIN, [1091500], paid=paid, untimed=untimed) == {1091500: ownership.TRACE}
    assert ownership.filter_owned(root, MAIN, [1091500], paid=paid, untimed=untimed) == set()
    assert ownership.excluded_appids(root, MAIN, [1091500], paid=paid, untimed=untimed) == {1091500}


def test_rule4_free_game_kept(tmp_path):
    """免费游戏（没票是正常的）必须保留 —— 实测 ``230410`` / ``438100`` / ``1830840``。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}})
    paid = frozenset({1091500})            # 免费游戏不在付费集合里
    library = [230410, 438100, 1830840, 480, 644560]
    assert ownership.classify(root, MAIN, library, paid=paid, untimed=frozenset()) == {
        appid: ownership.FREE for appid in library
    }
    assert ownership.filter_owned(root, MAIN, library, paid=paid, untimed=frozenset()) == set(library)


def test_rule4_paid_but_played_kept(tmp_path):
    """付费但**确实玩过** -> 规则 4 保留（口径先看 playtime，再看售价）。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}})
    paid = frozenset({322330})
    untimed = frozenset({322330})          # 就算两条判据都说它是痕迹
    kinds = ownership.classify(root, MAIN, {322330: {"playtime": 355}}, paid=paid, untimed=untimed)
    assert kinds == {322330: ownership.FREE}    # 玩过 -> 规则 4 前半段直接保留
    assert ownership.filter_owned(
        root, MAIN, {322330: {"playtime": 355}}, paid=paid, untimed=untimed) == {322330}


def test_rule4_unknown_price_kept(tmp_path, monkeypatch):
    """**最重要的一条**：读不到价格一律保留，绝不误杀（离线、绝不读真实 cache.db）。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}})
    # (1) 整个库读不到 -> 付款集合为 None -> 全部保留
    monkeypatch.setattr(ownership, "_cache_db_path", lambda: tmp_path / "missing.db")
    ownership.reset_cache()
    assert ownership.classify(root, MAIN, [9991500]) == {9991500: ownership.FREE}
    assert ownership.filter_owned(root, MAIN, [9991500]) == {9991500}
    # (2) 库读到了，但里面根本没有这个 appid（= 价格未知）-> 保留
    assert ownership.classify(root, MAIN, [9991501], paid=frozenset({123}),
                              untimed=frozenset({9991501})) == {9991501: ownership.FREE}
    assert ownership.filter_owned(root, MAIN, [9991501], paid=frozenset({123}),
                                  untimed=frozenset({9991501})) == {9991501}
    # (3) 显式传入空集合（= 库里一个付费条目都没有）-> 同样保留
    assert ownership.classify(root, MAIN, [9991502], paid=frozenset()) == {9991502: ownership.FREE}


def test_rule4_paid_lookup_degrades_to_keep_everything(tmp_path, monkeypatch):
    """默认（不传 ``paid``）时懒加载 cache.db；库不可用必须降级为**全部保留**。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}})
    monkeypatch.setattr(ownership, "_cache_db_path", lambda: tmp_path / "missing.db")
    ownership.reset_cache()
    assert ownership.classify(root, MAIN, [1091500]) == {1091500: ownership.FREE}
    assert ownership.filter_owned(root, MAIN, [1091500]) == {1091500}


def test_rule4_lazy_lookup_reads_cache_db(tmp_path, monkeypatch):
    """不显式传 ``paid`` 时**真的**会去读 cache.db（而不是永远降级）—— 用假库证明闭环。"""
    db = make_fake_db(tmp_path / "cache.db", [
        (1091500, 29800, None, '["RPG"]', '["角色扮演"]'),   # 付费痕迹 -> 剔除
        (230410, 0, 1, '["Free to Play"]', '["免费开玩"]'),  # 免费 -> 保留
    ])
    monkeypatch.setattr(ownership, "_cache_db_path", lambda: db)
    ownership.reset_cache()
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}},
                      apps={MAIN: {1091500: False, 230410: True}})
    kinds = ownership.classify(root, MAIN, [1091500, 230410])
    assert kinds == {1091500: ownership.TRACE, 230410: ownership.FREE}
    assert ownership.filter_owned(root, MAIN, [1091500, 230410]) == {230410}

def test_summarize_reports_trace_and_total(tmp_path):
    """``summarize`` 要自述 ``trace``；``total`` = 保留款数 = owned+played_shared+free。"""
    root = make_steam(tmp_path, {MAIN: {1: 0, 2: 2}}, apps={MAIN: {3: False}})
    library = {
        1: {"playtime": 100},     # owned
        2: {"playtime": 0},       # shared（剔除）
        3: {"playtime": 0},       # trace（剔除）
        4: {"playtime": 0},       # free（保留）
        5: {"playtime": 7},       # free，因为玩过（保留）
    }
    summary = ownership.summarize(root, MAIN, library, paid=frozenset({3}), untimed=frozenset({3}))
    assert (summary["owned"], summary["played_shared"], summary["shared"],
            summary["trace"], summary["free"]) == (1, 0, 1, 1, 2)
    assert summary["trace_appids"] == [3]
    assert summary["shared_appids"] == [2]
    assert summary["total"] == 3                   # 保留款数，不把被剔除的算进去
    assert len(library) - summary["total"] == 2    # 剔除了 shared + trace 各 1
    assert summary["degraded"] is False


def test_trace_is_not_charged_to_shared(tmp_path):
    """``trace`` 与 ``shared`` 是两类，互相不串（面板的「已剔除」提示才不会张冠李戴）。"""
    root = make_steam(tmp_path, {MAIN: {1: 2}, OWNER: {}}, apps={MAIN: {2: False}})
    library = {1: {"playtime": 0}, 2: {"playtime": 0}}
    summary = ownership.summarize(root, MAIN, library, paid=frozenset({2}), untimed=frozenset({2}))
    assert summary["shared"] == 1 and summary["shared_appids"] == [1]
    assert summary["trace"] == 1 and summary["trace_appids"] == [2]


def test_excluded_appids_includes_trace(tmp_path):
    """``excluded_appids`` = 共享未玩过 + 残留痕迹（面板灰显行据此列全）。"""
    root = make_steam(tmp_path, {MAIN: {1: 2}, OWNER: {}}, apps={MAIN: {2: False}})
    library = {1: {"playtime": 0}, 2: {"playtime": 0}, 3: {"playtime": 5}}
    assert ownership.excluded_appids(root, MAIN, library, paid=frozenset({2}),
                                     untimed=frozenset({2})) == {1, 2}
    assert ownership.filter_owned(root, MAIN, library, paid=frozenset({2}),
                                  untimed=frozenset({2})) == {3}


def test_degraded_never_uses_trace(tmp_path):
    """降级（读不到 vdf）时连 ``trace`` 也不许出现 —— 宁可留着痕迹，也不许误杀。"""
    root = make_steam(tmp_path, {MAIN: {1: 0}})
    kinds = ownership.classify(root, "999999999", [1091500], paid=frozenset({1091500}),
                               untimed=frozenset({1091500}))
    assert kinds == {1091500: ownership.FREE}
    assert ownership.summarize(root, "999999999", [1091500], paid=frozenset({1091500}),
                               untimed=frozenset({1091500}))["trace"] == 0

# ------------------------------------------------- T4 加固：Apps 段 Playtime 查询层


def test_app_playtime_flags_separates_entry_with_and_without_playtime(tmp_path):
    """``Apps`` 段的查询层：有条目+Playtime -> True；有条目没 Playtime -> False；没条目 -> 查不到。"""
    root = make_steam(tmp_path, {MAIN: {}},
                      apps={MAIN: {100: True, 200: False, 300: True}})
    flags = ownership.app_playtime_flags(root, MAIN)
    assert flags[100] is True
    assert flags[200] is False
    assert 999 not in flags, "Apps 段里没有的 appid 不该出现在表里（= 信息不足）"
    # untimed 只挑 False 的（= 有条目但没有 Playtime），没条目的与 True 的都不进
    assert ownership.untimed_appids(root, MAIN) == frozenset({200})


def test_app_playtime_flags_degrades_when_apps_node_missing(tmp_path):
    """**关键降级**：vdf 里没有 Apps 节点 -> ``None``（信息不足），绝不当成「痕迹」。"""
    root = make_steam(tmp_path, {MAIN: {100: 0}})      # 不传 apps -> 不写 Apps 段
    assert ownership.app_playtime_flags(root, MAIN) is None
    assert ownership.untimed_appids(root, MAIN) is None


def test_app_playtime_flags_degrades_on_missing_vdf_and_bad_input(tmp_path):
    root = make_steam(tmp_path, {MAIN: {}})
    assert ownership.app_playtime_flags(root, "999999999") is None    # 账号不存在
    assert ownership.app_playtime_flags(root / "nope", MAIN) is None  # 目录不存在
    assert ownership.app_playtime_flags(None, MAIN) is None
    assert ownership.app_playtime_flags(root, None) is None
    assert ownership.untimed_appids(root / "nope", MAIN) is None


def test_app_playtime_flags_broken_vdf_returns_none(tmp_path):
    root = make_steam(tmp_path, {MAIN: {}})
    (root / "userdata" / MAIN / "config" / "localconfig.vdf").write_text(
        "this is not a vdf {{{", encoding="utf-8")
    assert ownership.app_playtime_flags(root, MAIN) is None


def test_app_playtime_flags_missing_dependency_returns_none(tmp_path, monkeypatch):
    """vdf 库不可用（打包缺依赖）也不能炸、更不能退化成「全都有痕迹」。"""
    import builtins

    root = make_steam(tmp_path, {MAIN: {}}, apps={MAIN: {100: False}})
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "vdf":
            raise ImportError("no vdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ownership.reset_cache()
    assert ownership.app_playtime_flags(root, MAIN) is None
    assert ownership.untimed_appids(root, MAIN) is None


def test_app_playtime_flags_skips_non_numeric_keys(tmp_path):
    """Apps 段里的非数字键（Steam 会放一些不是 appid 的键）必须被跳过，不能炸。"""
    config = tmp_path / "userdata" / MAIN / "config"
    config.mkdir(parents=True)
    (config / "localconfig.vdf").write_text(
        '"UserLocalConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
        '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"Apps"\n\t\t\t\t{\n'
        '\t\t\t\t\t"not_an_appid"\n\t\t\t\t\t{\n\t\t\t\t\t\t"Playtime"\t\t"1"\n'
        '\t\t\t\t\t}\n\t\t\t\t\t"480"\n\t\t\t\t\t{\n'
        '\t\t\t\t\t\t"Playtime"\t\t"9"\n\t\t\t\t\t}\n'
        '\t\t\t\t}\n\t\t\t}\n\t\t}\n\t}\n}\n',
        encoding="utf-8",
    )
    flags = ownership.app_playtime_flags(tmp_path, MAIN)
    assert flags == {480: True}


# ------------------------------------------------- T4 加固：两个依据必须同时成立


def test_trace_requires_both_price_and_apps_trace(tmp_path):
    """**T4 核心**：两个依据必须**同时**成立才算痕迹，任一不成立都保留。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}},
                      apps={MAIN: {1: False, 2: True, 3: False}})
    paid = frozenset({1, 2, 3})
    untimed = frozenset({1, 3})
    # 1 = 有价 + Apps 无 Playtime -> 痕迹
    assert ownership.classify(root, MAIN, [1], paid=paid, untimed=untimed) == {1: ownership.TRACE}
    # 2 = 有价 但 Apps 里记着 Playtime（真拥有过）-> 保留，**不能**因为价格就删
    assert ownership.classify(root, MAIN, [2], paid=paid, untimed=untimed) == {2: ownership.FREE}
    # 3 = Apps 是痕迹 但**没价格**（免费 / 未知）-> 保留
    assert ownership.classify(root, MAIN, [3], paid=frozenset(), untimed=untimed) == {3: ownership.FREE}


def test_trace_kept_when_price_lookup_is_none(tmp_path, monkeypatch):
    """**最重要的一条之一**：读不到价格 -> ``paid=None`` -> 一律保留（哪怕 Apps 段是痕迹）。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}}, apps={MAIN: {1091500: False}})
    monkeypatch.setattr(ownership, "_cache_db_path", lambda: tmp_path / "missing.db")
    ownership.reset_cache()
    kinds = ownership.classify(root, MAIN, [1091500])       # 不传 paid，懒加载但库不可用
    assert kinds == {1091500: ownership.FREE}
    assert ownership.filter_owned(root, MAIN, [1091500]) == {1091500}


def test_trace_kept_when_apps_lookup_is_none(tmp_path):
    """**最重要的一条之一**：读不到 Apps 段 -> ``untimed=None`` -> 一律保留（哪怕有价格）。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}})      # 不写 Apps 段
    paid = frozenset({1091500})
    kinds = ownership.classify(root, MAIN, [1091500], paid=paid)   # untimed 懒加载 -> None
    assert kinds == {1091500: ownership.FREE}
    assert ownership.filter_owned(root, MAIN, [1091500], paid=paid) == {1091500}


def test_trace_kept_when_apps_has_no_entry_for_appid(tmp_path):
    """Apps 段读到了、但**这一条不在里面**（信息不足，不是痕迹）-> 保留。"""
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}}, apps={MAIN: {555: False}})
    paid = frozenset({1091500})
    kinds = ownership.classify(root, MAIN, [1091500], paid=paid)
    assert kinds == {1091500: ownership.FREE}


def test_paid_but_apps_has_playtime_is_kept_end_to_end(tmp_path):
    """T3 反例的**端到端**复现：真买的游戏（Apps 段有 Playtime）绝不能被判成痕迹。

    T3 独立调查指出：本机有大量「有票 + 付费 + ``Apps`` 段没有 Playtime」的真实游戏
    （``457140 缺氧`` / ``1446780 怪物猎人崛起`` …），它们现在**只因为「有票」才安全**。
    这里模拟「票丢了」的最坏情况：无票 + 本机没人拥有 + 0 分钟 + 有价格 —— 纯价格判据会
    误删它们，加固后的第二条依据必须挡住。
    """
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}},
                      apps={MAIN: {457140: True, 1446780: True, 1091500: False}})
    paid = frozenset({457140, 1446780, 1091500})
    kinds = ownership.classify(root, MAIN, [457140, 1446780, 1091500], paid=paid)
    assert kinds == {
        457140: ownership.FREE,        # Apps 段有 Playtime -> 真拥有过 -> 保留
        1446780: ownership.FREE,       # 同上
        1091500: ownership.TRACE,      # Apps 段只有痕迹 -> 剔除
    }
    assert ownership.filter_owned(root, MAIN, [457140, 1446780, 1091500], paid=paid) == {457140, 1446780}


def test_untimed_alone_is_not_enough_keeps_free_games(tmp_path):
    """**第二条判据不能单独用**：只有价格或只有 Apps 痕迹都不构成剔除。

    实测：单用「Apps 段只有痕迹」在本机会判 25 条，多出的 21 条里大量是 ``760 Steam`` /
    ``241100`` 这类免费 / 非游戏条目。这里用一个「Apps 段只有 cloud 记录」的免费游戏
    （真实结构）验证：没有价格时它必须保留。
    """
    root = make_steam(tmp_path, {MAIN: {}, OWNER: {}}, apps={MAIN: {760: False, 241100: False}})
    # 没传 paid = 空集合（免费游戏的正确位置）；untimed 懒加载会读到这两条是「痕迹候选」
    kinds = ownership.classify(root, MAIN, [760, 241100], paid=frozenset())
    assert kinds == {760: ownership.FREE, 241100: ownership.FREE}


def test_reset_cache_clears_apps_cache(tmp_path):
    """``reset_cache`` 必须把 Apps 段的缓存也清掉（否则测试之间会串味）。"""
    root = make_steam(tmp_path, {MAIN: {}}, apps={MAIN: {100: False}})
    assert ownership.app_playtime_flags(root, MAIN) == {100: False}
    assert ownership._APP_PLAYTIME_CACHE, "缓存没写进去"
    ownership.reset_cache()
    assert not ownership._APP_PLAYTIME_CACHE, "reset_cache 漏清了 Apps 段缓存"


def test_apps_cache_reacts_to_vdf_content_change(tmp_path):
    """Apps 段内容变了（同一路径）必须重新解析 —— 缓存不能只看路径。"""
    root = make_steam(tmp_path, {MAIN: {}}, apps={MAIN: {100: False}})
    assert ownership.untimed_appids(root, MAIN) == frozenset({100})
    make_steam(root, {MAIN: {}}, apps={MAIN: {100: True}})
    assert ownership.untimed_appids(root, MAIN) == frozenset()


# ------------------------------------------------------------------ 降级

def test_missing_account_keeps_everything(tmp_path):
    root = make_steam(tmp_path, {MAIN: {1: 0}})
    assert ownership.classify(root, "999999999", [1, 2]) == {1: ownership.FREE, 2: ownership.FREE}
    assert ownership.filter_owned(root, "999999999", [1, 2]) == {1, 2}
    degraded = ownership.summarize(root, "999999999", [1, 2])
    assert degraded["degraded"] is True
    assert degraded["played_shared"] == 0 and degraded["shared"] == 0


def test_broken_vdf_keeps_everything(tmp_path):
    root = make_steam(tmp_path, {MAIN: {}})
    (root / "userdata" / MAIN / "config" / "localconfig.vdf").write_text(
        "this is not a vdf {{{", encoding="utf-8"
    )
    assert ownership.read_ownership(root, MAIN)["available"] is False
    assert ownership.filter_owned(root, MAIN, [1, 2, 3]) == {1, 2, 3}
    assert ownership.summarize(root, MAIN, [1])["shared"] == 0


def test_missing_path_never_raises(tmp_path):
    for bad in (None, "", tmp_path / "does-not-exist"):
        assert ownership.read_ownership(bad, MAIN)["available"] is False
        assert ownership.classify(bad, MAIN, [1]) == {1: ownership.FREE}
        assert ownership.filter_owned(bad, MAIN, [1]) == {1}


def test_missing_vdf_dependency_keeps_everything(tmp_path, monkeypatch):
    """vdf 库不可用（打包缺依赖）也不能炸、不能清空库。"""
    import builtins

    root = make_steam(tmp_path, {MAIN: {1: 0}})
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "vdf":
            raise ImportError("no vdf")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ownership.reset_cache()
    assert ownership.filter_owned(root, MAIN, [1, 2]) == {1, 2}


def test_appids_accept_str_int_mapping(tmp_path):
    """函数要能吃 str 或 int 的 appid（本机 vdf 的 key 是 str）。"""
    root = make_steam(tmp_path, {MAIN: {1: 0, 2: 2}})
    assert ownership.classify(root, MAIN, ["1", "2"]) == {1: ownership.OWNED, 2: ownership.SHARED}
    assert ownership.classify(root, MAIN, {1: {}, 2: {}}) == {1: ownership.OWNED, 2: ownership.SHARED}
    assert ownership.classify(root, MAIN, {1, 2, "abc", None}) == {
        1: ownership.OWNED, 2: ownership.SHARED
    }


def test_read_ownership_returns_owned_and_shared_sets(tmp_path):
    root = make_steam(tmp_path, {MAIN: {1: 0, 2: 2, 3: 0}})
    result = ownership.read_ownership(root, MAIN)
    assert result["available"] is True
    assert result["owned"] == {1, 3}
    assert result["shared"] == {2}


# ------------------------------------------------------------------ LastOwner 交叉验证

def test_last_owner_agrees_with_flag(tmp_path):
    """appmanifest 的 LastOwner（独立数据源）应与 apptickets 的 flag 自洽。"""
    root = make_steam(tmp_path, {MAIN: {7001: 0, 7002: 2}, OWNER: {7002: 0}})
    make_manifest(root, 7001, MAIN)      # 自己拥有 -> LastOwner 是本账号
    make_manifest(root, 7002, OWNER)     # 家庭共享 -> LastOwner 是别人
    owners = ownership.read_last_owners(root)
    assert owners == {7001: int(MAIN), 7002: int(OWNER)}

    kinds = ownership.classify(root, MAIN, [7001, 7002])
    for appid, kind in kinds.items():
        if kind == ownership.OWNED:
            assert owners[appid] == int(MAIN)
        elif kind == ownership.SHARED:
            assert owners[appid] != int(MAIN)


def test_read_last_owners_never_raises(tmp_path):
    assert ownership.read_last_owners(tmp_path / "nope") == {}
    assert ownership.read_last_owners(None) == {}


# ------------------------------------------------------------------ 装载入口同口径

def test_loader_filters_and_keeps_meta_in_sync(tmp_path, monkeypatch):
    """main._load_library 过滤后：play_stats 与 games_meta 一起收缩、不留孤儿 meta。"""
    import argparse

    import main as cli

    root = make_steam(tmp_path, {MAIN: {8001: 0, 8002: 2}})
    monkeypatch.setattr(cli, "_resolve_steam_account", lambda args: (str(root), MAIN))
    monkeypatch.setattr(
        cli, "_read_playtime",
        # 8002 标 2 且 playtime == 0（共享且未玩过）-> 必须被剔
        lambda steam_path, account_id: {8001: {"playtime": 60}, 8002: {"playtime": 0}},
    )
    monkeypatch.setattr(
        cli, "_fetch_metadata_optional",
        lambda appids: {8001: {"name": "Mine"}, 8002: {"name": "Shared"}, 8003: {"name": "Orphan"}},
    )

    loaded = cli._load_library(argparse.Namespace(steam_path=str(root), account_id=MAIN), with_metadata=True)
    assert loaded is not None
    play_stats, games_meta, _statuses, _classified, _path, _account, own = loaded

    assert set(play_stats) == {8001}, "家庭共享条目必须从 play_stats 里消失"
    assert set(games_meta) == {8001}, "过滤后不得留孤儿 meta（8002 与 8003 都要走）"
    assert own["shared"] == 1 and own["owned"] == 1
    assert own["played_shared"] == 0
    assert own["shared_appids"] == [8002]


def test_loader_keeps_played_shared(tmp_path, monkeypatch):
    """A4：家庭共享但本账号玩过（playtime > 0）必须留在 play_stats / games_meta 里。"""
    import argparse

    import main as cli

    root = make_steam(tmp_path, {MAIN: {8101: 0, 8102: 2, 8103: 2}})
    monkeypatch.setattr(cli, "_resolve_steam_account", lambda args: (str(root), MAIN))
    monkeypatch.setattr(
        cli, "_read_playtime",
        lambda steam_path, account_id: {
            8101: {"playtime": 60}, 8102: {"playtime": 5}, 8103: {"playtime": 0},
        },
    )
    monkeypatch.setattr(cli, "_fetch_metadata_optional", lambda appids: {})

    loaded = cli._load_library(argparse.Namespace(steam_path=str(root), account_id=MAIN), with_metadata=False)
    play_stats, _meta, _statuses, _classified, _path, _account, own = loaded
    assert set(play_stats) == {8101, 8102}, "共享但玩过的 8102 必须保留"
    assert own["played_shared"] == 1
    assert own["played_shared_appids"] == [8102]
    assert own["shared"] == 1 and own["shared_appids"] == [8103]


def test_loader_never_wipes_library_on_degraded(tmp_path, monkeypatch):
    """读不到 vdf -> 一行都不许少（降级不清空用户库）。"""
    import argparse

    import main as cli

    monkeypatch.setattr(cli, "_resolve_steam_account", lambda args: (str(tmp_path / "nope"), MAIN))
    monkeypatch.setattr(
        cli, "_read_playtime", lambda steam_path, account_id: {1: {"playtime": 1}, 2: {"playtime": 2}}
    )
    loaded = cli._load_library(argparse.Namespace(steam_path=None, account_id=MAIN), with_metadata=False)
    play_stats, _meta, _statuses, _classified, _path, _account, own = loaded
    assert set(play_stats) == {1, 2}
    assert own["degraded"] is True


def test_core_library_filters_too(tmp_path, monkeypatch):
    """core.library.load_library（radar 等走它）与 CLI/面板同口径。"""
    from core import library as core_library

    root = make_steam(tmp_path, {MAIN: {9001: 0, 9002: 2, 9003: 2}})
    monkeypatch.setattr(
        core_library.vdf_io, "read_playtime_data",
        # 9002 标 2 但玩过（保留）；9003 标 2 且 playtime == 0（剔除）
        lambda steam_path, account_id: {
            9001: {"playtime": 60}, 9002: {"playtime": 999}, 9003: {"playtime": 0},
        },
    )
    lib = core_library.load_library(str(root), MAIN)
    assert set(lib) == {9001, 9002}, "共享但玩过的 9002 必须保留，共享且未玩过的 9003 才剔除"


# ------------------------------------------------------------------ 面板字段

def test_summary_exposes_ownership_but_stats_unchanged(monkeypatch):
    """面板 /api/summary 只多一个说明字段；统计数字仍来自已过滤的库。"""
    from webapp import api as panel_api

    monkeypatch.setattr(
        panel_api, "_require_library",
        lambda *a, **k: {
            "account_id": "1", "steam_path": "X", "named": 0,
            "play_stats": {1: {"playtime": 60, "last_played": 0}},
            "games_meta": {}, "statuses": {}, "classified": {},
            "ownership": {"owned": 1, "played_shared": 3, "shared": 1, "free": 0,
                          "played_shared_appids": [5], "shared_appids": [2],
                          "degraded": False, "shared_stats": {}},
        },
    )
    payload = panel_api.get_summary()
    assert payload["ownership"]["played_shared"] == 3
    assert payload["ownership"]["played_shared_appids"] == [5]
    assert payload["ownership"]["shared"] == 1
    assert payload["ownership"]["shared_appids"] == [2]
    assert "ownership" not in payload.get("errors", {})


def test_panel_load_library_tolerates_legacy_six_tuple(tmp_path, monkeypatch):
    """旧式 6 元加载器（既有测试桩）也不能让面板炸：ownership 退化为空说明。"""
    import argparse

    import main as cli
    from webapp import api as panel_api

    monkeypatch.setattr(
        cli, "_load_library",
        lambda args, with_metadata=False: (
            {1: {"playtime": 60}}, {}, {}, None, str(tmp_path), "000000001"
        ),
    )
    panel_api._LIB_CACHE.clear()
    data = panel_api.load_library(account_id="000000001", use_cache=False)
    assert data["ownership"] == {}
    assert panel_api._ownership_section(data)["shared"] == 0


def test_ownership_section_carries_played_shared(tmp_path):
    """面板 ownership 字段要带 played_shared / played_shared_appids；灰显行只列被剔除的。"""
    from webapp import api as panel_api

    data = {
        "steam_path": None,
        "ownership": {
            "owned": 81, "played_shared": 2, "shared": 1, "free": 3,
            "played_shared_appids": [374320, 1367550],
            "shared_appids": [1245620],
            "degraded": False,
            "shared_stats": {1245620: {"playtime": 0, "last_played": 0}},
        },
    }
    section = panel_api._ownership_section(data)
    assert section["played_shared"] == 2
    assert section["played_shared_appids"] == [374320, 1367550]
    assert section["shared"] == 1
    assert section["shared_appids"] == [1245620]
    # 「显示家庭共享」灰显行**只**含共享且未玩过的（played_shared 的正常入 games，不在此列）
    assert [row["appid"] for row in section["shared_games"]] == [1245620]


def test_ownership_section_forwards_trace(tmp_path):
    """E1：面板 ownership 字段要把 trace / trace_appids 转发出去（与 CLI 提示行同口径）。

    面板提示行原本只报「共享且未玩过」，漏了「Steam 残留痕迹」这一段；上游
    ``core.ownership.summarize()`` 早已返回这两个字段，是这一层没转发。
    """
    from webapp import api as panel_api

    data = {
        "steam_path": None,
        "ownership": {
            "owned": 85, "played_shared": 11, "shared": 31, "free": 6, "trace": 2,
            "played_shared_appids": [374320], "shared_appids": [1245620],
            "trace_appids": ["1091500", 1448440],       # 字符串也要被收成 int
            "degraded": False,
            "shared_stats": {1245620: {"playtime": 0, "last_played": 0}},
        },
    }
    section = panel_api._ownership_section(data)
    assert section["trace"] == 2
    assert section["trace_appids"] == [1091500, 1448440]
    # trace 与 shared 是**两类**被剔除的条目，绝不能互相污染灰显行
    assert [row["appid"] for row in section["shared_games"]] == [1245620]
    assert section["shared"] == 31


def test_ownership_section_trace_defaults_to_zero(tmp_path):
    """上游没给 trace（旧数据 / 降级）时必须是 0，而不是 None —— 前端拿它判 > 0。"""
    from webapp import api as panel_api

    data = {"steam_path": None, "ownership": {"owned": 5, "shared": 0, "degraded": False}}
    section = panel_api._ownership_section(data)
    assert section["trace"] == 0
    assert section["trace_appids"] == []


def test_games_rows_flags_played_shared_but_keeps_them(tmp_path):
    """_games_rows 给 played_shared 打标，但它们仍在 rows 里（正常排序、正常统计）。"""
    from webapp import api as panel_api

    lib = {
        "play_stats": {
            374320: {"playtime": 3948, "last_played": 0},
            1245620: {"playtime": 0, "last_played": 0},
        },
        "games_meta": {}, "statuses": {}, "classified": {}, "steam_path": None,
        "ownership": {"played_shared_appids": [374320], "shared_appids": [1245620]},
    }
    rows = {row["appid"]: row for row in panel_api._games_rows(lib)}
    assert rows[374320]["played_shared"] is True
    assert rows[374320].get("shared") is None      # played_shared 不是灰显行
    assert rows[1245620]["played_shared"] is False


# ------------------------------------------------------------------ Steam 客户端缓存（T12）

def make_client_cache(root: Path, account_id: str, owned, free=None) -> Path:
    """造一份 Steam 客户端 CEF cache blob（结构与真实 ``f_0006b4`` 同形）。

    真实 blob 是**反复 JSON 转义**过的（每个引号前都有反斜杠），去转义后关键形状是::

        "queryKey":["AccountOwnedApps",10000002] ...
        "setOwnedApps":{"_t":9,"v":[95400,...]},"setFreeApps":{"_t":9,"v":[...]}

    特意把 ``setFreeApps`` 也造出来（体积相近的大数组），用来验证解析**必须锚定字段名**，
    不会把免费游戏错当成自有。
    """
    def _array(values) -> str:
        return '{"_t":9,"v":[' + ",".join(str(v) for v in values) + "]}"

    body = (
        '{"queryKey":["AccountOwnedApps",' + account_id + "],"
        + '"queryHash":"["AccountOwnedApps",' + account_id + ']"},'
        + '"setOwnedApps":' + _array(owned) + ","
        + '"setFreeApps":' + _array(free or [])
    )
    # 模仿真实的反复转义：引号前统一加反斜杠（解析方 replace 掉它们）
    blob = body.replace('"', '\\"').encode("latin-1")
    cache_dir = root / "Steam" / "htmlcache" / "Default" / "Cache" / "Cache_Data"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "f_000001").write_bytes(b"pad" + blob)
    return root


@pytest.fixture
def _client_env(tmp_path, monkeypatch):
    """把 ``%LOCALAPPDATA%`` 指到临时目录，并让缓存字典干净。"""
    monkeypatch.setattr(ownership, "_LOCALAPPDATA", str(tmp_path / "local"))
    ownership.reset_cache()
    yield tmp_path / "local"
    ownership.reset_cache()


def test_client_cache_reads_owned_list_for_account(_client_env):
    """能读出本账号的自有清单（账号 id 在 queryKey 里**不带引号**，历史 bug 就在这）。"""
    owned = [1001 + i for i in range(30)]                 # 够长，过 _CLIENT_LIST_MIN_ITEMS
    make_client_cache(_client_env, MAIN, owned, free=[9001 + i for i in range(30)])
    got = ownership.client_owned_appids("C:/fake/steam", MAIN)
    assert got is not None
    assert set(owned) <= set(got)
    assert 9001 not in got                                # 免费游戏绝不能被算成自有


def test_client_cache_ignores_other_accounts_list(_client_env):
    """缓存是**别人**的账号 -> 返回 None（降级），绝不把别人的自有算成本账号的。"""
    make_client_cache(_client_env, OWNER, [2001 + i for i in range(30)])
    assert ownership.client_owned_appids("C:/fake/steam", MAIN) is None
    assert ownership.client_owned_appids("C:/fake/steam", OWNER) is not None


def test_client_cache_absent_degrades_to_none(_client_env):
    """缓存不存在（客户端没跑过 / 换机器）-> None，调用方保持票面判定不变。"""
    assert ownership.client_owned_appids("C:/fake/steam", MAIN) is None


def test_client_cache_flow_into_classify_and_owner_scan(tmp_path, monkeypatch):
    """端到端：票面漏掉的「真拥有」要从缓存补进来（弹丸论破 V3 那种没启动过的游戏）。

    ``3001`` 票面里没有、客户端有 -> ``OWNED``；``3002`` 票面 ``flag == 2``（家庭共享）
    且客户端**没有** -> ``SHARED``（不能被缓存误升级成自有）。
    """
    monkeypatch.setattr(ownership, "_LOCALAPPDATA", str(tmp_path / "local"))
    steam = make_steam(tmp_path / "steam", {MAIN: {3002: 2}})
    make_client_cache(tmp_path / "local", MAIN, [3001] + [4000 + i for i in range(30)])
    ownership.reset_cache()

    kinds = ownership.classify(steam, MAIN, [3001, 3002], playtimes={3001: 0, 3002: 0})
    assert kinds == {3001: ownership.OWNED, 3002: ownership.SHARED}
    # 规则 3 的「本机有人拥有」也要并上客户端清单
    assert 3001 in ownership.scan_local_owners(steam)
    ownership.reset_cache()


# ------------------------------------------------------------------ 库索引（T13）

def make_library_cache(root: Path, account_id: str, appids) -> Path:
    """造 Steam 客户端的库索引：``userdata/<id>/config/librarycache/<appid>.json``。

    真实目录里每个 appid 一个 json（内容对本模块无意义，只做**目录列举**，
    所以这里写最小占位内容）。
    """
    directory = root / "userdata" / account_id / "config" / "librarycache"
    directory.mkdir(parents=True, exist_ok=True)
    for appid in appids:
        (directory / f"{int(appid)}.json").write_text("[]", encoding="utf-8")
    # 目录里还有非数字项（``achievement_progress`` 之类），绝不能被当成 appid
    (directory / "achievement_progress.json").write_text("{}", encoding="utf-8")
    return root


def test_library_index_reads_appids(tmp_path):
    """库索引只列举 ``<appid>.json``；非数字项被忽略；目录不存在返回 ``None``（降级）。"""
    root = make_library_cache(tmp_path / "steam", MAIN, [1238840, 24960])
    assert ownership.library_index_appids(root, MAIN) == frozenset({1238840, 24960})
    assert ownership.library_index_appids(root, "999") is None      # 没这个账号
    assert ownership.library_index_appids(root, "all") is None      # 合流库不是真实账号


def test_library_index_supplies_owned_when_no_ticket_anywhere(tmp_path):
    """用户反馈核心：**买过但从没装过**（票面没写）的游戏要判「已拥有」（战地1 的根因）。

    ``1238840`` 只在库索引里、本机任何账号都没有它的票 -> ``OWNED``；
    ``553850`` 家庭共享（本账号票 ``flag == 2``）即使在库索引里也仍是 ``PLAYED_SHARED``/``SHARED``
    —— 库索引**绝不能**覆盖票面口径。
    """
    steam = make_steam(tmp_path / "steam", {MAIN: {553850: 2}})
    make_library_cache(steam, MAIN, [1238840, 24960, 553850])
    ownership.reset_cache()

    kinds = ownership.classify(
        steam, MAIN, [1238840, 24960, 553850], playtimes={553850: 30},
    )
    assert kinds[1238840] == ownership.OWNED, "库索引里有、本机没人有票 -> 真拥有"
    assert kinds[24960] == ownership.OWNED
    assert kinds[553850] == ownership.PLAYED_SHARED, "家庭共享票面优先，库索引不得覆盖"


def test_library_index_keeps_shared_when_other_account_has_ticket(tmp_path):
    """本机**别人**有这张票时，库索引不得把它升级成本账号自有（宁放过不误杀）。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}, OWNER: {3001: 0}})
    make_library_cache(steam, MAIN, [3001])
    ownership.reset_cache()

    kinds = ownership.classify(steam, MAIN, [3001], playtimes={})
    assert kinds[3001] == ownership.SHARED, "本机他人有票 -> 走规则 3，不能算本账号自有"


def test_library_index_never_rescues_trace(tmp_path):
    """库索引**不得**把残留痕迹（有售价 + Apps 段没有 Playtime）升级成自有。"""
    steam = make_steam(
        tmp_path / "steam", {MAIN: {}},
        apps={MAIN: {7701: False}},                  # 只有痕迹、没有 Playtime
    )
    make_library_cache(steam, MAIN, [7701])
    ownership.reset_cache()

    kinds = ownership.classify(
        steam, MAIN, [7701], playtimes={}, paid=frozenset({7701}),
    )
    assert kinds[7701] == ownership.TRACE, "痕迹优先于库索引，绝不能被升级成「已拥有」"


def test_library_index_absent_degrades(tmp_path):
    """没有 ``librarycache`` 目录（换机器 / 清过缓存）-> 行为与以前完全一致。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    ownership.reset_cache()
    assert ownership.library_owned_appids(steam, MAIN) is None
    assert ownership.classify(steam, MAIN, [1238840], playtimes={})[1238840] == ownership.FREE
# ------------------------------------------------------------------ 家庭组远端共享借用（T14）

def make_sharing_log(root: Path, lines) -> Path:
    """写 ``logs/librarysharing_log.txt``（行格式与真实日志一致）。

    真实样例行::

        [2025-04-04 10:40:44] Shared lock for app 24960 changed : available owner 0, preferred owner 1531614643, current player 1486800218
        [2025-04-04 10:53:51] Shared lock for app 24960 removed : preferred owner 1531614643
        [2025-04-05 12:00:00] Launching shared app 553850 : using preferred owner 1531614643

    ``available owner 0`` 是「当前没人持有」，绝不能被当成一个账号名。
    """
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "librarysharing_log.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return root


REMOTE = "1531614643"        # 家庭组里的**远端**成员：本机没有它的 userdata


def _changed(appid: int, owner: str, player: str = REMOTE) -> str:
    return (
        f"[2025-04-04 10:40:44] Shared lock for app {appid} changed : "
        f"available owner 0, preferred owner {owner}, current player {player}"
    )


def _removed(appid: int, owner: str) -> str:
    return (
        f"[2025-04-04 10:53:51] Shared lock for app {appid} removed : "
        f"preferred owner {owner}"
    )


def _launch(appid: int, owner: str) -> str:
    return f"[2025-04-05 12:00:00] Launching shared app {appid} : using preferred owner {owner}"


def test_borrowed_appids_flags_remote_owner(tmp_path):
    """owner 是本机**没有**的账号 -> 该 appid 是借来的；owner 是本机账号 -> 不是。

    用户点名的 ``24960 战地：叛逆连队2`` 正是「本机无票、本机 11 个账号也都没它，
    只有共享日志记得 owner 是远端成员」这一类。
    """
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_sharing_log(steam, [
        _changed(24960, REMOTE),
        _removed(24960, REMOTE),
        _launch(553850, REMOTE),
        _changed(3001, MAIN, MAIN),          # owner 就是本机账号 -> 不算借来
    ])
    ownership.reset_cache()
    got = ownership.borrowed_appids(steam)
    assert got == frozenset({24960, 553850})
    assert 3001 not in got


def test_borrowed_appids_degrades_without_log(tmp_path):
    """读不到日志 / 路径为空 -> 空集（= 没有否决信息，绝不改判任何条目）。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    ownership.reset_cache()
    assert ownership.borrowed_appids(steam) == frozenset()
    assert ownership.borrowed_appids("") == frozenset()
    assert ownership.borrowed_appids(None) == frozenset()
    # 目录里有一份空日志也一样
    make_sharing_log(steam, [])
    ownership.reset_cache()
    assert ownership.borrowed_appids(steam) == frozenset()


def test_classify_borrowed_remote_never_owned(tmp_path):
    """借来的游戏**绝不能**标「已拥有」：没玩过 -> ``SHARED``（剔除），玩过 -> ``PLAYED_SHARED``。

    这是用户这一轮的核心反馈：``24960`` 借来且没玩过，应「只有未拥有」；
    ``553850 绝地潜兵2`` 借来但玩过 -> 「玩过（家庭库）」。
    """
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_sharing_log(steam, [_changed(24960, REMOTE), _changed(553850, REMOTE)])
    ownership.reset_cache()

    kinds = ownership.classify(steam, MAIN, [24960, 553850], playtimes={553850: 30})
    assert kinds[24960] == ownership.SHARED
    assert kinds[553850] == ownership.PLAYED_SHARED
    assert ownership.filter_owned(
        steam, MAIN, [24960, 553850], playtimes={553850: 30},
    ) == {553850}


def test_classify_client_list_beats_stale_sharing_log(tmp_path, monkeypatch):
    """客户端权威自有清单压过过期的借用日志（日志是历史流水，清单是当前事实）。"""
    monkeypatch.setattr(ownership, "_LOCALAPPDATA", str(tmp_path / "local"))
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_sharing_log(steam, [_changed(95400, REMOTE)])
    make_client_cache(tmp_path / "local", MAIN, [95400] + [4000 + i for i in range(30)])
    ownership.reset_cache()

    kinds = ownership.classify(steam, MAIN, [95400], playtimes={})
    assert kinds[95400] == ownership.OWNED
    ownership.reset_cache()


def test_library_owned_drops_borrowed_remote(tmp_path):
    """库索引里混着家庭共享 -> 先减去借来的（``24960`` 被误判「已拥有」的根因）。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_library_cache(steam, MAIN, [24960, 1238840])
    make_sharing_log(steam, [_changed(24960, REMOTE)])
    ownership.reset_cache()

    got = ownership.library_owned_appids(steam, MAIN)
    assert got is not None
    assert 24960 not in got, "借来的条目要剔掉"
    assert 1238840 in got, "真正买过、只是没启动过的要留下"


def test_library_owned_intersects_client_owned_list(tmp_path, monkeypatch):
    """有客户端权威清单时取交集：库索引里**已退款**的残留要被剔掉。

    用户点名的 ``413410 Danganronpa: Trigger Happy Havoc``（买过又退款）只剩库索引
    里一份残留；清单是「当前自有全集」，交集后自然出局。
    """
    monkeypatch.setattr(ownership, "_LOCALAPPDATA", str(tmp_path / "local"))
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_library_cache(steam, MAIN, [413410, 95400])
    make_client_cache(tmp_path / "local", MAIN, [95400] + [4000 + i for i in range(30)])
    ownership.reset_cache()

    got = ownership.library_owned_appids(steam, MAIN)
    assert 413410 not in got, "退款残留不在权威清单里 -> 不算自有"
    assert 95400 in got, "清单里有 -> 自有"

    kinds = ownership.classify(steam, MAIN, [413410, 95400], playtimes={})
    assert kinds[413410] == ownership.FREE
    assert kinds[95400] == ownership.OWNED
    ownership.reset_cache()


def test_library_owned_keeps_entry_when_client_list_unavailable(tmp_path):
    """没有客户端清单（换机器 / 清缓存）-> 不做交集，绝不因为信息不足剔条目。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_library_cache(steam, MAIN, [413410])
    ownership.reset_cache()

    got = ownership.library_owned_appids(steam, MAIN)
    assert got == frozenset({413410})
def test_library_owned_drops_entries_visible_in_peer_library(tmp_path):
    """**T18 的核心修复**：只在**别的账号**库视图里出现的条目不算自有。

    用户第八次反馈：「还是把家庭库里的游戏算作当前账号的游戏」。库索引是**库页面视图**，
    家庭库里别人拥有的游戏在本账号库里也看得见，于是「本机无票」挡不住它们 ——
    实测主账号 467 条库索引里有 105 条同时出现在别的账号库里，其中 ``412020 地铁：离乡``
    / ``379720 DOOM`` / ``1057090 奥日与萤火意志`` 被当成「买过没写票」的自有游戏。

    ``7001`` 是我自己的（只有我的库里有）、``7002`` 是别人库里的 -> 只留 ``7001``。
    """
    steam = make_steam(tmp_path / "steam", {MAIN: {}, OWNER: {}})
    make_library_cache(steam, MAIN, [7001, 7002])
    make_library_cache(steam, OWNER, [7002])
    ownership.reset_cache()

    got = ownership.library_owned_appids(steam, MAIN)
    assert got is not None
    assert 7001 in got, "只有我的库里有 -> 仍算自有（买过没写票的那一类）"
    assert 7002 not in got, "别的账号库里也有 -> 家庭库条目，不算本账号自有"

    kinds = ownership.classify(steam, MAIN, [7001, 7002], playtimes={})
    assert kinds[7001] == ownership.OWNED
    assert kinds[7002] == ownership.FREE, "不再升级成自有；没票没玩过仍走保留分支"
    ownership.reset_cache()


def test_library_owned_keeps_own_entries_when_only_peers_have_cache(tmp_path):
    """本账号没有库索引时不改判（``None``），绝不因为「别的账号有库」而误剔。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}, OWNER: {}})
    make_library_cache(steam, OWNER, [7001, 7002])
    ownership.reset_cache()
    assert ownership.library_owned_appids(steam, MAIN) is None
    ownership.reset_cache()


def test_owned_library_appids_is_union_of_all_own_sources(tmp_path, monkeypatch):
    """``owned_library_appids`` = 票面 flag==0 ∪ 权威清单 ∪ 库索引补全（归属的唯一汇总）。

    ⚠️ 有权威清单时库索引要与之**取交集**（清单是账号级全集，优先级最高），所以这里
    ``1003`` 会被交集挡掉 —— 这正是「退款残留不算自有」那条既有约定的同一条路径。
    """
    monkeypatch.setattr(ownership, "_LOCALAPPDATA", str(tmp_path / "local"))
    steam = make_steam(tmp_path / "steam", {MAIN: {1001: 0, 1002: 2}})
    make_library_cache(steam, MAIN, [1003, 1004])
    make_library_cache(steam, OWNER, [1004])
    make_client_cache(tmp_path / "local", MAIN, [1005] + [4000 + i for i in range(30)])
    ownership.reset_cache()

    got = ownership.owned_library_appids(steam, MAIN)
    assert 1001 in got, "票面 flag==0"
    assert 1002 not in got, "flag==2 是家庭共享，不是自有"
    assert 1005 in got, "权威自有清单"
    # 有权威清单时库索引取交集：清单里没有的都不算自有（含别人的家庭库条目与退款残留）
    assert 1003 not in got and 1004 not in got
    ownership.reset_cache()


def test_owned_library_appids_without_client_list_uses_peer_gate(tmp_path):
    """没有权威清单时走「三道减法」：库索引补全仍然生效，但**别的账号库里有的**要剔掉。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {1001: 0}, OWNER: {}})
    make_library_cache(steam, MAIN, [1003, 1004])
    make_library_cache(steam, OWNER, [1004])
    ownership.reset_cache()

    got = ownership.owned_library_appids(steam, MAIN)
    assert 1001 in got, "票面 flag==0"
    assert 1003 in got, "只有我库里有 -> 库索引补全"
    assert 1004 not in got, "别的账号库里也有 -> 家庭库条目，不算自有"
    ownership.reset_cache()


# ------------------------------------------------------------------ 免费游戏归属（T15）

def test_free_appids_reads_is_free_and_f2p(tmp_path):
    """``free_appids``：``is_free`` 为真、或「免费开玩 + 无售价」才算免费。

    用户反馈「部分免费游戏不在游戏明细中」。免费游戏没有属于自己的授权票，
    只看票面会把它们当「别人借你的、你还没玩过」而剔除。
    """
    db = make_fake_db(tmp_path / "cache.db", [
        (90001, 0, 1, '["Action"]', '["动作"]'),                        # is_free 标记
        (90002, 0, None, '["Free to Play"]', '["免费开玩"]'),            # 免费开玩、无价
        (90003, 29800, None, '["RPG"]', '["角色扮演"]'),                 # 付费
        (90004, 29800, None, '["Free to Play"]', '["免费开玩"]'),        # 有价却标免费 -> 不算
        (90005, 9900, None, '[]', '[]'),                               # 一般付费
    ])
    assert ownership.free_appids(db) == {90001, 90002}


def test_free_appids_degrades_to_none(tmp_path):
    """整个库读不到 -> ``None``（调用方保持原有判定，绝不据此剔除）。"""
    assert ownership.free_appids(tmp_path / "nope.db") is None
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not sqlite")
    assert ownership.free_appids(broken) is None


def test_free_game_with_shared_flag_is_kept(tmp_path, monkeypatch):
    """**规则 2 的核心修复**：免费游戏带 ``flag == 2`` 时按 FREE 保留，不再被当共享剔除。

    真实场景（本机 ``10000002``）：CS2 / Apex / 彩虹六号 这些免费游戏在任何账号下
    都会被 Steam 写成 ``flag == 2`` + ``playtime == 0``，旧行为整批剔除。
    """
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001}))
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 2, 9002: 2}})
    ownership.reset_cache()

    kinds = ownership.classify(steam, MAIN, [9001, 9002], playtimes={})
    assert kinds[9001] == ownership.FREE, "免费游戏不该被当共享剔除"
    assert kinds[9002] == ownership.SHARED, "付费共享且没玩过仍要剔除"
    assert ownership.filter_owned(steam, MAIN, [9001, 9002], playtimes={}) == {9001}
    ownership.reset_cache()


def test_free_game_owned_by_peer_belongs_to_peer(tmp_path, monkeypatch):
    """**T18 的核心修复**：免费游戏的票落在**别的账号**名下 -> 它是别人的，本账号不该占。

    用户第八次反馈原话：「你还把别的账号的免费游戏同时算到别的账号中」。

    真实场景（本机 ``10000002`` / ``10000004``）：``544810 KARDS`` / ``1085660 命运2``
    / ``1399720 反物质维度`` 的 ``flag == 0`` 只落在主账号 ``10000001`` 名下，其余账号
    只是**库页面**能看见（家庭库视图）。旧口径把「在库里 + 是免费游戏」当成自己的一份，
    于是这些游戏在别的账号里也被算作拥有 —— 本用例锁死新口径：**谁持票就归谁**。

    （``9001`` 免费、``9002`` 付费，两者票都在 ``OWNER`` 名下 -> 对 ``MAIN`` 都是共享。）
    """
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001}))
    steam = make_steam(tmp_path / "steam", {MAIN: {}, OWNER: {9001: 0, 9002: 0}})
    ownership.reset_cache()

    kinds = ownership.classify(steam, MAIN, [9001, 9002], playtimes={})
    assert kinds[9001] == ownership.SHARED, "票在别人名下 -> 免费游戏也属于别人"
    assert kinds[9002] == ownership.SHARED, "付费的别人拥有、自己没票 -> 仍是共享"
    # 玩过就按「玩过（家庭库）」保留 —— 与自有 / 未拥有是两种口径
    played_kinds = ownership.classify(steam, MAIN, [9001], playtimes={9001: 30})
    assert played_kinds[9001] == ownership.PLAYED_SHARED
    ownership.reset_cache()


def test_free_game_owned_by_me_is_still_kept(tmp_path, monkeypatch):
    """反向保护：免费游戏的票落在**本账号**名下时照旧算拥有（规则 1，不受 T18 影响）。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001}))
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 0}, OWNER: {}})
    ownership.reset_cache()

    kinds = ownership.classify(steam, MAIN, [9001], playtimes={})
    assert kinds[9001] == ownership.OWNED
    ownership.reset_cache()


def test_free_table_unavailable_keeps_old_behaviour(tmp_path):
    """读不到免费表（``None``）-> 行为与修复前**完全一致**（绝不因信息不足改判）。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 2}})
    ownership.reset_cache()
    kinds = ownership.classify(steam, MAIN, [9001], playtimes={})
    assert kinds[9001] == ownership.SHARED
    ownership.reset_cache()


# ------------------------------------------------------------------ 归属自动体检（T15）

def test_audit_reports_clean_when_evidence_agrees(tmp_path, monkeypatch):
    """证据自洽时 ``ok=True``、无 issue —— 且**核对条数**要被报出来。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset())
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 0, 9002: 2}})
    make_library_cache(steam, MAIN, [9001, 9002])
    ownership.reset_cache()

    report = ownership.audit_ownership(steam, MAIN, [9001, 9002], {})
    assert report["ok"] is True
    assert report["degraded"] is False
    assert report["issues"] == []
    assert report["checked"] == 2
    ownership.reset_cache()


def test_audit_catches_free_game_dropped_as_shared(tmp_path, monkeypatch):
    """**体检要点**：免费游戏被判成共享剔除时，自己报 ``shared-free-game`` 错误。

    这正是用户这一轮反馈的问题 —— 修复前要人肉发现，现在工具自己报出来。
    这里把 ``classify`` 换成「修复前的错误判定」来复现该场景。
    """
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001}))
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 2}})
    make_library_cache(steam, MAIN, [9001])
    monkeypatch.setattr(
        ownership, "classify", lambda *a, **k: {9001: ownership.SHARED}
    )
    ownership.reset_cache()

    report = ownership.audit_ownership(steam, MAIN, [9001], {})
    assert report["ok"] is False
    assert report["errors"] == 1
    assert report["counts"] == {"shared-free-game": 1}
    assert report["issues"][0]["appid"] == 9001
    ownership.reset_cache()


def test_audit_catches_dropped_despite_playtime(tmp_path, monkeypatch):
    """**体检要点**：玩过的游戏被剔除时报 ``dropped-despite-playtime``。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset())
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 2}})
    make_library_cache(steam, MAIN, [9001])
    monkeypatch.setattr(
        ownership, "classify", lambda *a, **k: {9001: ownership.SHARED}
    )
    ownership.reset_cache()

    report = ownership.audit_ownership(steam, MAIN, [9001], {9001: 120})
    assert report["ok"] is False
    assert report["counts"] == {"dropped-despite-playtime": 1}
    ownership.reset_cache()


def test_audit_reports_clean_for_fixed_free_game(tmp_path, monkeypatch):
    """**修复后不再误报**：免费游戏正常判成 FREE 时，体检必须报干净。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001}))
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 2}})
    make_library_cache(steam, MAIN, [9001])
    ownership.reset_cache()

    kinds = ownership.classify(steam, MAIN, [9001], {})
    assert kinds[9001] == ownership.FREE
    report = ownership.audit_ownership(steam, MAIN, [9001], {})
    assert report["ok"] is True and report["issues"] == []
    ownership.reset_cache()


def test_audit_does_not_flag_normal_family_sharing(tmp_path, monkeypatch):
    """**不误报**：本机别的账号真拥有、本账号没票 —— 这是正常的家庭共享，不算问题。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset())
    steam = make_steam(tmp_path / "steam", {MAIN: {}, OWNER: {9001: 0}})
    make_library_cache(steam, MAIN, [9001])
    ownership.reset_cache()

    report = ownership.audit_ownership(steam, MAIN, [9001], {})
    assert report["ok"] is True, "正常的家庭共享不该被体检判成问题"
    assert report["issues"] == []
    ownership.reset_cache()


def test_audit_degrades_without_ticket_or_index(tmp_path):
    """读不到票面 / 库索引 -> ``degraded=True``，**绝不谎报「一切正常」**。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 0}})     # 没有 librarycache
    ownership.reset_cache()
    report = ownership.audit_ownership(steam, MAIN, [9001], {})
    assert report["degraded"] is True
    assert report["ok"] is False
    assert report["issues"] == []
    # 账号非法（合流库 id）同样降级，绝不按普通账号瞎判
    assert ownership.audit_ownership(steam, "all", [9001], {})["degraded"] is True
    ownership.reset_cache()


def test_audit_catches_owned_despite_peer_evidence(tmp_path, monkeypatch):
    """**T18 自动识别**：自己没票、却因「库里可见」判成拥有，而别的账号持有它 -> 报存疑。

    这条替代「用户逐个点名家庭库条目」—— 工具自己拿对端票面对账，不用人肉发现。
    """
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset())
    steam = make_steam(tmp_path / "steam", {MAIN: {}, OWNER: {9001: 0}})
    make_library_cache(steam, MAIN, [9001])
    monkeypatch.setattr(ownership, "classify", lambda *a, **k: {9001: ownership.OWNED})
    ownership.reset_cache()

    report = ownership.audit_ownership(steam, MAIN, [9001], {})
    assert report["counts"].get("owned-but-peer-evidence") == 1
    assert report["ok"] is True, "存疑不该把体检判成失败（只有确证错标才是 error）"
    ownership.reset_cache()


def test_audit_does_not_flag_shared_ownership(tmp_path, monkeypatch):
    """双方**各自持票**是合法的（同一款游戏可被多人拥有），绝不误报。

    本机实测 ``238960`` 在 ``10000003`` 与 ``10000002`` 下都是 ``flag == 0``。
    """
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset())
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 0}, OWNER: {9001: 0}})
    make_library_cache(steam, MAIN, [9001])
    ownership.reset_cache()

    report = ownership.audit_ownership(steam, MAIN, [9001], {})
    assert report["issues"] == [] and report["ok"] is True
    ownership.reset_cache()


def test_audit_does_not_flag_free_game_owned_by_peer(tmp_path, monkeypatch):
    """免费游戏的票明确落在别的账号名下 -> 判成共享是**正确**的，体检不该报错。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001}))
    steam = make_steam(tmp_path / "steam", {MAIN: {}, OWNER: {9001: 0}})
    make_library_cache(steam, MAIN, [9001])
    ownership.reset_cache()

    report = ownership.audit_ownership(steam, MAIN, [9001], {})
    assert report["counts"] == {}
    assert report["ok"] is True
    ownership.reset_cache()


def test_audit_catches_owned_without_evidence(tmp_path, monkeypatch):
    """判为已拥有却没有出处（不在票面 / 也不在库索引）-> ``owned-without-evidence`` 存疑。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset())
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_library_cache(steam, MAIN, [])
    monkeypatch.setattr(ownership, "classify", lambda *a, **k: {9002: ownership.OWNED})
    ownership.reset_cache()

    report = ownership.audit_ownership(steam, MAIN, [9002], {})
    assert report["counts"] == {"owned-without-evidence": 1}
    assert report["warnings"] == 1
    assert report["ok"] is True, "存疑不影响 ok（只有确证错标才 ok=False）"
    ownership.reset_cache()


# ------------------------------------------------------------------ 明细补全（M2f / T16）

def test_library_appids_is_union_of_index_and_client(tmp_path):
    """「库里有什么」= 库索引 ∪ 客户端自有清单；两侧都读不到 -> ``None``（降级）。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    assert ownership.library_appids(steam, MAIN) is None      # 两侧都没有 -> 信息不足
    make_library_cache(steam, MAIN, [9001, 9002])
    ownership.reset_cache()
    assert ownership.library_appids(steam, MAIN) == frozenset({9001, 9002})
    ownership.reset_cache()


def test_missing_free_library_games_only_reports_free(tmp_path, monkeypatch):
    """只报「在库里 + 免费 + 没显示」；付费游戏即使在库里也不补（证据不足，宁缺勿滥）。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001}))
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_library_cache(steam, MAIN, [9001, 9002])          # 9001 免费、9002 付费
    ownership.reset_cache()

    missing = ownership.missing_free_library_games(steam, MAIN, listed=[])
    assert missing == frozenset({9001}), "付费的 9002 不该被补进来"
    # 已经显示在明细里的不重复报
    assert ownership.missing_free_library_games(steam, MAIN, listed=[9001]) == frozenset()
    ownership.reset_cache()


def test_missing_free_library_games_skips_peers_free_games(tmp_path, monkeypatch):
    """**T18 的核心修复**：别的账号名下的免费游戏不补进本账号。

    用户第八次反馈：「你还把别的账号的免费游戏同时算到别的账号中」。免费游戏的**库页面**
    可见性会出现在所有家庭成员的库里，旧判据「(库索引 ∪ 客户端清单) ∩ 免费」于是把
    主账号名下的 ``544810 KARDS`` / ``1085660 命运2`` / ``1399720 反物质维度``
    补进了另一个账号（本机实测 ``10000002`` 11 条、``10000004`` 7 条）。

    ``9001`` = 主账号自己持有票的免费游戏（该补）；``9002`` = 票在别的账号名下（不该补）。
    """
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001, 9002}))
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 0}, OWNER: {9002: 0}})
    make_library_cache(steam, MAIN, [9001, 9002])
    ownership.reset_cache()

    assert ownership.missing_free_library_games(steam, MAIN, listed=[]) == frozenset({9001})
    # 反过来：9002 的持票人是 OWNER，对它自己才算「在库里的一份」
    assert ownership.missing_free_library_games(steam, OWNER, listed=[]) == frozenset({9002})
    ownership.reset_cache()


def test_missing_free_library_games_degrades_without_index(tmp_path, monkeypatch):
    """读不到库索引 / 读不到 cache.db -> 空集，**绝不凭空造条目**。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001}))
    steam = make_steam(tmp_path / "steam", {MAIN: {}})      # 没有 librarycache
    ownership.reset_cache()
    assert ownership.missing_free_library_games(steam, MAIN, listed=[]) == frozenset()
    # 库读得到、但免费表读不到 -> 同样不补
    make_library_cache(steam, MAIN, [9001])
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: None)
    ownership.reset_cache()
    assert ownership.missing_free_library_games(steam, MAIN, listed=[]) == frozenset()
    ownership.reset_cache()


def test_augment_library_adds_free_games_without_touching_existing(tmp_path, monkeypatch):
    """补进来的条目时长 0、状态「未玩」；**原有条目一字不改**，入参也不被改动。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001, 9002}))
    steam = make_steam(tmp_path / "steam", {MAIN: {9001: 0}})
    make_library_cache(steam, MAIN, [9002, 9003])           # 9002 免费、9003 付费
    ownership.reset_cache()

    original = {9001: {"playtime": 40, "last_played": 1234}}
    augmented = ownership.augment_library(original, steam, MAIN)
    assert original == {9001: {"playtime": 40, "last_played": 1234}}, "绝不能改入参"
    assert 9002 in augmented and 9003 not in augmented
    assert augmented[9002] == {"playtime": 0, "last_played": 0}
    assert augmented[9001] == {"playtime": 40, "last_played": 1234}, "原有条目必须原样保留"
    ownership.reset_cache()


def test_augment_library_skips_non_games(tmp_path, monkeypatch):
    """非游戏条目（Steam 客户端本体 / 工具）绝不补进明细。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({7, 9001}))
    monkeypatch.setattr(ownership, "_is_non_game", lambda appid: int(appid) == 7)
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_library_cache(steam, MAIN, [7, 9001])
    ownership.reset_cache()

    augmented = ownership.augment_library({}, steam, MAIN)
    assert 9001 in augmented and 7 not in augmented
    ownership.reset_cache()


def test_audit_catches_free_game_missing_from_detail(tmp_path, monkeypatch):
    """**自动识别**：在库里、是免费游戏、却不在明细里 -> ``free-in-library-not-listed``（error）。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({9001}))
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_library_cache(steam, MAIN, [9001])
    ownership.reset_cache()

    # 明细里漏掉了它 -> 工具必须自己报出来（不用用户点名）
    report = ownership.audit_ownership(steam, MAIN, [9010], {})
    assert report["ok"] is False
    assert report["counts"].get("free-in-library-not-listed") == 1
    assert any(i["reason"] == "free-in-library-not-listed" and i["appid"] == 9001
               for i in report["issues"])
    # 明细里已经有了 -> 不报（不误报）
    clean = ownership.audit_ownership(steam, MAIN, [9001], {})
    assert clean["ok"] is True and clean["issues"] == []
    # 不传明细 -> 不做漏项体检（不知道用户看到什么，绝不凭空断言）
    blind = ownership.audit_ownership(steam, MAIN, None, {})
    assert not any(i["reason"] == "free-in-library-not-listed" for i in blind["issues"])
    ownership.reset_cache()


# ------------------------------------------------------------------ Steam 客户端实时归属（T22 线）

def _fake_live(monkeypatch, owned, others, *, team=(MAIN,), group="g-test"):
    """把一个假的 Steam 客户端权威快照接到 ``core.steam_live`` 上（完全离线）。

    真实现是经 CDP 问 Steam 客户端要 ``CFamilyGroups_GetSharedLibraryApps``；
    测试只关心「拿到 ``{owned, others}`` 之后判据怎么用」，所以在这里替换掉取数那一步。
    """
    from core import steam_live

    snapshot = {"owned": frozenset(int(x) for x in owned),
                "others": frozenset(int(x) for x in others),
                "at": 1, "source": "live", "group": group}

    def fake(account_id, *, refresh=True, port=None, timeout=None):
        return snapshot if str(account_id) in {str(m) for m in team} else None

    monkeypatch.setattr(steam_live, "ownership_for", fake)
    ownership.reset_cache()
    return snapshot


def test_live_owned_beats_library_index(tmp_path, monkeypatch):
    """权威清单在手 -> 库索引推断**整条停用**（用户点名「命令与征服 / 杀手系列不对」的根因）。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_library_cache(steam, MAIN, [1238840, 17480])
    _fake_live(monkeypatch, owned={1238840}, others={17480})

    kinds = ownership.classify(steam, MAIN, [1238840, 17480], playtimes={}, paid=frozenset())
    assert kinds[1238840] == ownership.OWNED
    assert kinds[17480] == ownership.SHARED, "权威清单说归别人 -> 绝不能被库索引升级成自有"


def test_live_other_played_is_played_shared(tmp_path, monkeypatch):
    """家庭库条目 + 本账号玩过 -> ``PLAYED_SHARED``（不是拥有）。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    _fake_live(monkeypatch, owned=set(), others={17480})
    kinds = ownership.classify(steam, MAIN, [17480], playtimes={17480: 60}, paid=frozenset())
    assert kinds[17480] == ownership.PLAYED_SHARED


def test_live_owned_keeps_free_game_never_installed(tmp_path, monkeypatch):
    """用户点名的 ``386360 神之浩劫``：免费游戏 + 真拥有 + 从没玩过 -> 规则 1 判拥有。"""
    monkeypatch.setattr(ownership, "free_appids", lambda db=None: frozenset({386360}))
    steam = make_steam(tmp_path / "steam", {MAIN: {386360: 2}})
    _fake_live(monkeypatch, owned={386360}, others=set())

    kinds = ownership.classify(steam, MAIN, [386360], playtimes={}, paid=frozenset())
    assert kinds[386360] == ownership.OWNED, "权威清单说归自己，与票面 / 免费标记无关"


def test_live_helpers_return_none_when_unavailable(tmp_path):
    """读不到实时源（= 会话默认关闭）-> 一律 ``None``（信息不足，绝不改判）。"""
    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    assert ownership.live_owned_appids(steam, MAIN) is None
    assert ownership.live_other_owned_appids(steam, MAIN) is None
    assert ownership.live_ownership(steam, "all") is None


def test_authoritative_owned_skips_library_index_when_live(tmp_path, monkeypatch):
    steam = make_steam(tmp_path / "steam", {MAIN: {1238840: 0}})
    make_library_cache(steam, MAIN, [1238840, 17480])
    _fake_live(monkeypatch, owned={1238840}, others={17480})
    owned = ownership.authoritative_owned_appids(steam, MAIN)
    assert 1238840 in owned
    assert 17480 not in owned


def test_authoritative_owned_falls_back_to_library_index(tmp_path, monkeypatch):
    """Steam 没开（拿不到权威清单）-> 行为与 T22 之前完全一致（库索引兜底）。"""
    from core import steam_live

    steam = make_steam(tmp_path / "steam", {MAIN: {}})
    make_library_cache(steam, MAIN, [1238840])
    monkeypatch.setattr(steam_live, "ownership_for", lambda *a, **k: None)
    ownership.reset_cache()
    owned = ownership.authoritative_owned_appids(steam, MAIN)
    assert 1238840 in owned


def test_authoritative_owned_is_none_without_ticket(tmp_path):
    """读不到票面 -> ``None``（信息不足），调用方保持原判定。"""
    steam = make_steam(tmp_path / "steam", {})
    assert ownership.authoritative_owned_appids(steam, MAIN) is None
