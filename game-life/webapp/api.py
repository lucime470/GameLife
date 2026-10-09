"""本地网页面板的 API 层（把既有引擎能力封装成可 JSON 化的 dict）。

设计要点：
    * 业务逻辑一律复用 core / engine / render / main 里**已经存在**的函数，
      本模块只做「调用 + 组装 + 降级」，不重写任何判定/统计规则；
    * 所有对外函数出错时抛 :class:`ApiError`（带 HTTP 状态码），由 server.py
      转成 ``{"error": "..."}``——绝不会把 Python traceback 漏给前端；
    * 面板默认**不联网**：游戏名/类型取自本地 games 缓存（render_bridge.fill_metadata），
      只有用户显式勾选「联网抓取」时才走 Store 抓取。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import main as cli  # noqa: E402  复用 _load_library / _stable_output_dir / 写回与配置编排
from core import merge as merge_core  # noqa: E402  合流库（多个账号当作一个账号统计）

logger = logging.getLogger("game-life.webapp")

# 面板支持的 6 张可视化页面（kind 与 CLI 子命令同名）
PAGE_KINDS = ("report", "graveyard", "starmap", "timeline", "today", "checkup")

PAGE_LABELS = {
    "report": "年度报告",
    "graveyard": "游戏墓园",
    "starmap": "游戏星图",
    "timeline": "考古时间轴",
    "today": "时光机 · 去年今日",
    "checkup": "游戏体检",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

_CAPTURE_LOCK = threading.RLock()
_LIB_CACHE: dict[tuple, tuple[float, dict]] = {}
_LIB_TTL = 20.0


class ApiError(Exception):
    """带 HTTP 状态码的可展示错误。"""

    def __init__(self, message: Any, status: int = 400) -> None:
        super().__init__(str(message))
        self.message = str(message)
        self.status = int(status)


# ------------------------------------------------------------------ 通用工具

@contextlib.contextmanager
def _capture_stdout():
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        yield buf
    finally:
        sys.stdout = old


def _capture(fn):
    """执行 fn()，返回 (结果, 捕获到的控制台文本)；可重入（RLock）。"""
    with _CAPTURE_LOCK:
        with _capture_stdout() as buf:
            result = fn()
        return result, buf.getvalue()


def _strip_comments(node: Any) -> Any:
    """递归去掉 rules.json 里以下划线开头的说明键（_comment 等）。"""
    if isinstance(node, dict):
        return {k: _strip_comments(v) for k, v in node.items() if not str(k).startswith("_")}
    if isinstance(node, list):
        return [_strip_comments(v) for v in node]
    return node


def _output_dir() -> Path:
    """渲染产物目录：打包时用 exe 附近的稳定目录，源码运行用 game-life/output。"""
    stable = cli._stable_output_dir()
    if stable is not None:
        return Path(stable)
    return APP_ROOT / "output"


def _appid_set(play_stats) -> set[int]:
    out: set[int] = set()
    for appid in (play_stats or {}):
        try:
            out.add(int(appid))
        except (TypeError, ValueError):
            continue
    return out


# ------------------------------------------------------------------ 账号「已添加」模型

# 用户第 3 项：面板**不再自动把我本机所有账号塞进下拉框**，改成「先添加再用」。
# 只把用户主动添加的账号持久化到 data/accounts.json；本机 userdata 目录下的
# 任何文件都不会被改动、更不会被删除（「移除」只是从列表里拿掉）。
ADDED_ACCOUNTS_NAME = "accounts.json"
HIDDEN_ACCOUNTS_NAME = "hidden_accounts.json"   # 旧模型（只用于一次性迁移）
_ACCOUNTS_LOCK = threading.RLock()


def _stable_data_dir() -> Path:
    """运行期读写数据的真实 data 目录（与 core/engine 同一口径）。

    源码运行 = ``game-life/data``；onefile 打包后 ``__file__`` 指向进程退出即删的
    ``sys._MEIPASS``，若直接拼 ``APP_ROOT / "data"``，用户添加的账号、写回状态等
    会写进临时目录并随进程消失。:func:`data.schema.data_dir` 已按 frozen 解析到
    exe 附近的真实 data 目录。
    """
    try:
        from data import schema
        return Path(schema.data_dir())
    except Exception:  # 兜底：退回源码布局，不制造 import 期副作用
        return APP_ROOT / "data"


def accounts_path() -> Path:
    """已添加账号列表位置（真实 game-life/data/accounts.json，兼容打包运行）。"""
    return _stable_data_dir() / ADDED_ACCOUNTS_NAME


def hidden_accounts_path() -> Path:
    """旧「隐藏账号」文件位置（仅供迁移读取）。"""
    return _stable_data_dir() / HIDDEN_ACCOUNTS_NAME


def _read_json_file(target: Path, default):
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("读取 %s 失败，按默认值处理：%s", target.name, exc)
        return default


def load_added_accounts() -> list[dict]:
    """读取「已添加」账号列表：[{id, name, added_at}]；缺失/损坏一律当空列表。"""
    with _ACCOUNTS_LOCK:
        raw = _read_json_file(accounts_path(), None)
    if raw is None:
        return _migrate_legacy_accounts()
    if isinstance(raw, dict):
        raw = raw.get("accounts")
    entries: list[dict] = []
    seen: set[str] = set()
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, dict):
                aid = str(item.get("id") or "").strip()
                name = str(item.get("name") or "").strip()
                added_at = str(item.get("added_at") or "").strip()
            else:
                aid, name, added_at = str(item or "").strip(), "", ""
            if aid and aid not in seen:
                seen.add(aid)
                entries.append({"id": aid, "name": name, "added_at": added_at})
    return entries


def save_added_accounts(entries) -> list[dict]:
    """写入「已添加」列表（UTF-8 无 BOM + 原子替换）。"""
    clean: list[dict] = []
    seen: set[str] = set()
    for item in entries or []:
        if isinstance(item, dict):
            aid = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            added_at = str(item.get("added_at") or "").strip()
        else:
            aid, name, added_at = str(item or "").strip(), "", ""
        if not aid or aid in seen:
            continue
        seen.add(aid)
        clean.append({"id": aid, "name": name, "added_at": added_at})
    target = accounts_path()
    payload = json.dumps({"accounts": clean}, ensure_ascii=False, indent=2) + "\n"
    with _ACCOUNTS_LOCK:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            raise ApiError(f"保存账号列表失败：{exc}", 500) from exc
    return clean


def _migrate_legacy_accounts() -> list[dict]:
    """老版本「隐藏账号」模型 -> 新「已添加」模型的一次性迁移。

    老语义：下拉框显示「全部本机账号 − 被隐藏的」。只有当 hidden_accounts.json
    **存在且确实隐藏了账号**时才迁移（说明用户手动整理过，不能丢配置）；否则
    返回空列表，走「首次使用 → 引导添加」。
    """
    legacy = _read_json_file(hidden_accounts_path(), None)
    if legacy is None:
        return []
    ids = legacy.get("hidden") if isinstance(legacy, dict) else legacy
    hidden = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not hidden:
        return []
    try:
        local = _scan_local_accounts(None)
    except ApiError:
        return []
    hidden_set = set(hidden)
    entries = [
        {"id": a["id"], "name": a["name"], "added_at": ""}
        for a in local if a["id"] not in hidden_set
    ]
    if entries:
        logger.info("已把旧「隐藏账号」配置迁移为「已添加」账号：%d 个", len(entries))
        save_added_accounts(entries)
    return entries


def _scan_local_accounts(steam_path: str | None) -> list[dict]:
    """扫描本机 userdata（**只读**），返回全部候选账号 [{id, name}]。"""
    from core import locator

    if not steam_path:
        try:
            steam_path = locator.find_steam_path()
        except FileNotFoundError as exc:
            raise ApiError(
                f"{exc}（可用 --steam-path 指定安装目录）", 503
            ) from exc
    accounts: list[dict] = []
    try:
        for acc in locator.list_user_accounts(steam_path):
            accounts.append({
                "id": str(acc.get("id") or ""),
                "name": str(acc.get("name") or ""),
            })
    except FileNotFoundError as exc:
        raise ApiError(str(exc), 503) from exc
    except Exception as exc:  # 解析异常不应让面板整体不可用
        logger.warning("读取账号列表失败：%s", exc)
    return accounts


def accounts_view(steam_path: str | None = None, current: str | None = None) -> dict:
    """账号总览（下拉框 / 管理面板 / 添加对话框都用这一份数据）。

    - ``accounts``：**已添加**的账号（下拉框只显示这些）；
    - ``available``：本机还没添加的账号（「添加账号」对话框里可勾选）；
    - ``needs_setup``：一个都没添加时为 True，前端据此弹出引导；
    - ``suggested``：启动参数 --account-id 指定但尚未添加的账号，供用户一键确认。
    """
    local = _scan_local_accounts(steam_path)
    by_id = {a["id"]: a for a in local}
    added = load_added_accounts()

    # 名称以本机最新为准（用户可能改过 Steam 昵称）
    accounts: list[dict] = []
    for item in added:
        fresh = by_id.get(item["id"])
        accounts.append({
            "id": item["id"],
            "name": (fresh or {}).get("name") or item.get("name") or item["id"],
            "added_at": item.get("added_at") or "",
            "present": fresh is not None,   # 本机 userdata 里是否还找得到
        })

    added_ids = {a["id"] for a in accounts}
    available = [a for a in local if a["id"] not in added_ids]

    # 启动参数指定的账号：没添加过就只作为「建议项」返回给前端（前端提示用户在
    # 「添加账号」里手动加入），**绝不**自动写进 accounts.json —— 首次打开必须是空列表。
    suggested = None
    startup = str(current or "").strip()
    if startup and startup in by_id and startup not in added_ids:
        suggested = {"id": startup, "name": by_id[startup]["name"]}
        logger.debug("--account-id %s 尚未添加，仅作为建议项提示（未自动添加）", startup)

    # 没添加任何账号时 current 必须是空串：不回退去扫本机账号，避免下拉框拿到
    # 一个「用户并未添加」的 id。
    chosen = startup if startup in added_ids else ""
    if not chosen and accounts:
        chosen = _pick_default_account(steam_path, accounts) or accounts[0]["id"]
    # 合流库：把「已添加」的账号当作**同一个账号**统一统计。至少两个账号才有意义
    # （一个账号时它跟单独看那个账号完全一样），故用可选哨兵项呈现，默认不选中。
    merged_option = None
    if len(accounts) >= 2:
        merged_option = {
            "id": merge_core.MERGE_ID,
            "name": merge_core.merge_display_name(len(accounts)),
            "member_ids": [a["id"] for a in accounts],
            "member_names": [a.get("name") or a["id"] for a in accounts],
        }
    return {
        "steam_path": str(steam_path or ""),
        "accounts": accounts,
        "available": available,
        "current": chosen,
        "total_accounts": len(local),
        "needs_setup": not accounts,
        "suggested": suggested,
        "merged": merged_option,
    }


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def add_accounts(account_ids, steam_path: str | None = None,
                 current: str | None = None) -> dict:
    """把一个或多个本机账号加入「已添加」列表（只写 accounts.json）。

    只允许添加**本机 userdata 里真实存在**的账号；绝不改动 Steam 文件。
    """
    if isinstance(account_ids, str):
        wanted = [account_ids]
    elif isinstance(account_ids, (list, tuple, set)):
        wanted = list(account_ids)
    else:
        wanted = []
    wanted = [str(x or "").strip() for x in wanted if str(x or "").strip()]
    if not wanted:
        raise ApiError("请至少选择一个要添加的账号", 400)

    local = {a["id"]: a for a in _scan_local_accounts(steam_path)}
    added = load_added_accounts()
    have = {a["id"] for a in added}
    missing = [aid for aid in wanted if aid not in local]
    if missing:
        raise ApiError(f"账号 {'、'.join(missing)} 不在本机 userdata 中，无法添加", 404)

    fresh = list(added)
    newly_added: list[str] = []
    for aid in wanted:
        if aid in have:
            continue
        fresh.append({"id": aid, "name": local[aid]["name"], "added_at": _now_iso()})
        have.add(aid)
        newly_added.append(aid)
    if len(fresh) != len(added):
        save_added_accounts(fresh)

    result = accounts_view(steam_path, current=current)
    names = "、".join(local[aid]["name"] or aid for aid in wanted)
    result.update({
        "ok": True, "action": "add", "changed": wanted,
        "message": f"已添加账号：{names}（Steam 里的数据未做任何改动）",
    })
    # T6：账号进列表后**自动**把中文名 / 封面 / 价格 / 系列目录补齐（后台跑，不阻塞这次响应）。
    # 只对本次真正新增的账号入队；入队本身只碰内存，失败一律降级成「用户可手动点按钮」。
    for aid in newly_added:
        try:
            enqueue_localize(aid, steam_path=steam_path, reason="添加账号")
        except Exception as exc:
            logger.warning("自动补齐入队失败（已忽略）：%s", exc)
    return result


def remove_account(account_id: str, steam_path: str | None = None,
                   current: str | None = None) -> dict:
    """从「已添加」列表移除一个账号（**只改 accounts.json**，不删 Steam 文件）。"""
    aid = str(account_id or "").strip()
    if not aid:
        raise ApiError("缺少 account_id", 400)
    added = load_added_accounts()
    kept = [a for a in added if a["id"] != aid]
    if len(kept) != len(added):
        save_added_accounts(kept)
    result = accounts_view(steam_path, current=current)
    label = next((a.get("name") for a in added if a["id"] == aid), aid) or aid
    result.update({
        "ok": True, "action": "remove", "changed": aid,
        "message": f"已把账号「{label}」从列表移除（Steam 里的文件与存档都还在）",
    })
    return result


def get_accounts(steam_path: str | None = None, current: str | None = None) -> dict:
    """兼容旧名字：等价于 :func:`accounts_view`。"""
    return accounts_view(steam_path=steam_path, current=current)


# ------------------------------------------------------------------ 非游戏条目

# 用户第 7 项：Steam Client / Steam Screenshots / Steam Input Configs 这类条目
# 不是游戏，不该出现在游戏列表、覆盖率或推荐里。
# 判定口径已收敛到 core.non_game（全产品唯一口径）；这里保留 is_non_game 这个名字
# 作为薄转发，别让既有调用点炸。
from core import non_game  # noqa: E402

NON_GAME_APPIDS = non_game.NON_GAME_APPIDS
NON_GAME_TYPES = non_game.NON_GAME_TYPES


def is_non_game(appid) -> bool:
    """薄转发到 :func:`core.non_game.is_non_game`（全产品唯一口径）。

    保留这个名字是为了不让既有调用点炸；判定逻辑**只在 core.non_game 一处**。
    """
    return non_game.is_non_game(appid)


def exclude_non_games(play_stats) -> dict:
    """薄转发到 :func:`core.non_game.exclude_non_games`。"""
    return non_game.exclude_non_games(play_stats)


# ------------------------------------------------------------------ 账号与库

def _namespace(steam_path=None, account_id=None, **extra) -> SimpleNamespace:
    ns = SimpleNamespace(steam_path=steam_path or None, account_id=account_id or None)
    # 合流库的账号 id 归一化成 core.merge.MERGE_ID，避免把 "合流"/"merged" 等别名
    # 当成真实账号 id 透传下去。
    if account_id and merge_core.is_merge_id(account_id):
        ns.account_id = merge_core.MERGE_ID
    for key, value in extra.items():
        setattr(ns, key, value)
    return ns


def _pick_default_account(steam_path: str | None = None,
                          accounts: list[dict] | None = None) -> str:
    """自动挑主账号：userdata/<id>/config/localconfig.vdf 最大的那个（玩得最多）。

    多账号机器上 CLI 会交互式提问，面板不能因此卡住/报错，所以这里直接按体量选。
    """
    from core import locator

    if not steam_path:
        try:
            steam_path = locator.find_steam_path()
        except Exception:
            return ""
    ids = [str(a.get("id") or "") for a in (accounts or []) if a.get("id")]
    if not ids:
        try:
            ids = [str(a.get("id") or "") for a in locator.list_user_accounts(steam_path)]
        except Exception:
            return ""
    best, best_size = "", -1
    for account in ids:
        config = Path(str(steam_path)) / "userdata" / account / "config" / "localconfig.vdf"
        try:
            size = config.stat().st_size if config.is_file() else 0
        except OSError:
            size = 0
        if size > best_size:
            best, best_size = account, size
    return best


def load_library(
    account_id: str | None = None,
    with_metadata: bool = False,
    steam_path: str | None = None,
    use_cache: bool = True,
) -> dict:
    """加载一个账号的库数据（play_stats / games_meta / statuses / classified）。

    复用 main._load_library；随后用 render_bridge.fill_metadata 从**本地缓存**
    补齐游戏名/类型（只读、不联网）。同参数 20 秒内直接复用结果，面板刷新不至于卡。

    ``account_id`` 传 ``all``（或 ``merged`` / ``合流``）时是**合流库**：把面板里
    已添加的账号当作同一个账号统一统计，元数据里带 ``merged`` 标记与逐账号小计。
    """
    if not account_id:
        account_id = _pick_default_account(steam_path) or None
    elif merge_core.is_merge_id(account_id):
        account_id = merge_core.MERGE_ID
    key = (str(steam_path or ""), str(account_id or ""), bool(with_metadata))
    now = time.time()
    if use_cache:
        hit = _LIB_CACHE.get(key)
        if hit and now - hit[0] < _LIB_TTL:
            return hit[1]

    ns = _namespace(steam_path=steam_path, account_id=account_id, with_metadata=bool(with_metadata))
    loaded, captured = _capture(
        lambda: cli._load_library(ns, with_metadata=bool(with_metadata))
    )
    if loaded is None:
        message = captured.strip().splitlines()
        raise ApiError(message[-1] if message else "读取 Steam 库失败", 500)

    # 兼容只返回 6 元的旧式加载器（既有测试桩如此）：ownership 退化为空说明
    if len(loaded) >= 7:
        play_stats, games_meta, statuses, classified, real_path, real_account, ownership = loaded
    else:
        play_stats, games_meta, statuses, classified, real_path, real_account = loaded
        ownership = {}
    try:
        from core import render_bridge
        games_meta = render_bridge.fill_metadata(games_meta, play_stats)
    except Exception as exc:  # 缓存补名失败不影响主流程
        logger.warning("本地元数据补全失败，已忽略：%s", exc)

    data = {
        "steam_path": str(real_path),
        "account_id": str(real_account),
        "play_stats": play_stats or {},
        "games_meta": games_meta or {},
        "statuses": statuses or {},
        "classified": classified,
        "named": sum(1 for m in (games_meta or {}).values() if str(m.get("name") or "").strip()),
        # 授权归属过滤的自述信息（core.ownership 是唯一口径；这里只是转发）
        "ownership": ownership or {},
        # 合流库标记（单账号时为 False，前端据此切换文案与「不支持写回/回填」提示）
        "merged": bool((ownership or {}).get("merged", False)),
    }
    if use_cache:
        _LIB_CACHE[key] = (now, data)
    return data


def _require_library(account_id=None, with_metadata=False, steam_path=None) -> dict:
    data = load_library(account_id=account_id, with_metadata=with_metadata, steam_path=steam_path)
    if not data["play_stats"]:
        raise ApiError(f"账号 {data['account_id']} 没有读到任何游戏记录", 404)
    return data


# ------------------------------------------------------------------ 汉化（展示层）

# 用户第 4 项：面板每个区块都要中文。
# 展示层只做「兜底 + 变量替换」：数据层（B 线）已经提供了权威中文
# （games.genres_cn / name_cn、官方 franchise 中文名），优先用它们；
# 这里的手工表只覆盖「数据层还缺」的部分，缺失时原样回退，**绝不编造**。

# Steam 官方商店类型名 -> 中文（覆盖本机库出现的全部类型）
GENRE_CN = {
    "action": "动作",
    "adventure": "冒险",
    "casual": "休闲",
    "indie": "独立",
    "massively multiplayer": "大型多人在线",
    "rpg": "角色扮演",
    "simulation": "模拟",
    "strategy": "策略",
    "free to play": "免费开玩",
    "early access": "抢先体验",
    "sports": "体育",
    "racing": "竞速",
    "utilities": "实用工具",
    "design & illustration": "设计与插画",
    "animation & modeling": "动画与建模",
    "audio production": "音频制作",
    "education": "教育",
    "software training": "软件培训",
    "video production": "视频制作",
    "web publishing": "网页发布",
    "game development": "游戏开发",
    "photo editing": "图片编辑",
    "accounting": "会计",
    "animation": "动画",
    "action rpg": "动作角色扮演",
    "tower defense": "塔防",
    "roguelike": "Roguelike",
    "platformer": "平台跳跃",
    "point & click": "点击式冒险",
    "visual novel": "视觉小说",
    "shooter": "射击",
    "puzzle": "解谜",
    "survival": "生存",
    "horror": "恐怖",
    "sandbox": "沙盒",
    "open world": "开放世界",
    "card game": "卡牌",
    "mmorpg": "大型多人在线角色扮演",
    "fighting": "格斗",
}

# 类型名里的常见修饰词（"Massively Multiplayer" 这类长名已整表收录，这里兜底单词级）
_GENRE_WORDS = {
    "action": "动作", "adventure": "冒险", "casual": "休闲", "indie": "独立",
    "strategy": "策略", "simulation": "模拟", "sports": "体育", "racing": "竞速",
    "rpg": "角色扮演", "multiplayer": "多人", "massively": "大型", "online": "在线",
    "free": "免费", "to": "开", "play": "玩", "early": "抢先", "access": "体验",
    "training": "培训", "software": "软件", "utilities": "实用工具",
    "animation": "动画", "modeling": "建模", "design": "设计", "illustration": "插画",
    "production": "制作", "audio": "音频", "video": "视频", "education": "教育",
    "web": "网页", "publishing": "发布", "development": "开发", "editing": "编辑",
}

# 自动生成的占位类型（Steam 只给了数字 id）——无法可靠翻译，按用户允许保留原样。
_PLACEHOLDER_GENRE_RE = re.compile(r"^(类型|genre)\s*\d+$", re.IGNORECASE)


def localize_genre(name, cn_table=None) -> str:
    """类型名汉化：数据层中文优先 → 手工整表 → 单词级兜底 → 原样返回。"""
    text_name = str(name or "").strip()
    if not text_name:
        return ""
    # 已经是中文（数据层 genres_cn）直接返回
    if _CJK_RE.search(text_name):
        return text_name
    key = text_name.lower()
    if isinstance(cn_table, dict):
        hit = cn_table.get(text_name) or cn_table.get(key)
        if hit:
            return str(hit)
    hit = GENRE_CN.get(key)
    if hit:
        return hit
    if _PLACEHOLDER_GENRE_RE.match(text_name):
        return text_name          # 「类型 29」这类占位：无法可靠翻译，保留
    words = re.split(r"([\s&/,-]+)", text_name)
    translated = [_GENRE_WORDS.get(part.lower(), part) for part in words]
    joined = "".join(translated).strip()
    return joined or text_name


def localize_genres(names, cn_table=None) -> list[str]:
    """整组类型汉化（保序、去重、忽略空值）。"""
    out: list[str] = []
    for item in names or []:
        value = localize_genre(item, cn_table)
        if value and value not in out:
            out.append(value)
    return out


def score_label(score) -> str:
    """推荐分 -> 中文标签。"""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return ""
    if value >= 6.0:
        return "强烈推荐"
    if value >= 4.0:
        return "值得一试"
    if value >= 2.0:
        return "可以看看"
    return "随便玩玩"


def localize_series_name(name) -> str:
    """系列名兜底汉化：数据层（官方 franchise / 已汉化名）优先，缺了才用手工小表。

    只对**确实有通行中文译名**的系列做映射；拿不准的一律保留原名（不硬编）。
    """
    text_name = str(name or "").strip()
    if not text_name or _CJK_RE.search(text_name):
        return text_name
    return _SERIES_CN.get(text_name.lower(), text_name)


# 少量「官方 franchise 是英文、但通行中文译名稳定」的系列兜底；
# 数据层若已给出中文，会先被上面的 _CJK_RE 短路，不走这里。
_SERIES_CN = {
    "half-life": "半条命",
    "counter-strike": "反恐精英",
    "portal": "传送门",
    "left 4 dead": "求生之路",
    "dota": "Dota",
    "team fortress": "军团要塞",
    "civilization": "文明",
    "sid meier's civilization": "文明",
    "total war": "全面战争",
    "warhammer": "战锤",
    "the witcher": "巫师",
    "dark souls": "黑暗之魂",
    "elden ring": "艾尔登法环",
    "monster hunter": "怪物猎人",
    "resident evil": "生化危机",
    "devil may cry": "鬼泣",
    "street fighter": "街头霸王",
    "mortal kombat": "真人快打",
    "borderlands": "无主之地",
    "hades": "哈迪斯",
    "hollow knight": "空洞骑士",
    "rainbow six": "彩虹六号",
    "tom clancy's": "汤姆·克兰西",
    "assassin's creed": "刺客信条",
    "far cry": "孤岛惊魂",
    "battlefield": "战地",
    "need for speed": "极品飞车",
    "the sims": "模拟人生",
    "age of empires": "帝国时代",
    "fallout": "辐射",
    "the elder scrolls": "上古卷轴",
    "grand theft auto": "侠盗猎车手",
    "red dead": "荒野大镖客",
    "persona": "女神异闻录",
    "final fantasy": "最终幻想",
    "dragon quest": "勇者斗恶龙",
    "nioh": "仁王",
    "yakuza": "如龙",
    "xcom": "幽浮",
    "crusader kings": "十字军之王",
    "hearts of iron": "钢铁雄心",
    "stellaris": "群星",
    "europa universalis": "欧陆风云",
    "victoria": "维多利亚",
    "oxygen not included": "缺氧",
    "don't starve": "饥荒",
    "risk of rain": "雨中冒险",
    "slay the spire": "杀戮尖塔",
    "vampire survivors": "吸血鬼幸存者",
    "dead cells": "死亡细胞",
    "terraria": "泰拉瑞亚",
    "stardew valley": "星露谷物语",
    "the binding of isaac": "以撒的结合",
    "apex legends": "Apex 英雄",
    "destiny": "命运",
    "titanfall": "泰坦天降",
    "forza": "极限竞速",
    "the king of fighters": "拳皇",
    "guilty gear": "罪恶装备",
    "tekken": "铁拳",
    "yu-gi-oh": "游戏王",
    "danganronpa": "弹丸论破",
    "dead by daylight": "黎明杀机",
    "payday": "收获日",
    "insurgency": "叛乱",
    "killing floor": "杀戮空间",
    "warframe": "星际战甲",
    "path of exile": "流放之路",
    "grim dawn": "恐怖黎明",
    "last epoch": "最后纪元",
    "deep rock galactic": "深岩银河",
    "valheim": "英灵神殿",
    "raft": "木筏求生",
    "the forest": "森林",
    "subnautica": "深海迷航",
    "no man's sky": "无人深空",
    "outer wilds": "星际拓荒",
    "disco elysium": "极乐迪斯科",
    "divinity": "神界",
    "baldur's gate": "博德之门",
    "pillars of eternity": "永恒之柱",
    "tales of": "传说系列",
    "ni no kuni": "二之国",
    "kingdom hearts": "王国之心",
    "kingdom come": "天国拯救",
    "metro": "地铁",
    "stalker": "潜行者",
    "hitman": "杀手",
    "tomb raider": "古墓丽影",
    "uncharted": "神秘海域",
    "god of war": "战神",
    "horizon": "地平线",
    "spider-man": "蜘蛛侠",
    "batman": "蝙蝠侠",
    "lego": "乐高",
    "minecraft": "我的世界",
    "roblox": "罗布乐思",
    "garry's mod": "盖瑞模组",
    "left4dead": "求生之路",
    "the escapists": "逃脱者",
    "human: fall flat": "人类一败涂地",
    "overcooked": "胡闹厨房",
    "it takes two": "双人成行",
    "a way out": "逃出生天",
    "moving out": "胡闹搬家",
    "unravel": "毛线小精灵",
    "little nightmares": "小小梦魇",
    "inside": "囚禁",
    "limbo": "地狱边境",
    "cuphead": "茶杯头",
    "ori": "奥日",
    "celeste": "蔚蓝",
    "katana zero": "武士零",
    "hotline miami": "迈阿密热线",
    "enter the gungeon": "挺进地牢",
    "nuclear throne": "核弹王座",
    "isaac": "以撒",
    "enter the breach": "陷阵之志",
    "into the breach": "陷阵之志",
    "ftl": "超光速",
    "baba is you": "巴巴是你",
    "the witness": "见证者",
    "portal 2": "传送门 2",
    "superhot": "燥热",
    "firewatch": "看火人",
    "what remains of edith finch": "艾迪芬奇的记忆",
    "life is strange": "奇异人生",
    "the walking dead": "行尸走肉",
    "wolfenstein": "德军总部",
    "doom": "毁灭战士",
    "quake": "雷神之锤",
    "halo": "光环",
    "gears of war": "战争机器",
    "call of duty": "使命召唤",
    "medal of honor": "荣誉勋章",
    "sniper elite": "狙击精英",
    "ghost recon": "幽灵行动",
    "splinter cell": "细胞分裂",
    "watch dogs": "看门狗",
    "the division": "全境封锁",
    "for honor": "荣耀战魂",
    "rainbow 6": "彩虹六号",
    "anno": "纪元",
    "the settlers": "工人物语",
    "tropico": "海岛大亨",
    "cities: skylines": "城市：天际线",
    "cities skylines": "城市：天际线",
    "planet coaster": "过山车之星",
    "planet zoo": "动物园之星",
    "two point": "双点",
    "stellaris 2": "群星 2",
    "dead space": "死亡空间",
    "mass effect": "质量效应",
    "dragon age": "龙腾世纪",
    "star wars": "星球大战",
    "the lord of the rings": "指环王",
    "harry potter": "哈利波特",
    "hogwarts": "霍格沃茨",
    "pico park": "PICO PARK",
    "super raft boat": "Super Raft Boat",
    "goat's tale": "Goat's Tale",
}


_GENRE_CN_CACHE: dict = {}
_GENRE_CN_TS: float = 0.0


def genre_cn_table() -> dict:
    """数据层（B 线）提供的权威中文类型表：{英文类型: 中文类型}。

    取不到就返回 {}（展示层的手工表会兜底），绝不抛异常、不联网。
    """
    global _GENRE_CN_CACHE, _GENRE_CN_TS
    now = time.time()
    if _GENRE_CN_CACHE and now - _GENRE_CN_TS < 60.0:
        return _GENRE_CN_CACHE
    table: dict = {}
    db = None
    try:
        from data.schema import get_connection

        db = get_connection()
        for row in db.execute("SELECT genres, genres_cn FROM games"):
            en = _json_list(row["genres"])
            cn = _json_list(row["genres_cn"])
            for src, dst in zip(en, cn):
                if src and dst and src not in table:
                    table[str(src)] = str(dst)
    except Exception as exc:
        logger.debug("读取中文类型表失败（改用内置表）：%s", exc)
        table = {}
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    _GENRE_CN_CACHE = table
    _GENRE_CN_TS = now
    return table


def _json_list(raw) -> list:
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _localize_stats_block(stats: dict, cn_table=None) -> dict:
    """统计区块：类型 / 开发商分布里的类型名汉化（不改变数值与结构）。"""
    if not isinstance(stats, dict):
        return stats
    for key in ("top_genres",):
        items = stats.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and "name" in item:
                    item["name"] = localize_genre(item.get("name"), cn_table)
    return stats


def _localize_genre_rows(rows, cn_table=None):
    """把一组 {genre: ...} 记录里的 genre 汉化（保结构）。"""
    if not isinstance(rows, list):
        return rows
    for item in rows:
        if isinstance(item, dict) and item.get("genre"):
            item["genre"] = localize_genre(item["genre"], cn_table)
    return rows


# ------------------------------------------------------------------ 各分析区块

def _stats_section(lib: dict) -> dict:
    from engine.stats import compute_stats

    stats = compute_stats(lib["play_stats"], lib["games_meta"], statuses=lib["statuses"])
    return _localize_stats_block(stats, genre_cn_table())


def _persona_section(lib: dict, stats: dict | None = None) -> dict:
    from engine.persona import compute_persona

    return compute_persona(
        lib["play_stats"], lib["games_meta"], stats_result=stats, statuses=lib["statuses"]
    )


def _value_section(lib: dict) -> dict:
    from engine.value_ranking import compute_value_ranking

    result = compute_value_ranking(lib["play_stats"], lib["games_meta"], statuses=lib["statuses"])
    for key in ("value_top", "waste_top", "zero_cost_top"):
        for item in result.get(key) or []:
            if isinstance(item, dict):
                item["name_cn"] = _name_cn(item.get("appid"))
    return result


# 族谱每个系列框**统一最多显示 4 张**封面：系列作品数 >=4 且封面齐时恒为 4 张，
# 不足 4 部的系列「有多少显示多少」。
SERIES_THUMB_LIMIT = 4


def _series_members_ordered(item: dict) -> list[dict]:
    """系列成员的**缩略图取材顺序**：本账号已拥有优先、玩过的次之，其余按系列内原有顺序。

    「画哪几张缩略图」（:func:`_series_section`）与「去补抓哪几张」
    （:func:`post_localize`）共用这一顺序，保证「框里缺哪些」与「去抓哪些」永远一致。
    """
    rows = [g for g in (item.get("games") or []) if isinstance(g, dict)]
    return ([g for g in rows if g.get("owned")]
            + [g for g in rows if not g.get("owned") and g.get("played")]
            + [g for g in rows if not g.get("owned") and not g.get("played")])


def _series_thumb_appids(item: dict) -> list[int]:
    """该系列框要取材的成员 appid：顺序同上，取前 ``min(4, 作品总数)`` 个。"""
    members = _series_members_ordered(item)
    out: list[int] = []
    for game in members[:min(SERIES_THUMB_LIMIT, len(members))]:
        try:
            appid = int(game.get("appid"))
        except (TypeError, ValueError):
            continue
        if appid > 0:
            out.append(appid)
    return out


def _series_cover_gaps(lib: dict) -> list[int]:
    """族谱缩略图的**封面缺口**：各系列框取材范围内、本机还没有封面的 appid。

    与 :func:`_series_section` 同源同序（都走 ``engine.series_tree.compute_series_tree``，
    只算一次）；只做本地查找，**绝不联网**；任何异常都退化为空列表，绝不抛。
    """
    try:
        from engine.series_tree import compute_series_tree

        result = compute_series_tree(
            lib["play_stats"], lib["games_meta"], statuses=lib["statuses"],
            steam_path=lib.get("steam_path"), account_id=lib.get("account_id"),
        )
        steam_path = lib.get("steam_path")
        gaps: list[int] = []
        seen: set[int] = set()
        for item in result.get("series") or []:
            if not isinstance(item, dict):
                continue
            for appid in _series_thumb_appids(item):
                if appid in seen:
                    continue
                seen.add(appid)
                if not _has_cover(appid, steam_path):
                    gaps.append(appid)
        return gaps
    except Exception as exc:
        logger.warning("计算族谱封面缺口失败：%s", exc)
        return []


def _series_section(lib: dict) -> dict:
    from engine.series_tree import compute_series_tree

    # 传 steam_path / account_id：族谱据此按 core.ownership（唯一口径）区分
    # 「确实拥有 / 家庭库玩过 / 未拥有」；读不到授权票时引擎自动降级。
    result = compute_series_tree(
        lib["play_stats"], lib["games_meta"], statuses=lib["statuses"],
        steam_path=lib.get("steam_path"), account_id=lib.get("account_id"),
    )
    steam_path = lib.get("steam_path")
    for item in result.get("series") or []:
        if isinstance(item, dict):
            item["series"] = localize_series_name(item.get("series"))
            for game in item.get("games") or []:
                if isinstance(game, dict):
                    game["name_cn"] = _name_cn(game.get("appid"))
            # 面板系列框的缩略图：**统一最多 4 张**，只用本机已有的封面，
            # 绝不为画缩略图出网。顺序是「已拥有优先、玩过的次之」（其余按系列内原顺序），
            # 这样用户一眼看到的就是「自己的那几部」。
            # 目标张数 = min(4, 该系列作品总数)：凑够就停，凑不够就有多少显示多少。
            ordered = _series_members_ordered(item)
            target = min(SERIES_THUMB_LIMIT, len(ordered))
            thumbs: list[str] = []
            thumbs_owned: list[bool] = []
            thumbs_played: list[bool] = []
            uncovered: list[int] = []
            for game in ordered:
                if len(thumbs) >= target:
                    break
                url = _cover_url(game.get("appid"), steam_path)
                if url:
                    thumbs.append(url)
                    thumbs_owned.append(bool(game.get("owned")))
                    thumbs_played.append(bool(game.get("played")))
                    continue
                try:
                    appid = int(game.get("appid"))
                except (TypeError, ValueError):
                    continue
                if appid > 0:
                    uncovered.append(appid)
            item["thumbs"] = thumbs
            # 与 thumbs 一一对应：前端据此给「自己的封面」加标记
            item["thumbs_owned"] = thumbs_owned
            # 与 thumbs 一一对应：未拥有但玩过（家庭库）的缩略图标记（低调的第二种标记）
            item["thumbs_played"] = thumbs_played
            # 缺口分两种，前端据此说两种话（别再叫用户去抓官方根本没有的图）：
            #   thumbs_missing = 本该显示 min(4, 作品数) 张，实际少了几张；
            #   thumbs_no_art = 少掉的这几张里，有几张是**官方 CDN 确认无图（404）**的
            #                   （名单见 core.artwork.no_art_appids，纯本地只读、绝不联网）。
            missing = max(0, target - len(thumbs))
            no_art = _no_art_appids() if missing else set()
            item["thumbs_missing"] = missing
            item["thumbs_no_art"] = min(
                missing, sum(1 for appid in uncovered if appid in no_art)
            )
    return result


def _wishlist_section(lib: dict) -> dict:
    from engine.wishlist_guard import compute_wishlist_guard

    result = compute_wishlist_guard(lib["play_stats"], lib["games_meta"], statuses=lib["statuses"])
    table = genre_cn_table()
    _localize_genre_rows(result.get("genres"), table)
    for warning in result.get("warnings") or []:
        if not isinstance(warning, dict):
            continue
        english = str(warning.get("genre") or "")      # 先留原英文名
        cn = localize_genre(english, table)
        warning["genre"] = cn
        # 引擎生成的 warning_message 里内嵌了英文类型名，同步替换成中文
        message = str(warning.get("warning_message") or "")
        if message and english and cn and english != cn:
            warning["warning_message"] = message.replace(english, cn)
    return result


def _radar_section(lib: dict) -> dict:
    """成就雷达：只读本地成就缓存，不联网（保证面板响应速度）。"""
    from core import achievements as achievements_mod
    from engine.achievement_radar import compute_achievement_radar

    get_cached = getattr(achievements_mod, "get_cached", None)
    achievement_data: dict = {}
    playtime_minutes: dict = {}
    if callable(get_cached):
        for appid in lib["play_stats"]:
            playtime_minutes[appid] = int((lib["play_stats"].get(appid) or {}).get("playtime", 0))
            try:
                row = get_cached(int(appid))
            except Exception:
                row = None
            if not isinstance(row, dict):
                continue
            total = row.get("total_count")
            if not total:
                continue
            achievement_data[appid] = {
                "achieved": row.get("achieved_count"),
                "total": total,
                "completion_rate": row.get("completion_rate"),
            }
    result = compute_achievement_radar(achievement_data, lib["games_meta"], playtime_minutes)
    for candidate in result.get("candidates") or []:
        if isinstance(candidate, dict):
            candidate["name_cn"] = _name_cn(candidate.get("appid"))
    result["hint"] = (
        "暂无成就数据（建议先运行 stats/radar 采集）" if not achievement_data else ""
    )
    return result


def _challenge_section(lib: dict) -> dict:
    """本月挑战 + XP：用纯函数计算，只读数据库，不写入 XP 账本。"""
    from engine import challenge as challenge_mod

    play_stats = lib["play_stats"]
    if not play_stats:
        return {"available": False, "message": "当前账号没有读取到任何游戏，无法计算挑战"}

    today = date.today().isoformat()
    month = today[:7]
    base_total = 0
    try:
        base = challenge_mod.load_xp()
        if base:
            base_total = int(base.get("total_xp") or 0)
    except Exception as exc:
        logger.debug("读取累计 XP 失败：%s", exc)
    record = None
    try:
        record = challenge_mod.get_month_record(month)
    except Exception as exc:
        logger.debug("读取本月挑战记录失败：%s", exc)

    result = challenge_mod.compute_challenge(
        play_stats=play_stats,
        month=month,
        today=today,
        total_xp_base=base_total,
        account_appids=_appid_set(play_stats),
        challenge_record=record,
    )
    result["available"] = True
    result["recorded"] = bool(record)
    return result


# ------------------------------------------------------------------ 汇总与推荐

def get_summary(
    account_id: str | None = None,
    with_metadata: bool = False,
    steam_path: str | None = None,
) -> dict:
    """看板主数据：stats / persona / value / series / wishlist / challenge / radar / games。

    每个区块独立降级：某一块报错只在该块留下 error 字段，绝不让整页 500。
    """
    lib = _require_library(account_id, with_metadata, steam_path)
    payload: dict[str, Any] = {
        "account_id": lib["account_id"],
        "steam_path": lib["steam_path"],
        "with_metadata": bool(with_metadata),
        "named": lib["named"],
        # 合流库标记：本次统计的是「多个账号当作一个账号」的结果
        "merged": bool(lib.get("merged")),
        # 家庭共享过滤的说明字段：
        #   {"owned": N, "played_shared": P, "shared": M,
        #    "played_shared_appids": [...], "shared_appids": [...]}
        # **只做展示说明**，不改变 stats/games 里任何数字（那些数字来自已过滤过的库；
        # A4 起「共享但玩过」的 P 款留在库里正常参与所有统计，只有 M 款被剔除）。
        "ownership": _ownership_section(lib),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    sections = {
        "stats": lambda: _stats_section(lib),
        "persona": lambda: _persona_section(lib),
        "value": lambda: _value_section(lib),
        "series": lambda: _series_section(lib),
        "wishlist": lambda: _wishlist_section(lib),
        "challenge": lambda: _challenge_section(lib),
        "radar": lambda: _radar_section(lib),
    }
    for name, fn in sections.items():
        try:
            payload[name] = fn()
        except Exception as exc:
            logger.warning("区块 %s 计算失败：%s", name, exc)
            payload[name] = None
            payload.setdefault("errors", {})[name] = str(exc)

    try:
        payload["games"] = _games_rows(lib)
    except Exception as exc:
        payload["games"] = []
        payload.setdefault("errors", {})["games"] = str(exc)

    try:
        payload["coverage"] = _coverage(lib, rows=payload["games"])
    except Exception as exc:
        payload["coverage"] = None
        payload.setdefault("errors", {})["coverage"] = str(exc)

    # T6：面板加载时发现这个账号数据明显不全、又没在跑补齐任务，就自动续做一次。
    # 只做本地小 JSON 读 + 内存入队，绝不给这次响应加时延、更绝不联网。
    try:
        _maybe_auto_enqueue(lib, payload.get("coverage"))
    except Exception as exc:
        logger.debug("自动补齐判断失败（已忽略）：%s", exc)
    return payload


def _ownership_section(lib: dict) -> dict:
    """面板「已保留 P 款共享但玩过 / 已剔除 M 款共享且未玩过」提示行的数据源（core.ownership 唯一口径）。

    读不到授权票（降级）时 ``degraded=True``、``shared=0`` —— 面板据此不显示剔除提示，
    也绝不谎报剔除了多少款。

    A4 起：``played_shared`` 是「家庭共享但本账号玩过」的款数（**已正常计入 stats/games**），
    ``shared`` / ``shared_appids`` / ``shared_games`` 收窄为「共享**且从未玩过**、被剔除的」，
    供「显示家庭共享」勾选框列灰显行。

    T1/T4 起另有 ``trace`` / ``trace_appids``（Steam 残留痕迹，同样被剔除）——面板提示行要
    与 CLI 口径一致，也把这一段转发出去。
    """
    raw = lib.get("ownership") or {}
    played_shared_appids = sorted(
        int(a) for a in (raw.get("played_shared_appids") or []) if str(a).isdigit()
    )
    shared_appids = sorted(int(a) for a in (raw.get("shared_appids") or []) if str(a).isdigit())
    trace_appids = sorted(int(a) for a in (raw.get("trace_appids") or []) if str(a).isdigit())
    section = {
        "owned": int(raw.get("owned") or 0),
        "played_shared": int(raw.get("played_shared") or 0),
        "shared": int(raw.get("shared") or 0),
        "free": int(raw.get("free") or 0),
        "trace": int(raw.get("trace") or 0),
        "played_shared_appids": played_shared_appids,
        "shared_appids": shared_appids,
        "trace_appids": trace_appids,
        "degraded": bool(raw.get("degraded", False)),
        "shared_games": [],
    }
    # 合流库：把逐账号明细与合并小计一并转发，面板据此显示「这几个账号当作一个账号」
    if raw.get("merged"):
        section.update({
            "merged": True,
            "accounts": raw.get("accounts") or [],
            "account_count": int(raw.get("account_count") or 0),
            "games": int(raw.get("games") or 0),
            "total_hours": float(raw.get("total_hours") or 0),
            "overlap_games": int(raw.get("overlap_games") or 0),
            "overlap_minutes": int(raw.get("overlap_minutes") or 0),
        })
    # 「显示家庭共享」勾选框用的灰显行：**只用于展示**，不参与任何统计
    try:
        from core import render_bridge

        stats = raw.get("shared_stats") or {}
        rows = []
        for appid in shared_appids:
            info = stats.get(appid) or {}
            last_played = int(info.get("last_played", 0) or 0)
            rows.append({
                "appid": appid,
                "name": render_bridge.game_name(appid, {}),
                "name_cn": _name_cn(appid),
                "cover": _cover_url(appid, lib.get("steam_path")),
                "cover_kind": _cover_kind(appid, lib.get("steam_path")),
                "playtime_hours": render_bridge.minutes_to_hours(info.get("playtime", 0)),
                "last_played": (
                    datetime.fromtimestamp(last_played).strftime("%Y-%m-%d") if last_played else ""
                ),
                "shared": True,
            })
        rows.sort(key=lambda r: (-r["playtime_hours"], r["name"]))
        section["shared_games"] = rows
    except Exception as exc:                      # 灰显行失败不影响主数据
        logger.warning("家庭共享灰显行构建失败：%s", exc)

    # 归属自动体检（T15）：工具自己报出显示错标，不用用户逐个点名。
    # 合流库（不是真实账号）跳过：体检要按账号取票面，合流库没有自己的票。
    try:
        from core import ownership as ownership_mod

        steam_path = lib.get("steam_path")
        account_id = lib.get("account_id")
        if steam_path and account_id and str(account_id).isdigit():
            # 把「面板游戏明细里实际有哪些」交给体检：工具据此**自己**发现「在库里、
            # 是免费游戏、却没显示」的漏项（M2f / T16），不用用户逐个点名。
            listed: list[int] = []
            for raw_appid in (lib.get("play_stats") or {}):
                try:
                    listed.append(int(raw_appid))
                except (TypeError, ValueError):
                    continue
            report = ownership_mod.audit_ownership(steam_path, account_id, sorted(listed))
            section["audit"] = {
                "ok": bool(report.get("ok")),
                "degraded": bool(report.get("degraded")),
                "checked": int(report.get("checked") or 0),
                "errors": int(report.get("errors") or 0),
                "warnings": int(report.get("warnings") or 0),
                "counts": report.get("counts") or {},
                "issues": [
                    {
                        "level": item.get("level"),
                        "appid": int(item.get("appid") or 0),
                        "name": _name_cn(int(item.get("appid") or 0)),
                        "reason": item.get("reason"),
                        "evidence": item.get("evidence"),
                    }
                    for item in (report.get("issues") or [])
                ],
            }
    except Exception as exc:                      # 体检失败绝不影响面板主数据
        logger.debug("归属体检不可用（已忽略）：%s", exc)
    return section


def _games_rows(lib: dict, limit: int = 400) -> list[dict]:
    """轻量游戏列表（面板卡片用）：appid / 名称 / 中文名 / 中文类型 / 封面 / 状态 / 时长。

    A4：``played_shared=True`` 标记「家庭共享但本账号玩过」的条目 —— 它们**正常参与所有统计**，
    前端只给一个低调徽标，**不灰显、不排到末尾**（与灰显的 ``shared_games`` 完全不同）。
    """
    from core import render_bridge

    cn_table = genre_cn_table()
    rows: list[dict] = []
    play_stats = lib["play_stats"]
    classified = lib["classified"] or {}
    games_meta = lib["games_meta"] or {}
    played_shared = {
        int(a) for a in ((lib.get("ownership") or {}).get("played_shared_appids") or [])
        if str(a).isdigit()
    }
    for appid, raw in play_stats.items():
        info = classified.get(appid) or {}
        status = str(info.get("status") or lib["statuses"].get(appid) or "")
        status_name = str(info.get("status_name") or status)
        last_played = int((raw or {}).get("last_played", 0) or 0)
        meta = games_meta.get(appid) or games_meta.get(str(appid)) or {}
        rows.append(
            {
                "appid": int(appid),
                "name": render_bridge.game_name(appid, meta),
                "name_cn": _name_cn(appid),
                "cover": _cover_url(appid, lib.get("steam_path")),
                "cover_kind": _cover_kind(appid, lib.get("steam_path")),
                "genres": localize_genres(meta.get("genres"), cn_table),
                "status": status,
                "status_name": status_name,
                "playtime_hours": render_bridge.minutes_to_hours((raw or {}).get("playtime", 0)),
                "last_played": (
                    datetime.fromtimestamp(last_played).strftime("%Y-%m-%d") if last_played else ""
                ),
                # 家庭共享但本账号玩过：只加徽标，**照常排序、照常统计**
                "played_shared": int(appid) in played_shared,
            }
        )
    rows = [row for row in rows if not is_non_game(row["appid"])]
    rows.sort(key=lambda r: (-r["playtime_hours"], r["name"]))
    return rows[:limit]


def get_recommend(
    account_id: str | None = None,
    mood: str = "随便",
    time_left: str = "不限",
    genre: str = "不限",
    top_k: int | None = None,
    with_metadata: bool = False,
    steam_path: str | None = None,
) -> dict:
    from engine.recommender import recommend

    lib = _require_library(account_id, with_metadata, steam_path)
    result = recommend(
        lib["play_stats"],
        lib["games_meta"],
        mood=mood or "随便",
        time_left=time_left or "不限",
        genre=genre or "不限",
        statuses=lib["statuses"],
        top_k=int(top_k) if top_k else None,
    )
    result["account_id"] = lib["account_id"]
    try:  # 每条推荐带上中文名、竖版封面与中文类型（都只读本地，不联网）
        cn_table = genre_cn_table()
        games_meta = lib.get("games_meta") or {}
        for item in result.get("recommendations") or []:
            if not isinstance(item, dict):
                continue
            appid = item.get("appid")
            if appid is None:
                continue
            meta = games_meta.get(appid) or games_meta.get(str(appid)) or {}
            item["name"] = str(item.get("name") or meta.get("name") or "")
            item["name_cn"] = _name_cn(appid) or str(meta.get("name_cn") or "")
            item["cover"] = _cover_url(appid, lib.get("steam_path"))
            item["cover_kind"] = _cover_kind(appid, lib.get("steam_path"))
            item["genres_cn"] = localize_genres(
                item.get("genres") or meta.get("genres"), cn_table
            )
            # 推荐理由由引擎用中文生成（已核对），这里只需补一个中文分档标签
            item["score_label"] = score_label(item.get("score"))
    except Exception as exc:
        logger.warning("推荐结果补充封面失败：%s", exc)
    try:  # 推荐里也不能出现非游戏条目（用户第 7 项）
        items = result.get("recommendations")
        if isinstance(items, list):
            kept = [it for it in items if not is_non_game((it or {}).get("appid"))]
            if len(kept) != len(items):
                result["filtered_non_game"] = len(items) - len(kept)
            result["recommendations"] = kept
    except Exception as exc:
        logger.warning("过滤非游戏推荐失败：%s", exc)
    return result


# ------------------------------------------------------------------ 动作类接口

def post_run(
    account_id: str | None = None,
    dry_run: bool = True,
    with_metadata: bool = False,
    steam_path: str | None = None,
) -> dict:
    """执行分类（复用 CLI 的 run）。默认 dry_run=True，不做任何破坏性操作。"""
    if merge_core.is_merge_id(account_id):
        # 合流库不是账号：不写回 Steam、不存快照（会污染单账号快照）。只预览。
        dry_run = True
    ns = _namespace(
        steam_path=steam_path,
        account_id=account_id,
        with_metadata=bool(with_metadata),
        dry_run=bool(dry_run),
    )
    code, output = _capture(lambda: cli.cmd_run(ns))
    result = {
        "ok": code == cli.EXIT_OK,
        "exit_code": int(code or 0),
        "dry_run": bool(dry_run),
        "output": output,
    }
    try:
        lib = load_library(account_id=account_id, with_metadata=with_metadata, steam_path=steam_path)
        classified = lib["classified"] or {}
        counts: dict[str, int] = {}
        for info in classified.values():
            key = str(info.get("status_name") or info.get("status") or "未知")
            counts[key] = counts.get(key, 0) + 1
        result["status_counts"] = counts
        result["total_games"] = len(classified)
    except ApiError as exc:
        result["warning"] = exc.message
    return result


def post_writeback(
    account_id: str | None = None,
    steam_path: str | None = None,
) -> dict:
    """写回按钮：开启则分类写回 Steam，再次点击则移除状态分类。

    Steam 运行中时 steam_writer 会抛 SteamRunningError，此处转成友好错误。
    """
    try:
        from core.steam_writer import SteamRunningError  # noqa: F401
    except Exception:  # 缺依赖时仍允许调用（cmd_writeback 内部会提示）
        pass

    ns = _namespace(steam_path=steam_path, account_id=account_id)
    code, output = _capture(lambda: cli.cmd_writeback(ns))
    state = False
    try:
        state = bool(cli._load_writeback_state())
    except Exception as exc:
        logger.debug("读取写回状态失败：%s", exc)
    ok = code == cli.EXIT_OK
    return {
        "ok": ok,
        "exit_code": int(code or 0),
        "writeback": state,
        "message": (output or "").strip() or ("写回状态已切换" if ok else "写回操作失败"),
        "output": output,
    }


def post_backfill(
    account_id: str | None = None,
    dry_run: bool = True,
    overwrite: bool = False,
    min_days_ago: int = 1,
    with_metadata: bool = False,
    steam_path: str | None = None,
) -> dict:
    """历史快照回填（默认只预览）。"""
    if merge_core.is_merge_id(account_id):
        # 快照按账号记录，合流库没有「自己是哪个账号」的答案，绝不替用户猜。
        raise ApiError("合流库不支持历史快照回填：请切回单个账号再回填（快照是按账号保存的）", 400)
    lib = _require_library(account_id, with_metadata, steam_path)
    if not lib["classified"]:
        raise ApiError("状态引擎不可用，无法为回填快照附加状态", 500)

    from core import snapshot

    try:
        stats = snapshot.backfill_from_play_stats(
            lib["play_stats"],
            classified=lib["classified"],
            only_missing=not overwrite,
            min_days_ago=max(int(min_days_ago or 0), 0),
            dry_run=bool(dry_run),
        )
    except Exception as exc:
        logger.exception("回填历史快照失败")
        raise ApiError(f"回填失败：{exc}", 500) from exc

    result = {
        "ok": True,
        "dry_run": bool(dry_run),
        "overwrite": bool(overwrite),
        "stats": stats,
        "note": "历史 playtime 无法精确还原，回填值是以当前数据作为该日期的近似推断值。",
    }
    if not dry_run:
        try:
            result["snapshot_dates"] = len(snapshot.list_dates())
        except Exception as exc:
            logger.debug("读取快照日期失败：%s", exc)
    return result


def get_config() -> dict:
    """读取规则配置：返回值 = rules.json 原文（去说明键）+ 合并默认后的完整规则。"""
    rules_path = cli.RULES_PATH
    raw: dict = {}
    source = "内置默认规则（rules.json 不存在）"
    if rules_path.exists():
        try:
            raw = json.loads(rules_path.read_text(encoding="utf-8"))
            source = "rules.json"
        except (OSError, json.JSONDecodeError) as exc:
            raise ApiError(f"rules.json 解析失败：{exc}", 500) from exc
    merged: dict = {}
    try:
        from engine.stats import load_rules

        merged = load_rules()
    except Exception as exc:
        logger.debug("加载合并规则失败：%s", exc)
    return {
        "path": str(rules_path),
        "source": source,
        "file": _strip_comments(raw) if isinstance(raw, dict) else {},
        "merged": _strip_comments(merged) if isinstance(merged, dict) else {},
    }


def set_config(key: str, value: str) -> dict:
    """写入规则配置（复用 CLI 的 _config_set，含备份与原子替换）。"""
    if not key or not str(key).strip():
        raise ApiError("缺少配置键", 400)
    raw_value = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    code, output = _capture(lambda: cli._config_set(cli.RULES_PATH, str(key).strip(), raw_value))
    ok = code == cli.EXIT_OK
    return {
        "ok": ok,
        "exit_code": int(code or 0),
        "message": (output or "").strip(),
        "output": output,
    }


# ------------------------------------------------------------------ 页面渲染

def render_page(
    kind: str,
    account_id: str | None = None,
    with_metadata: bool = False,
    steam_path: str | None = None,
) -> Path:
    """生成指定可视化页面并返回产物路径（写进稳定 output 目录，不进临时解包目录）。"""
    if kind not in PAGE_KINDS:
        raise ApiError(f"未知页面类型：{kind}（可选：{'/'.join(PAGE_KINDS)}）", 404)

    lib = load_library(account_id=account_id, with_metadata=with_metadata, steam_path=steam_path)
    try:
        from core import render_bridge
    except ImportError as exc:
        raise ApiError(f"真实数据桥接不可用：{exc}", 500) from exc

    snapshots = None
    if kind in ("timeline", "today"):
        snapshots = render_bridge.load_snapshots()

    payload = render_bridge.build_payload(
        kind,
        lib["play_stats"],
        lib["games_meta"],
        classified=lib["classified"],
        snapshots=snapshots,
        user_name=_account_display_name(lib),
    )

    module_name = cli._RENDER_MODULES[kind]
    generator_name = cli._RENDER_GENERATORS[kind]
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ApiError(f"渲染模块 {module_name} 不可用：{exc}", 500) from exc
    generator = getattr(module, generator_name, None)
    if not callable(generator):
        raise ApiError(f"渲染模块 {module_name} 缺少 {generator_name}()", 500)

    outdir = _output_dir()
    module.DEFAULT_OUTPUT_DIR = outdir
    try:
        out = generator(payload, output_path=None, open_browser=False)
    except Exception as exc:
        logger.exception("生成页面失败：%s", kind)
        raise ApiError(f"生成页面失败：{exc}", 500) from exc
    _remember_page_artifact(kind, out, account_id)
    return Path(out)


def _account_display_name(lib: dict) -> str:
    """页面标题用的账号名；合流库回「合流库（N 个账号）」。"""
    if lib.get("merged") or merge_core.is_merge_id(lib.get("account_id")):
        count = int((lib.get("ownership") or {}).get("account_count") or 0)
        if not count:
            try:
                count = len(merge_core.resolve_members(lib.get("steam_path"), load_added_accounts()))
            except Exception as exc:
                logger.debug("合流成员数读取失败（按 0 处理）：%s", exc)
                count = 0
        return merge_core.merge_display_name(count)
    from core import locator

    try:
        for acc in locator.list_user_accounts(lib["steam_path"]):
            if str(acc.get("id")) == str(lib["account_id"]):
                return str(acc.get("name") or lib["account_id"])
    except Exception as exc:
        logger.debug("获取账号显示名失败：%s", exc)
    return str(lib["account_id"])


def list_pages(
    account_id: str | None = None,
    with_metadata: bool = False,
    steam_path: str | None = None,
) -> dict:
    """生成全部 6 张页面，返回链接与字节数（单张失败只标记该张）。"""
    pages = []
    outdir = _output_dir()
    for kind in PAGE_KINDS:
        item: dict[str, Any] = {
            "kind": kind,
            "label": PAGE_LABELS.get(kind, kind),
            "url": f"/page/{kind}",
        }
        try:
            path = render_page(kind, account_id=account_id, with_metadata=with_metadata, steam_path=steam_path)
            item["file"] = str(path)
            item["bytes"] = path.stat().st_size if path.exists() else 0
            item["ok"] = True
        except ApiError as exc:
            item["ok"] = False
            item["error"] = exc.message
        pages.append(item)
    return {"output_dir": str(outdir), "pages": pages}


def read_page(
    kind: str,
    account_id: str | None = None,
    with_metadata: bool = False,
    steam_path: str | None = None,
    prefer_existing: bool = True,
) -> str:
    """返回页面 HTML。

    ``prefer_existing=True``（默认）时，**产物存在、属于当前账号、且渲染代码没变过**
    就直接返回文件，不重新渲染 —— 否则面板一打开六个 iframe 会把六张页面全部重画一遍
    （实测 ~10 秒）。产物缺失、属于别的账号、或**是用旧渲染代码画的**（指纹不符）时才
    真正渲染一次：否则用户看到的永远是老图（T1 中文名修复被旧产物挡住就是这么来的）。
    """
    if kind not in PAGE_KINDS:
        raise ApiError(f"未知页面类型：{kind}", 404)

    if prefer_existing:
        state = page_artifact_state(kind, account_id)
        if state["file"] is not None:
            try:
                return state["file"].read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning("读取已有页面产物失败，改为重新生成：%s", exc)

    path = render_page(kind, account_id=account_id, with_metadata=with_metadata, steam_path=steam_path)
    if not path.exists():
        raise ApiError("页面生成后未找到产物文件", 500)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApiError(f"读取页面失败：{exc}", 500) from exc


# 页面产物清单：面板首屏「只列已有产物」时用来秒开，不需要重新渲染。
_PAGES_MANIFEST_NAME = ".pages_manifest.json"
_PAGES_MANIFEST_LOCK = threading.RLock()

# 各页面产物文件名（渲染模块里写死的默认名，见 render/*py 的 DEFAULT_OUTPUT_DIR 用法）
_PAGE_FILE_PATTERNS: dict[str, tuple[str, ...]] = {
    "report": ("annual_report_*.html",),     # 名字带年份，用通配
    "graveyard": ("graveyard.html",),
    "starmap": ("starmap.html",),
    "timeline": ("timeline.html",),
    "today": ("timemachine.html",),
    "checkup": ("checkup.html",),
}


# ------------------------------------------------------------------ 渲染器指纹
# 产物清单里除了「谁生成的」，还要记「用哪版渲染代码生成的」。
# 否则改了渲染逻辑（如 T1 的中文名优先）之后，面板首屏会继续发旧 HTML —— 用户看到的
# 还是老图，却不知道要手点「重新生成」。
#
# 只覆盖**真正决定页面内容**的代码（桥接层 + 渲染模块 + 模板 + 引擎层），
# 刻意**不**包含 cache.db / output/covers 这类易变数据：算进去的话每跑一次数据就会
# 误判过期、首屏又慢回 ~10 秒。指纹代表「代码版本」，不代表「数据版本」。
#
# 引擎层（engine/**/*.py）必须一起算：页面内容不只取决于 render_bridge 本身 ——
# 统计（engine.stats）、人格画像（engine.persona）、性价比（engine.value_ranking）、
# 类型展示（engine.display）等都是 render_bridge 调引擎算完再塞进模板的。
# 只覆盖 render 层的话，改引擎不会让旧产物过期，用户又会看到旧内容（正是本任务要根治的问题）。
_RENDERER_FINGERPRINT_VERSION = "gl-page-renderer-v3"   # 覆盖范围又变了（+core/ownership.py），老产物一次性作废
_RENDERER_FINGERPRINT_CACHE: str | None = None
_RENDERER_FINGERPRINT_LOCK = threading.RLock()

# 枚举源码时跳过的目录：字节码缓存不是源码，且每次编译都在变，算进去会误判过期。
_PY_SKIP_DIRS = frozenset({"__pycache__"})

# 冻结（onefile）态的补充覆盖：打包后 ``__file__`` 落在临时解包目录 ``sys._MEIPASS``，
# 而 .py 源码**不随包分发**（只编译进 exe 内部的 PYZ 存档）。于是上面这套「枚举源码文件
# 算内容哈希」在打包态只找得到随包分发的模板，``core/render_bridge.py`` 与整个 ``engine/**``
# 一个都进不了指纹。
#
# 后果不是「少算几项」这么轻：冻结态指纹退化成**只随模板变化**，于是「用新引擎代码重新打包
# 一个 exe」得到的指纹与旧 exe 完全相同 —— 用户升级 exe 后面板仍认为旧产物有效，继续发旧图
# （「页面还是英文名」会在升级 exe 后原样复现，只是触发条件从「改源码」变成「重新打包」）。
#
# 修法：冻结态把 **exe 自身内容** 的 sha256 当成一个额外条目混进摘要。onefile 的 exe 里同时
# 含模板资源与编译后的引擎/桥接代码，哈希它对「代码版本」是完备的：内容相同重建 exe -> 指纹
# 不变（不会白重画），内容变了 -> 指纹必变（不会发旧图）。
# 源码态一律不走这条分支，指纹逐字节不变（不制造新的「源码/exe 互相判过期」）。
_FROZEN_EXE_LABEL = "<frozen-exe>"
# exe 摘要缓存：键 = (路径, size, mtime_ns)，值 = 内容 sha256。首屏会反复问「过期没」，
# 但每进程只允许读一遍 15MB 的 exe；exe 被换掉（size/mtime 变）才重读。
# 读失败也缓存（值 None），避免每次调用都重试读大盘并重复打日志。
_FROZEN_EXE_DIGEST_CACHE: dict[tuple[str, int, int], str | None] = {}


def _iter_py_sources(root: Path) -> list[Path]:
    """递归收集 ``root`` 下的 .py 源码（跳过 __pycache__ 等），deterministic 排序。"""
    if not root.is_dir():
        return []
    found: list[Path] = []
    try:
        for path in root.rglob("*.py"):
            if not path.is_file():
                continue
            if any(part in _PY_SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            found.append(path)
    except OSError as exc:
        logger.debug("枚举 %s 下的源码失败：%s", root, exc)
    return sorted(found)


def _renderer_source_files() -> list[Path]:
    """参与指纹的渲染代码文件（deterministic 排序，保证同代码同指纹）。"""
    files: list[Path] = []
    bridge = APP_ROOT / "core" / "render_bridge.py"
    if bridge.is_file():
        files.append(bridge)
    # 非游戏条目判定（core.non_game）直接决定六页的库范围与展示名，
    # 它变了页面内容就会变：不纳入指纹的话，改了它仍会继续发旧产物。
    non_game_mod = APP_ROOT / "core" / "non_game.py"
    if non_game_mod.is_file():
        files.append(non_game_mod)
    # 授权归属判定（core.ownership）**同样直接决定六页的库范围**（哪一款游戏算这个账号的），
    # 与 non_game 是一个量级：不纳入指纹的话，改了它六页指纹不变 -> 面板继续发旧产物
    # （实测：改完 ownership 剔除赛博朋克后，墓园/时光轴里的赛博朋克还在）。
    ownership_mod = APP_ROOT / "core" / "ownership.py"
    if ownership_mod.is_file():
        files.append(ownership_mod)
    render_dir = APP_ROOT / "render"
    try:
        files.extend(sorted(f for f in render_dir.glob("*.py") if f.is_file()))
        files.extend(sorted(f for f in (render_dir / "templates").glob("*") if f.is_file()))
    except OSError as exc:
        logger.debug("枚举渲染代码文件失败：%s", exc)
    # 引擎层：页面里的统计/画像/性价比/类型等都是引擎算出来的，改了引擎页面内容也会变
    files.extend(_iter_py_sources(APP_ROOT / "engine"))
    return files


def _frozen_exe_path() -> Path | None:
    """冻结态下 exe 自身的路径；源码态返回 None（源码态行为必须保持原样）。"""
    if not getattr(sys, "frozen", False):
        return None
    exe = getattr(sys, "executable", None)
    if not exe:
        return None
    return Path(exe)


def _frozen_exe_digest(path: Path) -> str | None:
    """exe 内容的 sha256（分块读取，不把 15MB 常驻内存）；读不到返回 None。

    按 ``(路径, size, mtime_ns)`` 进程内缓存：首屏会反复问「过期没」，但每进程只读一遍 exe。
    读失败（极端情况）同样缓存，并降级回「只枚举包内可见文件」—— 此时指纹会退回本修复前的
    行为，所以用 warning 让它在日志里可见，别悄悄失效。
    """
    stat_ok = True
    key: tuple[str, int, int] = (str(path), 0, 0)
    try:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
    except OSError as exc:
        stat_ok = False
        logger.warning("取打包 exe 信息失败，渲染指纹将不含 exe（等同于修复前的行为）：%s：%s", path, exc)
    value: str | None = None
    with _RENDERER_FINGERPRINT_LOCK:
        if key in _FROZEN_EXE_DIGEST_CACHE:
            return _FROZEN_EXE_DIGEST_CACHE[key]
    if stat_ok:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            value = digest.hexdigest()
        except OSError as exc:
            logger.warning("读取打包 exe 内容失败，渲染指纹将不含 exe（等同于修复前的行为）：%s：%s", path, exc)
    with _RENDERER_FINGERPRINT_LOCK:
        # 只留当前 exe 的摘要，不长期持有老 exe 的条目（更不持有 exe 内容本身）
        _FROZEN_EXE_DIGEST_CACHE.clear()
        _FROZEN_EXE_DIGEST_CACHE[key] = value
    return value


def _compute_renderer_fingerprint() -> str:
    """渲染代码的内容哈希（文件相对路径 + 各自内容 sha256）。"""
    digest = hashlib.sha256()
    digest.update(_RENDERER_FINGERPRINT_VERSION.encode("utf-8") + b"\n")
    for path in _renderer_source_files():
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.debug("读取渲染代码失败（跳过该文件）：%s：%s", path, exc)
            continue
        try:
            rel = path.relative_to(APP_ROOT).as_posix()
        except ValueError:
            rel = path.name
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(raw).hexdigest().encode("ascii") + b"\n")
    # 冻结态：源码不进包（只编译进 exe 内部的 PYZ），必须把 exe 自身内容也当代码版本的一部分
    exe = _frozen_exe_path()
    if exe is not None:
        exe_digest = _frozen_exe_digest(exe)
        if exe_digest is not None:
            digest.update(_FROZEN_EXE_LABEL.encode("utf-8") + b"\0")
            digest.update(exe_digest.encode("ascii") + b"\n")
    return digest.hexdigest()


def _renderer_fingerprint(force: bool = False) -> str:
    """当前渲染代码指纹。

    进程内缓存一次计算（首屏会反复问「过期没」，不能每次都读一遍文件）；
    测试要模拟「渲染代码变了」时传 ``force=True`` 强制重算。
    """
    global _RENDERER_FINGERPRINT_CACHE
    with _RENDERER_FINGERPRINT_LOCK:
        if force or _RENDERER_FINGERPRINT_CACHE is None:
            _RENDERER_FINGERPRINT_CACHE = _compute_renderer_fingerprint()
        return _RENDERER_FINGERPRINT_CACHE


def _pages_manifest_path(outdir: Path | None = None) -> Path:
    return (outdir or _output_dir()) / _PAGES_MANIFEST_NAME


def _read_pages_manifest(outdir: Path | None = None) -> dict:
    """读产物清单；缺失/损坏一律当空清单（绝不抛）。"""
    try:
        raw = _pages_manifest_path(outdir).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _remember_page_artifact(kind: str, out, account_id: str | None) -> None:
    """记录某个页面产物属于哪个账号、用哪版渲染代码画的（失败只记日志，不影响渲染结果）。"""
    try:
        outdir = _output_dir()
        path = Path(out)
        with _PAGES_MANIFEST_LOCK:
            data = _read_pages_manifest(outdir)
            pages = data.get("pages")
            if not isinstance(pages, dict):
                pages = {}
            pages[str(kind)] = {
                "file": path.name,
                "account_id": str(account_id or ""),
                "generated_at": _now_iso(),
                # 生成这份产物时的渲染代码指纹：对不上就是「用旧代码画的」，要重画
                "renderer": _renderer_fingerprint(),
            }
            data["pages"] = pages
            data["_meta"] = {
                "purpose": "面板首屏秒开用的页面产物清单（可安全删除，会自动重建）",
                "renderer_fingerprint_version": _RENDERER_FINGERPRINT_VERSION,
            }
            tmp = _pages_manifest_path(outdir).with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(_pages_manifest_path(outdir))
    except Exception as exc:
        logger.debug("记录页面产物清单失败：%s", exc)


def _find_existing_page_file(outdir: Path, kind: str) -> Path | None:
    """按文件名约定找已有产物（同一 kind 有多个时取最新修改的那个）。"""
    best: Path | None = None
    for pattern in _PAGE_FILE_PATTERNS.get(kind, ()):
        try:
            candidates = [c for c in outdir.glob(pattern) if c.is_file()]
        except Exception:
            candidates = []
        for cand in candidates:
            if best is None or cand.stat().st_mtime > best.stat().st_mtime:
                best = cand
    return best


def page_artifact_state(kind: str, account_id: str | None = None) -> dict:
    """某个页面产物当前能不能**直接拿来用**（不渲染）。

    返回 ``{"file": Path|None, "exists": bool, "other_account": bool,
    "stale": bool, "renderer_changed": bool}``：

      * ``file`` 非空 = 可以拿来直接用（文件在、不是别的账号的、且**渲染代码没变**）；
      * ``other_account=True`` = 文件在，但清单记录它属于另一个账号 —— 必须重画，
        否则会把别人的图当成自己的（本机多个账号时会踩）；
      * ``stale=True`` = 文件在，但它是**旧渲染代码**画的（清单里的渲染器指纹对不上当前
        指纹，或老产物压根没记指纹）—— 不能直接发，否则用户看到的还是老图
        （T1 中文名修复被旧产物挡住的根因就是这个）。此时 ``file`` 仍为 ``None``。

    ``exists`` 只看「文件在不在」，与可用性无关，供调用方区分「没生成」和「过期」。
    """
    state = {"file": None, "exists": False, "other_account": False,
             "stale": False, "renderer_changed": False}
    if kind not in PAGE_KINDS:
        return state
    outdir = _output_dir()
    manifest = _read_pages_manifest(outdir)
    pages = manifest.get("pages") if isinstance(manifest.get("pages"), dict) else {}
    recorded = pages.get(kind) if isinstance(pages, dict) else None
    recorded = recorded if isinstance(recorded, dict) else {}
    current = str(account_id or "")
    recorded_account = str(recorded.get("account_id") or "")

    found: Path | None = None
    recorded_name = str(recorded.get("file") or "")
    if recorded_name:
        candidate = outdir / recorded_name
        if candidate.is_file():
            found = candidate
    if found is None:
        found = _find_existing_page_file(outdir, kind)     # 老产物 / 清单缺失时兜底
    if found is None:
        return state

    state["exists"] = True
    # 清单里**有这条记录**时，归属校验必须**严格相等**：空串也是「一个账号」（零账号状态），
    # 不能被当成「不限制」。旧写法「两边都非空才比较」会在任一侧为空时整段跳过校验 ——
    # 「上一次用过的账号的六张页面」会被当成当前（无账号）状态的图直接发出去，反向也会把
    # 空账号的产物发给指定账号。两边都为空视为同一个（空）账号，允许复用，否则每次打开都白重画。
    # 清单里**没有**这条记录（老产物兜底路径）时保持原行为：交给下面的指纹判定。
    if recorded and recorded_account != current:
        state["other_account"] = True
        return state

    # 渲染代码换过版本 → 这份产物已经过期，必须重画（不删文件，渲染时原子覆盖）
    recorded_renderer = str(recorded.get("renderer") or "")
    if recorded_renderer != _renderer_fingerprint():
        state["stale"] = True
        state["renderer_changed"] = True
        return state

    state["file"] = found
    return state


def list_existing_pages(account_id: str | None = None) -> dict:
    """**只列已经生成好的**页面产物（毫秒级返回，不渲染、不联网、不读库）。

    面板首屏用它自动铺满六个块；没有产物的那种返回 ``ok=False, missing=True``，
    前端显示「尚未生成」并提示点「生成 / 刷新全部页面」，不是错误。
    """
    outdir = _output_dir()
    pages = []
    for kind in PAGE_KINDS:
        item: dict[str, Any] = {
            "kind": kind,
            "label": PAGE_LABELS.get(kind, kind),
            "url": f"/page/{kind}",
        }
        state = page_artifact_state(kind, account_id)
        if state["file"] is None:
            # 三种「不能用」要分开说，别让用户以为坏了、也别让旧图冒充「已就绪」
            item.update(ok=False, missing=True)
            if state["other_account"]:
                item["error"] = "还没有为当前账号生成（点上面的「生成 / 刷新全部页面」）"
            elif state["stale"]:
                item["stale"] = True
                item["error"] = "渲染逻辑已更新，将重新生成"
            else:
                item["error"] = "尚未生成（点上面的「生成 / 刷新全部页面」）"
        else:
            path = state["file"]
            try:
                stat = path.stat()
                item.update(ok=True, file=str(path), bytes=stat.st_size,
                            generated_at=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"))
            except OSError:
                item.update(ok=True, file=str(path), bytes=0)
        pages.append(item)

    return {"output_dir": str(outdir), "only_existing": True, "pages": pages}


def state() -> dict:
    """面板当前状态：写回按钮 + 库缓存条目数。"""
    writeback = False
    try:
        writeback = bool(cli._load_writeback_state())
    except Exception as exc:
        logger.debug("读取写回状态失败：%s", exc)
    return {"writeback": writeback, "cached_libraries": len(_LIB_CACHE)}


# ------------------------------------------------------------------ 日志尾巴

# 面板「看日志」用：只读 logs/ 目录内的日志，最多回读这么多字节 / 这么多行。
_LOG_TAIL_DEFAULT = 200
_LOG_TAIL_MAX = 2000
_LOG_TAIL_MAX_BYTES = 512 * 1024


def logs_dir() -> Path:
    """日志目录：与 main.setup_logging 写入的位置一致（单一来源）。"""
    return Path(cli.LOGS_DIR)


def _pick_log_file(base: Path) -> Path | None:
    """在 logs/ 目录里挑一个日志文件；**绝不越出该目录**（防 ../ 与软链接逃逸）。"""
    try:
        root = base.resolve()
    except OSError:
        return None
    if not root.is_dir():
        return None
    primary = root / "game-life.log"
    candidates = [primary] if primary.is_file() else []
    if not candidates:
        try:
            candidates = sorted(
                (f for f in root.glob("*.log") if f.is_file()),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            candidates = []
    for picked in candidates:
        try:
            resolved = picked.resolve()
        except OSError:
            continue
        if root in resolved.parents:  # 解析后仍必须落在 logs/ 内
            return resolved
    return None


def _read_tail_lines(path: Path, limit: int) -> list[str]:
    """只回读文件末尾（最多 _LOG_TAIL_MAX_BYTES），日志再大也不会把内存吃满。"""
    size = path.stat().st_size
    start = max(0, size - _LOG_TAIL_MAX_BYTES)
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read()
    if start > 0:  # 从中间截断：丢掉可能不完整的第一行
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    return data.decode("utf-8", errors="replace").splitlines()[-limit:]


def get_log_tail(tail: int | str | None = _LOG_TAIL_DEFAULT) -> dict:
    """日志文件路径 + 最近 N 行（只读、限定在 logs/ 内、出错友好降级，不抛 500）。

    返回 ``{path, lines, total_bytes, missing?, error?, message?}``。
    """
    try:
        limit = int(tail)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        limit = _LOG_TAIL_DEFAULT
    limit = max(1, min(limit, _LOG_TAIL_MAX))

    base = logs_dir()
    path = _pick_log_file(base)
    if path is None:
        return {
            "path": str(base / "game-life.log"),
            "lines": [],
            "missing": True,
            "tail": limit,          # 与「读到文件」分支一致：如实回传生效的行数上限
            "message": "还没有日志文件（面板首次运行、或日志已被清理）",
        }
    try:
        lines = _read_tail_lines(path, limit)
        total = path.stat().st_size
    except OSError as exc:
        logger.warning("读取日志失败：%s", exc)
        return {"path": str(path), "lines": [], "error": f"读取日志失败：{exc}"}
    return {"path": str(path), "lines": lines, "total_bytes": total, "tail": limit}


# ------------------------------------------------------------------ 中文名 / 封面

_NAME_CN_CACHE: dict = {}
_NAME_CN_TS: float = 0.0
_COVER_MEMO: dict[tuple, tuple[float, bool]] = {}
_COVER_TTL = 60.0
# 封面朝向缓存：按真实像素判定要读文件头，60 秒内复用同一结果；抓封面后一并清空。
_COVER_KIND_MEMO: dict[tuple, tuple[float, str]] = {}
_COVER_KIND_TTL = 60.0


def _name_cn(appid) -> str:
    """中文名：元数据本地缓存 → 本机 appinfo → 百科补充表（全部只读、不联网）。

    抓取到的中文名可能来自：Steam 官方多语言数据、蒸汽平台官方商店、
    百度百科词条、人工校对补充表；这里只负责按优先级取第一个非空值。
    """
    try:
        from core import metadata_cn

        value = str(metadata_cn.name_cn_for(appid) or "").strip()
        if value:
            return value
    except Exception:
        pass
    try:
        table = _name_cn_table()
        value = str(table.get(int(appid)) or "").strip()
        if value:
            return value
    except Exception:
        pass
    try:
        from core import name_cn_baike

        return str(name_cn_baike.name_cn_for(appid) or "").strip()
    except Exception:
        return ""


def _name_cn_table() -> dict:
    """缓存「appid -> 中文名」整表（30 秒内复用，避免逐行查库）。"""
    global _NAME_CN_CACHE, _NAME_CN_TS
    now = time.time()
    if _NAME_CN_CACHE and now - _NAME_CN_TS < 30.0:
        return _NAME_CN_CACHE
    table: dict = {}
    try:
        from core import store_api

        table = store_api.cached_names_cn()
    except Exception:
        table = {}
    _NAME_CN_CACHE = table
    _NAME_CN_TS = now
    return table


def cover_path(appid, steam_path: str | None = None):
    """本地封面文件路径（缓存 → Steam 客户端，不联网）。"""
    try:
        from core import artwork

        return artwork.cover_for_panel(appid, steam_path)
    except Exception as exc:
        logger.warning("读取封面失败：%s", exc)
        return None


def _has_cover(appid, steam_path: str | None = None) -> bool:
    key = (str(steam_path or ""), int(appid) if str(appid).isdigit() else -1)
    now = time.time()
    hit = _COVER_MEMO.get(key)
    if hit and now - hit[0] < _COVER_TTL:
        return hit[1]
    found = cover_for_panel(appid, steam_path) is not None
    _COVER_MEMO[key] = (now, found)
    return found


def _cover_url(appid, steam_path: str | None = None) -> str:
    """面板用的封面地址（没有本地图就返回空串，前端不渲染 img）。"""
    return f"/covers/{int(appid)}.jpg" if _has_cover(appid, steam_path) else ""


def _no_art_appids() -> set[int]:
    """官方 CDN 确认无图（404）的 appid 集合（落盘名单见 ``core.artwork.no_art_appids``）。

    只读本地小 JSON，**绝不联网**；模块缺失 / 文件损坏一律退化为空集合，绝不抛。
    """
    try:
        from core import artwork

        return artwork.no_art_appids()
    except Exception as exc:
        logger.debug("读取官方无图名单失败：%s", exc)
        return set()


def cover_path_ex(appid, steam_path: str | None = None, size: str = "portrait"):
    """指定尺寸的本地封面路径（size=portrait 取竖版 600x900，缺了回落横版）。"""
    try:
        from core import artwork

        return artwork.local_cover(appid, steam_path, size)
    except Exception as exc:
        logger.debug("读取封面(%s)失败：%s", size, exc)
        return None


def cover_for_panel(appid, steam_path: str | None = None):
    """面板封面路径：**优先竖版**（Steam 标准库封面 600x900），缺了回落横版。"""
    return cover_path_ex(appid, steam_path, "portrait") or cover_path(appid, steam_path)


def _cover_kind(appid, steam_path: str | None = None) -> str:
    """封面朝向：portrait（竖版）/ landscape（横版）/ none（没有）。

    面板据此统一卡片：竖版铺满卡片，横版完整居中留边，绝不拉伸、绝不裁切。

    **朝向一律按「实际要服务的那张图」的真实像素判定**——即
    :func:`cover_for_panel`（与 ``/covers/<appid>.jpg`` 同源，见 server._serve_cover）。
    绝不用 ``cover_path_ex(..., "portrait")`` 是否返回路径来推断：该函数在找不到
    真竖版时会**回落横版**（``_LIBRARY_PORTRAIT_NAMES`` 里就含 ``header.jpg``），
    于是 460x215 的横版会被误标成 portrait，前端按 2:3 卡片 + ``object-fit: cover``
    渲染就把横版海报两侧的文字裁掉了（用户抱怨的「封面不适配」）。
    """
    try:
        aid = int(appid)
    except (TypeError, ValueError):
        return "none"
    path = cover_for_panel(aid, steam_path)
    if not path:
        return "none"
    key = (str(steam_path or ""), aid)
    now = time.time()
    hit = _COVER_KIND_MEMO.get(key)
    if hit and now - hit[0] < _COVER_KIND_TTL:
        return hit[1]
    try:
        from core import artwork

        kind = str(artwork.cover_kind(path) or "unknown")
    except Exception as exc:
        logger.debug("判定封面朝向失败：%s", exc)
        kind = "unknown"
    # 前端只有 portrait / landscape 两种卡片，square / unknown 都按横版完整显示（不裁切）
    kind = kind if kind in ("portrait", "landscape") else "landscape"
    _COVER_KIND_MEMO[key] = (now, kind)
    return kind


def _coverage(lib: dict | None = None, rows: list[dict] | None = None,
              steam_path: str | None = None) -> dict:
    """覆盖率：本账号库里有多少款已经有中文名 / 封面（缺的可一键补齐）。"""
    steam_path = steam_path or ((lib or {}).get("steam_path") if lib else None)
    play_stats = (lib or {}).get("play_stats") or {}

    if rows is None:
        try:
            rows = _games_rows(lib or {})
        except Exception:
            rows = []
    rows = rows or []
    name_cn_count = sum(1 for r in rows if str(r.get("name_cn") or "").strip())
    cover_count = sum(1 for r in rows if r.get("cover"))

    cover_dir = ""
    try:
        from core import artwork

        try:
            cover_dir = str(artwork.stats().get("dir") or "")
        except Exception:
            cover_dir = str(artwork.covers_dir())
    except Exception:
        pass

    local_index: dict = {}
    try:
        from core import appinfo

        local_index = appinfo.stats(steam_path)
    except Exception:
        pass

    # 价格覆盖率：只读本地 games 缓存（绝不联网），失败退化为 0 而不是让整页 500
    price_count = 0
    price_appids: list[int] = []
    for row in rows:
        try:
            price_appids.append(int(row.get("appid")))
        except (TypeError, ValueError):
            continue
    if not price_appids:
        for raw_appid in play_stats:
            try:
                price_appids.append(int(raw_appid))
            except (TypeError, ValueError):
                continue
    if price_appids:
        try:
            from core import store_api

            cached = store_api.cached_metadata(price_appids)
            price_count = sum(
                1 for appid in price_appids
                if store_api.price_state(cached.get(appid)) != "unknown"
            )
        except Exception as exc:
            logger.debug("统计价格覆盖率失败：%s", exc)
            price_count = 0

    total = len(rows) or len(play_stats)
    return {
        "total": total,
        "name_cn": name_cn_count,
        "cover": cover_count,
        "price": price_count,
        "missing_name_cn": max(0, total - name_cn_count),
        "missing_cover": max(0, total - cover_count),
        "missing_price": max(0, total - price_count),
        "cover_dir": cover_dir,
        "local_index": local_index,
    }


def get_cover(appid, steam_path: str | None = None) -> dict:
    """单张封面的元信息（面板 /api/cover）。"""
    try:
        appid_int = int(appid)
    except (TypeError, ValueError):
        raise ApiError(f"非法 appid：{appid!r}", 400)
    path = cover_path(appid_int, steam_path)
    return {
        "appid": appid_int,
        "ok": bool(path),
        "url": f"/covers/{appid_int}.jpg" if path else "",
        "path": str(path or ""),
        "bytes": path.stat().st_size if path else 0,
        "name_cn": _name_cn(appid_int),
    }


# 系列详情页的封面补齐：并发受限 + 总耗时预算，且只在允许联网时才出网。
SERIES_COVER_WORKERS = 6
SERIES_COVER_BUDGET = 18.0
SERIES_COVER_TIMEOUT = 6.0
SERIES_COVER_LIMIT = 120

_SERIES_COVER_LOCK = threading.RLock()
_SERIES_COVER_MEMO: dict[tuple, dict] = {}


def _series_network_disabled() -> bool:
    """是否禁止联网：复用系列引擎的开关（env 显式关闭，或正跑在 pytest 里）。"""
    try:
        from engine.series_tree import _network_disabled

        return bool(_network_disabled())
    except Exception:
        return "pytest" in sys.modules


def _cover_info(appid, steam_path: str | None = None) -> tuple[str, str]:
    """(封面地址, 朝向)；没有本地图时地址为空串（前端渲染占位块，不裂图）。"""
    url = _cover_url(appid, steam_path)
    return url, (_cover_kind(appid, steam_path) if url else "none")


def ensure_series_covers(
    appids,
    steam_path: str | None = None,
    *,
    workers: int = SERIES_COVER_WORKERS,
    budget: float = SERIES_COVER_BUDGET,
    timeout: float = SERIES_COVER_TIMEOUT,
    limit: int = SERIES_COVER_LIMIT,
) -> dict:
    """给一批 appid 按需补齐封面（官方 CDN），返回 requested/ok/failed/skipped。

    约束（对应用户「每款都要有封面，但不能卡死」）：
      * **并发受限**：只开 ``workers``（默认 6）个工作线程，从队列里取活干；
      * **总耗时预算**：到点就停，没轮到的算 skipped，页面照常打开；
      * 单款超时 + 任何异常都被吞掉，该款退化成占位块；
      * 结果进程内记忆，同一个系列反复打开不重复下载、也不重复等。
    """
    stats = {"requested": 0, "ok": 0, "failed": 0, "skipped": 0}
    try:
        ids = [int(a) for a in (appids or [])]
    except Exception:
        return stats
    ids = [a for a in ids if a > 0][: max(0, int(limit))]
    if not ids:
        return stats

    key_path = str(steam_path or "")
    with _SERIES_COVER_LOCK:
        missing = [a for a in ids if (key_path, a) not in _SERIES_COVER_MEMO]
    if not missing:
        return stats

    stats["requested"] = len(missing)
    if _series_network_disabled():
        stats["skipped"] = len(missing)
        return stats

    try:
        from core import artwork
    except Exception as exc:
        logger.debug("封面补齐不可用：%s", exc)
        stats["skipped"] = len(missing)
        return stats

    deadline = time.time() + max(1.0, float(budget))
    queue = list(missing)
    lock = threading.Lock()
    results: list[dict] = []

    def _pop():
        with lock:
            return queue.pop() if queue else None

    def _worker() -> None:
        while True:
            appid = _pop()
            if appid is None:
                return
            if time.time() >= deadline:
                row = {"appid": appid, "ok": False, "skipped": True}
            else:
                try:
                    cover = artwork.ensure_cover(appid, steam_path=steam_path, timeout=timeout)
                    row = {"appid": appid, "ok": bool(cover.get("ok")), "skipped": False}
                except Exception as exc:                 # 单款失败绝不冒泡
                    logger.debug("补齐封面失败 appid=%s：%s", appid, exc)
                    row = {"appid": appid, "ok": False, "skipped": False}
            with lock:
                results.append(row)

    # 自己起 daemon 线程（不用 ThreadPoolExecutor）：
    # 这样 join 到点就能返回，页面不会被一个卡住的下载拖住。
    threads = [
        threading.Thread(target=_worker, name="gl-cover", daemon=True)
        for _ in range(max(1, min(int(workers), len(missing))))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.time()))

    now = time.time()
    with _SERIES_COVER_LOCK:
        for row in results:
            appid = int(row.get("appid"))
            if row.get("skipped"):
                continue
            _SERIES_COVER_MEMO[(key_path, appid)] = {
                "ok": bool(row.get("ok")),
                "kind": "unknown",
            }
            if row.get("ok"):
                # _has_cover 会把「没图」缓存 60 秒，刚下载完必须一起刷新，
                # 否则详情页会一直显示占位块（实测踩过）。
                _COVER_MEMO[(key_path, appid)] = (now, True)

    stats["ok"] = sum(1 for r in results if r.get("ok"))
    stats["failed"] = sum(1 for r in results if not r.get("ok") and not r.get("skipped"))
    # 到点还没试过的（含仍在排队/被超时打断的）全部算 skipped：
    # 页面照常渲染占位块，下次打开再补，绝不为了等图把页面卡住。
    stats["skipped"] = max(0, len(missing) - stats["ok"] - stats["failed"])
    return stats


def _series_member_rows(lib: dict, item: dict, steam_path: str | None) -> list[dict]:
    """把族谱里的系列成员整理成详情页卡片行（含中文名 / 状态 / 时长 / 类型）。"""
    from core import render_bridge

    cn_table = genre_cn_table()
    play_stats = lib.get("play_stats") or {}
    classified = lib.get("classified") or {}
    games_meta = lib.get("games_meta") or {}
    rows: list[dict] = []

    for game in item.get("games") or []:
        if not isinstance(game, dict):
            continue
        try:
            appid = int(game.get("appid"))
        except (TypeError, ValueError):
            continue
        if is_non_game(appid):
            continue
        # 三态（与 engine.series_tree 同源）：
        #   owned  = 确实拥有（core.ownership 的 flag == 0）；
        #   played = 未拥有但本账号玩过（家庭库 / 免费体验，playtime > 0）；
        # 用 `appid in play_stats` 兜底：引擎没给 played 字段（旧数据）时按「在库里且有时长」判。
        owned = bool(game.get("owned"))
        played = bool(game.get("played")) or (
            not owned and appid in play_stats
            and int((play_stats.get(appid) or {}).get("playtime", 0) or 0) > 0
        )
        active = owned or played
        raw = play_stats.get(appid) or {}
        info = classified.get(appid) or {}
        meta = games_meta.get(appid) or games_meta.get(str(appid)) or {}
        status = str(game.get("status") or info.get("status") or "") if active else ""
        status_name = str(info.get("status_name") or status) if active else ""
        hours = render_bridge.minutes_to_hours(raw.get("playtime", 0)) if active else 0
        cover, kind = _cover_info(appid, steam_path)
        rows.append(
            {
                "appid": appid,
                "name": str(game.get("name") or render_bridge.game_name(appid, meta)),
                "name_cn": str(game.get("name_cn") or _name_cn(appid) or ""),
                "owned": owned,
                "played": played,
                "status": status,
                "status_name": status_name,
                "playtime_hours": hours,
                "genres": localize_genres(meta.get("genres"), cn_table) if active else [],
                "cover": cover,
                "cover_kind": kind,
            }
        )
    rows.sort(key=lambda r: (not r["owned"], not r["played"],
                             -float(r["playtime_hours"] or 0), r["name_cn"] or r["name"]))
    return rows


def get_series_detail(
    name: str,
    account_id: str | None = None,
    with_metadata: bool = False,
    steam_path: str | None = None,
    fetch_covers: bool = True,
) -> dict:
    """某个系列的全部作品（详情页 /api/series）。

    name 是面板里显示的系列名（已本地化，中文优先）。
    未拥有的作品也会返回，并尽力从官方 CDN 补封面（见 ensure_series_covers）。
    """
    wanted = str(name or "").strip()
    if not wanted:
        raise ApiError("缺少系列名（name 参数）", 400)

    lib = _require_library(account_id, with_metadata, steam_path)
    path = lib.get("steam_path")

    try:
        section = _series_section(lib)
    except Exception as exc:
        logger.warning("族谱计算失败：%s", exc)
        raise ApiError(f"族谱计算失败：{exc}", 500) from exc

    candidates = section.get("series") or []
    item = next((s for s in candidates if str(s.get("series") or "").strip() == wanted), None)
    if item is None:                                   # 兼容传入官方英文名
        item = next(
            (s for s in candidates
             if str(s.get("official_name") or "").strip().lower() == wanted.lower()),
            None,
        )
    if item is None:
        raise ApiError(f"没有找到系列「{wanted}」", 404)

    rows = _series_member_rows(lib, item, path)
    covers = {"requested": 0, "ok": 0, "failed": 0, "skipped": 0}
    if fetch_covers:
        missing = [r["appid"] for r in rows if not r["cover"]]
        if missing:
            covers = ensure_series_covers(missing, path)
        for row in rows:                               # 补齐后重新取一次本地地址
            loc = _cover_info(row["appid"], path)
            if loc[0]:
                row["cover"], row["cover_kind"] = loc

    return {
        "series": str(item.get("series") or wanted),
        "official_name": str(item.get("official_name") or ""),
        "account_id": lib.get("account_id"),
        "owned_count": sum(1 for r in rows if r["owned"]),
        "played_count": sum(1 for r in rows if r["played"]),
        "total_count": len(rows),
        "with_cover": sum(1 for r in rows if r["cover"]),
        "playtime_hours": round(
            sum(float(r["playtime_hours"] or 0) for r in rows if r["owned"] or r["played"]), 2
        ),
        "covers": covers,
        "games": rows,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _not_found_appids(stat) -> list[int]:
    """从抓取统计里挑出「官方 CDN 明确 404（无图）」的 appid。

    404 = 这个 appid 根本没有封面可抓（未发售 / 已下架 / 只有 DLC 图），
    **不算错误**，但下次再抓也不会变，所以值得记进 ``core.artwork`` 的官方无图名单；
    超时 / 网络错误**不算**（下次还要再试）。
    """
    if not isinstance(stat, dict) or stat.get("error"):
        return []
    rows = stat.get("results")
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, (list, tuple)):
        return []
    out: list[int] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("ok"):
            continue
        if "404" not in str(row.get("error") or ""):
            continue
        try:
            appid = int(row.get("appid"))
        except (TypeError, ValueError):
            continue
        if appid > 0:
            out.append(appid)
    return out


def _mark_covers_not_found(stat):
    """给封面统计补一个 ``not_found``：抓不到的里头有多少是 404（官方 CDN 无图）。

    404 说明这个 appid 根本没有封面可抓（未发售 / 已下架 / 只有 DLC 图），
    **不算错误**，但也要让用户看得见，所以单列一个计数。
    """
    if isinstance(stat, dict) and not stat.get("error"):
        stat["not_found"] = len(_not_found_appids(stat))
    return stat


def _localize_message(label: str, stat) -> str:
    """把抓取结果压成一句话（兼容不同模块的字段命名）。"""
    if not isinstance(stat, dict):
        return ""
    if stat.get("error"):
        return f"{label}：{stat['error']}"
    ok = stat.get("ok_count", stat.get("ok", 0))
    failed = stat.get("fail_count", stat.get("failed", 0))
    total = stat.get("total", 0)
    if isinstance(ok, bool):        # 有些模块的 ok 是布尔「整体是否成功」
        ok = stat.get("ok_count", 0)
    return f"{label}：成功 {ok} / 失败 {failed} / 共 {total}"


def _brief_rows(results) -> list[tuple]:
    """把各模块形态不一的 results 归一成 [(appid, row), ...]。

    - core.metadata_cn.ensure_names_cn → dict[appid -> row]
    - core.artwork.ensure_covers      → list[row]，appid 就在行内
    未知形态一律忽略，绝不抛异常（面板只能 200）。
    """
    rows: list[tuple] = []
    if isinstance(results, dict):
        for appid, row in results.items():
            if isinstance(row, dict):
                rows.append((appid, row))
    elif isinstance(results, (list, tuple)):
        for row in results:
            if isinstance(row, dict):
                rows.append((row.get("appid", ""), row))
    return rows


def _brief(stat) -> dict | None:
    """抓取结果瘦身：只留统计与少量样例，避免响应体过大。"""
    if not isinstance(stat, dict):
        return None
    if stat.get("error"):
        return {"error": stat["error"]}
    out = {k: v for k, v in stat.items() if k != "results"}
    sample = []
    for appid, row in _brief_rows(stat.get("results"))[:10]:
        sample.append({
            "appid": appid,
            "ok": bool(row.get("ok")),
            "name_cn": str(row.get("name_cn") or ""),
            "source": str(row.get("source") or ""),
            "error": str(row.get("error") or ""),
        })
    out["sample"] = sample
    return out


def post_localize(
    account_id: str | None = None,
    scope: str = "both",
    limit: int = 20,
    steam_path: str | None = None,
    with_metadata: bool = False,
) -> dict:
    """抓取中文名 / 封面（面板按钮「抓取中文名与封面」）。

    scope: names / covers / both；limit 默认 20 款（跑太多会很慢）。
    单个游戏失败不影响整体，模块缺失时返回友好错误而不是 500。
    """
    if scope not in ("names", "covers", "both"):
        raise ApiError(f"scope 只能是 names / covers / both，收到 {scope!r}", 400)
    try:
        limit = int(limit or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 500))

    lib = _require_library(account_id, bool(with_metadata), steam_path)
    appids = [int(a) for a in (lib["play_stats"] or {})]
    games_meta = lib["games_meta"] or {}

    names_stat = covers_stat = None
    messages: list[str] = []

    if scope in ("names", "both"):
        try:
            from core import metadata_cn

            items = [(a, str((games_meta.get(a) or {}).get("name") or "")) for a in appids]
            names_stat = metadata_cn.ensure_names_cn(
                items, steam_path=lib.get("steam_path"), limit=limit
            )
            messages.append(_localize_message("中文名", names_stat))
        except ImportError as exc:
            names_stat = {"error": f"中文名模块暂不可用：{exc}"}
        except Exception as exc:
            logger.warning("抓中文名失败：%s", exc)
            names_stat = {"error": f"抓中文名失败：{exc}"}

    if scope in ("covers", "both"):
        try:
            from core import artwork

            # 抓的不只是「本账号拥有的」，还要补上**族谱各系列框的封面缺口**：
            # 系列里未拥有的作品从来没抓过图，框里于是凑不满 4 张。缺口排在最前，
            # 面板「每批」有上限时也能先把族谱的框补齐（下轮重算缺口自然接着补）。
            targets = list(dict.fromkeys(_series_cover_gaps(lib) + appids))
            covers_stat = artwork.ensure_covers(
                targets, steam_path=lib.get("steam_path"), limit=limit
            )
            covers_stat = _mark_covers_not_found(covers_stat)
            # 把「官方确认无图（404）」的 appid 记进 data/covers/no_art.json：
            # 族谱的提示据此区分「可以点按钮补齐」与「官方暂无封面图」，不再骗用户。
            # 单独 try：这一步失败只影响提示文案，绝不能把抓取结果弄丢。
            try:
                artwork.mark_no_art(_not_found_appids(covers_stat))
            except Exception as exc:
                logger.warning("记录官方无图名单失败：%s", exc)
            message = _localize_message("封面", covers_stat)
            not_found = int((covers_stat or {}).get("not_found") or 0)
            if not_found:
                message += f"（其中 {not_found} 款官方 CDN 无图 404，属正常）"
            messages.append(message)
            remaining = int((covers_stat or {}).get("skipped") or 0)
            if remaining:
                messages.append(f"每批上限 {limit} 款，本轮还有 {remaining} 款没轮到（可再点一次）")
        except ImportError as exc:
            covers_stat = {"error": f"封面模块暂不可用：{exc}"}
        except Exception as exc:
            logger.warning("抓封面失败：%s", exc)
            covers_stat = {"error": f"抓封面失败：{exc}"}

    _COVER_MEMO.clear()
    _COVER_KIND_MEMO.clear()    # 新抓到封面后朝向可能变（横/竖），别复用旧判定
    _LIB_CACHE.clear()          # 名称/封面变了，让下次刷新重新组装
    return {
        "ok": True,
        "scope": scope,
        "limit": limit,
        "names": _brief(names_stat),
        "covers": _brief(covers_stat),
        "coverage": _coverage(lib),   # 内部会用刷新后的行重算
        "message": "；".join(m for m in messages if m) or "已完成",
    }


# ------------------------------------------------------------------ 账号数据补齐（T6）
#
# 用户诉求：「我添加好账号就应该自动爬取好」——加完账号不该让用户自己去点按钮。
# 这里提供一个**进程内单例后台任务**：
#   * 添加账号成功 / 面板加载时发现数据明显不全 -> 把该账号排进一个**全局串行队列**
#     （同一时刻只跑一个账号，多账号排队，避免并发把网络打满）；
#   * 一条守护线程按阶段补齐：系列目录 -> 中文名与元数据 -> 价格 -> 封面 -> 刷新面板缓存；
#   * 前端轮询只读端点 ``GET /api/job`` 看进度（纯读内存，绝不联网）。
#
# 硬边界：
#   * 面板渲染路径（/api/summary 等）**绝不出网**：只有这条后台线程会联网；
#   * 任何阶段失败都只把任务标成 ok:false + 一句人话提示，绝不抛、绝不 hang；
#   * 幂等：结果记在 ``data/localize_state.json``；覆盖率没变差且 30 天内跑过就不再入队
#     （第二次启动因此不会重复出网）；文件损坏 / 缺失一律降级成「需要补齐」，绝不抛。

LOCALIZE_STATE_NAME = "localize_state.json"
LOCALIZE_STATE_TTL_DAYS = 30      # 与元数据缓存同量级：30 天内跑过且没有新缺口就不再跑
JOB_RETRY_SUPPRESS_SECONDS = 600.0  # 一次补齐失败后，同一进程内 10 分钟内不再自动重试
JOB_SERIES_BUDGET_SECONDS = 60.0    # 系列目录阶段的**硬时间预算**：到点就停，剩下的留给后台渐进刷新

_JOB_LOCK = threading.RLock()
_JOB_QUEUE: list[dict] = []
_JOB_THREAD = None                # threading.Thread | None（运行中的守护线程）
_JOB_HISTORY: dict | None = None  # 上一个跑完的任务快照（前端据此弹完成提示）
_JOB_LAST_ATTEMPT: dict[str, float] = {}  # account_id -> 上次尝试时刻（失败后短时抑制，避免反复重跑）

_JOB_STAGE_LABELS = {
    "series": "游戏系列目录",
    "names": "中文名与元数据",
    "price": "价格",
    "covers": "封面",
    "pages": "刷新面板数据",
}


def _job_idle_state() -> dict:
    return {
        "running": False, "pending": 0, "account_id": "", "stage": "idle",
        "stage_label": "空闲", "done": 0, "total": 0,
        "message": "当前没有正在进行的自动补齐任务",
        "started_at": "", "finished_at": "", "ok": None, "error": "",
        "coverage": None, "stage_seconds": {},
        "series_fetched": 0, "series_deferred": 0,
        "price_rounds": [],
        "partial": False, "retry_needed": [],
    }


_JOB_STATE: dict = _job_idle_state()


def _job_network_disabled() -> bool:
    """自动补齐是否禁止出网：显式开关（离线用户可自行设置），或正跑在 pytest 里。

    面板渲染本身**永远**不出网（这是既有约定）；这个开关管的是那条后台补齐线程。
    """
    if os.environ.get("GAMELIFE_JOB_NO_NETWORK"):
        return True
    return _series_network_disabled()


def _job_message(label: str, done: int = 0, total: int = 0, hint: str = "") -> str:
    """给人看的进度文案（前端直接显示，不在前端拼字符串）。"""
    if total and total > 0:
        base = f"正在为你补齐数据：{label} {int(done)}/{int(total)}"
    else:
        base = f"正在为你补齐数据：{label}"
    return base + (f"（{hint}）" if hint else "")


def _job_set(**fields) -> None:
    with _JOB_LOCK:
        _JOB_STATE.update(fields)


def _job_stage(stage: str, label: str, done: int = 0, total: int = 0,
               message: str = "") -> None:
    _job_set(stage=stage, stage_label=label, done=int(done or 0), total=int(total or 0),
             message=message or _job_message(label, done, total))


def job_state() -> dict:
    """只读任务快照（``GET /api/job`` 用）：**纯读内存、绝不联网**，任何情况下都能 200。"""
    with _JOB_LOCK:
        snap = dict(_JOB_STATE)
        snap["pending"] = len(_JOB_QUEUE)
        snap["queue"] = [str(j.get("account_id") or "") for j in _JOB_QUEUE]
        snap["last"] = dict(_JOB_HISTORY) if isinstance(_JOB_HISTORY, dict) else None
    return snap


def _reset_localize_job() -> None:
    """测试 / 诊断用：清空队列与任务状态（**不删任何用户数据文件**）。"""
    global _JOB_HISTORY, _JOB_THREAD
    with _JOB_LOCK:
        _JOB_QUEUE.clear()
        _JOB_STATE.clear()
        _JOB_STATE.update(_job_idle_state())
        _JOB_HISTORY = None
        _JOB_LAST_ATTEMPT.clear()
        if _JOB_THREAD is not None and not _JOB_THREAD.is_alive():
            _JOB_THREAD = None


# ---------------------------------------------------- 幂等状态（data/localize_state.json）

def localize_state_path() -> Path:
    """补齐状态的落盘位置（真实 data 目录，兼容打包运行；不硬编码路径）。"""
    return _stable_data_dir() / LOCALIZE_STATE_NAME


def load_localize_state() -> dict:
    """读 ``data/localize_state.json`` -> ``{account_id: {last_full_at, coverage, runs}}``。

    缺失 / 损坏 / 形态不对一律返回空表（降级成「需要补齐」），**绝不抛**。
    """
    try:
        with open(localize_state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in accounts.items():
        name = str(key or "").strip()
        if name and isinstance(value, dict):
            out[name] = dict(value)
    return out


def save_localize_state(accounts: dict) -> dict:
    """原子落盘（先写 .tmp 再 replace）；任何失败只记日志，绝不抛。"""
    payload = {"updated_at": _now_iso(), "accounts": dict(accounts or {})}
    target = localize_state_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(target)
    except Exception as exc:
        logger.warning("写入补齐状态失败（已忽略）：%s", exc)
    return payload["accounts"]


def _coverage_snapshot(lib) -> dict:
    """把覆盖率压成幂等判断要用的几个整数（失败退化为空 dict，绝不抛）。"""
    try:
        cov = _coverage(lib)
    except Exception as exc:
        logger.debug("计算覆盖率失败：%s", exc)
        return {}
    keys = ("total", "name_cn", "cover", "price",
            "missing_name_cn", "missing_cover", "missing_price")
    out: dict[str, int] = {}
    for key in keys:
        try:
            out[key] = int((cov or {}).get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def _coverage_incomplete(coverage) -> bool:
    """覆盖率里还有缺口吗？（None / 形态不对都当「有缺口」，宁可多跑一次）。"""
    if not isinstance(coverage, dict):
        return True
    for key in ("missing_name_cn", "missing_cover", "missing_price"):
        try:
            if int(coverage.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _coverage_worse(current, stored) -> bool:
    """当前缺口是否比上次记录更多（例如刚买了新游戏）？"""
    if not isinstance(stored, dict):
        return True
    for key in ("missing_name_cn", "missing_cover", "missing_price"):
        try:
            now = int((current or {}).get(key) or 0)
            before = int(stored.get(key) or 0)
        except (TypeError, ValueError):
            return True
        if now > before:
            return True
    return False


def _iso_days_ago(stamp) -> float | None:
    """ISO 时间戳距今多少天；解析不了返回 None。"""
    try:
        return (datetime.now() - datetime.fromisoformat(str(stamp))).total_seconds() / 86400.0
    except (TypeError, ValueError):
        return None


def _localize_needed(account_id: str, coverage=None) -> bool:
    """是否需要为这个账号跑一次自动补齐（幂等判断，纯本地读小 JSON）。"""
    aid = str(account_id)
    last_try = _JOB_LAST_ATTEMPT.get(aid, 0.0)
    if last_try and (time.time() - last_try) < JOB_RETRY_SUPPRESS_SECONDS:
        return False                             # 刚试过（多半是网络炸了）：先别再来一轮
    record = load_localize_state().get(aid)
    if not isinstance(record, dict):
        return True                                  # 从没跑过
    if coverage is not None and not _coverage_incomplete(coverage):
        return False                                 # 什么都不缺：不用跑
    if record.get("partial"):
        return True                                  # 上次只补了一部分（如价格源不通）：下次接着补
    age = _iso_days_ago(record.get("last_full_at"))
    if age is None or age > LOCALIZE_STATE_TTL_DAYS:
        return True                                  # 从没跑成过 / 太久没跑
    if coverage is not None and _coverage_worse(coverage, record.get("coverage")):
        return True                                  # 又出现了新缺口（比如新买了游戏）
    return False                                     # 30 天内跑过且没变差：不重复出网


def _record_localize_state(account_id: str, coverage, *, partial: bool = False,
                           retry_needed=None) -> None:
    """把「刚跑完一次」的结果记进 ``localize_state.json``（失败只降级，绝不抛）。

    ``partial=True``（例如价格源暂时不通）时**不写 ``last_full_at``**，只记这次的时间 /
    缺口 / 该重试的项——:func:`_localize_needed` 因此仍会判定「需要重试」，下一轮只补缺的
    那部分（各阶段本身都是缓存感知的：中文名/封面已有的不会再下载，价格只补没价的）。
    """
    account_id = str(account_id or "").strip()
    if not account_id:
        return
    state = load_localize_state()
    prev = state.get(account_id) if isinstance(state.get(account_id), dict) else {}
    record = {
        "coverage": dict(coverage or {}),
        "runs": int(prev.get("runs") or 0) + 1,
        "partial": bool(partial),
        "retry_needed": [str(item) for item in (retry_needed or [])],
    }
    if partial:
        record["last_partial_at"] = _now_iso()
        if prev.get("last_full_at"):
            record["last_full_at"] = prev["last_full_at"]
    else:
        record["last_full_at"] = _now_iso()
    state[account_id] = record
    save_localize_state(state)


# ------------------------------------------------------------ 入队 / 队列 / 后台线程

def enqueue_localize(account_id, steam_path: str | None = None, reason: str = "",
                     coverage=None, force: bool = False) -> dict:
    """把一个账号排进「自动补齐」队列（**不阻塞调用方、不联网**）。

    幂等：30 天内跑过且覆盖率没变差就不重复入队；同一账号已在队列/正在跑也不重复入队。
    后台线程只在允许联网时启动（pytest / 显式离线开关下只入队，由测试同步排空）。
    """
    aid = str(account_id or "").strip()
    if not aid:
        return {"ok": False, "queued": False, "reason": "缺少 account_id"}
    if not force:
        try:
            if not _localize_needed(aid, coverage):
                return {"ok": True, "queued": False,
                        "reason": "数据已补齐（30 天内跑过且没有新缺口）"}
        except Exception as exc:      # 判断本身失败：按「需要补齐」处理，别拦着用户
            logger.debug("补齐幂等判断失败（按需要补齐处理）：%s", exc)
    with _JOB_LOCK:
        running_same = bool(_JOB_STATE.get("running")) and \
            str(_JOB_STATE.get("account_id") or "") == aid
        queued_same = any(str(j.get("account_id") or "") == aid for j in _JOB_QUEUE)
        if running_same or queued_same:
            return {"ok": True, "queued": False, "reason": "该账号的补齐任务已在队列中"}
        _JOB_QUEUE.append({"account_id": aid, "steam_path": steam_path,
                           "reason": str(reason or "自动补齐")})
        _ensure_job_thread_locked()
    return {"ok": True, "queued": True, "reason": str(reason or "已排入自动补齐队列")}


def _ensure_job_thread_locked() -> bool:
    """按需起一条守护线程（调用方必须已持有 ``_JOB_LOCK``）。"""
    global _JOB_THREAD
    if _job_network_disabled():
        return False
    if _JOB_THREAD is not None and _JOB_THREAD.is_alive():
        return False
    thread = threading.Thread(target=_job_worker, name="gl-localize-job", daemon=True)
    _JOB_THREAD = thread
    thread.start()
    return True


def _job_worker() -> None:
    """守护线程主体：把队列里的任务依次跑完；异常绝不外泄，状态必须落到 ``_JOB_STATE``。"""
    global _JOB_THREAD
    try:
        _drain_job_queue()
    except Exception as exc:                     # 兜底：绝不让线程带着异常静默死掉
        logger.warning("自动补齐线程异常：%s", exc)
        _job_finish(ok=False, error=f"{type(exc).__name__}: {str(exc)[:120]}")
    finally:
        with _JOB_LOCK:
            if _JOB_THREAD is threading.current_thread():
                _JOB_THREAD = None
            more = bool(_JOB_QUEUE)
            if more and not _job_network_disabled():
                _ensure_job_thread_locked()


def _drain_job_queue() -> int:
    """把队列里的任务**在当前线程**依次跑完（后台线程与测试共用这条路径）。"""
    done = 0
    while True:
        with _JOB_LOCK:
            if not _JOB_QUEUE:
                break
            job = _JOB_QUEUE.pop(0)
        _run_localize_job(job)
        done += 1
    return done


def _retry_hints(retry_needed, coverage=None) -> list[str]:
    """把「稍后自动重试」的项写成给人看的短句（前端直接显示）。"""
    cov = coverage if isinstance(coverage, dict) else {}
    keys = {str(item) for item in (retry_needed or [])}
    hints: list[str] = []
    if "price" in keys:
        try:
            miss = int(cov.get("missing_price") or 0)
        except (TypeError, ValueError):
            miss = 0
        hints.append((f"价格有 {miss} 款没取到，稍后会自动重试" if miss
                      else "价格有部分没取到，稍后会自动重试"))
    if "names" in keys:
        hints.append("中文名还有没抓到的，稍后会自动重试")
    if "covers" in keys:
        hints.append("封面还有没抓到的，稍后会自动重试")
    return hints


def _job_finish(ok: bool, error: str = "", coverage: dict | None = None,
                message: str = "", partial: bool = False,
                retry_needed=None) -> None:
    """收尾：三态。

      * ``ok=True, partial=False`` -> 全齐（前端绿色）；
      * ``ok=True, partial=True``  -> 基本补齐、个别项稍后自动重试（前端黄色/中性）；
      * ``ok=False``               -> 关键数据真没抓到（前端红色）。
    """
    global _JOB_HISTORY
    retry = [str(item) for item in (retry_needed or [])]
    with _JOB_LOCK:
        if ok and partial:
            stage, label = "partial", "已补齐（个别项稍后自动重试）"
        elif ok:
            stage, label = "done", "已完成"
        else:
            stage, label = "error", "未完成"
        _JOB_STATE.update(
            running=False, ok=bool(ok), error=str(error or ""),
            finished_at=_now_iso(), coverage=coverage or None,
            partial=bool(partial) and bool(ok), retry_needed=retry,
            stage=stage, stage_label=label,
        )
        if not ok and error:
            _JOB_STATE["message"] = (f"自动补齐没完成：{error}"
                                     "（可点「抓取中文名与封面」手动重试）")
        elif message:
            _JOB_STATE["message"] = message
        _JOB_HISTORY = dict(_JOB_STATE)


# ------------------------------------------------------------------ 各阶段（会出网）


def _owned_series_targets(appids) -> list[str]:
    """本账号拥有的游戏**所属系列**的官方 franchise 名（纯离线计算）。

    ``refresh_series_cache(names=None)`` 会把本机 appinfo 里的**全部** franchise 名
    都抓一遍（实测这台机器 129 个）；大陆网络下官方搜索域名时通时断，
    129 个名字的失败重试能把「首次补齐」拖到十几分钟。族谱只展示「本账号拥有作品的系列」，
    所以自动补齐只抓这些；其它系列仍由面板既有的后台刷新路径
    （``maybe_refresh_async``）在渲染族谱时慢慢补。
    """
    owned: set[int] = set()
    for raw in appids or ():
        try:
            owned.add(int(raw))
        except (TypeError, ValueError):
            continue
    if not owned:
        return []
    from engine import series_tree as st

    names: set[str] = set()
    try:
        from core import appinfo

        index = appinfo.franchise_index(games_only=True) or {}
        for name, members in index.items():
            try:
                if owned & {int(m) for m in (members or ())}:
                    names.add(str(name).strip())
            except (TypeError, ValueError):
                continue
    except Exception as exc:
        logger.debug("取本账号所属系列名失败：%s", exc)
    try:      # 手工表里「本账号拥有的作品」对应的英文 franchise 名
        reverse: dict[str, str] = {}
        for english, chinese in (st.load_official_aliases() or {}).items():
            reverse.setdefault(str(chinese).strip(), str(english).strip())
        for raw_appid, entry in (st.load_series_map() or {}).items():
            try:
                appid = int(raw_appid)
            except (TypeError, ValueError):
                continue
            if appid not in owned:
                continue
            english = reverse.get(str((entry or {}).get("series") or "").strip())
            if english:
                names.add(english)
    except Exception as exc:
        logger.debug("取手工表系列名失败：%s", exc)
    return sorted(name for name in names if name)


def _job_series_stage(appids) -> tuple[str, bool]:
    """阶段 4（限时兜底）：族谱系列目录——**受硬时间预算约束**，到点立即停止。

    这个阶段的网络源是 ``store.steampowered.com`` 的 franchise 检索，在大陆时通时断；
    本机全部 franchise 名逐个跑能把「首次补齐」拖到十几分钟（实测 482 秒还没跑完）。
    所以：

      * 只抓「本账号拥有作品所属系列」（:func:`_owned_series_targets`），而不是本机全部；
      * 整个阶段最多 ``JOB_SERIES_BUDGET_SECONDS`` 秒（在飞的请求最多再等一个 timeout）；
      * 到点把本轮已经抓到的部分照常写进缓存，**没轮到的记进 ``deferred``**，留给以后
        渲染族谱时的后台渐进刷新（``series_tree.maybe_refresh_async``）去补；
      * 这一阶段超时 / 失败**不算任务失败**（返回值不参与整体 ok），绝不会影响
        已经抓好的中文名 / 封面 / 价格。
    """
    from engine import series_tree as st

    try:
        stale = bool(st.series_cache_is_stale())
    except Exception as exc:
        logger.debug("系列目录新鲜度判断失败（按过期处理）：%s", exc)
        stale = True
    if not stale:
        _job_set(series_fetched=0, series_deferred=0)
        return "系列目录：缓存仍新鲜，已跳过", True

    names = _owned_series_targets(appids)
    total = 0
    try:
        stored = st._read_cache_file(None).get("series") or {}
        targets = st._refresh_targets(names) if names else st._refresh_targets(None)
        total = sum(1 for name in targets if st._entry_stale(stored.get(name)))
    except Exception as exc:
        logger.debug("系列目录预检失败（按未知总数处理）：%s", exc)
        total = 0

    label = _JOB_STAGE_LABELS["series"]
    hint = "后台慢慢补，不影响使用"
    _job_stage("series", label, 0, total, _job_message(label, 0, total, hint))
    counter = {"n": 0}

    def on_progress(_name, _count):
        counter["n"] += 1
        denom = total or counter["n"]
        _job_stage("series", label, counter["n"], denom,
                   _job_message(label, counter["n"], denom, hint))

    try:
        # 有界参数：单次 5 秒、每轮 1 次、共 2 轮，再加 deadline 硬预算；
        # 默认的 15s×10 次重试在大陆网络下能把一个系列拖到分钟级。
        result = st.refresh_series_cache(
            names if names else None, force=False,
            timeout=5.0, tries=1, rounds=2,
            deadline_seconds=JOB_SERIES_BUDGET_SECONDS, on_progress=on_progress)
    except Exception as exc:
        logger.warning("刷新系列目录失败（已忽略）：%s", exc)
        return f"系列目录刷新失败（已忽略）：{exc}", False
    result = result or {}
    fetched = len(result.get("fetched") or [])
    deferred = len(result.get("deferred") or [])
    failed = len(result.get("failed") or [])
    _job_set(series_fetched=fetched, series_deferred=deferred)
    if deferred:
        logger.info("系列目录阶段：%s 秒预算到点停止，本轮更新 %d 个系列，"
                    "%d 个留待后台渐进刷新", JOB_SERIES_BUDGET_SECONDS, fetched, deferred)
    if deferred:
        tail = (f"，还剩 {deferred} 个没抓（本轮 {JOB_SERIES_BUDGET_SECONDS:.0f} 秒预算到点停止，"
                "后台会继续补）")
    elif failed:
        tail = f"，有 {failed} 个没抓到（后台会继续补）"
    else:
        tail = "，已全部齐活"
    return f"系列目录：本轮更新 {fetched} 个系列{tail}", True


def _job_names_stage(appids: list[int], steam_path: str | None) -> tuple[str, bool]:
    """阶段 2：中文名 + 元数据（含类型）。"""
    from core import store_api

    label = _JOB_STAGE_LABELS["names"]
    total = len(appids)
    hint = "首次补齐较慢，约 3~6 分钟，可以先逛别的"
    _job_stage("names", label, 0, total, _job_message(label, 0, total, hint))
    counter = {"miss": 0}

    def on_progress(done, tot, record):
        record = record or {}
        if not str(record.get("name") or "").strip() \
                and not str(record.get("name_cn") or "").strip():
            counter["miss"] += 1
        denom = tot or total
        _job_stage("names", label, done, denom, _job_message(label, done, denom, hint))

    try:
        # with_price=False：价格走下面**独立**的阶段，进度才看得见（见 store_api.backfill_prices）
        result = store_api.fetch_metadata(
            appids, steam_path=steam_path, progress=on_progress, with_price=False)
    except Exception as exc:
        logger.warning("抓元数据失败（已忽略）：%s", exc)
        return f"中文名与元数据抓取失败（已忽略）：{exc}", False, True

    got = 0
    for record in (result or {}).values():
        record = record or {}
        if str(record.get("name") or "").strip() or str(record.get("name_cn") or "").strip():
            got += 1
    tail = (f"，{counter['miss']} 款没抓到（已记负缓存，6 小时内不重复请求）"
            if counter["miss"] else "")
    ok = got > 0 or total == 0
    return f"中文名与元数据：{got}/{total} 款有名字{tail}", ok, (not ok and total > 0)


def _job_price_stage(appids: list[int], steam_path: str | None) -> tuple[str, bool]:
    """阶段 3：价格（独立回填路径，进度可见；性价比榜靠它才有数）。

    温柔多轮（T7）：单轮有硬预算，到点停止、剩下留给下一轮或下次启动；
    相邻请求之间由 ``store_api`` 全局节流 1.5~2.0 秒（官方商店按主机限流，
    猛打会触发分钟级整体阻断）。每轮的成功率回调进 ``on_round``（可观测）。
    """
    from core import store_api

    label = _JOB_STAGE_LABELS["price"]
    hint = "一款一款问商店（温柔节奏，不会一次性打完）"
    _job_stage("price", label, 0, 0, _job_message(label, 0, 0, hint))
    seen = {"total": 0}
    rounds: list[dict] = []

    def on_progress(done, tot, _record):
        seen["total"] = int(tot or 0)
        _job_stage("price", label, done, tot, _job_message(label, done, tot, hint))

    def on_round(index, total_rounds, stats):
        rounds.append({"round": int(index), "of": int(total_rounds),
                       "success": int(stats.get("success") or 0),
                       "attempts": int(stats.get("attempts") or 0),
                       "seconds": float(stats.get("seconds") or 0.0),
                       "deferred": int(stats.get("deferred") or 0)})
        _job_set(price_rounds=list(rounds))

    try:
        priced = store_api.backfill_prices(
            appids, steam_path=steam_path, progress=on_progress, on_round=on_round,
            rounds=store_api.PRICE_ROUNDS,
            round_budget=store_api.PRICE_ROUND_BUDGET_SECONDS,
        )
    except Exception as exc:
        logger.warning("价格回填失败（已忽略）：%s", exc)
        return f"价格回填失败（已忽略）：{exc}", False, True
    if not seen["total"]:
        return "价格：都已取到（无需重复请求）", True, False
    got = len(priced or {})
    # 每轮成功率（成功/尝试/耗时）写进文案与 /api/job，别只报一个总数
    per_round = "；".join(
        f"第 {item['round']} 轮 {item['success']}/{item['attempts']}（{item['seconds']:.0f}s）"
        for item in rounds
    )
    if got:
        return f"价格：补到 {got} 款" + (f"（{per_round}）" if per_round else ""), True, False
    # 确实问过商店、但一款都没取到：这**不是失败**——未拥有的老作品 / 未发售新作本来就没有
    # 售价，大陆网络下官方商店也可能时通时断。只记「稍后自动重试」，别把成功说成失败。
    return ("价格：没取到新价格（这些游戏可能本就没有售价，或商店暂时不通，稍后会自动重试）"
            + (f"（{per_round}）" if per_round else ""),
            True, True)


def _job_covers_stage(appids: list[int], lib: dict) -> tuple[str, bool]:
    """阶段 4：封面（本账号拥有的 ∪ 族谱系列框的缺口；官方已确认无图的跳过）。"""
    from core import artwork

    label = _JOB_STAGE_LABELS["covers"]
    try:
        gaps = _series_cover_gaps(lib)
    except Exception as exc:
        logger.debug("计算族谱封面缺口失败（已忽略）：%s", exc)
        gaps = []
    known_no_art = _no_art_appids()
    targets: list[int] = []
    for raw in dict.fromkeys(list(gaps) + list(appids)):
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if aid > 0 and aid not in known_no_art and aid not in targets:
            targets.append(aid)

    total = len(targets)
    hint = "每张不到 1 秒，通常 1~2 分钟"
    _job_stage("covers", label, 0, total, _job_message(label, 0, total, hint))

    def on_progress(done, tot, _entry):
        denom = tot or total
        _job_stage("covers", label, done, denom, _job_message(label, done, denom, hint))

    # 缓存里如果有**完整的封面直链**（小黑盒 T9 或官方 appdetails 给的），
    # 就把它当第一候选交给封面阶段：官方 CDN 固定文件名模式 404 的作品靠它兜底。
    hints: dict = {}
    try:
        from core import store_api as _store

        for aid, row in (_store.cached_metadata(targets) or {}).items():
            value = str((row or {}).get("header_image") or "").strip()
            if value.startswith(("http://", "https://")):
                hints[aid] = value
    except Exception as exc:
        logger.debug("读取封面直链提示失败（已忽略）：%s", exc)
        hints = {}

    try:
        stat = artwork.ensure_covers(
            targets, steam_path=(lib or {}).get("steam_path"), progress=on_progress,
            header_image_hint=hints or None)
    except Exception as exc:
        logger.warning("补封面失败（已忽略）：%s", exc)
        return f"封面补齐失败（已忽略）：{exc}", False, True

    stat = _mark_covers_not_found(stat)
    try:      # 记「官方 CDN 明确 404」的无图名单：下次不再白跑
        artwork.mark_no_art(_not_found_appids(stat))
    except Exception as exc:
        logger.warning("记录官方无图名单失败：%s", exc)
    message = _localize_message("封面", stat)
    not_found = int((stat or {}).get("not_found") or 0)
    if not_found:
        message += f"（其中 {not_found} 款官方 CDN 无图 404，属正常）"
    ok = total == 0 or int((stat or {}).get("ok") or 0) > 0 or not_found > 0
    return message, ok, (total > 0 and not ok)


def _job_pages_stage(account_id: str, lib: dict) -> dict:
    """阶段 5：刷新面板要用的缓存，并算出最终覆盖率（纯本地）。"""
    global _NAME_CN_CACHE, _NAME_CN_TS

    label = _JOB_STAGE_LABELS["pages"]
    _job_stage("pages", label, 1, 1, _job_message(label, 1, 1))
    _LIB_CACHE.clear()
    _COVER_MEMO.clear()
    _COVER_KIND_MEMO.clear()          # 新抓到封面后朝向可能变（横/竖），别复用旧判定
    _NAME_CN_CACHE = {}
    _NAME_CN_TS = 0.0
    try:
        _SERIES_COVER_MEMO.clear()
    except Exception:
        pass
    return _coverage_snapshot(lib)


def _run_localize_job(job: dict) -> dict:
    """跑一个账号的完整补齐流程（**只有这里会联网**，且必须永不抛）。"""
    account_id = str(job.get("account_id") or "")
    steam_path = job.get("steam_path")
    reason = str(job.get("reason") or "自动补齐")
    with _JOB_LOCK:
        _JOB_STATE.update({
            "running": True, "account_id": account_id, "stage": "start",
            "stage_label": "准备中", "done": 0, "total": 0,
            "message": f"正在为你补齐数据（{reason}）…",
            "started_at": _now_iso(), "finished_at": "", "ok": None, "error": "",
            "coverage": None, "stage_seconds": {},
            "series_fetched": 0, "series_deferred": 0,
        })

    _JOB_LAST_ATTEMPT[account_id] = time.time()
    notes: list[str] = []
    try:
        lib = load_library(account_id=account_id, with_metadata=False,
                           steam_path=steam_path, use_cache=False)
    except Exception as exc:
        logger.warning("自动补齐读取库失败：%s", exc)
        _job_finish(ok=False, error=f"读取库失败：{exc}")
        return {"ok": False, "error": str(exc)}

    appids: list[int] = []
    for raw in (lib.get("play_stats") or {}):
        try:
            appids.append(int(raw))
        except (TypeError, ValueError):
            continue
    if not appids:
        note = "这个账号还没有读到任何游戏记录"
        _job_finish(ok=False, error=note, message=f"{note}（可先在本机 Steam 里同步一次库存）")
        return {"ok": False, "error": note}

    real_path = lib.get("steam_path") or steam_path
    # 阶段顺序按「用户最关心的先做完」：
    #   中文名与元数据 -> 价格 -> 封面 -> 刷新面板缓存 -> 系列目录（限时兜底）
    # 「系列目录」跑在最后且受 JOB_SERIES_BUDGET_SECONDS 硬预算约束：
    # 它超时 / 失败都不算任务失败，绝不会影响已经抓好的中文名 / 封面 / 价格。
    critical_stages = (
        ("names", _job_names_stage, (appids, real_path)),
        ("price", _job_price_stage, (appids, real_path)),
        ("covers", _job_covers_stage, (appids, lib)),
    )
    flags: dict[str, bool] = {}
    retry_needed: list[str] = []
    coverage: dict = {}
    stage_seconds: dict[str, float] = {}
    try:
        for key, func, args in critical_stages:
            started = time.monotonic()
            try:
                note, stage_ok, stage_retry = func(*args)
            finally:
                stage_seconds[key] = round(time.monotonic() - started, 1)
                _job_set(stage_seconds=dict(stage_seconds))
            notes.append(note)
            flags[key] = bool(stage_ok)
            if stage_retry:
                retry_needed.append(key)

        started = time.monotonic()
        try:
            coverage = _job_pages_stage(account_id, lib)
        except Exception as exc:                  # 纯本地：失败也不该让任务消失
            logger.warning("刷新面板缓存失败（已忽略）：%s", exc)
            coverage = {}
        finally:
            stage_seconds["pages"] = round(time.monotonic() - started, 1)
            _job_set(stage_seconds=dict(stage_seconds))
        notes.append(
            "覆盖率：中文名 {0}/{3} · 封面 {1}/{3} · 价格 {2}".format(
                coverage.get("name_cn", 0), coverage.get("cover", 0),
                coverage.get("price", 0), coverage.get("total", 0)))

        # 限时兜底：游戏系列目录。单独兜异常——它无论如何都不能把整体判成失败。
        started = time.monotonic()
        try:
            series_note = _job_series_stage(appids)[0]
        except Exception as exc:
            logger.warning("系列目录阶段异常（已忽略，不影响其它数据）：%s", exc)
            series_note = f"系列目录刷新失败（已忽略，后面会自动再补）：{exc}"
        finally:
            stage_seconds["series"] = round(time.monotonic() - started, 1)
            _job_set(stage_seconds=dict(stage_seconds))
        notes.append(series_note)
    except Exception as exc:                      # 关键阶段炸掉也不能让任务消失
        logger.warning("自动补齐 %s 失败：%s", account_id, exc)
        _job_finish(ok=False, error=f"{type(exc).__name__}: {str(exc)[:120]}")
        return {"ok": False, "error": str(exc), "coverage": coverage}

    names_ok = bool(flags.get("names"))
    covers_ok = bool(flags.get("covers"))
    # 三态：只有「中文名与封面**都**没拿到东西」才算真失败——价格 / 系列拿不到是正常情况
    # （未拥有的老作本就无售价、商店时通时断、系列目录本来就是后台渐进补）。
    ok = names_ok or covers_ok
    if not ok:
        for key in ("names", "covers"):
            if key not in retry_needed:
                retry_needed.append(key)
    retry_needed = sorted(set(retry_needed))
    partial = bool(ok and retry_needed)
    summary = "；".join(n for n in notes if n)
    if partial:
        summary = "；".join(["数据已补齐（个别项稍后自动重试）"]
                            + _retry_hints(retry_needed, coverage) + [summary])
    logger.info("自动补齐 %s %s：%s（各阶段秒数 %s）", account_id,
                "完成" if ok and not partial else ("基本补齐" if ok else "失败"),
                summary, stage_seconds)

    if not ok:
        # 真失败（关键数据一个都没抓到）：不记「已补齐」，下次还能重试；
        # 同一进程内 10 分钟内不重复跑（见 JOB_RETRY_SUPPRESS_SECONDS）。
        _job_finish(ok=False, error="中文名和封面都没抓到（网络可能不通）",
                    coverage=coverage, message=summary)
        return {"ok": False, "error": "critical", "coverage": coverage,
                "retry_needed": retry_needed}

    if partial:
        # 基本补齐：**不能**记成「已补齐」，否则下次不会重试；只记缺口与该补的项。
        try:
            _record_localize_state(account_id, coverage, partial=True,
                                   retry_needed=retry_needed)
        except Exception as exc:
            logger.debug("记录补齐状态失败（已忽略）：%s", exc)
        _job_finish(ok=True, partial=True, retry_needed=retry_needed,
                    coverage=coverage, message=summary)
        return {"ok": True, "error": "", "coverage": coverage, "partial": True,
                "retry_needed": retry_needed}

    try:
        _record_localize_state(account_id, coverage)
    except Exception as exc:
        logger.debug("记录补齐状态失败（已忽略）：%s", exc)
    _job_finish(ok=True, coverage=coverage, message=summary)
    return {"ok": True, "error": "", "coverage": coverage}


def _maybe_auto_enqueue(lib: dict, coverage) -> None:
    """面板加载时发现该账号数据不全、又没在跑，就自动续做一次（纯本地判断，绝不联网）。"""
    if _job_network_disabled():
        return
    account_id = str((lib or {}).get("account_id") or "")
    if not account_id:
        return
    if (lib or {}).get("merged") or merge_core.is_merge_id(account_id):
        # 合流库不是账号：它的成员该不该补齐由**各成员账号自己**的加载去判断，
        # 这里绝不能拿合流库的 id 去入队。
        return
    if not _coverage_incomplete(coverage):
        return
    enqueue_localize(account_id, steam_path=(lib or {}).get("steam_path"),
                     reason="面板发现数据不全", coverage=coverage)
