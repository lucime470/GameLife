"""A 线冻结态回归：打包（onefile）后 appinfo 类型索引必须落在**真实** data 目录。

背景（主 agent 在真 onefile exe 里实测，非推理）::

    同一个库、同一条件 recommend --account-id 10000001 --time 30m --mood 轻松
    源码（.venv 跑 main.py）   候选 166
    dist\\game-life.exe        候选 171   <- 第 3 名推荐是「Wallpaper Engine：壁纸引擎」

``engine/recommender.py`` 曾用 ``Path(__file__).parent.parent / "data"`` 定位
``appinfo_index.json``（剔除非游戏条目的唯一依据）。onefile 打包后 ``__file__``
落在**临时解包目录** ``sys._MEIPASS``（进程退出即删），而这份索引是易变数据、
按打包约定不随 exe 分发（见 ``build/game-life.spec`` 顶部注释）—— 解包目录里
根本没有它，于是类型校验读到空表，Config / Tool /
Application 类条目**全部漏进推荐候选池**（壁纸软件被当成游戏推荐）。

H 线改写说明
------------
判定口径后来**收敛到唯一一处** :mod:`core.non_game`：``recommender`` 不再自带
黑名单，``load_app_type_index`` / ``_type_index_path`` / ``_stable_data_dir`` 这些
自有路径解析也已删除。因此本文件不再断言那几个已消失的内部符号，改为断言**同一件
真实性质**由新归属承担：

  * 唯一口径所依赖的本地索引（``core.appinfo.cache_path()``）解析到 exe 附近
    **真实**的 ``game-life/data/appinfo_index.json``，文件存在、条目数 > 1700
    （本机实测 1801），绝不落在 ``_MEIPASS``；
  * :func:`core.non_game.is_non_game` 在 frozen 下**真读得到**索引，能判出
    ``Application`` / ``Tool`` / ``Config``；
  * 端到端：``Application`` 类型条目不进候选池（修复前为 6 个候选，修复后 1 个），
    且剔除动作**确实由唯一口径完成**（换成哨兵即失效）。

手法：monkeypatch ``sys.frozen`` / ``sys.executable`` / ``sys._MEIPASS`` 伪造打包
环境，并把 ``engine/recommender.py`` 的副本放进假的 ``_MEIPASS`` 解包目录**再从
那里加载**——只有这样才能真实复现「``__file__`` 指向解包目录」这一前提。
``schema._resolve_db_path()`` 与 ``core.appinfo.cache_path()`` 都是**调用时**
读 sys 的，所以伪造立刻生效。
"""

import importlib.util
import shutil
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_ROOT = REPO_ROOT / "game-life"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from core import appinfo, non_game  # noqa: E402
from data import schema             # noqa: E402
from engine import recommender      # noqa: E402

REAL_DATA = (APP_ROOT / "data").resolve()
REAL_INDEX = REAL_DATA / "appinfo_index.json"

# 主 agent 实测被漏进候选池的 appid（本机 appinfo 索引里的类型见注释）
WALLPAPER_ENGINE = 431960      # Application  —— exe 里的第 3 名推荐
EALINK = 1182480               # Config
MIRROR_NEWS = 1856540          # Application
STEAM_NOTES = 2371090          # Config
BENCHMARK_TOOL = 3132990       # Tool
LEAKED_APPIDS = (
    WALLPAPER_ENGINE, EALINK, MIRROR_NEWS, STEAM_NOTES, BENCHMARK_TOOL,
)
REAL_GAME = 900001             # 合成条目：库里正常的游戏

NOW = int(time.time())
DAY = 86400


@pytest.fixture()
def frozen_recommender(tmp_path, monkeypatch):
    """伪造 onefile 环境，并**从 _MEIPASS 解包目录**加载一份 recommender。

    ``sys.executable`` 指向真实的 ``dist/game-life.exe``，好让
    ``data.schema.data_dir()`` 解析回仓库里的真实 ``game-life/data``
    （与 exe 实际运行时的解析结果一致）。
    """
    meipass = tmp_path / "_MEI123456"
    (meipass / "engine").mkdir(parents=True)
    shutil.copyfile(
        APP_ROOT / "engine" / "recommender.py",
        meipass / "engine" / "recommender.py",
    )
    # 解包目录里的 data/ 故意留空：appinfo_index.json 按打包约定不分发
    (meipass / "data").mkdir()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(REPO_ROOT / "dist" / "game-life.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)

    name = "engine._frozen_recommender_probe"
    spec = importlib.util.spec_from_file_location(
        name, meipass / "engine" / "recommender.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        assert str(module.__file__).startswith(str(meipass)), module.__file__
        yield module
    finally:
        sys.modules.pop(name, None)


def _leaky_library():
    """合成库：5 个非游戏条目 + 1 个正常游戏（都不带 meta type，只靠索引判定）。"""
    games = {
        appid: {"name": f"appid {appid}", "genres": ["Utilities"], "session_minutes": 30}
        for appid in LEAKED_APPIDS
    }
    games[REAL_GAME] = {
        "name": "正常游戏", "genres": ["Casual"], "session_minutes": 30,
    }
    play_stats = {
        appid: {"playtime": 120, "last_played": NOW - 2 * DAY} for appid in games
    }
    return play_stats, games


# --------------------------------------------------------------- 源码态

def test_source_mode_index_is_repo_data():
    """源码运行（默认态）：唯一口径所依赖的索引解析到真实 game-life/data。"""
    path = appinfo.cache_path().resolve()
    assert path == REAL_INDEX, path
    assert path == schema.data_dir().resolve() / "appinfo_index.json"
    assert path.is_file(), f"本机索引缺失，无法验证：{path}"
    assert len(appinfo.load_index(force=True)) > 1700
    # 唯一口径真能消费它（不是「路径对了但没人用」）
    non_game.reset_cache()
    assert non_game.is_non_game(WALLPAPER_ENGINE) is True
    assert non_game.is_non_game(REAL_GAME) is False


# --------------------------------------------------------------- 冻结态

def test_frozen_index_path_is_real_data_dir_not_meipass(frozen_recommender):
    """A 线主断言：frozen 下索引路径必须是真实 data 目录，绝不是 _MEIPASS。"""
    resolved = appinfo.cache_path().resolve()
    assert resolved == REAL_INDEX, resolved
    assert resolved == schema.data_dir().resolve() / "appinfo_index.json"
    assert resolved.is_file(), f"frozen 下索引不可读：{resolved}"
    assert "MEI" not in str(resolved), f"落进临时解包目录了：{resolved}"
    assert "_MEI123456" not in str(resolved)


def test_frozen_index_is_loaded_not_empty(frozen_recommender):
    """frozen 下索引必须真读得到（修复前这里读空 → 非游戏条目全部放行）。"""
    index = appinfo.load_index(force=True)
    assert len(index) > 1700, f"frozen 下索引只有 {len(index)} 条"
    assert index[str(WALLPAPER_ENGINE)]["type"].lower() == "application"
    assert index[str(BENCHMARK_TOOL)]["type"].lower() == "tool"
    assert index[str(EALINK)]["type"].lower() == "config"
    # 唯一口径在 frozen 下真的用上了这份索引（不是只看得到、用不到）
    non_game.reset_cache()
    assert non_game.is_non_game(WALLPAPER_ENGINE) is True
    assert non_game.is_non_game(BENCHMARK_TOOL) is True
    assert non_game.is_non_game(EALINK) is True
    assert non_game.is_non_game(REAL_GAME) is False


def test_frozen_keeps_non_games_out_of_candidate_pool(frozen_recommender):
    """端到端：壁纸引擎等非游戏条目不得进候选池（修复前 6 个候选，修复后 1 个）。"""
    play_stats, games = _leaky_library()
    result = frozen_recommender.recommend(
        play_stats, games, mood="轻松", time_left="不限", statuses={}, top_k=50,
    )
    names = {item["appid"] for item in result["recommendations"]}
    assert result["candidate_count"] == 1, (
        f"候选 {result['candidate_count']} 个（应为 1），说明索引没读到：{sorted(names)}"
    )
    assert names == {REAL_GAME}, names
    for appid in LEAKED_APPIDS:
        assert appid not in names, f"非游戏条目漏进推荐：{appid}"

    # 抗假绿：剔除动作必须**真的由唯一口径完成**。把 core.non_game.is_non_game
    # 换成「永远返回 False」的哨兵（等价于「判定失效」），候选池必须重新涨回 6 个；
    # 否则说明这个测试碰巧通过（比如索引本来就空、或别处又偷偷过滤了一遍）。
    original = non_game.is_non_game
    non_game.is_non_game = lambda appid: False
    non_game.reset_cache()
    try:
        loose = frozen_recommender.recommend(
            play_stats, games, mood="轻松", time_left="不限", statuses={}, top_k=50,
        )
    finally:
        non_game.is_non_game = original
        non_game.reset_cache()
    assert loose["candidate_count"] == len(LEAKED_APPIDS) + 1, (
        f"唯一口径失效后候选应涨回 {len(LEAKED_APPIDS) + 1} 个，"
        f"实际 {loose['candidate_count']} 个 —— 说明剔除不是它做的"
    )


def test_frozen_index_path_follows_environment_switch(frozen_recommender, monkeypatch):
    """撤销 frozen 后路径必须回到同一份真实索引（缓存不粘住上一个环境）。"""
    frozen_path = appinfo.cache_path().resolve()
    monkeypatch.undo()
    assert appinfo.cache_path().resolve() == frozen_path == REAL_INDEX


# --------------------------------------------------------------- 源码守卫

def test_no_file_relative_appinfo_index_locator():
    """守卫：判定链路上的模块不得再用 __file__ 直接拼 appinfo_index.json。

    H 线后判定只剩 ``core.non_game`` 一处，它读索引走 ``core.appinfo``，而
    ``core.appinfo`` 走 ``data.schema.data_dir()``（frozen 正确）。因此把
    「谁在定位索引」也一并守起来，避免以后有人再写一份 __file__ 相对路径。
    """
    for rel in ("engine/recommender.py", "core/non_game.py"):
        source = (APP_ROOT / rel).read_text(encoding="utf-8")
        for line in source.splitlines():
            if ("appinfo_index.json" in line and "__file__" in line
                    and not line.lstrip().startswith("#")):
                pytest.fail(f"{rel} 仍在用 __file__ 相对定位索引：{line.strip()}")
