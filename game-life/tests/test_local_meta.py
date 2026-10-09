r"""离线元数据自测：中文名（core/metadata_cn.py）+ 本机成就（core/achievements_local.py）。

运行方式（game-life 目录）：
    ..\.venv\Scripts\python.exe tests\test_local_meta.py

零额外依赖、全部使用临时目录与打桩，**不发真实网络请求**、不写真实 data/。

覆盖：
    1. metadata_cn：缓存读写 / 只读查询 / 清理；
    2. 联网抓取（打桩 store_details）→ 写入中文名，第二次运行直接复用（不联网）；
    3. 离线模式不会把「没查到」记成已检查（否则之后联网会被 TTL 跳过）；
    4. 两个商店源 + steamcmd 镜像的兜底顺序（打桩）；
    5. 源健康短路：连续失败 N 次后不再尝试该源；
    6. achievements_local：Schema 数总数、本机 bin 数已解锁、多分组取并集；
    7. 缺文件 / 坏文件时的降级（返回 None 或 0，不抛异常）；
    8. appinfo / artwork / metadata_cn 三个模块的公开接口齐全（面板依赖的签名）；
"""

import struct
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import achievements_local as ach   # noqa: E402
from core import metadata_cn as mc           # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


# ------------------------------------------------------------ 造 Steam 二进制 KV

def kv_bytes(node: dict) -> bytes:
    """把 dict 编成 Steam 的 KeyValues 二进制（键为 \\0 结尾内联串）。"""
    out = bytearray()
    for key, value in node.items():
        raw = str(key).encode("utf-8")
        if isinstance(value, dict):
            out += b"\x00" + raw + b"\x00" + kv_bytes(value) + b"\x08"
        elif isinstance(value, str):
            out += b"\x01" + raw + b"\x00" + value.encode("utf-8") + b"\x00"
        elif isinstance(value, int):
            out += b"\x02" + raw + b"\x00" + struct.pack("<I", value)
        else:
            raise TypeError(f"不支持的测试值类型：{type(value)}")
    return bytes(out)


def make_steam_tree(root: Path, appid: int, total: int, unlocked: list[int],
                    account: str = "999999") -> None:
    stats_dir = root / "appcache" / "stats"
    stats_dir.mkdir(parents=True, exist_ok=True)
    bits = {str(i): {"name": f"ACH_{i}"} for i in range(total)}
    schema = {str(appid): {"stats": {"1": {"bits": bits}}, "version": 7, "gamename": "Test Game"}}
    (stats_dir / f"UserGameStatsSchema_{appid}.bin").write_bytes(kv_bytes(schema))
    times = {str(i): 1700000000 + i for i in unlocked}
    user = {"cache": {"crc": 1, "PendingChanges": 0, "1": {"data": 5, "AchievementTimes": times}}}
    (stats_dir / f"UserGameStats_{account}_{appid}.bin").write_bytes(kv_bytes(user))


# ------------------------------------------------------------------ 用例

def case_cache_roundtrip(tmp: Path) -> None:
    cache_file = tmp / "names_cn.json"
    original = mc.cache_path
    mc.cache_path = lambda: cache_file   # 先指向临时文件，再清理，绝不碰真实缓存
    mc.clear_cache()
    try:
        check(mc.load_cache()["items"] == {}, "初始缓存应为空")
        mc._items()["123"] = {"name_cn": "测试游戏", "source": "unit"}
        mc._save_cache()
        mc.clear_cache()                      # 清掉磁盘缓存
        check(not cache_file.exists(), "clear_cache 应删除磁盘缓存")
        mc._items()["456"] = {"name_cn": "另一个", "name_en": "Another", "source": "unit"}
        mc._save_cache()
        mc._cache = None                      # 强制重新读盘
        check(mc.load_cache()["items"]["456"]["name_cn"] == "另一个", "缓存应能读回")
        check(mc.name_cn_for(456) == "另一个", "name_cn_for 应命中缓存")
        check(mc.name_cn_for(789) is None, "未收录的 appid 应返回 None")
    finally:
        mc.clear_cache()          # 必须趁 cache_path 还指向 tmp 时清理，否则会删真实 data\names_cn.json
        mc.cache_path = original


def case_ensure_online(tmp: Path) -> None:
    cache_file = tmp / "names_online.json"
    original_path, original_store = mc.cache_path, mc.store_details
    mc.cache_path = lambda: cache_file
    mc.clear_cache()
    calls = {"n": 0}

    from core import appinfo

    fake_ids = [990001, 990002]           # 本机 appinfo 里不存在的 appid，才走联网分支
    for appid in fake_ids:
        check(appinfo.localized_name(appid) is None, f"{appid} 不应有本机中文名（用例前提）")

    def fake_store(appid, timeout=None, apis=None):
        calls["n"] += 1
        return {"name": "刀塔 2", "header_image": "https://example/header.jpg",
                "genres": [{"description": "策略"}], "developers": ["Valve"],
                "release_date": {"date": "2013 年 7 月 9 日"}}

    mc.store_details = fake_store
    try:
        stat = mc.ensure_names_cn([990001, 990001, 990002], limit=None)
        check(stat["ok_count"] == 2, f"应抓到 2 个中文名，实际 {stat['ok_count']}")
        check(calls["n"] == 2, f"去重后只应请求 2 次，实际 {calls['n']}")
        check(mc.name_cn_for(990001) == "刀塔 2", "抓到的中文名应可只读命中")
        calls["n"] = 0
        again = mc.ensure_names_cn([990001, 990002])
        check(calls["n"] == 0, "已抓过的游戏不应重复联网")
        check(again["skip_count"] == 2, f"应全部复用，实际 skip={again['skip_count']}")
    finally:
        mc.clear_cache()          # 先清 tmp，再恢复真实路径 / 打桩
        mc.cache_path = original_path
        mc.store_details = original_store


def case_offline_not_poison(tmp: Path) -> None:
    cache_file = tmp / "names_offline.json"
    original_path = mc.cache_path
    mc.cache_path = lambda: cache_file
    mc.clear_cache()
    try:
        out = mc.ensure_names_cn([222222], use_network=False)
        check(out["ok_count"] == 0, "离线且本机没有中文名时应是 0")
        entry = (mc.load_cache()["items"] or {}).get("222222") or {}
        check(not entry.get("checked_at"),
              "离线未命中不应写 checked_at（否则 30 天内不再联网补）")
    finally:
        mc.clear_cache()          # 先清 tmp，再恢复真实路径
        mc.cache_path = original_path


def case_mirror_fallback(tmp: Path) -> None:
    cache_file = tmp / "names_mirror.json"
    original_path, original_store, original_mirror = mc.cache_path, mc.store_details, mc.steamcmd_details
    mc.cache_path = lambda: cache_file
    mc.clear_cache()
    mc.store_details = lambda *a, **k: {}                     # 两个商店源都没结果
    mc.steamcmd_details = lambda *a, **k: {"name": "ELDEN RING", "name_cn": "艾尔登法环"}
    try:
        out = mc.ensure_names_cn([990003])
        row = out["results"][990003]
        check(row["name_cn"] == "艾尔登法环", "应回落到 steamcmd 镜像拿中文名")
        check(row["source"] == "steamcmd", f"来源标记应为 steamcmd，实际 {row['source']}")
    finally:
        mc.clear_cache()          # 先清 tmp，再恢复真实路径 / 打桩
        mc.cache_path = original_path
        mc.store_details = original_store
        mc.steamcmd_details = original_mirror


def case_host_circuit_breaker() -> None:
    mc.reset_hosts()
    try:
        check(mc._host_of(mc.STORE_APIS[0]) == "store.steamchina.com",
              "第一个源应是蒸汽平台")
        for _ in range(mc._MAX_HOST_FAILS):
            mc._note_host_fail("store.steampowered.com")
        check("store.steampowered.com" in mc._DEAD_HOSTS, "连续失败后应短路该源")
        check("store.steamchina.com" not in mc._DEAD_HOSTS, "不应误伤其它源")
    finally:
        mc.reset_hosts()


def case_achievement_counts(tmp: Path) -> None:
    ach.clear_cache()
    steam = tmp / "steam_a"
    make_steam_tree(steam, 1001, total=3, unlocked=[0, 2])
    row = ach.local_achievement(1001, account_id="999999", steam_path=str(steam))
    check(row is not None, "有 Schema+记录时应返回数据")
    if row:
        check(row["achieved_count"] == 2, f"已解锁应为 2，实际 {row['achieved_count']}")
        check(row["total_count"] == 3, f"总数应为 3，实际 {row['total_count']}")
        check(abs(row["completion_rate"] - 66.67) < 0.01, "完成率应为 66.67")
        check(row["source"] == "steam_client", "来源应为 steam_client")
    check(ach.stats("999999", str(steam))["schema_files"] == 1, "stats 应数到 1 个定义文件")


def case_achievement_union(tmp: Path) -> None:
    """多分组（多次写入）的解锁记录应取并集。"""
    ach.clear_cache()
    steam = tmp / "steam_b"
    stats_dir = steam / "appcache" / "stats"
    stats_dir.mkdir(parents=True, exist_ok=True)
    bits = {str(i): {"name": f"A{i}"} for i in range(10)}
    (stats_dir / "UserGameStatsSchema_2002.bin").write_bytes(
        kv_bytes({"2002": {"stats": {"1": {"bits": bits}}}})
    )
    user = {"cache": {
        "crc": 1, "PendingChanges": 0,
        "1": {"data": 1, "AchievementTimes": {"0": 1, "1": 2}},
        "2": {"data": 2, "AchievementTimes": {"1": 3, "5": 4}},
    }}
    (stats_dir / "UserGameStats_999999_2002.bin").write_bytes(kv_bytes(user))
    row = ach.local_achievement(2002, account_id="999999", steam_path=str(steam))
    check(row and row["achieved_count"] == 3, f"并集应为 3 个成就，实际 {row}")


def case_achievement_missing(tmp: Path) -> None:
    ach.clear_cache()
    steam = tmp / "steam_c"
    (steam / "appcache" / "stats").mkdir(parents=True, exist_ok=True)
    check(ach.local_achievement(3003, "999999", str(steam)) is None,
          "没有 Schema 时应返回 None（而不是 0/0）")
    check(ach.local_achievements([3003, 3004], "999999", str(steam)) == {},
          "批量接口应跳过没有数据的游戏")
    check(ach.has_local_data(3003, str(steam)) is False, "has_local_data 应为 False")

    # 有 Schema 但没有玩家记录 → 0 / 总数，而不是 None
    make_steam_tree(steam, 3005, total=4, unlocked=[])
    (steam / "appcache" / "stats" / "UserGameStats_999999_3005.bin").unlink()
    row = ach.local_achievement(3005, "999999", str(steam))
    check(row and row["achieved_count"] == 0 and row["total_count"] == 4,
          f"缺玩家记录应为 0/4，实际 {row}")

    # 坏文件 → 不抛异常
    (steam / "appcache" / "stats" / "UserGameStatsSchema_3006.bin").write_bytes(b"\x00bad\x00")
    check(ach.local_achievement(3006, "999999", str(steam)) is None,
          "坏 Schema 应降级为 None，不抛异常")


def case_public_api_shape() -> None:
    from core import appinfo, artwork

    for module, names in (
        (appinfo, ("localized_name", "app_metadata", "local_cover_files", "load_index")),
        (artwork, ("ensure_cover", "ensure_covers", "cover_for_panel", "covers_dir")),
        (mc, ("ensure_names_cn", "name_cn_for", "load_cache", "store_details")),
        (ach, ("local_achievement", "local_achievements", "stats")),
    ):
        for name in names:
            check(callable(getattr(module, name, None)),
                  f"{module.__name__} 缺少接口 {name}")


def main() -> int:
    cases = [
        ("缓存读写 / 只读查询 / 清理", case_cache_roundtrip),
        ("联网抓取 + 复用（打桩）", case_ensure_online),
        ("离线未命中不污染缓存", case_offline_not_poison),
        ("商店源失败 → steamcmd 镜像兜底", case_mirror_fallback),
        ("源健康短路（连续失败即停用）", case_host_circuit_breaker),
        ("本机成就：总数/已解锁/完成率", case_achievement_counts),
        ("本机成就：多分组取并集", case_achievement_union),
        ("本机成就：缺文件/坏文件降级", case_achievement_missing),
        ("四个模块的公开接口齐全", case_public_api_shape),
    ]
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="glmeta_") as tmpname:
        tmp = Path(tmpname)
        for index, (name, fn) in enumerate(cases, 1):
            print(f"  [{index}/{len(cases)}] {name} ...", end="", flush=True)
            before = len(FAILURES)
            try:
                if fn.__code__.co_argcount:
                    fn(tmp)
                else:
                    fn()
            except Exception as exc:
                import traceback

                FAILURES.append(f"{name} 抛异常: {type(exc).__name__}: {exc}")
                traceback.print_exc()
            print(" 失败" if len(FAILURES) > before else " 通过")

    if FAILURES:
        print(f"\n共 {len(FAILURES)} 项断言失败：")
        for item in FAILURES:
            print(f"  - {item}")
        print(f"\n失败 {len(FAILURES)} 项，未通过")
        return 1
    print(f"\n全部 {len(cases)} 项验证通过（用时 {time.time() - started:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())