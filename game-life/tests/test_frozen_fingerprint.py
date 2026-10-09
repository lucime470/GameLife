"""B 线回归测试：onefile 打包（冻结）态下渲染指纹必须看得见「代码版本」。

运行方式（仓库根目录）：
    .venv\\Scripts\\python.exe -m pytest game-life\\tests\\test_frozen_fingerprint.py -q

缺陷背景（主 agent 实测复现）：
    ``webapp/api.py`` 的渲染指纹靠 ``APP_ROOT = Path(__file__).parent.parent`` 枚举
    渲染代码文件（``core/render_bridge.py`` + ``core/non_game.py`` + ``core/ownership.py``
    + ``render/*.py`` + 模板 + ``engine/**``；T4 起为 28 个）。
    onefile 打包后 ``__file__`` 落在 ``sys._MEIPASS``（进程退出即删的解包临时目录），而
    ``.py`` 源码**不随包分发**（只编译进 exe 内部的 PYZ），所以冻结态只找得到随包分发的
    7 个模板文件::

        源码态   26 文件 -> af13a7e3f3e562356a241a46…（修复缺陷当时的文件数；G1 线新增
                  ``core/non_game.py`` 后为 27 文件，T4 线新增 ``core/ownership.py`` 后 28 文件）
        冻结态    7 文件 -> 3d7d69932f0ec8542c00c830…（只含 render/templates/*.html）

    最要命的后果：冻结态指纹退化成**只随模板变化**，于是「拿新引擎代码重新打包的 exe」与
    旧 exe 指纹**完全相同** -> 面板认为 output/ 里用旧代码画的产物仍然有效，继续发旧图
    -> 上一轮修好的「六个页面还是英文名」在用户升级 exe 后原样复现（触发条件从「改源码」
    变成「重新打包」）。

修复：冻结态把 **exe 自身内容** 的 sha256 混进摘要（onefile 的 exe 里同时含模板资源与
编译后的引擎/桥接代码，对「代码版本」是完备的）；源码态逐字节不变。

本文件的用例是回归闸门：一旦有人把冻结分支改回「只枚举 ``_renderer_source_files()``」，
``test_frozen_fingerprint_follows_executable_content`` 与
``test_frozen_exe_upgrade_invalidates_old_page_artifacts`` 立刻变红。

局限说明（为什么用 monkeypatch）：``APP_ROOT`` 是 import 期固化的模块常量，伪造
``sys.frozen`` 不会让它改指向，所以「包内只有模板、没有 .py」这一条只能在需要时把
``api.APP_ROOT`` 一起指到假的包内目录来复现。
"""

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_ROOT = REPO_ROOT / "game-life"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from webapp import api  # noqa: E402

# 改动前的源码态基准指纹（26 个文件时代的值，v2）。注意：engine/** 也在覆盖范围内，所以改引擎源码
# 会**合法地**改变这个值 —— 因此它只作为报告里的证据，不作为本文件的硬断言
# （硬断言用 _source_mode_fingerprint() 独立复算，对源码内容变化免疫）。
SOURCE_BASELINE_FINGERPRINT = (
    "af13a7e3f3e562356a241a46a21de8730520a91ac3817a0b68396b1fc30b98c1"
)


def _source_mode_fingerprint() -> str:
    """改动前源码态口径的**独立复算**：只哈希 ``_renderer_source_files()``，与 exe 无关。

    它不复用 ``api._compute_renderer_fingerprint()``，这样才能把「源码态口径没被改动」
    这件事断言出来（用被测函数自己和自己比是同义反复）。
    """
    digest = hashlib.sha256()
    digest.update(api._RENDERER_FINGERPRINT_VERSION.encode("utf-8") + b"\n")
    for path in api._renderer_source_files():
        rel = path.relative_to(api.APP_ROOT).as_posix()
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"\n")
    return digest.hexdigest()


EXE_BODY = b"MZ" + bytes(range(256)) * 512


def _simulate_process_restart() -> None:
    """模拟「面板进程重启」：新进程里指纹缓存本来就是空的。

    用户升级 exe 一定伴随重启（旧进程还在跑时 exe 甚至被占用无法覆盖），所以「换 exe」
    在同一个进程里必须显式模拟重启，否则会读到进程内的旧指纹。
    """
    api._RENDERER_FINGERPRINT_CACHE = None
    api._FROZEN_EXE_DIGEST_CACHE.clear()


@pytest.fixture()
def frozen_env(tmp_path, monkeypatch):
    """伪造 onefile 运行环境：sys.frozen=True + 真实存在的 sys.executable + 临时 _MEIPASS。

    ``sys.executable`` 必须指向**真实文件**（被测代码要哈希它的内容），所以这里造一个
    几千字节的假 exe；``_MEIPASS`` 里只放随包分发的模板，不放任何 ``.py``。
    """
    exe = tmp_path / "dist" / "game-life.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(EXE_BODY)

    meipass = tmp_path / "_MEI123456"
    templates = meipass / "render" / "templates"
    templates.mkdir(parents=True)
    (templates / "base.html").write_text("<html>base</html>", encoding="utf-8")
    (templates / "graveyard.html").write_text("<html>graveyard</html>", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    api._RENDERER_FINGERPRINT_CACHE = None
    api._FROZEN_EXE_DIGEST_CACHE.clear()
    try:
        yield {"exe": exe, "meipass": meipass, "templates": templates}
    finally:
        # 指纹缓存按调用时环境算；退出前把两份缓存都清空（不重算 —— 此刻 monkeypatch
        # 还没撤销，重算会把「冻结态 + 被改过的 APP_ROOT」的结果留进缓存污染别的用例）
        api._RENDERER_FINGERPRINT_CACHE = None
        api._FROZEN_EXE_DIGEST_CACHE.clear()


# ----------------------------------------------------------- 源码态：口径不许变


def test_source_mode_fingerprint_is_byte_identical_to_original_algorithm():
    """源码态（默认态）行为逐字节不变：仍枚举 28 个代码文件，且指纹 = 原算法复算值。

    28 = 桥接层 3（core/render_bridge.py + core/non_game.py + core/ownership.py，
    T4 新增 ownership：授权归属同样直接决定六页的库范围）
    + render/*.py 7 + 模板 7 + engine/*.py 11。
    """
    files = api._renderer_source_files()
    assert len(files) == 28, (
        f"源码态应是 28 个渲染代码文件（3+7+7+11），实际 {len(files)}："
        f"{[f.relative_to(api.APP_ROOT).as_posix() for f in files]}"
    )
    rel = {f.relative_to(api.APP_ROOT).as_posix() for f in files}
    for needed in (
        "core/render_bridge.py", "core/non_game.py", "core/ownership.py",
        "render/templates/base.html",
        "engine/recommender.py", "engine/display.py", "engine/stats.py",
    ):
        assert needed in rel, f"指纹漏了 {needed}"
    assert not any(n.endswith((".db", ".json", ".exe")) for n in rel), \
        f"指纹混入了易变数据 / exe（源码态不该碰 exe）：{sorted(rel)}"

    assert api._frozen_exe_path() is None, "源码态不该解析出 exe 路径"
    assert api._renderer_fingerprint(force=True) == _source_mode_fingerprint(), (
        "源码态指纹不再等于原算法独立复算值 —— 冻结态修复改动了源码态口径"
        "（若确实要升版本号/改口径，必须同步升 _RENDERER_FINGERPRINT_VERSION）"
    )
    print(f"PASS 源码态：{len(files)} 个代码文件 · 指纹 {api._renderer_fingerprint()} · 口径与原算法一致")


# ----------------------------------------------------------- 冻结态：三条核心断言


def test_frozen_fingerprint_covers_executable_not_just_templates(frozen_env):
    """冻结态指纹必须含 exe（= 代码版本），因此**不等于**「只枚举源码文件」的口径。"""
    source_style = _source_mode_fingerprint()          # 改动前的口径：与 exe 无关
    frozen_fp = api._renderer_fingerprint(force=True)

    assert api._frozen_exe_path() == frozen_env["exe"]
    assert frozen_fp != source_style, (
        "冻结态指纹与「只枚举源码文件」的口径相同 —— exe 内容没进指纹，"
        "拿新引擎代码重新打包 exe 会得到和旧 exe 一样的指纹（会继续发旧图）"
    )
    assert frozen_fp == api._renderer_fingerprint(force=True), "冻结态指纹不稳定"

    # 包内模板仍要被枚举（冻结态不该退步成「什么都看不见」）
    rel = {f.relative_to(api.APP_ROOT).as_posix() for f in api._renderer_source_files()}
    assert "render/templates/base.html" in rel
    print(f"PASS 冻结态：指纹 {frozen_fp} != 无 exe 口径 {source_style}")


def test_frozen_fingerprint_follows_executable_content(frozen_env, monkeypatch):
    """**本任务的核心断言**：冻结态指纹随 exe 内容变化而变化，内容不变则稳定。

    同时把 ``api.APP_ROOT`` 指到「包内」临时目录，复现真 onefile 的处境：
    那里只有模板、没有任何 ``.py``（源码只编进 exe 内部）。此时**只有** exe 能代表代码版本。
    """
    monkeypatch.setattr(api, "APP_ROOT", frozen_env["meipass"])
    bundled = api._renderer_source_files()
    rel = [f.relative_to(api.APP_ROOT).as_posix() for f in bundled]
    assert rel == ["render/templates/base.html", "render/templates/graveyard.html"], (
        f"模拟包内目录应只有模板（复现真 exe 的处境），实际 {rel}"
    )

    before = api._renderer_fingerprint(force=True)

    # exe 不变 -> 指纹稳定（连算多次都一样）
    assert api._renderer_fingerprint(force=True) == before
    assert api._renderer_fingerprint() == before

    # 换一份 exe 内容（= 用新引擎代码重新打包）-> 指纹必变
    frozen_env["exe"].write_bytes(b"MZ" + b"rebuilt-with-new-engine" * 200)
    after = api._renderer_fingerprint(force=True)
    assert after != before, (
        "改了 exe 内容指纹却没变 —— 冻结态看不见代码，重新打包后仍会发旧产物"
        "（「页面还是英文名」会原样复现）"
    )

    # 回到同一份内容（= 内容相同的重建）-> 指纹必须回到原值，不白重画
    frozen_env["exe"].write_bytes(EXE_BODY)
    assert api._renderer_fingerprint(force=True) == before, \
        "exe 内容没变指纹却变了 —— 会导致每次重建都白重画六个页面"
    print(f"PASS exe 内容 -> 指纹：{before[:12]}… 变 {after[:12]}… 回归 {before[:12]}…")


def test_frozen_fingerprint_does_not_degrade_to_templates_only(frozen_env, monkeypatch):
    """直接对「瞎眼」本身下断言：相同模板 + 不同 exe 必须给出不同指纹。

    这条把缺陷钉死：修复前冻结态指纹**只由模板决定**，所以模板一样 -> 指纹必然一样，
    拿它当「代码版本」用等于失效。
    """
    monkeypatch.setattr(api, "APP_ROOT", frozen_env["meipass"])
    first = api._renderer_fingerprint(force=True)

    frozen_env["exe"].write_bytes(b"MZ" + b"\x01\x02\x03\x04" * 900)
    second = api._renderer_fingerprint(force=True)

    # 模板（唯一「可见」的输入）逐字节没变，
    assert api._renderer_source_files() == api._renderer_source_files()
    assert (frozen_env["templates"] / "base.html").read_bytes() == b"<html>base</html>"
    assert first != second, "模板没变、exe 变了，指纹却没变：冻结态指纹退化成「只随模板变化」"
    print("PASS 模板不变 + exe 变 -> 指纹变（冻结态不再瞎眼）")


def test_frozen_exe_digest_is_read_once_per_process(frozen_env, monkeypatch):
    """性能：exe 15MB，每进程只允许读一遍（走缓存），不许每次调用都重读。"""
    api._FROZEN_EXE_DIGEST_CACHE.clear()
    real_open = io.open
    opened: list[tuple[str, str]] = []

    def spy(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        opened.append((str(file), str(mode)))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", spy)
    exe_path = str(frozen_env["exe"])

    def exe_reads() -> int:
        # 只数「读」：写 exe（Path.write_bytes）走的是 "wb"，不算读
        return sum(1 for path, mode in opened if path == exe_path and mode == "rb")

    api._renderer_fingerprint(force=True)
    assert exe_reads() == 1, f"首次计算没恰好读一遍 exe：{opened}"

    api._renderer_fingerprint(force=True)
    for _ in range(50):
        api._renderer_fingerprint()
    assert exe_reads() == 1, f"exe 被反复重读（{exe_reads()} 次）：缓存没生效"

    # 缓存里只留摘要串，不常驻 exe 内容
    cached = list(api._FROZEN_EXE_DIGEST_CACHE.values())
    assert cached and all(len(v) == 64 for v in cached), f"缓存里存了非摘要内容：{cached}"

    # exe 被换掉（size/mtime 变）-> 恰好重读一次
    frozen_env["exe"].write_bytes(b"MZ" + b"\xff" * 2048)
    api._renderer_fingerprint(force=True)
    assert exe_reads() == 2, f"exe 换掉后应重读一次，实际共 {exe_reads()} 次"
    print("PASS exe 每进程只读一遍；换 exe 才重读一次（缓存 key 带 size+mtime_ns）")


def test_frozen_exe_read_once_under_concurrent_first_screen(frozen_env, monkeypatch):
    """首屏并发问「过期没」时，exe 也只能被读一遍（指纹缓存 + 摘要缓存 + 同一把锁）。"""
    import threading

    api._RENDERER_FINGERPRINT_CACHE = None
    api._FROZEN_EXE_DIGEST_CACHE.clear()
    real_open = io.open
    reads: list[int] = []
    lock = threading.Lock()
    exe_path = str(frozen_env["exe"])

    def spy(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if str(file) == exe_path and str(mode) == "rb":
            with lock:
                reads.append(1)
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(io, "open", spy)
    results: list[str] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(20):
            results.append(api._renderer_fingerprint())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(reads) == 1, f"并发首屏把 exe 读了 {len(reads)} 遍（应只读一遍）"
    assert len(set(results)) == 1, f"并发下指纹不一致：{set(results)}"
    print(f"PASS 8 线程 × 20 次调用：exe 只读 {len(reads)} 遍，指纹一致")


# --------------------------------------------- 用户可见后果：升级 exe 不再发旧产物


def test_frozen_fingerprint_degrades_when_exe_is_unreadable(tmp_path, monkeypatch):
    """极端情况：exe 读不到（被占用/权限）时不许抛异常，退回「只枚举可见文件」并缓存该结果。

    面板首屏靠这个函数判过期，抛异常会让整个首屏 500。降级是可接受的（等同修复前行为），
    但必须是**可观测的**（warning）且**每进程只试一次**（不反复读盘重试）。
    """
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "missing" / "game-life.exe"))
    api._RENDERER_FINGERPRINT_CACHE = None
    api._FROZEN_EXE_DIGEST_CACHE.clear()
    try:
        fp = api._renderer_fingerprint(force=True)
        assert isinstance(fp, str) and len(fp) == 64, f"读不到 exe 时应降级而不是抛异常：{fp!r}"
        assert list(api._FROZEN_EXE_DIGEST_CACHE.values()) == [None], \
            "读失败的结论没被缓存，会每次调用都重试读盘"
        assert api._renderer_fingerprint(force=True) == fp
        print("PASS exe 读不到 -> 降级（不抛异常）+ 失败结论被缓存")
    finally:
        api._RENDERER_FINGERPRINT_CACHE = None
        api._FROZEN_EXE_DIGEST_CACHE.clear()


def test_frozen_exe_upgrade_invalidates_old_page_artifacts(frozen_env, tmp_path, monkeypatch):
    """端到端：面板产物清单必须把「旧 exe 画的产物」判过期，逼它重画。

    这正是用户看得见的那条：升级 exe（= 重新打包）后，旧的中文名之前生成的页面会被判过期
    并重画，而不是继续发旧图。
    """
    work = tmp_path / "output"
    work.mkdir()
    monkeypatch.setattr(api.cli, "_stable_output_dir", lambda: work)
    account = "10000001"

    page = work / "starmap.html"
    page.write_text("<html><body>OLD-ENGLISH-NAMES</body></html>", encoding="utf-8")
    api._remember_page_artifact("starmap", page, account)
    original_fp = api._renderer_fingerprint()

    ready = api.page_artifact_state("starmap", account)
    assert ready["file"] is not None and ready["stale"] is False, f"刚生成的产物应可直接用：{ready}"

    # 用户升级 exe：exe 内容变了（= 新引擎代码重新打包），随后面板重启
    frozen_env["exe"].write_bytes(b"MZ" + b"upgraded-exe" * 400)
    _simulate_process_restart()
    stale = api.page_artifact_state("starmap", account)
    assert stale["stale"] is True and stale["renderer_changed"] is True, \
        f"升级 exe 后旧产物没被判过期（会继续发旧图）：{stale}"
    assert stale["file"] is None, "过期产物不能直接发给用户"

    # 用**相同内容**重新打包（= 同一版代码重建 exe）-> 指纹必须回到原值，不白重画
    frozen_env["exe"].write_bytes(EXE_BODY)
    _simulate_process_restart()
    assert api._renderer_fingerprint() == original_fp, \
        "exe 内容相同、重建后指纹却变了：每次重建都会白重画六个页面"
    work.joinpath("starmap.html").write_text("<html><body>NEW</body></html>", encoding="utf-8")
    api._remember_page_artifact("starmap", work / "starmap.html", account)
    back = api.page_artifact_state("starmap", account)
    assert back["file"] is not None and back["stale"] is False, \
        f"exe 内容未变却判过期（会每次重建都白重画六页）：{back}"

    # 清单里记的应是「冻结态」指纹，而不是源码态口径
    recorded = json.loads((work / api._PAGES_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert recorded["pages"]["starmap"]["renderer"] == api._renderer_fingerprint()
    assert recorded["pages"]["starmap"]["renderer"] != _source_mode_fingerprint()
    print("PASS 升级 exe -> 旧产物判过期重画；内容相同的重建 -> 不误判")


def test_new_cases_detect_the_original_bug():
    """自检：本文件的核心断言确实能抓住「冻结态看不见代码」这个 bug。

    这里用「旧行为」= 只枚举源码文件（等价于修复前的冻结分支）复算，断言它**无法**区分
    两份不同的 exe；而修复后的 ``_renderer_fingerprint()`` 能区分。
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="gl-b-selftest-"))
    fake_exe = tmp / "game-life.exe"
    meipass = tmp / "_MEI123"
    (meipass / "render" / "templates").mkdir(parents=True)
    (meipass / "render" / "templates" / "base.html").write_text("<html/>", encoding="utf-8")

    saved = (getattr(sys, "frozen", None), sys.executable, getattr(sys, "_MEIPASS", None),
             api.APP_ROOT)
    try:
        sys.frozen = True
        sys.executable = str(fake_exe)
        sys._MEIPASS = str(meipass)
        api.APP_ROOT = meipass
        fake_exe.write_bytes(b"MZ-A")
        buggy_a, fixed_a = _source_mode_fingerprint(), api._renderer_fingerprint(force=True)
        # 第二份必须与第一份「大小不同」：_frozen_exe_digest() 的缓存键是
        # (路径, size, mtime_ns)，NTFS 上连续同尺寸写入的 mtime_ns 有相当概率完全相同
        # （实测约 37%），此时会命中第一份的缓存条目、拿到同一摘要，断言就会间歇性失败。
        # 让 size 参与区分，本用例就不再依赖文件系统时间戳的精度。
        fake_exe.write_bytes(b"MZ-BBBBB")
        buggy_b, fixed_b = _source_mode_fingerprint(), api._renderer_fingerprint(force=True)
    finally:
        if saved[0] is None:
            del sys.frozen
        else:
            sys.frozen = saved[0]
        sys.executable = saved[1]
        if saved[2] is None:
            del sys._MEIPASS
        else:
            sys._MEIPASS = saved[2]
        api.APP_ROOT = saved[3]
        api._FROZEN_EXE_DIGEST_CACHE.clear()
        api._renderer_fingerprint(force=True)

    assert buggy_a == buggy_b, (
        "自检前提不成立：旧口径本应对两份不同 exe 给出相同指纹（这正是 bug 本身）"
    )
    assert fixed_a != fixed_b, "修复后的指纹也没能区分两份 exe"
    print("PASS 自检：旧口径对不同 exe 同指纹（bug 可复现）；新口径能区分")
