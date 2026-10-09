# -*- coding: utf-8 -*-
"""E 线：外部中文名入库（core/metadata_cn.record_names）+ 本机 appinfo 兜底。

全部打桩 / 临时目录，**不发真实网络请求**、不写真实 data/。
覆盖：
    1. record_names 只补空缺、幂等、原子落盘；
    2. record_names 拒绝英文名（不让英文名冒充中文名）与空值；
    3. 全部被跳过时不落盘、不建文件；
    4. 缺键 ≠ 删除哨兵：没查过的 appid 要走本机 appinfo 兜底（E4 根因）；
    5. 负缓存（checked_at 且无中文名）依然短路 appinfo。
"""

import contextlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import appinfo  # noqa: E402
from core import metadata_cn as mc  # noqa: E402

# 这些 appid 一定不存在于本机 appinfo（appid 是 uint32）
GHOST_A, GHOST_B, GHOST_C = 1990000001, 1990000002, 1990000005


@contextlib.contextmanager
def tmp_cache(name: str = "names_cn_unit.json"):
    """把缓存指向临时文件（绝不碰真实 data/names_cn.json）。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="glnames_"))
    path = tmpdir / name
    original = mc.cache_path
    mc.cache_path = lambda: path
    mc.clear_cache()
    try:
        yield path
    finally:
        mc.clear_cache()                 # 必须先清临时文件
        mc.cache_path = original


def test_record_names_fills_and_is_idempotent():
    """只补空缺：已有中文名的条目不会被覆盖（幂等）。"""
    with tmp_cache() as path:
        stat = mc.record_names({1234: "测试游戏", "5678": "另一个游戏"}, source="unit")
        assert stat["added"] == 2 and stat["skipped"] == 0, stat
        assert mc.name_cn_for(1234) == "测试游戏"

        again = mc.record_names({1234: "改掉的名字", 9999: "新游戏"}, source="unit")
        assert again["added"] == 1 and again["skipped"] == 1, again
        assert mc.name_cn_for(1234) == "测试游戏", "已有中文名不该被覆盖"
        assert mc.name_cn_for(9999) == "新游戏"

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["version"] == 1 and isinstance(payload["items"], dict)
        assert payload["items"]["1234"]["source"] == "unit"
        assert not path.with_name(path.name + ".tmp").exists(), "不该残留 .tmp"
    print("PASS record_names：补空缺 + 幂等 + 原子落盘")


def test_record_names_rejects_english_and_blank():
    """英文名 / 空值一律不入库：不允许英文名冒充中文名。"""
    with tmp_cache() as path:
        stat = mc.record_names({GHOST_A: "Spacewar", GHOST_B: "", GHOST_C: "   ",
                                GHOST_A + 6: "《Battlefield™ Hardline》", GHOST_A + 7: None})
        assert stat["added"] == 0 and stat["skipped"] == 5, stat
        assert not path.exists(), "全部被跳过时不该落盘"
        assert mc.name_cn_for(GHOST_A) is None
    print("PASS record_names：英文名/空值不入库，全跳过时不落盘")


def test_record_names_overwrite_flag():
    """overwrite=True 时才允许覆盖（人工纠正译名用）。"""
    with tmp_cache():
        mc.record_names({42: "旧名字"})
        assert mc.record_names({42: "新名字"})["skipped"] == 1
        assert mc.name_cn_for(42) == "旧名字"
        assert mc.record_names({42: "新名字"}, overwrite=True)["added"] == 1
        assert mc.name_cn_for(42) == "新名字"
    print("PASS record_names：默认不覆盖，overwrite=True 才覆盖")


def test_name_cn_for_falls_back_to_local_appinfo():
    """缺键 ≠ 删除哨兵：没查过的 appid 也要走本机 appinfo 兜底。"""
    original = appinfo.localized_name
    appinfo.localized_name = lambda appid, *a, **k: "细胞分裂"
    try:
        with tmp_cache():
            assert mc._entry(GHOST_A) == {}, "「从没查过」应返回 {}（触发回读），不是 None"
            assert mc._disk_only_entry(GHOST_A) == (False, {})
            assert mc.name_cn_for(GHOST_A) == "细胞分裂", "应退回本机 appinfo 取中文名"
    finally:
        appinfo.localized_name = original
    print("PASS name_cn_for：缺键时退回本机 appinfo")


def test_deleted_sentinel_and_negative_cache():
    """删除哨兵仍然算「没有」；已确认无中文名的记录不再问 appinfo。"""
    original = appinfo.localized_name
    appinfo.localized_name = lambda appid, *a, **k: "不该被用到"
    try:
        with tmp_cache():
            mc._items()[str(GHOST_B)] = mc._NONE            # 显式删除
            assert mc.name_cn_for(GHOST_B) is None
            mc._items()[str(GHOST_C)] = {"appid": GHOST_C, "name_cn": "",
                                         "checked_at": "2026-01-01"}   # 负缓存
            assert mc.name_cn_for(GHOST_C) is None, "查过且确认没有 → 不该再回退"
    finally:
        appinfo.localized_name = original
    print("PASS 删除哨兵 / 负缓存语义未变")