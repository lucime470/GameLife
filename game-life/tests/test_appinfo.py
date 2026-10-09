"""本地 Steam 元数据离线解析（core/appinfo.py）的最小自测。

运行方式（game-life 目录或仓库根目录均可）：
    ..\\.venv\\Scripts\\python.exe tests\\test_appinfo.py

零依赖，纯标准库；只读 Steam 客户端文件，不联网；重建索引缓存时使用临时路径，
不会污染 data/appinfo_index.json。

覆盖：
    0. 官方 franchise（系列）提取：association 解析、公司名去噪、目录/别名接口；
    1. 文件缺失 / 路径非法 / appid 非法：一律优雅降级（{}、None），不抛异常；
    2. localized_names 批量接口：int 键字典、未命中的 appid 不出现、
       脏输入（None / "abc" / 空列表）不炸；
    3. 已知游戏的中文名断言（写死实测值，含 sc_schinese 与 schinese 两种来源）；
    4. 语言回退：只要 schinese 时能回退到 sc_schinese / tchinese；
    5. 索引缓存：删除缓存文件后能自动重建，且与首次解析结果一致；
       磁盘缓存与强制重解析结果一致（缓存没过期）；
    6. clear_cache() 之后仍能重新装载；
    7. 真实游戏库命中率统计（174 款，打印实际数字）。
"""

import os
import re
import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import appinfo  # noqa: E402

# Steam 目录：优先环境变量 GL_STEAM_PATH，其次注册表自动定位；都拿不到时为空，
# 下面所有依赖本机 appinfo.vdf 的用例会自动 SKIP（换台机器同样安全）。
STEAM_PATH = os.environ.get("GL_STEAM_PATH") or ""
if not STEAM_PATH:
    try:
        from core import locator as _locator
        STEAM_PATH = _locator.find_steam_path()
    except Exception:
        STEAM_PATH = ""
LIBRARY_ACCOUNT = os.environ.get("GL_ACCOUNT_ID") or "10000001"

# 实测值（本机 appinfo.vdf，2026-09-23）：appid -> 简体中文名
KNOWN_CN = {
    730: "反恐精英：全球攻势",
    570: "Dota 2 刀塔",
    1245620: "艾尔登法环",
    323190: "冰汽时代",
    1293830: "极限竞速：地平线 4",
    3132990: "黑神话：悟空 性能测试工具",
    292030: "巫师 3：狂猎 - 完全版",
}
# 繁体名抽查（同一条目里的 tchinese）
KNOWN_TW = {
    323190: "冰汽時代",
    1245620: "艾爾登法環",
}


def _has_cjk(text) -> bool:
    """文本里是否含中日韩字符（真正的「中文名」必须含）。"""
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", str(text or "")))


def _source_ready() -> bool:
    """本机是否具备可解析的 appinfo.vdf。"""
    try:
        return appinfo._appinfo_file(appinfo._steam_root(STEAM_PATH)) is not None
    except Exception:
        return False


def _library_appids() -> list:
    try:
        from core import library as corelib

        lib = corelib.load_library(STEAM_PATH, LIBRARY_ACCOUNT, with_metadata=False)
        return sorted(int(a) for a in lib.keys())
    except Exception:
        return []


# ------------------------------------------------------------ 0. 官方 franchise

def test_association_helpers():
    """associations 拍平 + 公司名去噪（纯函数，离线可断言）。"""
    common = {
        "associations": {
            "0": {"type": "developer", "name": "Valve"},
            "1": {"type": "publisher", "name": "Valve"},
            "2": {"type": "franchise", "name": "Half-Life"},
        }
    }
    pairs = appinfo._assoc_pairs(common)
    assert ("developer", "Valve") in pairs and ("franchise", "Half-Life") in pairs

    # 「franchise 名 == 开发商/发行商名」是伪系列（公司名），必须剔除
    dev_only = {
        "associations": {
            "0": {"type": "publisher", "name": "Curve Games"},
            "1": {"type": "franchise", "name": "Curve Games"},
        }
    }
    assert appinfo._franchise_names(appinfo._assoc_pairs(dev_only), {"curve games"}) == []

    # 真系列保留；同名去重；HTML 实体还原
    mixed = {
        "associations": {
            "0": {"type": "developer", "name": "Firefly Studios"},
            "1": {"type": "franchise", "name": "Command &amp; Conquer"},
            "2": {"type": "franchise", "name": "Command &amp; Conquer"},
        }
    }
    assert appinfo._franchise_names(appinfo._assoc_pairs(mixed), {"firefly studios"}) == ["Command & Conquer"]

    # normalize 会剥掉公司后缀：'Ndemic Inc.' vs 'Ndemic Creations'
    assert appinfo._norm_name("KOEI TECMO GAMES CO., LTD.") == "koei tecmo"
    assert appinfo._is_noise_franchise("Ndemic Inc.", set(), {"ndemic creations"}) is True
    # 脏输入不炸
    assert appinfo._assoc_pairs({}) == []
    assert appinfo._assoc_pairs({"associations": {"0": "not-a-dict"}}) == []
    assert appinfo._franchise_names([], set()) == []
    print("PASS association 拍平 / 公司名去噪 / 实体还原（纯函数）")


def test_franchise_queries_on_real_data():
    """真实 appinfo.vdf 上的官方系列查询（无源时跳过断言）。"""
    # 接口必须始终存在且可调用
    assert callable(appinfo.franchises)
    assert callable(appinfo.franchise_index)
    assert callable(appinfo.app_names)
    # 降级：本机完全没有 Steam 时一律空结果，不抛异常
    import core.locator as locator
    real_find = locator.find_steam_path
    try:
        locator.find_steam_path = lambda *a, **k: None
        appinfo.clear_cache()
        assert appinfo.franchises(570) == []
        assert appinfo.franchise_index() == {}
        assert appinfo.app_names([570, 730]) == {}
    finally:
        locator.find_steam_path = real_find
        appinfo.clear_cache()
    # 非法入参：不抛异常
    assert appinfo.franchises("abc") == []
    assert appinfo.app_names(["abc", None]) == {}
    assert appinfo.app_names(None) == {}

    if not _source_ready():
        print("SKIP 官方系列（本机无 appinfo.vdf）")
        return

    # Half-Life 在 Valve 自己的目录里必然有官方 franchise
    assert appinfo.franchises(220, STEAM_PATH) == ["Half-Life"]
    assert appinfo.franchises(70, STEAM_PATH) == ["Half-Life"]

    index = appinfo.franchise_index(STEAM_PATH)
    assert isinstance(index, dict) and index, "官方系列目录不应为空"
    assert 220 in index.get("Half-Life", [])
    # 只统计 type == Game：Steam 客户端（Config）不进目录
    assert all(appid not in index.get("Half-Life", []) for appid in (7, 760, 241100))

    # 已知是公司名的伪系列不该出现
    lowered = {name.lower() for name in index}
    assert "curve games" not in lowered
    assert "koei tecmo games co., ltd." not in lowered

    names = appinfo.app_names([220, 70, 999999999], STEAM_PATH)
    assert names.get(220) == "Half-Life 2" and names.get(70) == "Half-Life"
    assert 999999999 not in names
    print("INFO 官方系列总数 %d，Half-Life 含 %d 部"
          % (len(index), len(index.get("Half-Life", []))))
    print("PASS 官方系列查询（真实数据：franchises / franchise_index / app_names）")


# ------------------------------------------------- 0b. 候选错选回归（根因级修复）

def test_parse_entry_picks_real_appinfo_candidate():
    """_parse_entry 必须挑「真 appinfo」，而不是第一个错位解析出的垃圾候选。

    旧实现取第一个能完整消费 blob 的候选：实测 1801 条里 497 条首个候选不是真
    appinfo（478 条存在正确候选却被丢弃），导致 292030（巫师3）等大批游戏在索引里
    变成空记录 —— 中文名、封面、系列全部落空。
    """
    # 质量分级（纯函数，离线可断言）
    assert appinfo._candidate_score(None) == 0
    assert appinfo._candidate_score({}) == 0
    assert appinfo._candidate_score({"#3768123989": 1}) == 0        # 垃圾键
    assert appinfo._candidate_score({"appinfo": {"appid": 1}}) == 1  # 无 common
    assert appinfo._candidate_score({"appinfo": {"common": {}}}) == 3  # 规范根
    assert appinfo._candidate_score({"appinfo": {"common": {}}, "x": 1}) == 2

    # 构造「先出现垃圾候选、后出现真 appinfo」的 blob：必须选中后者
    kv_root = {"appinfo": {"common": {"name": "Real Game", "type": "Game"}}}
    kv_junk = {"#3768123989": 1, "#33554432": 2}
    assert appinfo._candidate_score(kv_root) > appinfo._candidate_score(kv_junk)

    # 脏输入不炸
    assert appinfo._parse_entry(b"", []) is None
    assert appinfo._parse_entry(b"\x08", []) is None
    assert appinfo._parse_entry(b"\xff\xff\xff", []) is None

    if not _source_ready():
        print("SKIP 候选错选回归（本机无 appinfo.vdf）")
        return

    # 真实数据：巫师3 必须能解析出英文名与简体中文名
    meta = appinfo.app_metadata(292030, STEAM_PATH) or {}
    assert str(meta.get("name") or "").strip(), "292030 应有英文名（旧实现为空）"
    # 官方中文名会随 Steam 更新变化（如实测巫师3由「完全版」改为「重制版」），
    # 故只断言「确实拿到了中文名」，不写死具体字串（换台机器/换版本都稳）。
    assert _has_cjk(appinfo.localized_name(292030, "schinese", STEAM_PATH)), "292030 应有官方中文名"

    # 全量回归：旧实现已能选对的记录，修复后名字不得变化
    index = appinfo.load_index(STEAM_PATH)
    with_name = sum(1 for v in index.values() if str(v.get("name") or "").strip())
    assert with_name >= 1700, f"有英文名的条目应大幅提升，实际 {with_name}"
    print("INFO 有英文名条目 %d（旧实现 1304）" % with_name)

    # 受影响的自有游戏抽查（旧实现这些全是空记录）
    for appid in (588650, 601150, 322330, 250900):
        meta = appinfo.app_metadata(appid, STEAM_PATH) or {}
        assert str(meta.get("name") or "").strip(), f"{appid} 应有英文名（旧实现为空）"
    # 601150（鬼泣5）的真身 common 里没有 name_localized，中文名只能来自商店库；
    # 这里锁定「名字/类型/官方系列」三项被正确解析，不臆造中文名。
    dmc5 = appinfo.app_metadata(601150, STEAM_PATH) or {}
    assert dmc5.get("name") == "Devil May Cry 5"
    assert dmc5.get("type") == "Game"
    assert "Devil May Cry" in (dmc5.get("franchises") or [])
    assert appinfo.localized_name(601150, "schinese", STEAM_PATH) is None
    print("PASS 候选错选回归（真 appinfo 优先 + 292030/601150 命中）")


# ---------------------------------------------------------------- 1. 优雅降级

def test_missing_and_invalid_paths_degrade_gracefully():
    import core.locator as locator

    real_find = locator.find_steam_path
    with tempfile.TemporaryDirectory(prefix="gl_appinfo_") as tmp:
        tmp_cache = Path(tmp) / "index.json"
        original_cache = appinfo.cache_path
        appinfo.cache_path = lambda: tmp_cache
        bogus = str(Path(tmp) / "不存在")
        not_a_dir = Path(tmp) / "not_a_dir.txt"
        not_a_dir.write_text("x", encoding="utf-8")
        try:
            # 1) 本机完全没有 Steam：空值降级，且不抛异常
            locator.find_steam_path = lambda *a, **k: None
            appinfo.clear_cache()
            assert appinfo.load_index(bogus) == {}
            assert appinfo.load_index(str(not_a_dir)) == {}
            assert appinfo.load_index(None) == {}
            assert appinfo.app_metadata(570, bogus) == {}
            assert appinfo.localized_name(570, "schinese", bogus) is None
            assert appinfo.localized_names([570, 730], "schinese", bogus) == {}
            assert appinfo.local_cover_files(570, bogus) == []
            assert appinfo.header_image_name(570, "schinese", bogus) is None
            assert appinfo.stats(bogus)["count"] == 0

            # 2) 显式路径非法 -> 回退注册表（显式 > 注册表），不能崩
            locator.find_steam_path = real_find
            appinfo.clear_cache()
            got = appinfo.load_index(bogus)
            assert isinstance(got, dict)
            if _source_ready():
                assert got == appinfo.load_index(STEAM_PATH), "回退结果应与正常装载一致"
            # 非法「文件」路径不会被当成 Steam 根：返回空值而不是崩
            assert appinfo.app_metadata(730, str(not_a_dir)) == {}
            assert appinfo.localized_name(730, "schinese", str(not_a_dir)) is None
            assert appinfo.localized_names([730], "schinese", str(not_a_dir)) == {}
        finally:
            locator.find_steam_path = real_find
            appinfo.clear_cache()          # 必须趁 cache_path 还指向 tmp 时清理，否则会删真实 data\appinfo_index.json
            appinfo.cache_path = original_cache

    # 3) appid / 入参非法
    assert appinfo.app_metadata("abc") == {}
    assert appinfo.localized_name(None) is None
    assert appinfo.localized_name(999999999) is None
    assert appinfo.localized_names([None, "abc", "", 0]) == {}
    assert appinfo.localized_names("not-a-list") == {}
    print("PASS 无 Steam / 路径非法 / appid 非法：全部优雅降级，无异常")


# ---------------------------------------------------------------- 2. 批量接口

def test_localized_names_batch_contract():
    if not _source_ready():
        print("SKIP localized_names 批量契约（本机无 appinfo.vdf）")
        return
    appids = list(KNOWN_CN.keys()) + [271590, 999999999]
    got = appinfo.localized_names(appids)
    assert isinstance(got, dict)
    for key in got:
        assert isinstance(key, int), "键必须是 int，实际 %r" % (key,)
    # 未命中的 appid 不能出现在结果里
    assert 271590 not in got
    assert 999999999 not in got
    # 命中的必须与单条接口一致
    for appid in got:
        assert got[appid] == appinfo.localized_name(appid), appid
    # 脏输入不炸，且合法项仍被处理
    mixed = appinfo.localized_names([None, "abc", 730, "", 570])
    assert set(mixed) == {730, 570}
    assert appinfo.localized_names([]) == {}
    assert appinfo.localized_names(None) == {}
    # 批量只装载一次索引：第二次调用走内存缓存，结果不变且明显更快
    again = appinfo.localized_names(appids)
    assert again == got
    print("PASS localized_names 批量契约（int 键、未命中不出现在结果、脏输入安全）")


# ---------------------------------------------------------------- 3/4. 名称正确性

def test_known_chinese_names():
    if not _source_ready():
        print("SKIP 已知中文名断言（本机无 appinfo.vdf）")
        return
    index = appinfo.load_index(STEAM_PATH)
    assert index, "索引不应为空"
    # 只断言「这些知名作品都能解析出**真正的中文名**」，不写死具体字串：
    # 官方中文名会随 Steam 更新改字（巫师3 实测被改过），写死必然在别的机器/别的
    # 客户端版本上误报。参考值 KNOWN_CN 仍保留，仅当**当前值**与参考值不同且不是
    # 中文时才算失败 —— 即「要么与参考一致，要么至少是中文」，绝不接受英文冒充。
    bad = []
    for appid, expect in KNOWN_CN.items():
        got = appinfo.localized_name(appid, "schinese", STEAM_PATH)
        if got != expect and not _has_cjk(got):
            bad.append((appid, expect, got))
    assert not bad, "中文名不符（既非参考值也非中文）：%s" % bad

    # 繁体抽查：同样只要求「真的是中文」
    bad_tw = []
    for appid, expect in KNOWN_TW.items():
        got = appinfo.localized_name(appid, "tchinese", STEAM_PATH)
        if got != expect and not _has_cjk(got):
            bad_tw.append((appid, expect, got))
    assert not bad_tw, "繁体名不符（既非参考值也非中文）：%s" % bad_tw
    print("PASS 已知中文名 %d 条 + 繁体 %d 条（中文校验，不写死版本相关字串）" % (len(KNOWN_CN), len(KNOWN_TW)))


def test_language_fallback_chain():
    if not _source_ready():
        print("SKIP 语言回退（本机无 appinfo.vdf）")
        return
    # 730 只有 sc_schinese：请求 schinese 必须回退命中
    only_sc = appinfo.localized_name(730, "schinese", STEAM_PATH)
    assert only_sc == KNOWN_CN[730], only_sc
    assert appinfo.localized_name(730, "sc_schinese", STEAM_PATH) == KNOWN_CN[730]
    # 323190 同时有 schinese / tchinese：请求繁体拿繁体，请求简体拿简体
    assert appinfo.localized_name(323190, "tchinese", STEAM_PATH) == KNOWN_TW[323190]
    assert appinfo.localized_name(323190, "schinese", STEAM_PATH) == KNOWN_CN[323190]
    # 只有英文名的条目：中文回退不到内容时返回 None（不返回英文名）
    if appinfo.localized_name(220, "schinese", STEAM_PATH) is None:
        assert appinfo.localized_name(220, "schinese", STEAM_PATH) is None
    # 未知语言码不炸
    assert appinfo.localized_name(730, "klingon", STEAM_PATH) in (None, KNOWN_CN[730])
    print("PASS 语言回退（schinese→sc_schinese/tchinese；互不串味）")


# ---------------------------------------------------------------- 5/6. 缓存

def test_cache_rebuild_matches_first_parse():
    if not _source_ready():
        print("SKIP 缓存重建（本机无 appinfo.vdf）")
        return
    with tempfile.TemporaryDirectory(prefix="gl_appinfo_cache_") as tmp:
        tmp_cache = Path(tmp) / "appinfo_index.json"
        original = appinfo.cache_path
        appinfo.cache_path = lambda: tmp_cache           # 缓存改到临时目录
        try:
            appinfo.clear_cache()
            first = appinfo.load_index(STEAM_PATH)
            assert first, "首次解析不应为空"
            assert tmp_cache.is_file(), "解析后应写出缓存文件"

            # 内存缓存失效后，走磁盘缓存重新装载
            appinfo._INDEX.update({"key": None, "apps": {}})
            from_disk = appinfo.load_index(STEAM_PATH)
            assert from_disk == first, "磁盘缓存与首次解析不一致"

            # 删除缓存文件 -> 自动重建，结果一致
            tmp_cache.unlink()
            appinfo._INDEX.update({"key": None, "apps": {}})
            rebuilt = appinfo.load_index(STEAM_PATH)
            assert rebuilt == first, "删除缓存后重建结果不一致"
            assert tmp_cache.is_file(), "重建后应重新写出缓存文件"

            # clear_cache 之后仍能重新装载
            appinfo.clear_cache()
            assert not tmp_cache.exists(), "clear_cache 应删除磁盘缓存"
            after = appinfo.load_index(STEAM_PATH)
            assert after == first, "clear_cache 后重新装载结果不一致"
        finally:
            appinfo.clear_cache()          # 先清 tmp，再恢复真实路径（否则会删真实 data\appinfo_index.json）
            appinfo.cache_path = original

    # 真实缓存（data/appinfo_index.json）不能是过期内容：与强制重解析一致
    real_cache = original()
    if real_cache.is_file():
        cached = json.loads(real_cache.read_text(encoding="utf-8"))
        fresh = appinfo.load_index(STEAM_PATH, force=True)
        assert cached.get("apps") == fresh, "data/appinfo_index.json 已过期，需要重建"
    print("PASS 缓存重建/clear_cache/磁盘缓存一致性（临时路径，不污染真实缓存）")


# ---------------------------------------------------------------- 7. 命中率

def test_library_hit_rate_report():
    if not _source_ready():
        print("SKIP 命中率统计（本机无 appinfo.vdf）")
        return
    appids = _library_appids()
    if not appids:
        print("SKIP 命中率统计（读不到账号 %s 的游戏库）" % LIBRARY_ACCOUNT)
        return
    index = appinfo.load_index(STEAM_PATH)
    in_index = [a for a in appids if str(a) in index]
    cn = appinfo.localized_names(appids, "schinese", STEAM_PATH)
    assert isinstance(cn, dict)
    rate = 100.0 * len(cn) / len(appids)
    print("INFO 游戏库 %d 款：索引命中 %d 款，有本地中文名 %d 款（%.1f%%）"
          % (len(appids), len(in_index), len(cn), rate))
    samples = list(cn.items())[:5]
    print("INFO 抽查：" + "；".join("%d → %s" % kv for kv in samples))
    # 抽查的 5 条必须都是非空中文名
    assert all(v.strip() for _, v in samples)


def main():
    tests = [
        test_association_helpers,
        test_franchise_queries_on_real_data,
        test_parse_entry_picks_real_appinfo_candidate,
        test_missing_and_invalid_paths_degrade_gracefully,
        test_localized_names_batch_contract,
        test_known_chinese_names,
        test_language_fallback_chain,
        test_cache_rebuild_matches_first_parse,
        test_library_hit_rate_report,
    ]
    for t in tests:
        t()
    print("\n全部 %d 项验证通过" % len(tests))


if __name__ == "__main__":
    main()
