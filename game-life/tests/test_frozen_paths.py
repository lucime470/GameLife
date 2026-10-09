"""P1 冻结态路径测试：onefile 打包后「运行时读写的数据文件」必须落在真实 data 目录。

运行方式（仓库根目录）：
    .venv\\Scripts\\python.exe -m pytest game-life\\tests\\test_frozen_paths.py -q

背景（P1 诊断实测，在真实 onefile exe 里打印的结果）::

    frozen                 = True
    sys._MEIPASS           = C:\\Users\\...\\Temp\\_MEI00003f2c2
    store_api.APP_ROOT     = %TEMP%\\_MEI00003f2c2   <- Path(__file__).parent.parent
    name_cn_baike.APP_ROOT = %TEMP%\\_MEI00003f2c2
    schema.data_dir()      = <repo>\\game-life\\data  <- 正确（exe 附近真实目录）
    metadata_cn.cache_path = <repo>\\game-life\\data\\names_cn.json
    artwork covers dir     = <repo>\\game-life\\data\\covers

``sys._MEIPASS`` 是解包临时目录，**进程退出即删**：写进去的汇率缓存、价格审计、
百科缓存、账号名单、写回状态全部随进程消失，读也读不到。
因此本文件断言：运行时读写的数据文件一律由 ``data.schema.data_dir()`` 解析，
绝不来自 ``__file__`` / ``_MEIPASS``。

手法：用 monkeypatch 伪造 frozen 环境（sys.frozen / sys.executable / sys._MEIPASS
指向临时目录），再看各模块解析出的路径落在哪。``schema._resolve_db_path()``
是**调用时**读取 sys 的，所以伪造立刻生效。
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_ROOT = REPO_ROOT / "game-life"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import main as cli  # noqa: E402
from core import artwork, name_cn_baike, metadata_cn, store_api  # noqa: E402
from data import schema  # noqa: E402
from webapp import api  # noqa: E402

SPEC_PATH = APP_ROOT / "build" / "game-life.spec"
LAUNCHER_PATH = REPO_ROOT / "启动面板.bat"

# core/store_api.py、core/name_cn_baike.py 现已改走 data_dir()
# （stable_data_dir / readonly_data_path / price_audit_path）。
# 这些用例是无条件执行的回归守卫：一旦有人把运行时数据路径改回 __file__/_MEIPASS，
# 它们会立刻变红。


@pytest.fixture()
def frozen_env(tmp_path, monkeypatch):
    """伪造 onefile 运行环境：dist/game-life.exe + 同级 game-life/data + 临时 _MEIPASS。"""
    repo = tmp_path / "repo"
    data_dir = repo / "game-life" / "data"
    data_dir.mkdir(parents=True)
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


# ----------------------------------------------------------- schema 路径解析


def test_frozen_data_dir_is_real_dir_not_meipass(frozen_env):
    """frozen 下 data_dir() 必须是 exe 旁的 game-life/data，而不是 _MEIPASS。"""
    resolved = schema.data_dir().resolve()
    assert resolved == frozen_env["data"].resolve(), resolved
    assert "MEI" not in str(resolved), f"落进临时解包目录了：{resolved}"
    assert resolved == frozen_env["data"], "data_dir() 必须等于 exe 旁的 data/"


def test_source_mode_db_path_is_repo_data():
    """源码运行：DB_PATH 指向真实 game-life/data/cache.db。"""
    assert Path(schema.DB_PATH).resolve() == (APP_ROOT / "data" / "cache.db").resolve()


def test_frozen_data_dir_falls_back_next_to_exe(tmp_path, monkeypatch):
    """没有仓库布局时退回 exe 同级 data/（首次运行自动建库），仍不是 _MEIPASS。"""
    exe = tmp_path / "standalone" / "game-life.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "_MEI999"), raising=False)
    resolved = schema.data_dir().resolve()
    assert resolved == (exe.parent / "data").resolve(), resolved
    assert "_MEI999" not in str(resolved)


def test_never_reads_from_meipass_when_repo_data_exists(frozen_env):
    """即使 _MEIPASS 里也有 data/ 目录，也必须优先用真实 data 目录。"""
    (frozen_env["meipass"] / "data" / "cache.db").write_bytes(b"decoy")
    resolved = schema.data_dir().resolve()
    assert resolved == frozen_env["data"].resolve()
    assert "MEI" not in str(resolved)


# ------------------------------------------------- 各模块的运行时数据路径


def test_runtime_data_paths_stay_out_of_meipass(frozen_env):
    """store_api / name_cn_baike 的运行时读写路径必须等于 data_dir()。"""
    want = schema.data_dir().resolve()
    got = {
        "store_api.fx_cache_path": store_api.fx_cache_path(),
        "name_cn_baike.cache_path": name_cn_baike.cache_path(),
    }
    for label, path in got.items():
        parent = path.resolve().parent
        assert parent == want, f"{label} 落在 {parent}，应为 {want}"
        assert "MEI" not in str(parent), f"{label} 落进临时解包目录：{parent}"


def test_price_audit_path_is_stable(frozen_env):
    """价格审计是**写入**文件，绝不能写进退出即删的 _MEIPASS。"""
    path = store_api.price_audit_path().resolve()
    assert path.parent == schema.data_dir().resolve(), path
    assert path.name == "price_audit.json"
    assert "MEI" not in str(path)


def test_readonly_table_prefers_real_dir_then_bundle(frozen_env):
    """只读静态表：先查真实 data 目录，查不到再回退打包进 _MEIPASS 的副本。"""
    real = store_api.readonly_data_path("genre_cn.json")
    assert real.is_file(), f"真实 data 目录里应有 genre_cn.json：{real}"
    assert real.resolve().parent == schema.data_dir().resolve()

    fallback = store_api.readonly_data_path("__definitely_missing__.json")
    assert fallback.name == "__definitely_missing__.json"
    assert fallback.is_absolute()


def test_already_stable_modules_follow_frozen_resolution(frozen_env):
    """metadata_cn / artwork 已经走 data_dir()，必须跟着 frozen 解析走（回归守卫）。"""
    want = schema.data_dir().resolve()
    assert metadata_cn.cache_path().resolve().parent == want
    assert metadata_cn.cache_path().name == "names_cn.json"

    # artwork 在 import 期缓存 DB_PATH.parent（真实运行中 frozen 在 import 前就已为真，
    # 所以同样落在真实 data 目录）；测试里只能与 import 期的 DB_PATH 常量对齐。
    assert artwork._stable_data_dir().resolve() == Path(schema.DB_PATH).resolve().parent
    assert "MEI" not in str(artwork._stable_data_dir().resolve())


def test_artwork_covers_dir_is_repo_data_in_source_mode():
    """源码运行（默认态）：封面缓存在真实 game-life/data/covers，不在 _MEIPASS。"""
    assert artwork._COVERS_DIR.resolve() == (APP_ROOT / "data" / "covers").resolve()
    assert artwork._COVERS_DIR.resolve() == schema.data_dir().resolve() / "covers"


# ------------------------------------- 面板账号 / 写回状态 / 日志（G9 冻结态）

# 用户第 3 项的核心场景：打包版「已添加的账号」与「分类写回」开关状态必须
# 在重启后仍在。二者都曾用 APP_ROOT（= 打包态 sys._MEIPASS）拼路径，写进
# 进程退出即删的临时目录，重启即丢。这里是无条件回归守卫。


def test_frozen_accounts_path_uses_real_data_dir(frozen_env):
    """frozen 下 accounts_path() 必须是 exe 旁真实 data 目录，不是 _MEIPASS。"""
    resolved = api.accounts_path().resolve()
    assert resolved == frozen_env["data"].resolve() / "accounts.json", resolved
    assert resolved == schema.data_dir().resolve() / "accounts.json"
    assert "MEI" not in str(resolved), f"落进临时解包目录了：{resolved}"

    hidden = api.hidden_accounts_path().resolve()
    assert hidden.parent == frozen_env["data"].resolve()
    assert "MEI" not in str(hidden)


def test_frozen_accounts_roundtrip_survives_outside_meipass(frozen_env):
    """真写一次：添加账号落在真实 data 目录、可读回、_MEIPASS 里一点东西都不留。"""
    before = sorted(p.name for p in (frozen_env["meipass"] / "data").iterdir())

    entry = {"id": "10000001", "name": "本地号"}
    api.save_added_accounts([entry])
    target = frozen_env["data"] / "accounts.json"
    assert target.is_file(), "账号文件没有写进真实 data 目录"

    # 模拟「重启面板」：清掉进程内缓存后重新读
    assert api.load_added_accounts() == [{"id": "10000001", "name": "本地号", "added_at": ""}]

    after = sorted(p.name for p in (frozen_env["meipass"] / "data").iterdir())
    assert after == before, f"临时解包目录里出现了写入物：{after}"


def test_frozen_writeback_state_path_uses_real_data_dir(frozen_env):
    """frozen 下写回状态路径（函数 + 旧名）都必须落在真实 data 目录。"""
    want = frozen_env["data"].resolve() / "writeback_state.json"
    assert cli._writeback_state_path().resolve() == want
    assert Path(cli._WRITEBACK_STATE_PATH).resolve() == want, "旧名常量没跟着环境走"
    assert "MEI" not in str(want)


def test_frozen_writeback_state_roundtrip_outside_meipass(frozen_env):
    """真写一次：写回开关状态落在真实 data 目录、可读回、_MEIPASS 无残留。"""
    before = sorted(p.name for p in (frozen_env["meipass"] / "data").iterdir())

    assert cli._load_writeback_state() is False  # 首次为空
    cli._save_writeback_state(True)
    target = frozen_env["data"] / "writeback_state.json"
    assert target.is_file(), "写回状态没有写进真实 data 目录"
    assert cli._load_writeback_state() is True  # 重启后仍是「开」

    after = sorted(p.name for p in (frozen_env["meipass"] / "data").iterdir())
    assert after == before, f"临时解包目录里出现了写入物：{after}"


def test_frozen_app_and_logs_dir_out_of_meipass(frozen_env):
    """运行期 logs/ 与 game-life 根目录也必须在 exe 附近，不在 _MEIPASS。"""
    app_dir = cli._stable_app_dir().resolve()
    assert app_dir == frozen_env["repo"].resolve() / "game-life", app_dir
    assert "MEI" not in str(app_dir)

    logs = (app_dir / "logs").resolve()
    assert logs.name == "logs"
    assert "MEI" not in str(logs)


def test_source_mode_accounts_and_writeback_are_repo_data():
    """源码运行（默认态）：账号 / 写回状态仍在真实 game-life/data。"""
    assert api.accounts_path().resolve() == (APP_ROOT / "data" / "accounts.json").resolve()
    assert api.hidden_accounts_path().resolve().parent == (APP_ROOT / "data").resolve()
    assert cli._writeback_state_path().resolve() == \
        (APP_ROOT / "data" / "writeback_state.json").resolve()
    assert cli._stable_app_dir().resolve() == APP_ROOT.resolve()

# ------------------------------------------------------- 打包 / 启动脚本守卫


def _spec_data_entries() -> list:
    """取出 spec 里 datas 列表的条目行（忽略注释行）。"""
    text = SPEC_PATH.read_text(encoding="utf-8")
    body = text.split("datas = [", 1)[1].split("\n]", 1)[0]
    return [ln.strip() for ln in body.splitlines() if ln.strip().startswith("(str(")]


def test_spec_ships_readonly_tables_but_no_mutable_data():
    """只读静态表要打包；可写/易变数据（缓存、封面、账号、审计）绝不能打包。"""
    entries = "\n".join(_spec_data_entries())
    for needed in ("genre_cn.json", "series_map.json", "name_cn_extra.json"):
        assert needed in entries, f"只读静态表应随包分发（兜底）：{needed}"

    for banned in (
        "cache.db", "covers", "price_audit.json", "fx_rates.json",
        "accounts.json", "names_cn.json", "series_official.json",
        "appinfo_index.json", "name_cn_baike.json",
    ):
        assert banned not in entries, f"易变/可写数据不应打包：{banned}"


def test_spec_bundles_templates_and_rules():
    entries = "\n".join(_spec_data_entries())
    for needed in ("render", "webapp", "rules.json"):
        assert needed in entries, f"spec 缺少 {needed}"


def test_launcher_scan_covers_data_files():
    """启动器「源码比 exe 新」扫描必须覆盖数据文件，且保持 GBK + CRLF。"""
    raw = LAUNCHER_PATH.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n"), "启动面板.bat 必须全 CRLF"
    text = raw.decode("gbk")  # 不是 GBK 会在这里抛异常

    scan = [ln for ln in text.split("\r\n") if "call :mark_newer" in ln and "for %%F in" in ln]
    assert scan, "找不到扫描数据文件的 for %%F in (...) 行"
    line = "\n".join(scan)

    # 只读的源码 / 数据表：必须扫，改了它们才算「源码更新」
    for name in ("series_map.json", "name_cn_extra.json", "genre_cn.json"):
        assert name in line, f"扫描列表缺少只读数据文件：{name}"
    assert "main.py" in line and "rules.json" in line

    # 会被程序运行期写出的文件绝不能扫：写过之后时间戳必然比 exe 新，
    # 会让每次正常启动都误报「源码更新」（启动器随即徒劳地回退源码模式）。
    for banned in (
        "names_cn.json", "series_official.json", "fx_rates.json",
        "accounts.json", "cache.db", "price_audit.json",
        "appinfo_index.json", "name_cn_baike.json",
    ):
        assert banned not in line, f"扫描列表不应包含运行期写入的文件：{banned}"
