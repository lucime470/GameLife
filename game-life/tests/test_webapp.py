"""本地网页面板（webapp）验收测试：离线打桩 + 环境相关冒烟。

运行方式（仓库根目录）：
    python game-life/tests/test_webapp.py

零额外依赖（标准库 + 既有 requirements.txt）：
    - 不访问网络、不写入真实 Steam 目录、不改动 data/cache.db 与 data/writeback_state.json；
    - 需要真实 Steam 库的断言统一标注「环境相关，跳过」，不影响整体通过；
    - 纯逻辑断言一律用打桩（临时替换模块函数）构造数据，稳定可复现。

覆盖：
    1. 端口探测 pick_port：首选被占用时顺延，释放后回到首选；
    2. 首页与静态资源：GET / 200 且含标志性文案；/static/*.css|js 200；
       缺失静态文件 404；/static/../main.py 路径穿越被拒；
    3. 路由兜底：未知路径 404 JSON；/page/<未知 kind> 404；
    4. 只读接口冒烟：/api/state、/api/config、/api/accounts（缺环境时跳过）；
    5. 汇总区块降级：单区块抛异常时 HTTP 仍 200，错误只落在 errors 字段；
    6. 未捕获异常兜底：返回 {"error": ...}（不泄露 traceback），且服务不崩；
    7. 写回按钮：Steam 运行中给出友好中文提示且不翻转按钮状态；
    8. CLI 接线：main.py panel --help 退出码 0 且含全部参数名。
"""

import json
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_ROOT = REPO_ROOT / "game-life"
MAIN = APP_ROOT / "main.py"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from webapp import api, server  # noqa: E402

PANEL_HELP_FLAGS = ["--port", "--steam-path", "--account-id", "--with-metadata", "--no-open"]


class Panel:
    """在后台线程里跑一个真实面板实例（port=0 -> 系统分配空闲端口）。

    这样测试不会和用户正在使用的真实面板抢 8765 端口。
    """

    def __init__(self, **kwargs):
        self.httpd = server.build_server(port=0, **kwargs)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, path, method="GET", payload=None):
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = resp.read().decode("utf-8")
                return resp.status, resp.headers.get("Content-Type", ""), body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            return exc.code, exc.headers.get("Content-Type", ""), body

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)


def test_pick_port_falls_back():
    """首选端口被占用时顺延；释放后回到首选端口。"""
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        preferred = busy.getsockname()[1]
        picked = server.pick_port("127.0.0.1", preferred)
        assert picked != preferred, f"端口 {preferred} 已占用，却仍被选出"
        assert preferred < picked <= preferred + server.PORT_SCAN_LIMIT, (
            f"顺延范围异常：preferred={preferred} picked={picked}"
        )
        print(f"PASS pick_port：{preferred} 被占用 -> 顺延到 {picked}")

    again = server.pick_port("127.0.0.1", preferred)
    assert again == preferred, f"端口释放后应回到首选 {preferred}，实际 {again}"
    print(f"PASS pick_port：端口释放后回到首选 {preferred}")


def test_dashboard_and_static():
    """首页与静态资源；缺失文件 404；路径穿越被拒。"""
    panel = Panel()
    try:
        status, ctype, body = panel.request("/")
        assert status == 200, f"GET / 状态码 {status}"
        assert "text/html" in ctype, f"首页 Content-Type 异常：{ctype}"
        assert "GameLife" in body, "首页缺少 GameLife 标志文案"
        assert len(body) > 500, f"首页过短：{len(body)} 字节"
        print(f"PASS GET /：200 + HTML（{len(body)} 字节）")

        status, ctype, _body = panel.request("/static/style.css")
        assert status == 200, f"GET /static/style.css 状态码 {status}"
        assert "css" in ctype, f"静态资源 Content-Type 异常：{ctype}"
        print("PASS GET /static/style.css：200 + text/css")

        status, _ctype, _body = panel.request("/static/app.js")
        assert status == 200, f"GET /static/app.js 状态码 {status}"
        print("PASS GET /static/app.js：200")

        status, _ctype, body = panel.request("/static/definitely-missing.css")
        assert status == 404, f"缺失静态文件应 404，实际 {status}"
        assert "error" in json.loads(body), f"404 响应缺少 error 字段：{body[:120]}"
        print("PASS 缺失静态文件：404 + error")

        status, _ctype, _body = panel.request("/static/../main.py")
        assert status in (403, 404), f"路径穿越 /static/../main.py 应被拒，实际 {status}"
        print(f"PASS 路径穿越 /static/../main.py：{status}")
    finally:
        panel.close()


def test_route_fallbacks():
    """未知 API 路径与未知页面类型都返回 404 JSON。"""
    panel = Panel()
    try:
        status, _ctype, body = panel.request("/api/definitely-not-here")
        assert status == 404, f"未知 API 路径应 404，实际 {status}"
        data = json.loads(body)
        assert "error" in data, f"404 响应缺少 error：{body[:120]}"
        print(f"PASS GET /api/definitely-not-here：404 + {data['error'][:24]}")

        status, _ctype, body = panel.request("/page/not-a-kind")
        assert status == 404, f"/page/<未知 kind> 应 404，实际 {status}"
        data = json.loads(body)
        assert "error" in data, f"404 响应缺少 error：{body[:120]}"
        assert "error" not in body.lower().replace('"error"', ""), "错误信息应为单层结构"
        print(f"PASS GET /page/not-a-kind：404 + {data['error'][:24]}")

        status, _ctype, body = panel.request("/page/report/nope")
        assert status == 404, f"/page/report/nope 应 404，实际 {status}"
        print("PASS GET /page/report/nope：404")
    finally:
        panel.close()


def test_readonly_endpoints_smoke():
    """只读接口冒烟：/api/state 与 /api/config 必须稳定 200；
    /api/accounts 依赖本机 Steam，缺失时标注跳过。"""
    panel = Panel()
    try:
        status, _ctype, body = panel.request("/api/state")
        assert status == 200, f"/api/state 状态码 {status}：{body[:200]}"
        data = json.loads(body)
        assert set(data) >= {"writeback", "cached_libraries"}, f"/api/state 字段异常：{data}"
        assert isinstance(data["writeback"], bool), "writeback 应为布尔"
        print(f"PASS /api/state：writeback={data['writeback']} cached={data['cached_libraries']}")

        status, _ctype, body = panel.request("/api/config")
        assert status == 200, f"/api/config 状态码 {status}：{body[:200]}"
        data = json.loads(body)
        assert {"path", "source", "file", "merged"} <= set(data), f"/api/config 字段异常：{list(data)}"
        assert isinstance(data["merged"], dict), "merged 应为对象"
        print(f"PASS /api/config：source={data['source']}")

        status, _ctype, body = panel.request("/api/accounts")
        if status != 200:
            print(f"SKIP /api/accounts（环境相关，跳过：状态码 {status}）")
            return
        data = json.loads(body)
        assert isinstance(data.get("accounts"), list), f"accounts 应为列表：{list(data)}"
        ids = [str(a.get("id")) for a in data["accounts"]]
        assert all(ids), f"账号 id 不应为空：{data['accounts']}"
        if ids:
            assert data.get("current") in ids, f"current={data.get('current')} 不在账号列表中"
        print(f"PASS /api/accounts：{len(ids)} 个账号，current={data.get('current')}")
    finally:
        panel.close()


# 首启行为回归：账号全部在**临时 data 目录**里造，绝不碰真实的
# game-life/data/accounts.json（否则会把开发者的真实配置改坏，且不可重复跑）。
FAKE_LOCAL = [
    {"id": "900001", "name": "打桩账号甲"},
    {"id": "900002", "name": "打桩账号乙"},
]


class TempAccounts:
    """把 api 的 accounts.json 指到临时目录，并打桩本机账号扫描。

    用法::

        with TempAccounts() as box:      # 临时 accounts.json 初始为空列表
            box.view(current="900001")
    """

    def __init__(self, entries=None, local=None):
        self.entries = list(entries or [])
        self.local = list(FAKE_LOCAL if local is None else local)
        self._tmp = None
        self._orig = {}

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="gl_accounts_")
        self.path = Path(self._tmp.name) / "accounts.json"
        self._write(self.entries)
        self._orig = {"accounts_path": api.accounts_path,
                      "_scan_local_accounts": api._scan_local_accounts}
        api.accounts_path = lambda: self.path
        api._scan_local_accounts = lambda steam_path=None: [dict(a) for a in self.local]
        return self

    def __exit__(self, *_exc):
        for key, value in self._orig.items():
            setattr(api, key, value)
        self._tmp.cleanup()
        return False

    def _write(self, entries) -> None:
        payload = json.dumps({"accounts": list(entries)}, ensure_ascii=False, indent=2) + "\n"
        self.path.write_text(payload, encoding="utf-8")

    def raw(self) -> str:
        """accounts.json 的原始文本（用来证明「没被偷偷写回」）。"""
        return self.path.read_text(encoding="utf-8")

    def load(self) -> list:
        return json.loads(self.raw()).get("accounts")

    def view(self, current=None, steam_path=None):
        return api.accounts_view(steam_path=steam_path, current=current)


def test_accounts_first_run_never_auto_adds():
    """首启：一个账号都没添加时，--account-id 只作为 suggested 提示。

    回归「首次打开自动把 --account-id 塞进 accounts.json」与「current 回退去扫
    本机账号」两个问题。全部在临时目录里跑。
    """
    with TempAccounts(local=[{"id": "900001", "name": "打桩账号甲"}]) as box:
        before = box.raw()
        data = box.view(current="900001")

        assert data["accounts"] == [], f"首启不应有任何账号：{data['accounts']}"
        assert data["needs_setup"] is True, "首启 needs_setup 应为 True"
        assert data["current"] == "", f"零账号时 current 必须为空串，实际 {data['current']!r}"
        assert data["suggested"] == {"id": "900001", "name": "打桩账号甲"}, (
            f"suggested 应指向启动参数账号：{data['suggested']}"
        )
        assert [a["id"] for a in data["available"]] == ["900001"], (
            f"available 应列出本机未添加账号：{data['available']}"
        )
        assert data["total_accounts"] == 1, f"total_accounts 异常：{data['total_accounts']}"

        after = box.raw()
        assert after == before, f"首启不应写回 accounts.json：\n{before!r}\n->\n{after!r}"
        assert box.load() == [], f"临时 accounts.json 应仍为空：{box.load()}"
        print("PASS 首启：accounts=[] / needs_setup=True / current='' / suggested=900001，文件未被写回")

        # 不带 --account-id 时同样不该回退去扫本机账号
        data2 = box.view(current=None)
        assert data2["current"] == "", f"零账号且无启动参数时 current 也必须为空串：{data2['current']!r}"
        assert data2["suggested"] is None, f"无启动参数时不应有 suggested：{data2['suggested']}"
        assert box.load() == [], "无启动参数时也不应写回 accounts.json"
        print("PASS 首启（无 --account-id）：current='' / suggested=None，文件仍为空")


def test_accounts_add_then_remove_roundtrip():
    """添加后可读回、移除后回到 needs_setup；全程只动临时目录里的 accounts.json。"""
    with TempAccounts(local=FAKE_LOCAL) as box:
        added = api.add_accounts(["900002"], current="900002")
        assert added["ok"] is True, f"添加应成功：{added}"
        assert added["needs_setup"] is False, "添加后 needs_setup 应为 False"
        assert [a["id"] for a in added["accounts"]] == ["900002"], f"添加结果异常：{added['accounts']}"
        assert added["current"] == "900002", f"current 应命中刚添加的账号：{added['current']!r}"
        assert [a["id"] for a in added["available"]] == ["900001"], (
            f"available 应剔除已添加账号：{added['available']}"
        )
        assert [a["id"] for a in api.load_added_accounts()] == ["900002"], "文件里应读回该账号"
        print(f"PASS 添加：accounts=[900002] / needs_setup=False / current=900002")

        removed = api.remove_account("900002", current="900002")
        assert removed["ok"] is True, f"移除应成功：{removed}"
        assert removed["accounts"] == [], f"移除后应无账号：{removed['accounts']}"
        assert removed["needs_setup"] is True, "移除后应回到 needs_setup=True"
        assert removed["current"] == "", f"移除后 current 应为空串：{removed['current']!r}"
        assert box.load() == [], f"临时 accounts.json 应回到空列表：{box.load()}"
        print("PASS 移除：accounts=[] / needs_setup=True / current=''")


# 合成 appid：刻意用**任何来源都查不到**的号段，展示名才不会被本地中文名表
# 或官方 appinfo 里真实游戏的中文名接住（否则「打桩游戏甲」会被替换掉）。
FAKE_A = 77700101
FAKE_B = 77700102


def _fake_library(account_id="10001"):
    """面板用的合成库数据（一个在玩、一个未玩），不触碰真实 Steam。"""
    return {
        "steam_path": r"C:\fake\steam",
        "account_id": str(account_id),
        "play_stats": {
            FAKE_A: {"playtime": 120, "last_played": 1700000000},
            FAKE_B: {"playtime": 0, "last_played": 0},
        },
        "games_meta": {
            FAKE_A: {"name": "打桩游戏甲", "genres": ["动作"]},
            FAKE_B: {"name": "打桩游戏乙", "genres": ["独立"]},
        },
        "statuses": {FAKE_A: "playing", FAKE_B: "dust"},
        "classified": {
            FAKE_A: {"status": "playing", "status_name": "在玩"},
            FAKE_B: {"status": "dust", "status_name": "搁置"},
        },
        "named": 2,
    }


def test_summary_sections_degrade():
    """单区块抛异常时：HTTP 仍 200，该区块为 null，错误只落在 errors 字段。"""
    original_require = api._require_library
    original_stats = api._stats_section

    def fake_require(account_id=None, with_metadata=False, steam_path=None):
        return _fake_library(account_id or "10001")

    def boom(_lib):
        raise RuntimeError("打桩：区块计算失败")

    api._require_library = fake_require
    api._stats_section = boom
    panel = Panel(account_id="10001")
    try:
        status, _ctype, body = panel.request("/api/summary?account_id=10001")
        assert status == 200, f"单区块异常不应改变状态码，实际 {status}：{body[:200]}"
        assert "Traceback" not in body, "响应体泄露了 traceback"
        data = json.loads(body)
        assert data.get("stats") is None, "抛异常的区块应降级为 null"
        errors = data.get("errors") or {}
        assert "stats" in errors, f"errors 缺少 stats：{errors}"
        assert "打桩" in errors["stats"], f"错误信息应保留原因：{errors['stats']}"
        print("PASS /api/summary：单区块异常 -> 200 + stats=null + errors.stats")

        assert data.get("games"), "游戏列表不应受单区块异常影响"
        assert data["games"][0]["name"] == "打桩游戏甲", f"游戏列表异常：{data['games'][:1]}"
        assert data["games"][0]["status_name"] == "在玩", f"状态名异常：{data['games'][:1]}"
        assert data["account_id"] == "10001", f"account_id 透传异常：{data['account_id']}"
        print(f"PASS /api/summary：其余区块与 {len(data['games'])} 行游戏列表不受影响")

        for section in ("persona", "value", "series", "wishlist", "challenge", "radar"):
            assert section in data, f"缺少区块 {section}"
        print("PASS /api/summary：7 个区块字段齐全")
    finally:
        api._require_library = original_require
        api._stats_section = original_stats
        panel.close()


def test_engine_error_is_caught():
    """未捕获异常兜底成 500 + {"error": ...}，且服务仍然存活。"""
    original = api.get_accounts

    def boom(*_args, **_kwargs):
        raise RuntimeError("打桩：账号读取失败")

    api.get_accounts = boom
    panel = Panel()
    try:
        status, _ctype, body = panel.request("/api/accounts")
        assert status == 500, f"未捕获异常应兜底为 500，实际 {status}"
        assert "Traceback" not in body, "响应体泄露了 traceback"
        data = json.loads(body)
        assert "error" in data, f"响应缺少 error 字段：{body[:200]}"
        assert "打桩" in data["error"], f"错误原因丢失：{data['error']}"
        print(f"PASS 未捕获异常兜底：500 + error（{data['error'][:28]}）")

        status, _ctype, _body = panel.request("/api/state")
        assert status == 200, f"异常后服务不应崩溃，/api/state 实际 {status}"
        print("PASS 异常后服务仍存活（/api/state 200）")
    finally:
        api.get_accounts = original
        panel.close()


def test_writeback_friendly_when_steam_running():
    """写回按钮：Steam 运行中 -> 友好中文提示、按钮状态不翻转、状态文件不变。"""
    from core import steam_writer

    state_file = APP_ROOT / "data" / "writeback_state.json"
    before = state_file.read_text(encoding="utf-8") if state_file.exists() else None

    original_is_running = steam_writer.is_steam_running
    original_resolve = api.cli._resolve_steam_account
    original_read = api.cli._read_playtime
    original_state = api.cli._load_writeback_state

    steam_writer.is_steam_running = lambda *a, **k: True
    api.cli._resolve_steam_account = lambda args: (r"C:\fake\steam", "10001")
    api.cli._read_playtime = lambda steam_path, account_id: {
        10: {"playtime": 120, "last_played": 1700000000}
    }
    api.cli._load_writeback_state = lambda: False

    panel = Panel()
    try:
        status, _ctype, body = panel.request(
            "/api/writeback", method="POST", payload={"account_id": "10001"}
        )
        assert status == 200, f"写回接口不应 500，实际 {status}：{body[:200]}"
        assert "Traceback" not in body, "响应体泄露了 traceback"
        data = json.loads(body)
        assert data.get("ok") is False, f"Steam 运行中应 ok=false：{data}"
        assert data.get("writeback") is False, "Steam 运行中按钮状态不应翻转"
        message = data.get("message") or ""
        assert "Steam" in message, f"提示未提及 Steam：{message}"
        assert "退出" in message or "正在运行" in message, f"提示不够明确：{message}"
        print(f"PASS POST /api/writeback：ok=false + 友好提示（{message[:30]}…）")

        after = state_file.read_text(encoding="utf-8") if state_file.exists() else None
        assert after == before, "写回失败时不应改动 writeback_state.json"
        print("PASS 写回失败未改动 writeback_state.json")
    finally:
        steam_writer.is_steam_running = original_is_running
        api.cli._resolve_steam_account = original_resolve
        api.cli._read_playtime = original_read
        api.cli._load_writeback_state = original_state
        panel.close()


def test_panel_cli_help():
    """CLI 接线：main.py panel --help 退出码 0 且含全部参数名。"""
    result = subprocess.run(
        [sys.executable, str(MAIN), "panel", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0, (
        f"panel --help 退出码 {result.returncode}\n{result.stdout[-600:]}\n{result.stderr[-600:]}"
    )
    text = (result.stdout or "") + (result.stderr or "")
    missing = [flag for flag in PANEL_HELP_FLAGS if flag not in text]
    assert not missing, f"panel --help 缺少参数：{missing}"
    assert "Traceback" not in text, "panel --help 出现 traceback"
    print("PASS panel --help：含参数 " + "、".join(PANEL_HELP_FLAGS))


def main():
    tests = [
        test_pick_port_falls_back,
        test_dashboard_and_static,
        test_route_fallbacks,
        test_readonly_endpoints_smoke,
        test_accounts_first_run_never_auto_adds,
        test_accounts_add_then_remove_roundtrip,
        test_summary_sections_degrade,
        test_engine_error_is_caught,
        test_writeback_friendly_when_steam_running,
        test_panel_cli_help,
    ]
    for test in tests:
        test()
    print(f"\n全部 {len(tests)} 组验证通过")


if __name__ == "__main__":
    main()
