"""steam_writer 写回模块的测试（依赖 vdf/psutil，缺失时跳过）。"""

import sys
import tempfile
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vdf
from core import steam_writer as sw

ACCT = "12345"


def _make_vdf(root):
    """在 root 下创建模拟 sharedconfig.vdf，返回 steam_path。"""
    steam_path = root / "Steam"
    remote = steam_path / "userdata" / ACCT / "7" / "remote"
    remote.mkdir(parents=True)
    vdf_path = remote / "sharedconfig.vdf"

    data = {
        "UserRoamingConfigStore": {
            "Software": {
                "Valve": {
                    "Steam": {
                        "Apps": {
                            "10": {"tags": {"0": "动作"}},
                            "20": {"tags": {"0": "收藏"}},
                            "30": {"tags": {"0": "收藏"}},
                        }
                    }
                }
            }
        }
    }
    with open(vdf_path, "w", encoding="utf-8-sig") as f:
        vdf.dump(data, f)
    return str(steam_path)


def test_write_categories():
    """写入后 tags 包含状态名 + 保留手动分类。"""
    root = Path(tempfile.mkdtemp(prefix="gl_sw_"))
    try:
        steam_path = _make_vdf(root)
        orig = sw.is_steam_running
        sw.is_steam_running = lambda *a, **k: False
        try:
            backup = sw.write_categories(steam_path, ACCT, {10: ["在玩"]})
            assert Path(backup).exists()

            with open(Path(steam_path) / "userdata" / ACCT / "7" / "remote" / "sharedconfig.vdf",
                      "r", encoding="utf-8-sig") as f:
                data = vdf.load(f)
            apps = (data.get("UserRoamingConfigStore", {})
                    .get("Software", {}).get("Valve", {})
                    .get("Steam", {}).get("Apps", {}))
            tags = sorted(apps["10"]["tags"].values())
            assert "在玩" in tags
            assert "动作" in tags
            print("PASS 写入分类")
        finally:
            sw.is_steam_running = orig
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_idempotent():
    """重复写入不产生重复标签。"""
    root = Path(tempfile.mkdtemp(prefix="gl_sw_"))
    try:
        steam_path = _make_vdf(root)
        orig = sw.is_steam_running
        sw.is_steam_running = lambda *a, **k: False
        try:
            sw.write_categories(steam_path, ACCT, {10: ["在玩"]})
            sw.write_categories(steam_path, ACCT, {10: ["在玩"]})
            with open(Path(steam_path) / "userdata" / ACCT / "7" / "remote" / "sharedconfig.vdf",
                      "r", encoding="utf-8-sig") as f:
                data = vdf.load(f)
            apps = (data.get("UserRoamingConfigStore", {})
                    .get("Software", {}).get("Valve", {})
                    .get("Steam", {}).get("Apps", {}))
            tags = list(apps["10"]["tags"].values())
            assert tags.count("在玩") == 1, f"重复标签: {tags}"
            print("PASS 幂等写入")
        finally:
            sw.is_steam_running = orig
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_manual_categories_preserved():
    """写入状态分类时，用户手动分类保留。"""
    root = Path(tempfile.mkdtemp(prefix="gl_sw_"))
    try:
        steam_path = _make_vdf(root)
        orig = sw.is_steam_running
        sw.is_steam_running = lambda *a, **k: False
        try:
            sw.write_categories(steam_path, ACCT, {10: ["在玩"]})
            with open(Path(steam_path) / "userdata" / ACCT / "7" / "remote" / "sharedconfig.vdf",
                      "r", encoding="utf-8-sig") as f:
                data = vdf.load(f)
            apps = (data.get("UserRoamingConfigStore", {})
                    .get("Software", {}).get("Valve", {})
                    .get("Steam", {}).get("Apps", {}))
            tags = sorted(apps["10"]["tags"].values())
            assert "动作" in tags
            assert "在玩" in tags
            print("PASS 用户手动分类保留")
        finally:
            sw.is_steam_running = orig
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_remove_categories():
    """删除状态分类后，状态名消失，手动分类保留。"""
    root = Path(tempfile.mkdtemp(prefix="gl_sw_"))
    try:
        steam_path = _make_vdf(root)
        orig = sw.is_steam_running
        sw.is_steam_running = lambda *a, **k: False
        try:
            # 先写入状态分类
            sw.write_categories(steam_path, ACCT, {30: ["在玩"]})
            # 删除状态分类
            sw.remove_categories(steam_path, ACCT, [30])
            with open(Path(steam_path) / "userdata" / ACCT / "7" / "remote" / "sharedconfig.vdf",
                      "r", encoding="utf-8-sig") as f:
                data = vdf.load(f)
            apps = (data.get("UserRoamingConfigStore", {})
                    .get("Software", {}).get("Valve", {})
                    .get("Steam", {}).get("Apps", {}))
            tags = sorted(apps["30"]["tags"].values())
            assert "在玩" not in tags
            assert "收藏" in tags
            print("PASS 删除状态分类")
        finally:
            sw.is_steam_running = orig
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_steam_running_raises():
    """Steam 进程运行时抛 SteamRunningError（monkeypatch psutil）。
    """
    root = Path(tempfile.mkdtemp(prefix="gl_sw_"))
    try:
        steam_path = _make_vdf(root)

        class _FakeProc:
            def __init__(self, info):
                self.info = info

        import psutil
        saved_psutil = sys.modules.get("psutil")
        fake_psutil = type(sys)("psutil")
        fake_psutil.process_iter = lambda attrs=None: [
            _FakeProc({"name": "steam.exe"})]
        fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
        sys.modules["psutil"] = fake_psutil
        try:
            try:
                sw.write_categories(steam_path, ACCT, {10: ["在玩"]})
                raise AssertionError("应抛 SteamRunningError")
            except sw.SteamRunningError:
                pass
            print("PASS Steam 运行中止")
        finally:
            if saved_psutil is not None:
                sys.modules["psutil"] = saved_psutil
            else:
                sys.modules.pop("psutil", None)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_backup_exists():
    """写回后备份文件存在。"""
    root = Path(tempfile.mkdtemp(prefix="gl_sw_"))
    try:
        steam_path = _make_vdf(root)
        orig = sw.is_steam_running
        sw.is_steam_running = lambda *a, **k: False
        try:
            sw.write_categories(steam_path, ACCT, {10: ["在玩"]})
            backup = Path(steam_path) / "userdata" / ACCT / "7" / "remote" / "sharedconfig.bak"
            assert backup.exists()
            print("PASS 备份文件存在")
        finally:
            sw.is_steam_running = orig
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    tests = [
        test_write_categories, test_idempotent,
        test_manual_categories_preserved, test_remove_categories,
        test_steam_running_raises, test_backup_exists,
    ]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 组 steam_writer 验证通过")


if __name__ == "__main__":
    main()
