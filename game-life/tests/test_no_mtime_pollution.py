r"""回归闸门：跑测试**不得刷新真实仓库文件的 mtime**（否则启动器会弃用刚打好的 exe）。

背景（主 agent 实测，非推理）::

    dist\\game-life.exe          mtime = 2026-09-24 18:54:04
    跑一次 pytest tests -q 之后
    engine\\stats.py             mtime = 2026-09-24 18:56:55   <- 比 exe 新
    engine\\display.py           mtime = 2026-09-24 18:56:55
    engine\\value_ranking.py     mtime = 2026-09-24 18:56:55
    engine\\persona.py           mtime = 2026-09-24 18:56:55

根因：``test_webapp_series.test_renderer_fingerprint_reacts_to_engine_changes``
为了验证「改引擎源码 -> 渲染指纹变」，会**真的改写** engine/ 下的 4 个源码文件；
虽然还原了**字节**，但写入动作把 **mtime 刷成了「现在」**。

而 ``启动面板.bat`` 的 ``:mark_newer`` 是**按 mtime** 判「源码是否比打包程序新」的::

    for /r game-life\engine %%F in (*.py *.js *.css *.html) do call :mark_newer "%%F"

于是每跑一次测试，用户的「一键启动器」就跳过刚打好的 exe、改走源码模式
（``SRC_NEWER=1``），把打包产物藏起来，也让「我明明重新打包了」变得不可信。

本文件不加任何断言豁免：它**直接调用**那个会改写真实源码的用例，然后检查
（1）相关源文件/数据文件的内容逐字节未变；（2）它们的 **mtime 纳秒级不变**。
把 ``test_webapp_series.py`` 里的 ``os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))``
还原逻辑去掉，本用例立刻变红。
"""

import hashlib
import os
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
APP_ROOT = TESTS_DIR.parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import test_webapp_series  # noqa: E402  （被测用例所在模块，必须复用同一份逻辑）


def _snapshot(path: Path) -> tuple[int, int, str] | None:
    """(mtime_ns, size, sha256)；文件不存在返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return (st.st_mtime_ns, st.st_size, hashlib.sha256(path.read_bytes()).hexdigest())


def _watched_files() -> list[Path]:
    """会被那个用例「动手脚」的真实仓库文件（外加 engine/ 全部 *.py 兜底）。"""
    files: list[Path] = []
    engine_dir = APP_ROOT / "engine"
    files.extend(sorted(p for p in engine_dir.glob("*.py") if p.is_file()))
    files.extend(APP_ROOT / rel for rel in
                 ("data/names_cn.json", "data/cache.db", "data/genres_cn.json"))
    return files


def test_running_fingerprint_probe_does_not_touch_real_file_mtimes():
    """跑「改引擎源码」用例后：真实文件的**内容和 mtime 都必须原样**。"""
    watched = _watched_files()
    covers = APP_ROOT / "output" / "covers"
    before_files = {p: _snapshot(p) for p in watched}
    before_covers = _snapshot_mtime(covers)
    assert before_files, "engine/ 下没找到任何 *.py —— 目录结构变了？"

    # 直接复用被测用例（同一份逻辑，改它就等于改被测行为）
    test_webapp_series.test_renderer_fingerprint_reacts_to_engine_changes()

    changed_content = [
        p for p in watched
        if before_files[p] is not None and _snapshot(p) is not None
        and before_files[p][2] != _snapshot(p)[2]
    ]
    assert not changed_content, (
        "跑测试把真实源码/数据文件的内容改了（连字节都没还原）："
        f"{[str(p.relative_to(APP_ROOT)) for p in changed_content]}"
    )

    changed_mtime = [
        p for p in watched
        if before_files[p] is not None and _snapshot(p) is not None
        and before_files[p][0] != _snapshot(p)[0]
    ]
    detail = ", ".join(
        f"{p.name}: {before_files[p][0]} -> {_snapshot(p)[0]}" for p in changed_mtime
    )
    assert not changed_mtime, (
        "跑测试刷新了真实仓库文件的 mtime（内容虽还原，但 启动面板.bat 按 mtime 判新旧 "
        "-> SRC_NEWER=1 -> 弃用刚打好的 exe）。受影响：" + detail
    )

    if before_covers is not None:
        assert _snapshot_mtime(covers) == before_covers, (
            "跑测试刷新了 output/covers 目录的 mtime"
        )

    print(f"PASS 时间戳卫生：跑完『改引擎源码』用例后，{len(watched)} 个真实文件"
          "（engine/*.py + data/ 数据）内容与 mtime 均未变")


def test_probe_restores_mtime_to_nanosecond_precision():
    """更严格的一层：直接照抄探测写法，断言 mtime 纳秒级可还原（证明 ns 精度无损）。"""
    target = APP_ROOT / "engine" / "stats.py"
    if not target.is_file():
        pytest.skip("engine/stats.py 不存在（目录结构变了？）")

    before = target.stat()
    original = target.read_bytes()
    try:
        # 照抄被测用例的写法：写探测字节 -> 逐字节还原内容 -> 用 ns 还原 mtime
        target.write_bytes(original + b"\n# mtime probe\n")
        target.write_bytes(original)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = target.stat()
        assert after.st_mtime_ns == before.st_mtime_ns, (
            f"ns 精度还原失败：{before.st_mtime_ns} -> {after.st_mtime_ns}"
        )
        assert target.read_bytes() == original, "内容还原失败"
    finally:
        # 兜底：无论断言怎么炸，都别把探测字节留在真实源码里
        if target.read_bytes() != original:
            target.write_bytes(original)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))


def _snapshot_mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None
