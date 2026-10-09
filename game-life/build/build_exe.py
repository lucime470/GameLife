"""GameLife 一键打包脚本（PyInstaller）。

运行方式（仓库根目录）：
    python game-life/build/build_exe.py

行为：
    1. 检查当前解释器能否 import PyInstaller；不可用则打印
       「请先 pip install pyinstaller」并返回非零退出码，绝不联网安装；
    2. 以当前解释器执行 PyInstaller --clean --noconfirm 执行 spec；
    3. 打印产物路径（仓库根 dist/game-life.exe）。

注意：PyInstaller 的 dist/ 与 build/ 目录相对**当前工作目录**生成；本脚本以
cwd=仓库根 调用（spec 内路径均为绝对路径解析），因此 onefile 产物落在仓库根的
dist/game-life.exe，而不是 game-life/dist/。
"""

import subprocess
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1

_BUILD_DIR = Path(__file__).resolve().parent
_GAME_LIFE_DIR = _BUILD_DIR.parent
_REPO_ROOT = _GAME_LIFE_DIR.parent
SPEC_PATH = _BUILD_DIR / "game-life.spec"
# 与下方 subprocess.run(cwd=_REPO_ROOT) 保持一致：PyInstaller 相对 cwd 生成 dist/
DIST_EXE = _REPO_ROOT / "dist" / "game-life.exe"


def _pyinstaller_available() -> bool:
    """检查当前解释器是否可用 PyInstaller（与下方 -m 调用保持一致）。

    注意：不能只查 PATH 上的 pyinstaller.exe——那可能属于别的解释器，
    而本脚本是用 sys.executable -m PyInstaller 调用。
    """
    try:
        import PyInstaller  # noqa: F401
        return True
    except ImportError:
        return False


def main() -> int:
    print("== GameLife 打包 ==")
    if not _pyinstaller_available():
        print("检测不到 PyInstaller。")
        print("请先 pip install pyinstaller")
        print("（本脚本不会自动联网安装，请手动安装后重试）")
        return EXIT_ERROR

    if not SPEC_PATH.exists():
        print(f"找不到 spec 文件：{SPEC_PATH}")
        return EXIT_ERROR

    print(f"使用 spec：{SPEC_PATH}")
    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(SPEC_PATH)]
    print("执行：" + " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(_REPO_ROOT))
    if result.returncode != 0:
        print(f"打包失败：PyInstaller 退出码 {result.returncode}")
        return EXIT_ERROR

    print("打包完成。")
    if DIST_EXE.exists():
        print(f"产物：{DIST_EXE}")
    else:
        print(f"产物未在预期位置找到（预期 {DIST_EXE}），请检查 PyInstaller 输出。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
