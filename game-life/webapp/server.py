"""本地网页面板 HTTP 服务（标准库 http.server，无新增依赖）。

    python main.py panel            # 启动并自动打开浏览器
    python main.py panel --no-open  # 只起服务（测试/脚本用）

只监听 127.0.0.1；默认端口 8765，被占用时自动顺延。
"""

from __future__ import annotations

import errno
import json
import logging
import socket
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from webapp import api  # noqa: E402

logger = logging.getLogger("game-life.webapp")

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_SCAN_LIMIT = 50

STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_MIME = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    # 产物页面里的封面（covers/*.jpg 等）也要给对 Content-Type
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
}


def pick_port(host: str = HOST, preferred: int = DEFAULT_PORT, limit: int = PORT_SCAN_LIMIT) -> int:
    """返回一个可用端口：优先 preferred，被占用则顺延 limit 次。"""
    for port in range(int(preferred), int(preferred) + int(limit) + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"{host} 上 {preferred}~{preferred + limit} 均被占用，请用 --port 指定其它端口")


def _render_dashboard() -> str:
    """读取面板模板（无 Jinja 依赖，纯文本返回，便于零配置启动）。"""
    path = TEMPLATES_DIR / "dashboard.html"
    if not path.exists():
        raise api.ApiError("面板模板缺失：dashboard.html", 500)
    return path.read_text(encoding="utf-8")


def _render_series_page() -> str:
    """系列详情页模板（纯文本返回；页面自己去调 /api/series 渲染卡片）。"""
    path = TEMPLATES_DIR / "series.html"
    if not path.exists():
        raise api.ApiError("系列详情页模板缺失：series.html", 500)
    return path.read_text(encoding="utf-8")


def _send_bytes(handler: "PanelHandler", status: int, body: bytes, content_type: str) -> None:
    """把字节直接写给客户端（含 Content-Length 与禁缓存头）。"""
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


# 客户端提前断开（关标签页 / 切走 / 取消请求）会抛出下面这些异常。它们不是服务端故障，
# 仅记 DEBUG 降噪，别用 ERROR + traceback 刷屏；**其它异常照旧记 ERROR**，别把兜底一起吞了。
_CLIENT_GONE_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
_CLIENT_GONE_ERRNOS = frozenset(
    n for n in (
        getattr(errno, "ECONNRESET", None),
        getattr(errno, "ECONNABORTED", None),
        getattr(errno, "EPIPE", None),
        getattr(errno, "ESHUTDOWN", None),
        getattr(errno, "WSAECONNABORTED", None),
        getattr(errno, "WSAECONNRESET", None),
        getattr(errno, "WSAESHUTDOWN", None),
        10053,  # WSAECONNABORTED（WinError 10053：软件中止了已建立的连接）
        10054,  # WSAECONNRESET
        10058,  # WSAESHUTDOWN
    )
    if n is not None
)


def _is_client_gone(exc: BaseException) -> bool:
    """异常是不是「客户端提前断开」（正常现象），而不是服务端故障。"""
    if isinstance(exc, _CLIENT_GONE_ERRORS):
        return True
    if isinstance(exc, OSError):
        if exc.errno in _CLIENT_GONE_ERRNOS:
            return True
        if getattr(exc, "winerror", None) in _CLIENT_GONE_ERRNOS:
            return True
    return False


def _as_bool(value, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "是"}


class PanelHandler(BaseHTTPRequestHandler):
    server_version = "GameLifePanel/0.1"
    protocol_version = "HTTP/1.1"

    # 默认参数（由 run() 注入到 server 实例上，这里透传访问）
    defaults: dict = {}

    # -------------------------------------------------------------- 基础设施

    def log_message(self, fmt, *args):  # 降噪：交给 logging 而不是 stderr
        logger.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_text(self, status: int, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        self._send(status, text.encode("utf-8"), content_type)

    def _query(self) -> dict:
        parsed = urlsplit(self.path)
        return {k: v[-1] for k, v in parse_qs(parsed.query).items()}

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise api.ApiError("请求体不是合法 JSON", 400)
        if not isinstance(data, dict):
            raise api.ApiError("请求体必须是 JSON 对象", 400)
        return data

    # -------------------------------------------------------------- 路由

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if method == "GET":
                self._route_get(path)
            else:
                self._route_post(path)
        except api.ApiError as exc:
            self._safe_send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # 兜底：绝不泄露 traceback
            if _is_client_gone(exc):  # 浏览器提前断开：静默（不占日志、不算故障）
                logger.debug("客户端提前断开 %s %s：%s", method, self.path, exc)
                return
            logger.exception("处理 %s %s 失败", method, self.path)
            self._safe_send_json(500, {"error": f"服务内部错误：{exc}"})

    def _safe_send_json(self, status: int, payload: dict) -> None:
        """发 JSON 响应；客户端已经走了就当无事发生（不再刷 ERROR）。"""
        try:
            self._send_json(status, payload)
        except Exception as exc:
            if _is_client_gone(exc):
                logger.debug("响应未发出（客户端已断开）：%s", exc)
            else:
                logger.exception("发送响应失败：%s", exc)

    def _defaults(self, params: dict) -> tuple[str, bool, str]:
        account = params.get("account_id") or self.defaults.get("account_id")
        with_meta = _as_bool(params.get("with_metadata"), bool(self.defaults.get("with_metadata")))
        steam_path = params.get("steam_path") or self.defaults.get("steam_path")
        # 合流库的账号 id 归一化成 "all"，别把 "merged"/"合流" 等别名当真实账号透传下去
        if account and api.merge_core.is_merge_id(account):
            account = api.merge_core.MERGE_ID
        return account, with_meta, steam_path

    def _route_get(self, path: str):
        if path == "/":
            self._send_text(200, _render_dashboard())
            return
        if path == "/static" or path.startswith("/static/"):
            self._serve_static(path)
            return
        if path == "/covers" or path.startswith("/covers/"):
            self._serve_cover(path)
            return
        if path.startswith("/page/"):
            # 产物 HTML 里封面是相对路径 covers/x.jpg，在 /page/<kind> 下会被解析成
            # /page/covers/x.jpg —— 这里把 /page/ 下**非页面名**的请求映射到产物目录，
            # 同时保留双击 HTML 直接打开（相对路径）的能力。
            tail = path[len("/page/"):]
            if tail.split("/", 1)[0] not in api.PAGE_KINDS:
                self._serve_output_asset(tail)
                return
        if path == "/page" or path.startswith("/page/"):
            kind = path.split("/", 2)[2] if path.startswith("/page/") else ""
            params = self._query()
            account, with_meta, steam_path = self._defaults(params)
            html = api.read_page(
                kind,
                account_id=account,
                with_metadata=with_meta,
                steam_path=steam_path,
                # 默认直接用已有产物，避免面板一打开就把六张页面全部重画（~10 秒）
                prefer_existing=not _as_bool(params.get("fresh"), False),
            )
            self._send_text(200, html)
            return
        if path == "/series":
            # 系列详情页：只发模板，数据由页面调 /api/series 取
            self._send_text(200, _render_series_page())
            return
        if path == "/api/accounts":
            params = self._query()
            self._send_json(
                200,
                api.get_accounts(
                    steam_path=params.get("steam_path"),
                    current=params.get("account_id") or self.defaults.get("account_id"),
                ),
            )
            return
        if path == "/api/cover":
            params = self._query()
            _account, _meta, steam_path = self._defaults(params)
            self._send_json(200, api.get_cover(params.get("appid"), steam_path=steam_path))
            return
        if path == "/api/state":
            self._send_json(200, api.state())
            return
        if path == "/api/job":
            # T6：自动补齐任务的进度快照。**纯读内存，绝不联网**（前端 1~2 秒轮询一次）。
            self._send_json(200, api.job_state())
            return
        if path == "/api/log":
            # 只读日志尾巴（限定在 logs/ 目录内，见 api.get_log_tail）
            self._send_json(200, api.get_log_tail(self._query().get("tail")))
            return
        if path == "/api/config":
            self._send_json(200, api.get_config())
            return
        if path == "/api/summary":
            params = self._query()
            account, with_meta, steam_path = self._defaults(params)
            self._send_json(
                200,
                api.get_summary(account_id=account, with_metadata=with_meta, steam_path=steam_path),
            )
            return
        if path == "/api/series":
            params = self._query()
            account, with_meta, steam_path = self._defaults(params)
            self._send_json(
                200,
                api.get_series_detail(
                    params.get("name") or params.get("series") or "",
                    account_id=account,
                    with_metadata=with_meta,
                    steam_path=steam_path,
                    fetch_covers=_as_bool(params.get("covers"), True),
                ),
            )
            return
        if path == "/api/recommend":
            params = self._query()
            account, with_meta, steam_path = self._defaults(params)
            self._send_json(
                200,
                api.get_recommend(
                    account_id=account,
                    mood=params.get("mood") or "随便",
                    time_left=params.get("time") or "不限",
                    genre=params.get("genre") or "不限",
                    top_k=params.get("top_k"),
                    with_metadata=with_meta,
                    steam_path=steam_path,
                ),
            )
            return
        if path == "/api/pages":
            params = self._query()
            account, with_meta, steam_path = self._defaults(params)
            if _as_bool(params.get("only_existing"), False):
                # 首屏秒开：只列**已有**产物，不重新渲染（重新渲染要 +10 秒）
                self._send_json(200, api.list_existing_pages(account_id=account))
                return
            self._send_json(
                200,
                api.list_pages(account_id=account, with_metadata=with_meta, steam_path=steam_path),
            )
            return
        if path == "/api/backfill":
            params = self._query()
            account, with_meta, steam_path = self._defaults(params)
            self._send_json(
                200,
                api.post_backfill(
                    account_id=account,
                    dry_run=_as_bool(params.get("dry_run"), True),
                    overwrite=_as_bool(params.get("overwrite"), False),
                    min_days_ago=int(params.get("min_days_ago") or 1),
                    with_metadata=with_meta,
                    steam_path=steam_path,
                ),
            )
            return
        self._send_json(404, {"error": f"未知路径：{path}"})

    def _route_post(self, path: str):
        if path == "/api/accounts/add":
            data = self._body()
            self._send_json(
                200,
                api.add_accounts(
                    data.get("account_ids") if data.get("account_ids") is not None
                    else data.get("account_id"),
                    steam_path=data.get("steam_path") or self.defaults.get("steam_path"),
                    current=data.get("current") or None,
                ),
            )
            return
        if path == "/api/accounts/remove":
            data = self._body()
            self._send_json(
                200,
                api.remove_account(
                    account_id=str(data.get("account_id") or ""),
                    steam_path=data.get("steam_path") or self.defaults.get("steam_path"),
                    current=data.get("current") or None,
                ),
            )
            return
        if path == "/api/run":
            data = self._body()
            account, with_meta, steam_path = self._defaults(data)
            self._send_json(
                200,
                api.post_run(
                    account_id=account,
                    dry_run=_as_bool(data.get("dry_run"), True),
                    with_metadata=with_meta,
                    steam_path=steam_path,
                ),
            )
            return
        if path == "/api/writeback":
            data = self._body()
            account, _meta, steam_path = self._defaults(data)
            self._send_json(
                200, api.post_writeback(account_id=account, steam_path=steam_path)
            )
            return
        if path == "/api/backfill":
            data = self._body()
            account, with_meta, steam_path = self._defaults(data)
            self._send_json(
                200,
                api.post_backfill(
                    account_id=account,
                    dry_run=_as_bool(data.get("dry_run"), True),
                    overwrite=_as_bool(data.get("overwrite"), False),
                    min_days_ago=int(data.get("min_days_ago") or 1),
                    with_metadata=with_meta,
                    steam_path=steam_path,
                ),
            )
            return
        if path == "/api/config":
            data = self._body()
            key = data.get("key")
            value = data.get("value")
            if value is None:
                raise api.ApiError("缺少 value", 400)
            self._send_json(200, api.set_config(str(key or ""), value))
            return
        if path == "/api/localize":
            data = self._body()
            account, with_meta, steam_path = self._defaults(data)
            self._send_json(
                200,
                api.post_localize(
                    account_id=account,
                    scope=str(data.get("scope") or "both"),
                    limit=data.get("limit") or 20,
                    steam_path=steam_path,
                    with_metadata=bool(data.get("with_metadata") or with_meta),
                ),
            )
            return
        if path == "/api/pages":
            data = self._body()
            account, with_meta, steam_path = self._defaults(data)
            self._send_json(
                200,
                api.list_pages(account_id=account, with_metadata=with_meta, steam_path=steam_path),
            )
            return
        self._send_json(404, {"error": f"未知路径：{path}"})

    def _serve_cover(self, path: str) -> None:
        """GET /covers/<appid>.jpg —— 只服务本机已有的真实封面文件。

        appid 必须是纯数字，天然挡住 ../ 之类的路径穿越。
        """
        raw = path[len("/covers/"):] if path.startswith("/covers/") else ""
        stem = raw.split(".", 1)[0].strip()
        if not stem.isdigit():
            self._send_json(404, {"error": f"封面不存在：{raw or '缺少 appid'}"})
            return
        target = api.cover_for_panel(stem, self.defaults.get("steam_path"))
        if not target or not Path(target).is_file():
            self._send_json(
                404,
                {"error": f"本机还没有 appid {stem} 的封面（可点「抓取中文名与封面」）"},
            )
            return
        try:
            body = Path(target).read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"读取封面失败：{exc}"})
            return
        try:
            from core import artwork

            ctype = artwork.mime_for_cover(target)
        except Exception:
            ctype = "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_output_asset(self, rel: str) -> None:
        """GET /page/<产物目录下的相对路径> —— 只服务 output 目录内的真实文件。

        用于让 /page/<kind> 里 ``covers/x.jpg`` 这样的相对路径也能加载；
        解析后用 relative_to 校验，杜绝 .. 之类的目录穿越。
        """
        rel = (rel or "").strip().lstrip("/")
        root = api._output_dir().resolve()
        if not rel:
            self._send_json(404, {"error": "缺少产物路径"})
            return
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            self._send_json(403, {"error": "非法路径"})
            return
        if not target.is_file():
            self._send_json(404, {"error": f"产物文件不存在：{rel}"})
            return
        try:
            body = target.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"读取产物文件失败：{exc}"})
            return
        _send_bytes(self, 200, body, _MIME.get(target.suffix.lower(), "application/octet-stream"))

    def _serve_static(self, path: str) -> None:
        rel = path[len("/static/"):] if path.startswith("/static/") else ""
        if not rel:
            self._send_json(404, {"error": "缺少静态文件名"})
            return
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_json(403, {"error": "非法路径"})
            return
        if not target.is_file():
            self._send_json(404, {"error": f"静态文件不存在：{rel}"})
            return
        try:
            body = target.read_bytes()
        except OSError as exc:
            self._send_json(500, {"error": f"读取静态文件失败：{exc}"})
            return
        ctype = _MIME.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, body, ctype)


def build_server(
    port: int = DEFAULT_PORT,
    host: str = HOST,
    account_id: str | None = None,
    steam_path: str | None = None,
    with_metadata: bool = False,
) -> ThreadingHTTPServer:
    """构造 HTTP 服务（不启动）。端口被占用时自动顺延。"""
    real_port = pick_port(host, port)
    handler = type("BoundPanelHandler", (PanelHandler,), {
        "defaults": {
            "account_id": account_id,
            "steam_path": steam_path,
            "with_metadata": bool(with_metadata),
        }
    })
    server = ThreadingHTTPServer((host, real_port), handler)
    server.daemon_threads = True
    return server


def run(
    port: int = DEFAULT_PORT,
    account_id: str | None = None,
    steam_path: str | None = None,
    with_metadata: bool = False,
    open_browser: bool = True,
    host: str = HOST,
) -> int:
    """启动面板：监听本机 →（可选）打开浏览器 → 阻塞服务，Ctrl+C 关闭。"""
    try:
        server = build_server(
            port=port,
            host=host,
            account_id=account_id,
            steam_path=steam_path,
            with_metadata=with_metadata,
        )
    except RuntimeError as exc:
        print(f"[panel] {exc}")
        return 1

    real_port = server.server_address[1]
    url = f"http://{host}:{real_port}/"
    print("=" * 52)
    print("  GameLife 本地网页面板")
    print(f"  面板已启动：{url}")
    if real_port != int(port):
        print(f"  （端口 {port} 被占用，已自动改用 {real_port}）")
    if account_id:
        print(f"  默认账号：{account_id}")
    print("  在浏览器里操作全部功能；关闭面板请在本窗口按 Ctrl+C")
    print("=" * 52)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # 打不开浏览器不影响服务
            logger.warning("自动打开浏览器失败：%s", exc)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[panel] 已关闭面板（Ctrl+C）")
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        server.server_close()
    return 0
