"""Steam 客户端**实时归属**源（T22 线）——「这款游戏到底归谁」的唯一权威答案。

用户反馈（实测，不是推理）
--------------------------
静态痕迹（``apptickets`` / ``librarycache`` / 本机 ``userdata``）**分辨不了**这两类条目：

* 本账号**买过、却从没安装过**的游戏 —— 真拥有，本机却一条痕迹都没有；
* 家庭组**远端成员**共享给本账号的游戏 —— 本机同样什么都没留下。

于是本机 ``10000001`` 的 467 条库索引里有 200 多条其实归家庭组别人
（``17480 命令与征服：红色警戒 3`` / ``6850 Hitman 2: Silent Assassin`` …），
被 ``core.ownership.library_owned_appids`` 当成自有 —— 用户原话「命令与征服系列、
杀手系列等等都不对」。反方向同样错：``1238840 战地1`` / ``386360 神之浩劫`` 真拥有、
只是没玩过，任何「本机痕迹闸门」都会把它们误杀。

数据在哪
--------
Steam 客户端（``steamwebhelper.exe``）**默认**带 ``--remote-debugging-port=8080``
（本机实测：进程命令行里就有，用户不需要加任何启动参数），CEF 的
``SharedJSContext`` 因此暴露一个 CDP（Chrome DevTools Protocol）端点。通过它直接调用
客户端自己的 ``CFamilyGroups_GetSharedLibraryApps``，拿到**每个 appid 的
``owner_steamids``**：这是 Steam 给出的归属，不存在推测，也不用硬编码任何 appid。

判据（一句话）
--------------
``owner_steamids`` 含本账号 -> 真拥有；不含 -> **绝不拥有**（家庭库 / 别人名下）。

只对「当前登录的账号」有效？
----------------------------
客户端只会回答**当前登录账号**所属家庭组的清单（问别的 steamid 会返回空），
所以实时结果**按家庭组整份落盘**（``data/steam_live_ownership.json``）：一次成功，
家庭组里每个成员（含以后换号登录的）都能复用，Steam 没开着时也一样。
客户端答的不是被问的账号 / 清单里没有这个账号 -> 一律**拒收**（宁缺勿错）。

降级与安全
----------
* Steam 没运行 / 端口不通 / 不在任何家庭组 / 拒收 -> 返回 ``None``，调用方**保持
  原判定**，绝不因为读不到而改判；
* 只连 ``127.0.0.1``（不走系统代理）、只读（只调两个查询接口，不写任何 Steam 文件）、
  绝不抛异常；
* 测试 / 排障：``GAMELIFE_STEAM_LIVE=0`` 关闭本模块，``GAMELIFE_STEAM_LIVE_PORT``
  覆盖端口。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# SteamID64 ↔ account_id（Steam32）的固定偏移（与 core.ownership.STEAM64_BASE 同源）
STEAM64_BASE = 76561197960265728
# Steam 客户端 CEF 远程调试端口默认值（实测 steamwebhelper 命令行里就是它）
DEFAULT_PORT = 8080
# 环境变量：改端口 / 关掉实时源（测试与排障用）
ENV_PORT = "GAMELIFE_STEAM_LIVE_PORT"
ENV_DISABLE = "GAMELIFE_STEAM_LIVE"
# 落盘缓存（与 data/accounts.json 同目录）
CACHE_NAME = "steam_live_ownership.json"
CACHE_VERSION = 2
# 超时：连得上才值得等，``GetSharedLibraryApps`` 本机实测约 3 秒
CONNECT_TIMEOUT = 2.0
EVAL_TIMEOUT = 20.0
# 关闭开关的取值
_OFF_VALUES = frozenset({"0", "false", "off", "no", "disable", "disabled"})

_LOCK = threading.Lock()
# (账号, 端口) -> 快照 | None（进程内只问一次，绝不反复连端口）
_MEMO: dict[tuple, "dict | None"] = {}

# 在 Steam 客户端的 SharedJSContext 里执行：
#   1. 从 webpack 模块注册表里**按能力**找出家庭组服务（不依赖任何写死的模块号，
#      Steam 每次更新构建号都会重排模块 id）；
#   2. 问出该 steamid 的 family_groupid；
#   3. 调 GetSharedLibraryApps 拿全量清单（含自己 / 含被排除项 / 含非游戏）。
# 只做查询，不写任何状态；返回值一律是 JSON 字符串（returnByValue）。
_JS = r"""
(async () => {
  const SID = "__STEAM_ID__";
  const fail = (m) => JSON.stringify({ok: false, error: String(m)});
  try {
    const wc = window.webpackChunksteamui;
    if (!wc || typeof wc.push !== "function") return fail("no webpack chunk");
    let req = null;
    wc.push([[Symbol("gamlife")], {}, function (m) { req = m; }]);
    if (!req || !req.m) return fail("no module registry");
    const pick = () => {
      for (const id in req.m) {
        let src; try { src = String(req.m[id]); } catch (e) { continue; }
        if (src.indexOf("GetSharedLibraryApps") < 0) continue;
        let mod; try { mod = req(id); } catch (e) { continue; }
        if (!mod || typeof mod !== "object") continue;
        for (const key in mod) {
          const v = mod[key];
          if (v && typeof v === "object" && typeof v.GetSharedLibraryApps === "function"
              && typeof v.GetFamilyGroupForUser === "function") return v;
        }
      }
      return null;
    };
    const ex = pick();
    if (!ex) return fail("family service not found");
    const cm = window.appStore && window.appStore.m_cm;
    const tr = cm && cm.GetServiceTransport && cm.GetServiceTransport();
    if (!tr) return fail("no service transport");
    const fg = (await ex.GetFamilyGroupForUser(tr, {steamid: SID}, () => {})).Body().toObject();
    const gid = fg && fg.family_groupid;
    if (!gid) return JSON.stringify({ok: true, group: null, owner: "", apps: {}});
    const resp = (await ex.GetSharedLibraryApps(tr, {
      family_groupid: String(gid), include_own: true, include_excluded: true,
      include_non_games: true, language: "schinese", max_apps: 5000,
    }, () => {})).Body().toObject();
    const apps = {};
    for (const a of (resp.apps || [])) {
      apps[String(a.appid)] = (a.owner_steamids || []).map(String);
    }
    return JSON.stringify({ok: true, group: String(gid),
                           owner: String(resp.owner_steamid || ""), apps: apps});
  } catch (e) { return fail(e && e.message ? e.message : e); }
})()
"""


def _off() -> bool:
    """``GAMELIFE_STEAM_LIVE=0`` 时整个模块短路（测试与排障用）。"""
    return (os.environ.get(ENV_DISABLE) or "").strip().lower() in _OFF_VALUES


def _port() -> int:
    text = (os.environ.get(ENV_PORT) or "").strip()
    try:
        value = int(text)
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return value if 0 < value < 65536 else DEFAULT_PORT


def _clean(account_id: Any) -> "str | None":
    """账号 id 规整：非空且全数字才认（合流库 ``all`` -> ``None``）。"""
    text = str(account_id or "").strip()
    return text if text.isdigit() else None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _account_of(steam_id: Any) -> "int | None":
    """SteamID64 -> account_id；非法返回 ``None``。"""
    try:
        value = int(steam_id)
    except (TypeError, ValueError):
        return None
    if value <= STEAM64_BASE:
        return None
    return value - STEAM64_BASE


def _owners_of(owners: Any) -> "set[int]":
    """一条清单项里的 owner 集合（**统一用 account_id 表示**）。"""
    found: set[int] = set()
    for item in owners if isinstance(owners, (list, tuple)) else ():
        value = _int(item)
        if value > 0:
            found.add(value)
    return found


def _members(apps: Any) -> "set[int]":
    """清单里出现过的全部 owner 账号 id（= 这个家庭组的成员）。"""
    found: set[int] = set()
    for owners in (apps or {}).values() if isinstance(apps, dict) else ():
        found |= _owners_of(owners)
    return found


def _view(account: Any, apps: Any, *, at: int, source: str, group: str) -> "dict | None":
    """把「appid -> owner 列表」折算成某个账号的视角；它不在清单里就返回 ``None``。"""
    mine = _int(account)
    if mine <= 0 or not isinstance(apps, dict):
        return None
    if mine not in _members(apps):
        # 这个账号不是该家庭组成员（或客户端答的是别人）—— 宁缺勿错
        return None
    owned: set[int] = set()
    others: set[int] = set()
    for key, owners in apps.items():
        appid = _int(key)
        if appid <= 0:
            continue
        ids = _owners_of(owners)
        if mine in ids:
            owned.add(appid)
        else:
            # 一条 owner 都没有 = Steam 明确标成「不属于任何人」（非游戏条目），
            # 同样**绝不能**算成自己的。
            others.add(appid)
    if not owned and not others:
        return None
    return {"owned": frozenset(owned), "others": frozenset(others),
            "at": int(at), "source": source, "group": str(group)}


# ------------------------------------------------------------------ 极简 CDP 客户端

def _handshake(sock: socket.socket, host: str, port: str, path: str) -> bytes:
    """WebSocket 升级握手；返回握手时多读到的那截字节。"""
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
    )
    sock.sendall(request.encode())
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("handshake closed")
        buffer += chunk
    head, _, rest = buffer.partition(b"\r\n\r\n")
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError("handshake failed: %r" % head[:120])
    return rest


class _DebuggerClient:
    """够用就好的 WebSocket 客户端：发一条 ``Runtime.evaluate``、收回它的结果。

    只依赖标准库；不处理分片（CDP 的回复都是单帧）、自动回 pong、忽略事件消息。
    """

    def __init__(self, url: str, timeout: float) -> None:
        rest = url[5:] if url.startswith("ws://") else url
        host_port, _, path = rest.partition("/")
        host, _, port_text = host_port.partition(":")
        self._sock = socket.create_connection((host, int(port_text or 80)), timeout)
        self._sock.settimeout(timeout)
        self._buffer = _handshake(self._sock, host, port_text, "/" + path)
        self._msgid = 0

    # -- 收发 ---------------------------------------------------------------
    def _recv_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise RuntimeError("connection closed")
            self._buffer += chunk
        head, self._buffer = self._buffer[:size], self._buffer[size:]
        return head

    def _read_frame(self) -> "tuple[int, bytes]":
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        payload = self._recv_exact(length)
        if opcode == 0x9:                       # ping -> pong
            self._send_frame(payload, 0xA)
            return self._read_frame()
        if opcode == 0x8:
            raise RuntimeError("server closed")
        return opcode, payload

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", size)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def _call(self, method: str, params: dict, timeout: float) -> dict:
        self._msgid += 1
        wanted = self._msgid
        self._sock.settimeout(timeout)
        self._send_frame(json.dumps({"id": wanted, "method": method, "params": params}).encode())
        while True:
            opcode, payload = self._read_frame()
            if opcode != 0x1:
                continue
            try:
                message = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            if message.get("id") == wanted:
                return message

    # -- 对外 ---------------------------------------------------------------
    def evaluate(self, expression: str, timeout: float) -> Any:
        """执行 JS 并取回值（``awaitPromise``）；异常一律抛给调用方。"""
        reply = self._call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout,
        )
        result = reply.get("result") or {}
        if "exceptionDetails" in result:
            raise RuntimeError((result["exceptionDetails"] or {}).get("text") or "js error")
        return (result.get("result") or {}).get("value")

    def close(self) -> None:
        try:
            self._send_frame(b"", 0x8)
        except Exception:                       # 关闭失败无所谓
            pass
        try:
            self._sock.close()
        except Exception:
            pass


def _targets(port: int, timeout: float) -> list:
    """CDP 的 ``/json/list``；**绕开系统代理**，只连本机。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = "http://127.0.0.1:%d/json/list" % port
    with opener.open(url, timeout=timeout) as handle:
        data = json.loads(handle.read().decode("utf-8", "replace"))
    return data if isinstance(data, list) else []


def _evaluate(port: int, expression: str, timeout: float) -> Any:
    for target in _targets(port, CONNECT_TIMEOUT):
        if not isinstance(target, dict):
            continue
        if str(target.get("title") or "") != "SharedJSContext":
            continue
        url = str(target.get("webSocketDebuggerUrl") or "")
        if not url.startswith("ws://"):
            continue
        client = _DebuggerClient(url, timeout)
        try:
            return client.evaluate(expression, timeout)
        finally:
            client.close()
    return None


# ------------------------------------------------------------------ 解析

def _parse(account: str, steam_id: str, raw: Any) -> "dict | None":
    """把客户端返回的 JSON 解析成「家庭组清单 + 本账号视角」；异常一律当作信息不足。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.debug("Steam 客户端返回的不是 JSON（降级）")
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        logger.debug("Steam 客户端无法回答归属查询（降级）：%s",
                     payload if isinstance(payload, dict) else str(raw)[:160])
        return None
    if not payload.get("group"):
        return None                             # 不在任何家庭组：没有家庭清单可用
    owner = str(payload.get("owner") or "")
    apps = payload.get("apps")
    if not isinstance(apps, dict):
        return None
    # 客户端给的是 SteamID64，这里统一折算成 account_id（缓存与内存同一套表示）
    normalized: dict[str, list[int]] = {}
    for key, owners in apps.items():
        appid = _int(key)
        if appid <= 0:
            continue
        normalized[str(appid)] = sorted(
            value for value in (_account_of(item) for item in (owners or ())) if value
        )
    view = _view(account, normalized, at=time.time(), source="live",
                 group=str(payload.get("group")))
    if view is None:
        if owner and owner != steam_id:
            logger.debug("Steam 客户端答的是别的账号（%s），本次拒收（降级）", owner)
        else:
            logger.debug("Steam 清单里没有账号 %s，本次拒收（降级）", account)
        return None
    view["apps"] = normalized
    return view


# ------------------------------------------------------------------ 落盘缓存

def _cache_path() -> Path:
    try:
        from core import merge

        return Path(merge.data_dir()) / CACHE_NAME
    except Exception:                           # 兜底：不制造 import 期副作用
        return Path(__file__).resolve().parent.parent / "data" / CACHE_NAME


def _read_cache_file() -> dict:
    """``{"groups": {gid: {"at":…, "apps": {appid: [owner…]}}}, "accounts": {id: {"group": gid}}}``。"""
    try:
        raw = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
        raw = {}                                # 缺失 / 版本不符 -> 空缓存（不抛异常）
    groups = raw.get("groups") if isinstance(raw.get("groups"), dict) else {}
    accounts = raw.get("accounts") if isinstance(raw.get("accounts"), dict) else {}
    return {"groups": groups, "accounts": accounts}


def _write_cache_file(data: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"version": CACHE_VERSION, "updated_at": int(time.time()),
                        "groups": data.get("groups") or {},
                        "accounts": data.get("accounts") or {}},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        logger.debug("写入 Steam 实时归属缓存失败（忽略）：%s", exc)


def _store_live(account: str, view: dict) -> None:
    """把整份家庭组清单落盘：组内任何成员以后都能离线复用。"""
    data = _read_cache_file()
    group = str(view.get("group") or "")
    if group:
        apps = view.get("apps") if isinstance(view.get("apps"), dict) else {}
        data["groups"][group] = {
            "at": int(view.get("at") or 0),
            "apps": {str(key): sorted(_owners_of(owners))
                     for key, owners in apps.items() if _int(key) > 0},
        }
    data["accounts"][str(account)] = {"group": group, "at": int(view.get("at") or 0)}
    _write_cache_file(data)


def _from_cache(account: str) -> "dict | None":
    """落盘缓存里找这个账号所属家庭组的整份清单（先试记录过的组，再试所有组）。

    ``_view`` 自带「账号必须是该组成员」的闸门，所以遍历所有已缓存家庭组也**不会**
    把别的家庭的清单错套到这个账号头上。
    """
    data = _read_cache_file()
    entry = data["accounts"].get(str(account))
    order: list[str] = []
    if isinstance(entry, dict) and str(entry.get("group") or ""):
        order.append(str(entry["group"]))
    order.extend(gid for gid in data["groups"] if gid not in order)
    for group in order:
        snapshot = data["groups"].get(group)
        if not isinstance(snapshot, dict):
            continue
        apps = snapshot.get("apps")
        if not isinstance(apps, dict):
            continue
        view = _view(account, apps, at=_int(snapshot.get("at")), source="cache", group=str(group))
        if view is not None:
            return view
    return None


# ------------------------------------------------------------------ 对外接口

def fetch(account_id: Any, *, port: "int | None" = None,
          timeout: "float | None" = None) -> "dict | None":
    """**直接问 Steam 客户端**要这个账号的权威归属；Steam 没开 / 问不到返回 ``None``。"""
    account = _clean(account_id)
    if account is None or _off():
        return None
    steam_id = str(STEAM64_BASE + int(account))
    expression = _JS.replace("__STEAM_ID__", steam_id)
    try:
        raw = _evaluate(port or _port(), expression, timeout or EVAL_TIMEOUT)
    except Exception as exc:                    # 端口不通 / 超时 / 客户端换了接口
        logger.debug("连接 Steam 客户端调试端口失败（降级）：%s", exc)
        return None
    return _parse(account, steam_id, raw)


def ownership_for(account_id: Any, *, refresh: bool = True,
                  port: "int | None" = None,
                  timeout: "float | None" = None) -> "dict | None":
    """本账号的权威归属快照；``None`` = 信息不足。

    返回 ``{"owned": frozenset[int], "others": frozenset[int], "at": int,
    "source": "live"|"cache", "group": str}``：

    * ``owned``  —— Steam 自己说「这些归本账号」（真拥有，含从未安装 / 免费游戏）；
    * ``others`` —— Steam 自己说「这些归**别人**」（本账号绝不拥有）。

    先问实时源（成功后整份落盘），问不到就退回落盘缓存。整个进程内只问一次。
    """
    account = _clean(account_id)
    if account is None or _off():
        return None
    key = (account, port or _port())
    with _LOCK:
        if key in _MEMO:
            return _MEMO[key]
    result = fetch(account, port=port, timeout=timeout) if refresh else None
    if result is not None:
        _store_live(account, result)
    else:
        result = _from_cache(account)
    with _LOCK:
        _MEMO[key] = result
    return result


def reset_cache() -> None:
    """清空进程内记忆（测试用）；不影响落盘缓存。"""
    with _LOCK:
        _MEMO.clear()


def cache_path() -> Path:
    """落盘缓存的位置（排障 / 面板展示用）。"""
    return _cache_path()
