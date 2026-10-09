"""全命令验收冒烟测试（P6 发布准备）。

运行方式（仓库根目录）：
    python game-life/tests/test_smoke.py
零额外依赖（标准库 + 既有 requirements.txt），完全离线：
    - 不访问网络、不读取/写入真实 Steam 目录、不改动 data/cache.db 既有数据；
    - 可视化命令输出到临时目录，不触碰 game-life/output/ 中既有产物。

覆盖：
    1. 6 个离线可视化命令（report/graveyard/starmap/timeline/today/checkup）
       以 --demo --no-open 生成 HTML，断言退出码 0、文件存在、>2KB、
       且包含各自标志性中文文案（文案依据：实际生成 HTML 后核对确认）；
    2. game-life --help 退出码 0，且帮助文本包含全部已接线命令名；
       说明：已接线命令名依据实际 --help 输出核对（含 backfill/challenge）；
    3. 需要账号的数据命令：显式传入不存在的 --steam-path 与 --account-id
       离线探测接线情况，结果依赖本机环境，统一标注「环境相关，跳过」，
       不导致测试失败。
    4. backfill --help 为纯离线可断言项：退出码 0，且包含
       --dry-run / --overwrite / --min-days-ago 三个参数名。
"""

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAIN = REPO_ROOT / "game-life" / "main.py"

MIN_HTML_BYTES = 2048  # > 2KB

# 6 个离线可视化命令：命令名 -> (输出文件名, 标志性中文文案)
# 文案依据：已实际运行 --demo 生成 HTML 并核对（title/h1 等）：
#   report     -> <title>Neo · 2026 年度游戏报告 · GameLife</title>  含「年度」
#   graveyard  -> <h1>游戏墓园</h1>                                   含「墓园」
#   starmap    -> <h1>游戏星图</h1>                                   含「星图」
#   timeline   -> <h1>游戏考古时间轴</h1>                             含「游戏考古时间轴」
#   today      -> <title>Neo · 时光机 · GameLife</title>              含「时光机」
#   checkup    -> <title>🏥 游戏体检报告 · Neo</title>                 含「体检」
VISUAL_COMMANDS = {
    "report": ("annual_report.html", "年度"),
    "graveyard": ("graveyard.html", "墓园"),
    "starmap": ("starmap.html", "星图"),
    "timeline": ("timeline.html", "游戏考古时间轴"),
    "today": ("timemachine.html", "时光机"),
    "checkup": ("checkup.html", "体检"),
}

# 帮助文本中应出现的已接线命令名（取自实际 --help 输出；
# backfill 现已接线，与 challenge 一并纳入断言）
EXPECTED_COMMANDS = [
    "setup", "recommend", "stats", "run", "persona",
    "report", "graveyard", "config", "writeback",
    "price", "family", "wishlist", "starmap", "timeline",
    "today", "checkup", "radar", "challenge",
    "backfill",
]

# 需要真实 Steam 账号的数据命令（setup 不支持 --account-id，其余支持）
ACCOUNT_COMMANDS = [
    "setup", "run", "stats", "recommend", "persona",
    "price", "family", "wishlist", "radar", "challenge", "backfill",
    "writeback",
]

# backfill 离线可断言的帮助文案：三个参数名必须出现在 backfill --help 中
BACKFILL_HELP_FLAGS = ["--dry-run", "--overwrite", "--min-days-ago"]


def _run(argv: list[str], timeout: int = 180):
    """在仓库根目录运行子进程并合并输出。"""
    return subprocess.run(
        argv,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


def test_visual_commands():
    """6 个离线可视化命令：退出码 0 + HTML 存在 + >2KB + 标志性文案。"""
    with tempfile.TemporaryDirectory(prefix="gl_smoke_") as tmp:
        out_dir = Path(tmp)
        for name, (out_name, marker) in VISUAL_COMMANDS.items():
            out = out_dir / out_name
            result = _run(
                [sys.executable, str(MAIN), name, "--demo", "--no-open",
                 "--output", str(out)]
            )
            assert result.returncode == 0, (
                f"[{name}] 退出码 {result.returncode}\n{result.stdout[-800:]}"
            )
            assert out.exists(), f"[{name}] 未生成 {out}"
            size = out.stat().st_size
            assert size > MIN_HTML_BYTES, (
                f"[{name}] HTML 过小：{size} 字节（要求 > {MIN_HTML_BYTES}）"
            )
            html = out.read_text(encoding="utf-8")
            assert marker in html, f"[{name}] HTML 缺少文案「{marker}」"
            print(f"PASS {name} -> {out.name}（{size} 字节，含「{marker}」）")
    print("PASS 6 个离线可视化命令均生成合格 HTML")


def test_help_lists_wired_commands():
    """--help 退出码 0，且包含全部已接线命令名。"""
    result = _run([sys.executable, str(MAIN), "--help"])
    assert result.returncode == 0, (
        f"--help 退出码 {result.returncode}\n{result.stdout[-800:]}"
    )
    help_text = result.stdout
    missing = [
        name for name in EXPECTED_COMMANDS
        if not re.search(r"(?m)^\s+" + re.escape(name) + r"\s", help_text)
    ]
    assert not missing, f"帮助文本缺少已接线命令：{missing}"
    print("PASS --help 包含全部已接线命令：" + "、".join(EXPECTED_COMMANDS))


def test_account_commands_probe():
    """账号类命令接线探测：环境相关行为一律标注「跳过」，不判定失败。"""
    with tempfile.TemporaryDirectory(prefix="gl_smoke_") as tmp:
        fake_steam = Path(tmp) / "nonexistent_steam"
        for name in ACCOUNT_COMMANDS:
            argv = [sys.executable, str(MAIN), name, "--steam-path", str(fake_steam)]
            if name != "setup":  # setup 子命令未提供 --account-id
                argv += ["--account-id", "0"]
            try:
                result = _run(argv)
            except subprocess.TimeoutExpired as exc:
                print(f"SKIP {name}（环境相关，跳过：超时 {exc}）")
                continue
            if result.returncode != 0:
                print(
                    f"SKIP {name}（环境相关，跳过：需要真实 Steam 账号，"
                    f"退出码 {result.returncode}）"
                )
            else:
                print(f"PASS {name}（无真实账号仍正常结束，退出码 0）")
            assert "Traceback" not in (result.stdout or ""), (
                f"[{name}] 出现未捕获 Traceback\n{result.stdout[-800:]}"
            )
        print("PASS 账号类命令接线探测完成（跳过项均为环境相关）")


def test_backfill_offline_semantics():
    """backfill 离线断言：--help 稳定可断言；缺环境时标注跳过不失败。"""
    # 1) --help 纯离线：退出码 0，且包含三个参数名（真实帮助文本已核对）
    result = _run([sys.executable, str(MAIN), "backfill", "--help"])
    assert result.returncode == 0, (
        f"backfill --help 退出码 {result.returncode}\n{result.stdout[-800:]}"
    )
    help_text = result.stdout or ""
    missing = [flag for flag in BACKFILL_HELP_FLAGS if flag not in help_text]
    assert not missing, f"backfill --help 缺少参数：{missing}"
    print("PASS backfill --help 含参数：" + "、".join(BACKFILL_HELP_FLAGS))

    # 2) 环境无法满足时（不存在的 Steam 路径 + account-id 0）不得失败，标注跳过
    with tempfile.TemporaryDirectory(prefix="gl_smoke_") as tmp:
        fake_steam = Path(tmp) / "nonexistent_steam"
        try:
            probe = _run([
                sys.executable, str(MAIN), "backfill",
                "--steam-path", str(fake_steam), "--account-id", "0",
            ])
        except subprocess.TimeoutExpired as exc:
            print(f"SKIP backfill（环境相关，跳过：超时 {exc}）")
            return
        assert "Traceback" not in (probe.stdout or ""), (
            f"[backfill] 出现未捕获 Traceback\n{probe.stdout[-800:]}"
        )
        if probe.returncode != 0:
            print(
                f"SKIP backfill（环境相关，跳过：需要真实 Steam 账号，"
                f"退出码 {probe.returncode}）"
            )
        else:
            print("PASS backfill（无真实账号仍正常结束，退出码 0）")
    print("PASS backfill 离线语义断言完成（跳过项为环境相关）")


def main():
    tests = [
        test_visual_commands,
        test_help_lists_wired_commands,
        test_account_commands_probe,
        test_backfill_offline_semantics,
    ]
    for test in tests:
        test()
    print(f"\n全部 {len(tests)} 组验证通过")


if __name__ == "__main__":
    main()
