r"""并发写安全回归：缓存不能再用「旧内存快照」全量覆盖磁盘。

对应缺陷（实测复现）：
    同时跑多个 ``main.py panel`` 进程时，``data/names_cn.json`` 会被某个
    长时间运行的进程用**进程启动时的旧快照**整个覆盖写盘（典型 lost update）。
    重建到 174 条后，被一个 15:23 启动的旧面板进程在 16:09 覆盖成 5 条。

覆盖范围：
    1. metadata_cn（names_cn.json）：read-modify-write，内存记录优先、磁盘新数据不丢；
    2. name_cn_baike（name_cn_baike.json）：同上（含 lookup 落盘路径）；
    3. 落盘原子性：中断写入不产生半截 JSON / 残留临时文件；
    4. 写入失败（目录被占/只读）时不抛异常且原文件完好；
    5. 两个并发写者：后写者不会把先写者的条目抹掉；
    6. metadata_cn 公开接口的 memory / disk 双读一致性（面板读取路径）。

运行方式（game-life 目录）：
    ..\.venv\Scripts\python.exe tests\test_concurrent_cache.py

只使用 tempfile 临时目录 + 打桩，**绝不碰真实 data\ 目录**、不发网络请求。
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import metadata_cn as mc           # noqa: E402
from core import name_cn_baike as nb         # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def read_items(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("items") or {}


def write_items(path: Path, items: dict) -> None:
    path.write_text(
        json.dumps({"version": 1, "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )


def entry(name: str, *, source: str = "unit") -> dict:
    return {"name_cn": name, "name_en": f"Game {name}", "source": source,
            "checked_at": "2026-09-23"}


# ------------------------------------------------------------------ 模拟「两个进程」

def _reader_process_script(cache: Path) -> str:
    """子进程脚本：完整走一遍 load_cache（旧快照）→ 磁盘被改 → 保存。"""
    return (
        "import sys, json\n"
        f"sys.path.insert(0, r'{ROOT}')\n"
        "from core import metadata_cn as mc\n"
        f"mc.cache_path = lambda: __import__('pathlib').Path(r'{cache}')\n"
        "mc.load_cache()\n"
        "snapshot = len(mc._cache['items'])\n"
        "mc._items()['777'] = {'name_cn': '进程A写的', 'name_en': 'A', 'source': 'proc_a',"
        " 'checked_at': '2026-09-23'}\n"
        "mc._save_cache()\n"
        "print('snapshot', snapshot)\n"
    )


def case_lost_update_two_process(tmp: Path) -> None:
    """进程 A 拿旧快照保存：进程 B 后来写进磁盘的新条目不许丢。"""
    cache = tmp / "names_lost_update.json"
    original = mc.cache_path
    mc.cache_path = lambda: cache
    try:
        mc.clear_cache()
        # 进程 A 启动时的磁盘内容（N 条）
        write_items(cache, {"1": entry("最早的一条"), "2": entry("第二条")})
        # 模拟「进程启动时读盘」：clear_cache 会把 _cache 置为空 dict，
        # 此时普通 load_cache() 会命中内存快照而不重读磁盘，必须 force=True。
        mc.load_cache(force=True)             # A 读到 N=2 条快照
        check(len(mc._cache["items"]) == 2, "A 的快照应为 2 条")

        # 进程 B 后来把磁盘更新到 M 条（A 完全不知道）
        write_items(cache, {
            "1": entry("最早的一条"),
            "2": entry("第二条"),
            "3": entry("B_新条目"),
            "4": entry("B_新条目2"),
        })

        # A 用旧快照保存
        mc._items()["777"] = entry("进程A写的", source="proc_a")
        mc._save_cache()

        items = read_items(cache)
        check(len(items) == 5, f"A 保存后应有 5 条（2+2 磁盘 + 1 内存），实际 {len(items)}")
        for key, wanted in (("3", "B_新条目"), ("4", "B_新条目2"), ("777", "进程A写的")):
            check(str(items.get(key, {}).get("name_cn")) == wanted,
                  f"条目 {key} 应保留 {wanted!r}，实际 {items.get(key)!r}")
        check(set(mc._cache["items"]) == set(items),
              "保存后内存应与磁盘合并结果一致（否则下次保存又会回退）")
    finally:
        mc.clear_cache()          # 必须趁 cache_path 还指向 tmp 时清理，否则会删真实 data\ 文件
        mc.cache_path = original


def case_lost_update_subprocess(tmp: Path) -> None:
    """真·独立进程：A 进程自己 load → 保存，B 的新条目不许丢。"""
    cache = tmp / "names_proc.json"
    original = mc.cache_path
    mc.cache_path = lambda: cache
    try:
        mc.clear_cache()
        write_items(cache, {"10": entry("磁盘原有")})
        script = _reader_process_script(cache)
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        check(proc.returncode == 0, f"子进程应正常退出：{proc.stderr[-400:]}")
        items = read_items(cache)
        check(str(items.get("10", {}).get("name_cn")) == "磁盘原有", "子进程不该丢掉磁盘旧条目")
        check("777" in items, "子进程写入的新条目应落盘")
        del script
    finally:
        mc.clear_cache()          # 必须趁 cache_path 还指向 tmp 时清理，否则会删真实 data\ 文件
        mc.cache_path = original


def case_baike_lost_update(tmp: Path) -> None:
    """name_cn_baike：同一个 lost update 模式（含 lookup 的落盘路径）。"""
    cache = tmp / "baike.json"
    original = nb.cache_path
    nb.cache_path = lambda: cache
    try:
        nb.clear_cache()
        nb._MEMORY["1"] = entry("百科已有")
        nb._save()
        check(len(read_items(cache)) == 1, "百科首次保存应写 1 条")

        nb._MEMORY["2"] = entry("百科新增")
        nb._save()

        # 别的「进程」往磁盘塞了 3 / 4
        disk = read_items(cache)
        disk["3"] = entry("别的进程写的3")
        disk["4"] = entry("别的进程写的4")
        write_items(cache, disk)

        nb._MEMORY["5"] = entry("百科再新增")
        nb._save()
        items = read_items(cache)
        check(len(items) == 5, f"百科保存后应有 5 条，实际 {len(items)}：{sorted(items)}")
        check("3" in items and "4" in items, "百科不许覆盖别的进程写入的条目")

        # lookup 的落盘路径（打桩联网，不真的请求百度）
        original_q = nb._query
        nb._query = lambda key: ("塞尔达传说", "这是一款游戏")
        try:
            nb._MEMORY.pop("9", None)
            value = nb.lookup(9, "The Legend of Zelda")
        finally:
            nb._query = original_q
        check(value == "塞尔达传说", f"打桩查询应命中中文名，实际 {value!r}")
        items = read_items(cache)
        check("9" in items, "lookup 结果应落盘")
        check("3" in items and "4" in items, "lookup 落盘也不许覆盖别的进程条目")
    finally:
        nb.clear_cache()          # 同上：先清理 tmp，再恢复真实路径
        nb.cache_path = original


def case_atomic_write(tmp: Path) -> None:
    """原子落盘：写入过程中磁盘上要么是完整旧内容、要么是完整新内容，不留半截。"""
    cache = tmp / "names_atomic.json"
    original = mc.cache_path
    mc.cache_path = lambda: cache
    try:
        mc.clear_cache()
        write_items(cache, {"1": entry("旧内容")})
        mc.load_cache()
        seen_ok = True
        stop = threading.Event()

        def watcher() -> None:
            nonlocal seen_ok
            while not stop.is_set():
                if cache.is_file():
                    try:
                        data = json.loads(cache.read_text(encoding="utf-8"))
                    except ValueError:
                        seen_ok = False       # 读到半截 JSON 就是没原子落盘
                        return
                    if not isinstance(data.get("items"), dict):
                        seen_ok = False
                        return

        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        for index in range(40):
            mc._items()[f"k{index}"] = entry(f"条目{index}")
            mc._save_cache()                  # 每次走「临时文件 + os.replace」
            tmp_file = cache.with_name(cache.name + ".tmp")
            check(not tmp_file.exists(), "保存结束后不该残留 .tmp 临时文件")
        stop.set()
        thread.join(timeout=5)
        check(seen_ok, "落盘过程中磁盘上出现了半截 JSON")

        payload = json.loads(cache.read_text(encoding="utf-8"))
        check(payload.get("version") == 1, "落盘应保留 version 字段")
        check(len(payload["items"]) == 41, f"最终应 41 条，实际 {len(payload['items'])}")
    finally:
        mc.clear_cache()          # 必须趁 cache_path 还指向 tmp 时清理，否则会删真实 data\ 文件
        mc.cache_path = original


def case_interrupted_write_keeps_file(tmp: Path) -> None:
    """写到一半失败（磁盘满 / 被杀）：原文件必须完好，不能变成半截 JSON。"""
    cache = tmp / "names_interrupted.json"
    original_path = mc.cache_path
    original_replace = mc.os.replace
    mc.cache_path = lambda: cache
    try:
        mc.clear_cache()
        write_items(cache, {"1": entry("原文件内容")})
        mc.load_cache()
        before = cache.read_text(encoding="utf-8")

        def boom(src, dst):
            raise OSError("模拟 os.replace 失败")

        mc.os.replace = boom
        mc._items()["2"] = entry("这次写不进去")
        mc._save_cache()                      # 契约：绝不抛异常
        mc.os.replace = original_replace

        check(cache.read_text(encoding="utf-8") == before, "写入失败后原文件不应被改动")
        check(read_items(cache) == {"1": entry("原文件内容")}, "原文件内容应可正常解析")
        check(not cache.with_name(cache.name + ".tmp").exists(), "失败后应清掉临时文件")

        # 真正的失败路径：临时文件都写不出来（父路径不是目录）
        cache2 = tmp / "not_a_dir" / "names.json"
        (tmp / "not_a_dir").write_text("我是文件不是目录", encoding="utf-8")
        mc.cache_path = lambda: cache2
        mc._save_cache()                      # 契约：绝不抛异常
        check(read_items(cache) == {"1": entry("原文件内容")}, "别处失败也不该动到另一个文件")
    finally:
        mc.os.replace = original_replace
        mc.clear_cache()          # 先清理 tmp（此时 mc.cache_path 还指向 cache2）
        mc.cache_path = original_path


def case_concurrent_writers(tmp: Path) -> None:
    """两个写者并发保存：后写者不得把先写者的条目抹掉。"""
    cache = tmp / "names_concurrent.json"
    original = mc.cache_path
    mc.cache_path = lambda: cache
    try:
        mc.clear_cache()
        write_items(cache, {})

        barrier = threading.Barrier(2, timeout=30)

        # 每个写者用**私有快照**模拟一个独立进程：单进程内的模块全局 ``_cache``
        # 做不到「两个进程各自持有自己的内存快照」——两个线程 swap 同一个全局，
        # 会互相把对方刚写的条目从内存里挤掉，那测的是用例自己的竞态而不是
        # 落盘的 read-modify-write。允许 monkeypatch ``_cache`` 是模块的既有用法
        # （tests/test_local_meta.py 就写过 ``mc._cache = None``）。
        def writer(tag: str) -> None:
            try:
                barrier.wait()
                for index in range(25):
                    mc._cache = {"version": 1, "items": {tag: {}}}     # 快照先放本写者的键
                    mc._items()[f"{tag}_{index}"] = entry(f"{tag}-{index}", source=tag)
                    mc._save_cache()
            except Exception as exc:  # pragma: no cover
                FAILURES.append(f"写者 {tag} 抛异常：{exc!r}")

        threads = [threading.Thread(target=writer, args=(tag,)) for tag in ("A", "B")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        items = read_items(cache)
        missing = [f"A_{i}" for i in range(25) if f"A_{i}" not in items]
        missing += [f"B_{i}" for i in range(25) if f"B_{i}" not in items]
        check(not missing, f"并发写后丢失了条目：{missing[:8]}（共丢 {len(missing)}）")
    finally:
        mc.clear_cache()          # 必须趁 cache_path 还指向 tmp 时清理，否则会删真实 data\ 文件
        mc.cache_path = original


def case_public_api_and_reads(tmp: Path) -> None:
    """公开接口语义不变；只读查询能读到磁盘被别的进程刷新的结果。"""
    cache = tmp / "names_api.json"
    original = mc.cache_path
    mc.cache_path = lambda: cache
    try:
        mc.clear_cache()
        for name in ("load_cache", "name_cn_for", "ensure_names_cn", "store_details",
                     "stats", "cache_path", "clear_cache"):
            check(callable(getattr(mc, name, None)), f"metadata_cn 缺少公开接口 {name}")
        for name in ("lookup", "ensure_names", "name_cn_for", "stats"):
            check(callable(getattr(nb, name, None)), f"name_cn_baike 缺少公开接口 {name}")

        check(mc.load_cache()["items"] == {}, "clear_cache 后应为空")
        write_items(cache, {"5": entry("别的进程新写的", source="other")})
        check(mc.name_cn_for(5) == "别的进程新写的",
              "内存没命中时应回读磁盘，而不是继续返回旧快照")
        check(mc.load_cache(force=True)["items"]["5"]["source"] == "other",
              "load_cache(force=True) 应重新读盘")
        check(mc.stats()["count"] == 1, "stats 应数到磁盘上的 1 条")

        mc.clear_cache()
        check(not cache.exists(), "clear_cache 应删除磁盘文件")
        check(mc.load_cache()["items"] == {}, "clear_cache 后内存也应为空")
    finally:
        mc.clear_cache()          # 必须趁 cache_path 还指向 tmp 时清理，否则会删真实 data\ 文件
        mc.cache_path = original


def case_survives_parent_missing(tmp: Path) -> None:
    """父目录不存在时也能建目录落盘（面板第一次运行）；坏 JSON 不抛异常。"""
    cache = tmp / "deep" / "nested" / "names.json"
    original = mc.cache_path
    mc.cache_path = lambda: cache
    try:
        mc.clear_cache()
        mc._items()["1"] = entry("深层目录")
        mc._save_cache()
        check(read_items(cache) == {"1": entry("深层目录")}, "应自动创建父目录并落盘")

        cache.write_text("{ 这不是 JSON", encoding="utf-8")
        mc._cache = None
        check(mc.load_cache()["items"] == {}, "坏 JSON 应降级为空，不抛异常")
        mc._items()["2"] = entry("坏文件后继续写")
        mc._save_cache()
        check(read_items(cache) == {"2": entry("坏文件后继续写")}, "坏文件后应能恢复写入")
    finally:
        mc.clear_cache()          # 必须趁 cache_path 还指向 tmp 时清理，否则会删真实 data\ 文件
        mc.cache_path = original


def main() -> int:
    cases = [
        ("进程 A 旧快照保存 → 进程 B 条目不丢", case_lost_update_two_process),
        ("真·独立子进程复现 lost update", case_lost_update_subprocess),
        ("name_cn_baike 同类修复（含 lookup 落盘）", case_baike_lost_update),
        ("落盘原子性（无半截 JSON / 无残留 .tmp）", case_atomic_write),
        ("写入失败不抛异常且原文件完好", case_interrupted_write_keeps_file),
        ("两个并发写者不互相覆盖", case_concurrent_writers),
        ("公开接口语义 + 只读回读磁盘", case_public_api_and_reads),
        ("父目录缺失 / 坏 JSON 降级", case_survives_parent_missing),
    ]
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="glcc_") as tmpname:
        tmp = Path(tmpname)
        for index, (name, fn) in enumerate(cases, 1):
            print(f"  [{index}/{len(cases)}] {name} ...", end="", flush=True)
            before = len(FAILURES)
            try:
                fn(tmp)
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