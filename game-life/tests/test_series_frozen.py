"""P2 冻结态回归：打包（onefile）后族谱的权威缓存必须落在**真实** data 目录。

背景（P 线用真 onefile 探针实测，非推理）::

    frozen                 = True
    schema.data_dir()      = <repo>\\game-life\\data          <- 正确
    series_tree:SERIES_CACHE_PATH -> _MEIPASS/data/series_official.json -> exists = False

``engine/series_tree.py`` 曾用 ``Path(__file__).parent.parent / "data"`` 定位
``series_official.json``（128 个系列的权威成员目录）。onefile 打包后 ``__file__``
落在**临时解包目录** ``sys._MEIPASS``（进程退出即删）→ 打包版**读不到**权威目录，
就算联网抓到了也写进临时目录下次启动即丢 → 族谱退回覆盖不全的本机 appinfo 缓存，
复现用户反馈的「系列不全」。

本文件断言：frozen 下
  * 权威缓存 ``series_official.json``（**可写**）解析到 exe 附近的真实 data 目录；
  * 手工表 ``series_map.json``（**只读**）真实目录优先、查不到才回退随包副本；
  * 两者都不落在 ``_MEIPASS``；写进去的内容重启后仍在（路径稳定）。

手法同 ``tests/test_frozen_paths.py``：monkeypatch ``sys.frozen`` /
``sys.executable`` / ``sys._MEIPASS`` 伪造打包环境。
``schema._resolve_db_path()`` 与 ``series_tree._stable_data_dir()`` 都是**调用时**
读取 sys 的，所以伪造立刻生效。
"""

import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_ROOT = REPO_ROOT / "game-life"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from data import schema            # noqa: E402
from engine import series_tree as st  # noqa: E402

REAL_DATA = (APP_ROOT / "data").resolve()


@pytest.fixture()
def frozen_env(tmp_path, monkeypatch):
    """伪造 onefile 运行环境：dist/game-life.exe + 同级 game-life/data + 临时 _MEIPASS。"""
    repo = tmp_path / "repo"
    data_dir = repo / "game-life" / "data"
    data_dir.mkdir(parents=True)
    # schema._resolve_db_path() 靠 schema.py 这个 marker 认出真实 data 目录
    (data_dir / "schema.py").write_text("# marker\n", encoding="utf-8")
    exe = repo / "dist" / "game-life.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ fake")
    meipass = tmp_path / "_MEI123456"
    (meipass / "data").mkdir(parents=True)

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    return {"repo": repo, "data": data_dir, "exe": exe, "meipass": meipass}


# --------------------------------------------------------------- 源码态

def test_source_mode_paths_are_repo_data():
    """源码运行（默认态）：读写路径仍是真实 game-life/data，行为与以往一致。"""
    assert st.series_cache_path().resolve() == REAL_DATA / "series_official.json"
    assert st.series_map_path().resolve() == REAL_DATA / "series_map.json"
    assert st.SERIES_CACHE_PATH.resolve() == REAL_DATA / "series_official.json"
    assert st.SERIES_MAP_PATH.resolve() == REAL_DATA / "series_map.json"
    assert str(st.series_cache_path()).find("_MEI") == -1


# --------------------------------------------------------------- 冻结态

def test_frozen_cache_path_uses_real_data_dir_not_meipass(frozen_env):
    """P2 主断言：frozen 下权威缓存必须落在真实 data 目录，绝不是 _MEIPASS。"""
    resolved = st.series_cache_path().resolve()
    assert resolved == frozen_env["data"].resolve() / "series_official.json", resolved
    # 与 P 线修好的统一口径一致（同一份 data_dir()）
    assert resolved == schema.data_dir().resolve() / "series_official.json"
    assert "MEI" not in str(resolved), f"落进临时解包目录了：{resolved}"
    assert resolved != (frozen_env["meipass"] / "data" / "series_official.json").resolve()


def test_frozen_cache_roundtrip_writes_outside_meipass(frozen_env):
    """真写一次：内容落在真实 data 目录、可读回、_MEIPASS 里一点东西都不留。"""
    st._write_cache_file(None, {
        "Monster Hunter": {
            "updated": date.today().isoformat(),
            "total_count": 1,
            "members": [[582010, "Monster Hunter: World"]],
        },
    })
    target = frozen_env["data"] / "series_official.json"
    assert target.is_file(), "权威缓存没有写进真实 data 目录"
    assert st.load_series_cache() == {"Monster Hunter": [582010]}
    assert st.load_series_cache_titles()[582010] == "Monster Hunter: World"
    assert st.series_cache_is_stale() is False  # 新鲜缓存，重启不必重抓
    leaked = list((frozen_env["meipass"] / "data").iterdir())
    assert leaked == [], f"临时解包目录里出现了写入物：{leaked}"


def test_frozen_series_map_prefers_real_dir_then_bundle(frozen_env):
    """只读手工表：真实 data 目录优先（用户可自行更新），再回退 _MEIPASS 随包副本。"""
    real_map = frozen_env["data"] / "series_map.json"
    real_map.write_text('{"1": {"series": "真目录", "name": "A"}}', encoding="utf-8")
    picked = st.series_map_path().resolve()
    assert picked == real_map.resolve(), picked
    assert st.load_series_map() == {1: {"series": "真目录", "name": "A"}}

    # 真实目录里没有这张表 → 回退随包副本，仍然可用（绝不因缺表崩）
    real_map.unlink()
    bundled = frozen_env["meipass"] / "data" / "series_map.json"
    bundled.write_text('{"2": {"series": "随包", "name": "B"}}', encoding="utf-8")
    fallback = st.series_map_path()
    assert fallback.resolve() == bundled.resolve(), fallback
    assert st.load_series_map() == {2: {"series": "随包", "name": "B"}}

    # 两边都没有：返回真实目录下的路径（只读用途，缺失时上层安全降级为 {}）
    bundled.unlink()
    missing = st.series_map_path()
    assert missing.parent == frozen_env["data"].resolve()
    assert missing.name == "series_map.json"
    assert st.load_series_map() == {}
    assert st.load_official_aliases() == {}


def test_path_resolution_follows_environment_switch(frozen_env, tmp_path, monkeypatch):
    """换一个打包布局后路径必须跟着变（缓存不能粘住上一个环境）。"""
    first = st.series_cache_path().resolve()

    other_data = tmp_path / "other" / "game-life" / "data"
    other_data.mkdir(parents=True)
    (other_data / "schema.py").write_text("# marker\n", encoding="utf-8")
    other_exe = tmp_path / "other" / "dist" / "game-life.exe"
    other_exe.parent.mkdir(parents=True)
    other_exe.write_bytes(b"MZ fake")
    monkeypatch.setattr(sys, "executable", str(other_exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEI999999"), raising=False)

    second = st.series_cache_path().resolve()
    assert first != second
    assert second == other_data.resolve() / "series_official.json"
    assert "MEI" not in str(second)


def test_source_mode_after_frozen_still_repo_data(monkeypatch):
    """frozen 撤销后必须立刻回到仓库 data（缓存按环境签名失效，不残留）。"""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(REPO_ROOT / "dist" / "game-life.exe"),
                        raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(REPO_ROOT / "_MEI000000"), raising=False)
    monkeypatch.undo()
    assert st.series_cache_path().resolve() == REAL_DATA / "series_official.json"


def test_no_module_level_file_relative_cache_path():
    """守卫：模块源码里不得再用 __file__ 拼 series_official.json / series_map.json。"""
    source = (APP_ROOT / "engine" / "series_tree.py").read_text(encoding="utf-8")
    for name in ("series_official.json", "series_map.json"):
        for line in source.splitlines():
            if name in line and "__file__" in line and not line.lstrip().startswith("#"):
                pytest.fail(f"仍在用 __file__ 相对定位：{line.strip()}")