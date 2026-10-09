r"""面板「中文名 + 封面」接线自测（webapp/server.py + webapp/api.py + core/*）。

运行方式（game-life 目录）：
    ..\.venv\Scripts\python.exe tests\test_webapp_localize.py

零额外依赖（纯标准库）、临时端口、临时封面目录，**不污染真实 data/covers**，
默认不发起真实网络请求（联网抓取用打桩替身）。

覆盖：
    1. 面板首页含「抓取中文名与封面」按钮与封面列；
    2. /covers/<appid>.jpg 正常命中：200 + image/*，字节与磁盘一致；
    3. 缺图 → 404 JSON（不 500）；
    4. 非数字 appid / 路径穿越 / 带后缀花活 → 一律 404，绝不越权读文件；
    5. /api/summary 每行含 name_cn / cover 字段，coverage 统计自洽；
    6. /api/cover 单张查询；
    7. /api/localize：scope 非法 → 400；打桩后 → 返回 names/covers/coverage；
       封面 results 是 list（artwork）还是 dict（metadata_cn）都必须 200 且 sample 字段齐全；
    8. 抓取模块不可用 → 友好 error，不 500（降级路径）；
    9. 未知 /page/<kind> 仍 404；
   10. main.py artwork --help / localize --help 退出码 0。
"""

import json
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import artwork            # noqa: E402
from webapp import api              # noqa: E402
from webapp import server as panel_server  # noqa: E402

FAILURES: list[str] = []
_INFO: list[str] = []

# 1x1 的最小合法 JPEG（magic + 结构完整）
_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffc0001108000100"
    "0103011100021101031101ffc4001f00000105010101010101000000000000000001020304"
    "05060708090a0bffc400b5100002010303020403050504040000017d010203000411051221"
    "31410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a2526"
    "2728292a3435363738393a434445464748494a535455565758595a636465666768696a7374"
    "75767778797a838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6"
    "b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4"
    "f5f6f7f8f9faffda0008010100003f00fefe2800ffd9"
)


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)
        print(f"    ! {message}")


def info(message: str) -> None:
    _INFO.append(message)
    print(f"    i {message}")


class Panel:
    """起一个临时端口的面板服务，退出时自动关闭。"""

    def __init__(self, account_id: str | None = None) -> None:
        self.server = panel_server.build_server(port=0, account_id=account_id)
        self.port = int(self.server.server_address[1])
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def get(self, path: str, timeout: float = 180.0):
        try:
            with urllib.request.urlopen(self.base + path, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers or {})

    def post(self, path: str, payload: dict, timeout: float = 300.0):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw}

    def close(self) -> None:
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()


def main() -> int:
    tmp = tempfile.TemporaryDirectory(prefix="glpanel_")
    covers = Path(tmp.name) / "covers"
    covers.mkdir(parents=True, exist_ok=True)
    real_covers = artwork._COVERS_DIR
    artwork._COVERS_DIR = covers          # 整个测试都指向临时目录
    account = _default_account()
    panel = Panel(account_id=account)
    _STATE["account"] = account
    print(f"临时面板：{panel.base}（封面目录 {covers}，账号 {account or '自动'}）")
    try:
        run_tests(panel, covers)
    finally:
        panel.close()
        artwork._COVERS_DIR = real_covers
        tmp.cleanup()

    if FAILURES:
        print(f"\n共 {len(FAILURES)} 项断言失败：")
        for item in FAILURES:
            print(f"  - {item}")
        print(f"\n失败 {len(FAILURES)} 项，未通过")
        return 1
    print(f"\n全部 {_COUNT['n']} 项验证通过")
    return 0


_COUNT = {"n": 0}
_STATE: dict = {"account": None}


def _default_account() -> str | None:
    """按本机 userdata 里最大的 localconfig 选一个账号（没有就交给面板自己挑）。"""
    try:
        return str(api.get_accounts().get("current") or "") or None
    except Exception:
        return None


def case_label(name: str):
    """把函数登记成一项测试（计数用）。

    注意：不要叫 ``test``——pytest 会把 test_*.py 里所有 ``test*`` 顶层函数
    当成用例收集，并去要一个名为 name 的 fixture；本函数只是给 case 登记名字，
    由本文件的 run_tests() 驱动，故改名避免误收集。
    """

    def wrapper(fn):
        fn._case_name = name
        return fn

    return wrapper


def run_tests(panel: "Panel", covers: Path) -> None:
    cases = [
        ("面板首页含封面列与抓取按钮", lambda: case_dashboard(panel)),
        ("/covers/<appid>.jpg 命中返回图片", lambda: case_cover_ok(panel, covers)),
        ("/covers 缺图 404 且不 500", lambda: case_cover_missing(panel)),
        ("/covers 非数字与路径穿越一律 404", lambda: case_cover_safety(panel)),
        ("/api/summary 行含 name_cn / cover 且 coverage 自洽", lambda: case_summary(panel)),
        ("/api/cover 单张查询", lambda: case_cover_api(panel)),
        ("/api/localize scope 非法 → 400", lambda: case_localize_badscope(panel)),
        ("/api/localize 打桩 → 返回统计", lambda: case_localize_stub()),
        ("/api/localize 封面(results 为 list) → 不再 500", lambda: case_localize_covers_list()),
        ("/api/localize 封面(results 为 dict) → 回归不破", lambda: case_localize_covers_dict()),
        ("抓取模块不可用时友好降级", lambda: case_localize_degraded()),
        ("未知 /page/<kind> → 404", lambda: case_unknown_page(panel)),
        ("main.py artwork/localize --help 退出码 0", lambda: case_cli_help()),
    ]
    for index, (name, fn) in enumerate(cases, 1):
        print(f"  [{index}/{len(cases)}] {name} ...", end="", flush=True)
        before = len(FAILURES)
        try:
            fn()
        except Exception as exc:  # 测试自身炸了也要报出来
            import traceback

            FAILURES.append(f"{name} 抛异常: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        _COUNT["n"] = index
        print(" 失败" if len(FAILURES) > before else " 通过")


# ------------------------------------------------------------------ 用例

def case_dashboard(panel: "Panel") -> None:
    status, body, _ = panel.get("/")
    html = body.decode("utf-8", "replace")
    check(status == 200, f"首页状态码应为 200，实际 {status}")
    check("btnLocalize" in html, "首页缺少「抓取中文名与封面」按钮")
    check("game-grid" in html and 'id="games"' in html, "首页缺少封面卡片区")
    check("coverage" in html, "首页缺少覆盖率标签")


def case_cover_ok(panel: "Panel", covers: Path) -> None:
    payload = _JPEG + b"\x00" * 16
    (covers / "999001.jpg").write_bytes(payload)
    status, body, headers = panel.get("/covers/999001.jpg")
    check(status == 200, f"已有封面应返回 200，实际 {status}")
    check(body == payload, "返回的图片字节与磁盘不一致")
    ctype = (headers.get("Content-Type") or "").lower()
    check(ctype.startswith("image/"), f"Content-Type 应为图片，实际 {ctype!r}")
    check("max-age" in (headers.get("Cache-Control") or ""), "缺少缓存头")


def case_cover_missing(panel: "Panel") -> None:
    status, body, _ = panel.get("/covers/987654321.jpg")
    check(status == 404, f"缺图应为 404，实际 {status}")
    try:
        data = json.loads(body.decode("utf-8"))
        check("error" in data, "404 响应应带 error 字段")
    except ValueError:
        FAILURES.append("404 响应不是 JSON")


def case_cover_safety(panel: "Panel") -> None:
    for path in ("/covers/abc.jpg", "/covers/..%2f..%2fmain.py",
                 "/covers/../../game-life/main.py", "/covers/-1.jpg", "/covers/1e3.jpg"):
        status, body, _ = panel.get(path)
        check(status == 404, f"{path} 应 404（路径穿越防护），实际 {status}")
        check(b"<!DOCTYPE" not in body, f"{path} 疑似返回了文件内容")
    # 后缀花活：只认 appid 数字部分，且绝不能把任意文件读出来
    status, body, _ = panel.get("/covers/999001.png/../../game-life/main.py")
    check(b"import " not in body and b"argparse" not in body,
          "带后缀的穿越路径读到了源码内容")
    check(status in (200, 404), f"异常路径应 200(合法图)/404，实际 {status}")
    if status == 200:
        check(body.startswith(b"\xff\xd8"), "200 时必须是那张合法封面图")


def case_summary(panel: "Panel") -> None:
    status, body, _ = panel.get("/api/summary")
    if status == 404:
        info("本机没有含游戏的账号，跳过 summary 断言")
        return
    check(status == 200, f"/api/summary 应为 200，实际 {status}")
    data = json.loads(body.decode("utf-8"))
    rows = data.get("games") or []
    check(isinstance(rows, list), "games 应为列表")
    if rows:
        sample = rows[0]
        check("name_cn" in sample, "游戏行缺少 name_cn 字段")
        check("cover" in sample, "游戏行缺少 cover 字段")
        check(isinstance(sample.get("cover"), str), "cover 应为字符串地址")
        for row in rows:
            if row.get("cover"):
                check(row["cover"] == f"/covers/{row['appid']}.jpg", "cover 地址格式不对")
                break
    coverage = data.get("coverage")
    check(isinstance(coverage, dict), "coverage 区块缺失")
    if isinstance(coverage, dict):
        names = sum(1 for r in rows if str(r.get("name_cn") or "").strip())
        cvs = sum(1 for r in rows if r.get("cover"))
        check(coverage.get("name_cn") == names, "coverage.name_cn 与实际行数不符")
        check(coverage.get("cover") == cvs, "coverage.cover 与实际行数不符")
        check(coverage.get("total") == len(rows), "coverage.total 与行数不符")
        info(f"覆盖率：中文名 {names}/{len(rows)}，封面 {cvs}/{len(rows)}")


def case_cover_api(panel: "Panel") -> None:
    status, body, _ = panel.get("/api/cover?appid=987654321")
    check(status == 200, f"/api/cover 应为 200，实际 {status}")
    data = json.loads(body.decode("utf-8"))
    check(data.get("ok") is False, "不存在的封面 ok 应为 False")
    check("url" in data and "name_cn" in data, "缺少 url / name_cn 字段")
    status, _, _ = panel.get("/api/cover?appid=abc")
    check(status == 400, f"非法 appid 应 400，实际 {status}")


def case_localize_badscope(panel: "Panel") -> None:
    status, data = panel.post("/api/localize", {"scope": "bogus"})
    check(status == 400, f"非法 scope 应 400，实际 {status}")
    check("error" in data, "错误响应应带 error")


def _stub_library() -> tuple:
    """把库加载整个打桩掉：临时 appid/游戏名，完全不碰真实 Steam 库与 data\。"""
    import main as cli
    from core import locator

    saved = (cli._load_library, locator.find_steam_path)

    def fake_load(_args, with_metadata=False):
        play_stats = {270001: 10.0, 270002: 3.0}
        games_meta = {270001: {"name": "Stub One"}, 270002: {"name": "Stub Two"}}
        statuses = {270001: "在玩", 270002: "吃灰"}
        return play_stats, games_meta, statuses, None, r"X:\steam-stub", "000000001"

    cli._load_library = fake_load
    locator.find_steam_path = lambda: r"X:\steam-stub"
    api._LIB_CACHE.clear()
    return saved


def _restore_library(saved: tuple) -> None:
    import main as cli
    from core import locator

    cli._load_library, locator.find_steam_path = saved
    api._LIB_CACHE.clear()


def _covers_list_stat() -> dict:
    """core.artwork.ensure_covers 的真实形态：results 是 list[dict]。"""
    return {
        "total": 2, "ok": 1, "failed": 1, "skipped": 0, "unsupported": 0,
        "by_source": {"cache": 1},
        "results": [
            {"appid": 270001, "ok": True, "path": "270001.jpg", "source": "cache",
             "bytes": 1234, "error": None, "kind": "header"},
            {"appid": 270002, "ok": False, "path": None, "source": "cdn:stub",
             "bytes": 0, "error": "下载失败", "kind": "unknown"},
        ],
    }


def case_localize_covers_list() -> None:
    """回归：artwork 的 results 是 list，_brief() 曾经 AttributeError → 500。"""
    from core import artwork

    saved_lib = _stub_library()
    original = artwork.ensure_covers
    artwork.ensure_covers = lambda appids, **kwargs: _covers_list_stat()
    try:
        result = api.post_localize(account_id="000000001", scope="covers", limit=2)
    finally:
        artwork.ensure_covers = original
        _restore_library(saved_lib)

    check(result.get("ok") is True, "post_localize 应返回 ok=True")
    brief = result.get("covers") or {}
    check(isinstance(brief, dict), "covers 区块应为 dict")
    check("error" not in brief, f"covers 不应降级为 error：{brief}")
    check(brief.get("failed") == 1, "covers 统计未透传（failed 应为 1）")
    sample = brief.get("sample") or []
    check(len(sample) == 2, f"sample 应含 2 条，实际 {len(sample)}")
    if len(sample) == 2:
        first, second = sample
        for row in sample:
            for field in ("appid", "ok", "name_cn", "source", "error"):
                check(field in row, f"sample 条目缺少 {field} 字段")
        check(first.get("appid") == 270001, "list 形态的 appid 应取自行内 appid")
        check(first.get("ok") is True and first.get("source") == "cache", "首条样例字段不对")
        check(first.get("name_cn") == "", "封面结果没有 name_cn，应回填空字符串")
        check(second.get("appid") == 270002 and second.get("ok") is False,
              "失败条目应保留 appid 且 ok=False")
        check("下载失败" in second.get("error", ""), "失败原因未透传")
    info(f"covers(list)：sample {len(sample)} 条，appid {[r.get('appid') for r in sample]}") 


def case_localize_covers_dict() -> None:
    """回归：results 为 dict 的旧形态不能被改坏（metadata_cn 分支）。"""
    from core import artwork

    saved_lib = _stub_library()
    original = artwork.ensure_covers
    artwork.ensure_covers = lambda appids, **kwargs: {
        "total": 1, "ok": 1, "failed": 0, "skipped": 0, "unsupported": 0,
        "by_source": {"cache": 1},
        "results": {270001: {"ok": True, "name_cn": "替身一号", "source": "cache"}},
    }
    try:
        result = api.post_localize(account_id="000000001", scope="covers", limit=2)
    finally:
        artwork.ensure_covers = original
        _restore_library(saved_lib)

    check(result.get("ok") is True, "dict 形态也应返回 ok=True")
    sample = (result.get("covers") or {}).get("sample") or []
    check(len(sample) == 1, f"dict 形态 sample 应含 1 条，实际 {len(sample)}")
    if sample:
        check(sample[0].get("appid") == 270001, "dict 形态 appid 应取自 key")
        check(sample[0].get("name_cn") == "替身一号", "dict 形态 name_cn 未透传")


def case_localize_stub() -> None:
    from core import metadata_cn

    original = metadata_cn.ensure_names_cn

    def stub(items, **kwargs):
        return {"ok": True, "total": 1, "ok_count": 1, "fail_count": 0, "skip_count": 0,
                "results": {570: {"ok": True, "name_cn": "刀塔 2", "source": "stub"}},
                "message": "中文名：新增 1 / 未获取 0 / 复用 0 / 共 1"}

    metadata_cn.ensure_names_cn = stub
    try:
        result = api.post_localize(account_id=_STATE["account"], scope="names", limit=1)
    finally:
        metadata_cn.ensure_names_cn = original
    check(result.get("ok") is True, "post_localize 应返回 ok=True")
    check(result.get("names", {}).get("ok_count") == 1, "names 统计未透传")
    check("coverage" in result, "返回缺少 coverage")
    check("中文名" in (result.get("message") or ""), "message 缺少中文名汇总")


def case_localize_degraded() -> None:
    from core import metadata_cn

    original = metadata_cn.ensure_names_cn

    class Boom:
        @staticmethod
        def ensure_names_cn(*_args, **_kwargs):
            raise RuntimeError("模拟抓取崩溃")

    import core as core_pkg

    saved = core_pkg.metadata_cn
    core_pkg.metadata_cn = Boom
    try:
        result = api.post_localize(account_id=_STATE["account"], scope="names", limit=1)
    finally:
        core_pkg.metadata_cn = saved
        metadata_cn.ensure_names_cn = original
    check(result.get("ok") is True, "崩溃时也应返回结构完整的响应")
    check("error" in (result.get("names") or {}), "崩溃时应降级为 error 文案")


def case_unknown_page(panel: "Panel") -> None:
    status, body, _ = panel.get("/page/unknown")
    check(status == 404, f"未知页面应 404，实际 {status}")
    status, _, _ = panel.get("/page/report")
    check(status in (200, 500), f"/page/report 不应 404，实际 {status}")


def case_cli_help() -> None:
    python = sys.executable
    for command in ("artwork", "localize"):
        proc = subprocess.run(
            [python, str(ROOT / "main.py"), command, "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(ROOT), timeout=120,
        )
        check(proc.returncode == 0, f"{command} --help 退出码应为 0，实际 {proc.returncode}")
        check("--limit" in (proc.stdout or ""), f"{command} --help 缺少 --limit 参数")


if __name__ == "__main__":
    raise SystemExit(main())