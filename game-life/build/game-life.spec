# -*- mode: python ; coding: utf-8 -*-
"""GameLife 单文件 exe 打包配置（PyInstaller spec）。

用法（仓库根目录）：
    python game-life/build/build_exe.py
等价于：
    pyinstaller --clean --noconfirm game-life/build/game-life.spec

说明：
    - 入口为 game-life/main.py，输出单文件 exe：dist/game-life.exe（onefile）。
    - render 各模块运行时用 jinja2 FileSystemLoader 从 render/templates 读取
      模板文件，必须通过 datas 一并打包，否则解包后报「模板缺失」。
    - 本地面板（main.py panel 子命令）用的模板与静态资源位于
      webapp/templates 与 webapp/static，同样需要打包，否则 exe 运行 panel 时页面不可用。
    - main.py 用 importlib.import_module("render.xxx") 字符串动态导入，
      静态扫描收不到，需用 collect_submodules 补充隐藏导入。
"""

import sys
from pathlib import Path

# spec 位于 game-life/build/，由此定位仓库内各路径
_SPEC_DIR = Path(SPECPATH).resolve()
_GAME_LIFE_DIR = _SPEC_DIR.parent          # game-life/
_REPO_ROOT = _GAME_LIFE_DIR.parent         # 仓库根

# 让 game-life/ 下的包可被 collect_submodules 解析
if str(_GAME_LIFE_DIR) not in sys.path:
    sys.path.insert(0, str(_GAME_LIFE_DIR))

from PyInstaller.utils.hooks import collect_submodules  # noqa: E402

# 隐藏导入：engine/core/render 各子模块统一收集，避免动态导入漏收
hiddenimports = (
    collect_submodules("engine")
    + collect_submodules("core")
    + collect_submodules("render")
)

# 非 Python 数据文件（运行期以文件路径读取）
#
# 打包原则（P1 冻结态路径约定，务必遵守）：
#   1. **运行期读写的易变数据一律不打包**——cache.db / covers/ / fx_rates.json /
#      price_audit.json / names_cn.json / series_official.json / accounts.json
#      这些由 ``data.schema.data_dir()`` 解析到 exe 附近的真实 game-life/data，
#      打进 onefile 只会得到一份「退出即删」的临时副本。
#   2. **纯只读的内置静态表才进 datas**，作为真实 data 目录缺失时的兜底副本。
#      读取方必须先查 data_dir()，查不到再回退 sys._MEIPASS（见
#      core/store_api.py 的 _readonly_data_path）。
datas = [
    # 可视化模板：解包后位于 sys._MEIPASS/render/templates（render 各模块按
    # Path(__file__).parent / "templates" 定位，onefile 下 __file__ 指向解包目录）
    (str(_GAME_LIFE_DIR / "render" / "templates"), "render/templates"),
    # 本地面板：HTML 模板与静态资源（css/js），解包后位于
    # sys._MEIPASS/webapp/templates 与 sys._MEIPASS/webapp/static
    (str(_GAME_LIFE_DIR / "webapp" / "templates"), "webapp/templates"),
    (str(_GAME_LIFE_DIR / "webapp" / "static"), "webapp/static"),
    (str(_GAME_LIFE_DIR / "rules.json"), "."),
    # 系列手工补充表：engine.series_tree 按包相对路径读取（只读）
    (str(_GAME_LIFE_DIR / "data" / "series_map.json"), "data"),
    # 中文名人工校对补充表（store_api 兜底读取，缺了只是少一批中文名；只读）
    (str(_GAME_LIFE_DIR / "data" / "name_cn_extra.json"), "data"),
    # 类型 ID -> 中英文名权威静态表（只读；缺了会退化成「类型 29」这类占位）
    (str(_GAME_LIFE_DIR / "data" / "genre_cn.json"), "data"),
]

a = Analysis(
    [str(_GAME_LIFE_DIR / "main.py")],
    pathex=[str(_GAME_LIFE_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# onefile：binaries/datas 全部打进单个 exe
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="game-life",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # CLI 工具，保留控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)