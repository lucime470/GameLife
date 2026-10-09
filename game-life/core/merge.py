"""账号合流（Confluence）——把用户添加的多个账号当作**同一个账号**统一统计。

用户诉求原话：
    「将用户添加进来的账号全部视作一个账号，即数据共享，再做统计等。」

做法（一句话）
--------------
在**装载层**把每个账号各自读一遍（各自过滤授权归属），再按 appid 合并成一份库；
下游的状态判定 / 统计 / 推荐 / 人格 / 族谱 / 六张可视化**一行都不用改**，
自动就是「合流库」的口径。

合并规则（唯一口径，改动请同步本文件与 `文档/09_变更记录.md`）
--------------------------------------------------------------
* 同一款游戏：``playtime`` **相加**（同一个人的多个号，时长当然要合起来算）；
* ``last_played`` 取**最大值**（最近一次玩过）；
* 元数据（游戏名 / 类型 / 价格 …）**取第一个非空值**，列表取更长的那个；
* 授权归属**先按账号各自过滤、再合并**：共享且本账号没玩过的条目不进合流库；
  一款游戏只要在任一账号里「自己有 / 玩过」，在合流库里就保留；
* 合流库不是账号：**不写回 Steam 分类、不写历史快照**（快照与写回都是按账号的），
  这两条由 CLI / 面板显式拦下，绝不做「代表哪个账号」的猜测。

对外契约（CLI 与面板共用，请勿随意改签名）::

    MERGE_ID                      # 合流库的账号 id（"all"）
    is_merge_id(value) -> bool
    merge_display_name(count) -> str
    added_accounts() / added_account_ids()          # 读 data/accounts.json
    resolve_members(steam_path, added=None) -> list[dict]
    merge_play_stats(parts) -> dict
    merge_games_meta(parts) -> dict
    merge_library_parts(parts) -> dict              # 合并的总入口
    summary_line(info) -> str                       # 人话版说明（CLI/面板共用）

约定：本模块**只读本地文件、不联网、绝不抛异常**（读不到就降级为空表）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

# 合流库的「账号 id」：面板下拉框与 CLI --account-id 都用它
MERGE_ID = "all"
# 别名（大小写不敏感）：CLI 手输时容错，避免用户记不住用哪个词
MERGE_ALIASES = frozenset({"all", "merge", "merged", "confluence", "合流", "合流库"})
# 展示名
MERGE_NAME = "合流库"
ADDED_ACCOUNTS_NAME = "accounts.json"

_META_LIST_FIELDS = ("genres", "categories", "developers", "genres_cn", "developers_cn")


def is_merge_id(value: Any) -> bool:
    """``"all"`` / ``"merged"`` / ``"合流"`` … 一律认作合流库（大小写不敏感）。"""
    return str(value or "").strip().lower() in MERGE_ALIASES


def merge_display_name(count: Any = None) -> str:
    """``合流库（3 个账号）``；不知道数量时只回「合流库」。"""
    total = _to_int(count)
    return f"{MERGE_NAME}（{total} 个账号）" if total > 0 else MERGE_NAME


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _norm_key(key: Any) -> Any:
    """appid 键规整成 int；非数字键原样保留（让它自然不命中）。"""
    try:
        return int(key)
    except (TypeError, ValueError):
        return key


def _is_blank(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {} or value == ()


# ------------------------------------------------------------------ 「已添加」账号

def data_dir() -> Path:
    """运行期 data 目录（打包态 = exe 附近的真实目录，与 data.schema 一致）。"""
    try:
        from data import schema

        return Path(schema.data_dir())
    except Exception:  # pragma: no cover - 兜底不制造 import 期副作用
        return Path(__file__).resolve().parent.parent / "data"


def accounts_path() -> Path:
    return data_dir() / ADDED_ACCOUNTS_NAME


def added_accounts() -> list[dict]:
    """读 ``data/accounts.json`` 的「已添加」账号：``[{id, name, added_at}]``。

    缺失 / 损坏 / 结构不对一律按空表处理，绝不抛异常。面板侧自己有一份带兼容迁移
    的读取器（``webapp.api.load_added_accounts``）；CLI 用这份轻量实现，两边读的
    是**同一个文件**。
    """
    try:
        raw = json.loads(accounts_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("accounts")
    entries: list[dict] = []
    seen: set[str] = set()
    for item in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(item, dict):
            account_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or "").strip()
            added_at = str(item.get("added_at") or "").strip()
        else:
            account_id, name, added_at = str(item or "").strip(), "", ""
        if account_id and account_id not in seen:
            seen.add(account_id)
            entries.append({"id": account_id, "name": name, "added_at": added_at})
    return entries


def added_account_ids() -> list[str]:
    return [item["id"] for item in added_accounts()]


def local_accounts(steam_path: Any) -> list[dict]:
    """本机 userdata 里的账号（读不到返回空表）。"""
    try:
        from core import locator

        return [dict(acc) for acc in locator.list_user_accounts(str(steam_path))]
    except Exception as exc:
        logger.debug("扫描本机账号失败（按空表处理）：%s", exc)
        return []


def resolve_members(steam_path: Any, added: Iterable[Mapping[str, Any]] | None = None) -> list[dict]:
    """合流库的成员账号：**优先用「已添加」的账号**，一个都没添加才退回本机全部。

    返回值 ``[{id, name, source}]``，``source`` 为 ``"added"`` / ``"local"``，
    顺序稳定（保持传入顺序）。
    """
    local = {str(acc.get("id") or ""): str(acc.get("name") or "") for acc in local_accounts(steam_path)}
    source_entries = list(added) if added is not None else added_accounts()

    members: list[dict] = []
    seen: set[str] = set()
    for item in source_entries:
        account_id = str((item or {}).get("id") or "").strip()
        if not account_id or account_id in seen:
            continue
        seen.add(account_id)
        members.append({
            "id": account_id,
            "name": local.get(account_id) or str((item or {}).get("name") or "") or account_id,
            "source": "added",
        })
    if members:
        return members

    for account_id, name in local.items():
        if account_id and account_id not in seen:
            seen.add(account_id)
            members.append({"id": account_id, "name": name or account_id, "source": "local"})
    return members


def resolve_member_ids(steam_path: Any, added: Iterable[Mapping[str, Any]] | None = None) -> list[str]:
    return [member["id"] for member in resolve_members(steam_path, added)]


# ------------------------------------------------------------------ 合并

def merge_play_stats(parts: Iterable[Mapping[Any, Any]]) -> dict:
    """按 appid 合并游玩数据：时长**相加**、``last_played`` 取最大。"""
    merged: dict[Any, dict] = {}
    for stats in parts:
        for key, info in (stats or {}).items():
            appid = _norm_key(key)
            info = info if isinstance(info, Mapping) else {}
            minutes = _to_int(info.get("playtime"))
            last_played = _to_int(info.get("last_played"))
            slot = merged.get(appid)
            if slot is None:
                slot = {"playtime": minutes, "last_played": last_played}
                # 顺带保留上游可能带的额外字段（如 name），首见为准
                for extra_key, extra_value in info.items():
                    if extra_key not in ("playtime", "last_played") and not _is_blank(extra_value):
                        slot[extra_key] = extra_value
                merged[appid] = slot
            else:
                slot["playtime"] += minutes
                slot["last_played"] = max(int(slot.get("last_played") or 0), last_played)
    return merged


def _merge_meta_into(target: dict, src: Mapping[Any, Any]) -> None:
    for key, value in (src or {}).items():
        if _is_blank(value):
            continue
        current = target.get(key)
        if key in _META_LIST_FIELDS and isinstance(current, list) and isinstance(value, list):
            if len(value) > len(current):
                target[key] = list(value)
            continue
        if _is_blank(current):
            target[key] = value


def merge_games_meta(parts: Iterable[Mapping[Any, Any]]) -> dict:
    """按 appid 合并元数据：逐字段取第一个非空值，列表取更长的那个。"""
    merged: dict[Any, dict] = {}
    for meta in parts:
        for key, info in (meta or {}).items():
            appid = _norm_key(key)
            if not isinstance(info, Mapping):
                continue
            slot = merged.setdefault(appid, {})
            _merge_meta_into(slot, info)
    return merged


def merge_ownership(records: Iterable[Mapping[str, Any]]) -> dict:
    """聚合每个账号的授权归属说明，并标 ``merged``（面板/CLI 逐账号自述用）。"""
    records = list(records)
    ownerships = [record.get("ownership") or {} for record in records]

    def _total(field: str) -> int:
        return sum(_to_int(own.get(field)) for own in ownerships)

    def _union(field: str) -> list:
        items: set = set()
        for own in ownerships:
            for value in own.get(field) or []:
                items.add(value)
        return sorted(items, key=lambda value: (str(type(value)), str(value)))

    accounts: list[dict] = []
    for record in records:
        own = record.get("ownership") or {}
        stats = record.get("play_stats") or {}
        minutes = sum(_to_int((info or {}).get("playtime")) for info in stats.values())
        accounts.append({
            "id": str(record.get("id") or ""),
            "name": str(record.get("name") or record.get("id") or ""),
            "games": len(stats),
            "minutes": minutes,
            "hours": round(minutes / 60.0, 2),
            "owned": _to_int(own.get("owned")),
            "played_shared": _to_int(own.get("played_shared")),
            "shared": _to_int(own.get("shared")),
            "free": _to_int(own.get("free")),
            "trace": _to_int(own.get("trace")),
            "degraded": bool(own.get("degraded")),
        })

    return {
        "merged": True,
        "merged_name": MERGE_NAME,
        "account_count": len(records),
        "accounts": accounts,
        "owned": _total("owned"),
        "played_shared": _total("played_shared"),
        "shared": _total("shared"),
        "free": _total("free"),
        "trace": _total("trace"),
        "total": _total("total"),
        "played_shared_appids": _union("played_shared_appids"),
        "shared_appids": _union("shared_appids"),
        "trace_appids": _union("trace_appids"),
        # 只要有任一账号读不到授权票，就不敢说「已按归属过滤干净」
        "degraded": any(bool(own.get("degraded")) for own in ownerships) if ownerships else True,
    }


def merge_library_parts(parts: Iterable[Mapping[str, Any]]) -> dict:
    """合并总入口。

    每个 part::

        {"id": str, "name": str, "play_stats": {...}, "games_meta": {...}, "ownership": {...}}

    返回（供装载层继续做 fill_metadata + 状态判定）::

        {"play_stats", "games_meta", "ownership", "accounts",
         "games", "total_minutes", "overlap_games", "overlap_minutes"}
    """
    records = [dict(part) for part in parts]
    stats_parts = [record.get("play_stats") or {} for record in records]
    meta_parts = [record.get("games_meta") or {} for record in records]

    play_stats = merge_play_stats(stats_parts)
    games_meta = merge_games_meta(meta_parts)
    ownership = merge_ownership(records)

    # 重叠统计：几款游戏在 ≥2 个账号里都有记录、因此合流后「多出来」多少时长
    seen_count: dict[Any, int] = {}
    overlap_minutes = 0
    first_minutes: dict[Any, int] = {}
    for stats in stats_parts:
        for key, info in (stats or {}).items():
            appid = _norm_key(key)
            minutes = _to_int((info or {}).get("playtime"))
            if seen_count.get(appid):
                overlap_minutes += minutes
            else:
                first_minutes[appid] = minutes
            seen_count[appid] = seen_count.get(appid, 0) + 1
    overlap_games = sum(1 for count in seen_count.values() if count > 1)

    total_minutes = sum(_to_int(info.get("playtime")) for info in play_stats.values())
    ownership.update({
        "games": len(play_stats),
        "total_minutes": total_minutes,
        "total_hours": round(total_minutes / 60.0, 2),
        "overlap_games": overlap_games,
        "overlap_minutes": overlap_minutes,
    })
    return {
        "play_stats": play_stats,
        "games_meta": games_meta,
        "ownership": ownership,
        "accounts": ownership["accounts"],
        "games": len(play_stats),
        "total_minutes": total_minutes,
        "overlap_games": overlap_games,
        "overlap_minutes": overlap_minutes,
    }


# ------------------------------------------------------------------ 人话版说明（CLI / 面板共用）

def summary_line(info: Mapping[str, Any] | None) -> str:
    """把合并结果说成一句人话：成员、条数、时长、重叠、归属剔除。

    条数 / 时长是**原始记录口径**（``play_stats`` 的行数，含壁纸引擎这类非游戏条目），
    与 ``core.ownership`` 的归属计数同源；面板 KPI / 统计看板另按「只算游戏」口径
    再剔一次非游戏条目，所以那里看到的款数会更少 —— 这里如实写「条」以作区分。
    """
    data = dict(info or {})
    accounts = data.get("accounts") or []
    names = "、".join(str(acc.get("name") or acc.get("id") or "") for acc in accounts if acc)
    count = _to_int(data.get("account_count")) or len(accounts)
    head = f"{MERGE_NAME}：{count} 个账号" + (f"（{names}）" if names else "")
    if data.get("games"):
        head += (f" → 合并后 {int(data['games'])} 条记录（含非游戏条目）、"
                 f"{float(data.get('total_hours') or 0):.1f} 小时")
    if _to_int(data.get("overlap_games")):
        head += (f"；其中 {int(data['overlap_games'])} 款在两个及以上账号都有记录，"
                 f"时长已相加（额外并入 {float(data.get('overlap_minutes') or 0) / 60.0:.1f}h）")
    shared = _to_int(data.get("shared"))
    played_shared = _to_int(data.get("played_shared"))
    if played_shared:
        head += f"；{played_shared} 款家庭共享但玩过，已保留"
    if shared:
        head += f"；{shared} 款家庭共享且未玩过，未计入"
    if _to_int(data.get("trace")):
        head += f"；{int(data['trace'])} 款 Steam 残留痕迹，未计入"
    return head + "。"
