"""清库挑战系统（分析引擎层 · 模块 8）。

依赖 core/snapshot.py 的历史快照对比，计算本月通关/新增游玩/XP/等级。
升级规则存放于 rules.json 的 challenge 段。

分层约定：
    * :func:`compute_challenge` 是**纯函数**，只做计算，绝不碰数据库；
    * 下面的 ``load_xp`` / ``apply_month_xp`` / ``get_or_create_monthly_challenge``
      / :func:`save` 负责把结果落库（``player_xp`` 单行累计 + ``challenges`` 月度记录）；
    * :func:`run` 负责「计算 → 落库 → 打印」编排。

XP 是**累计值**：等级 = ``int(sqrt(累计 XP / 100))``（设计文档 10.2），
跨月只增不减；同一月份重复运行不重复加分（幂等记账见 ``apply_month_xp``）。
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Callable

from . import display
from .stats import load_rules, _to_int

RULES_PATH = Path(__file__).resolve().parent.parent / "rules.json"

# player_xp 只有一行，id 固定为 1
XP_ROW_ID = 1

_DEFAULT_XP = {
    "complete": 100,
    "monthly_challenge": 200,
    "resume_shelved_per_hour": 5,
    "streak_login": 10,
}
_DEFAULT_BONUSES = {"resume_shelved": 50, "new_playing": 30}
_DEFAULT_TITLES = {
    "1-3": "库存清理学徒",
    "4-7": "补票大师",
    "8-14": "游戏收藏家",
    "15-24": "全成就猎人",
    "25+": "游戏人生传奇",
}
_DEFAULT_CHALLENGE = {
    "options": ["通关 2 款搁置游戏", "玩满 10 小时搁置游戏"],
    "game_count": 3,
}


def _resolve_challenge_rules(rules: dict[str, Any] | None) -> dict[str, Any]:
    rules = load_rules() if rules is None else rules
    raw = rules.get("challenge", {})
    out: dict[str, Any] = {}
    xp = raw.get("xp", {})
    for k, v in _DEFAULT_XP.items():
        out[k] = _to_int(xp.get(k, v))
    out["resume_shelved_bonus"] = _to_int(
        raw.get("resume_shelved_bonus", _DEFAULT_BONUSES["resume_shelved"])
    )
    out["new_playing_bonus"] = _to_int(
        raw.get("new_playing_bonus", _DEFAULT_BONUSES["new_playing"])
    )
    out["level_titles"] = {**_DEFAULT_TITLES, **raw.get("level_titles", {})}
    out["monthly_challenge_options"] = raw.get(
        "monthly_challenge_options", _DEFAULT_CHALLENGE["options"]
    )
    out["monthly_challenge_game_count"] = _to_int(
        raw.get("monthly_challenge_game_count", _DEFAULT_CHALLENGE["game_count"])
    )
    return out


def _month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    first = date(y, m, 1).isoformat()
    if m == 12:
        last = date(y + 1, 1, 1).isoformat()
    else:
        last = date(y, m + 1, 1).isoformat()
    return first, last


def _latest_status_map(
    dates: list[str], month: str, loader: Callable[[str], list[dict]]
) -> dict[int, dict]:
    members = [d for d in dates if d.startswith(month)]
    if not members:
        return {}
    latest = max(members)
    rows = loader(latest)
    return {r["appid"]: r for r in rows if isinstance(r, dict)}


def _pick_goal(month: str, options: list[str]) -> str:
    """按月份确定性地挑一个挑战目标（同一月份永远得到同一目标）。

    设计文档 10.3 要求「随机选择」，但为了让月度挑战可复现，
    这里用月份串的摘要当随机种子（纯函数，不依赖 random 状态）。
    """
    opts = [str(o) for o in options if str(o).strip()]
    if not opts:
        return ""
    digest = hashlib.md5(month.encode("utf-8")).hexdigest()
    return opts[int(digest, 16) % len(opts)]


def compute_challenge(
    play_stats: dict | None = None,
    snapshot_dates: list[str] | None = None,
    month: str | None = None,
    snapshot_loader: Callable[[str], list[dict]] | None = None,
    rules: dict[str, Any] | None = None,
    today: str | None = None,
    challenge_record: dict[str, Any] | None = None,
    total_xp_base: int | None = None,
    account_appids: set | None = None,
) -> dict[str, Any]:
    """清库挑战进度（纯函数：只计算，不读写数据库）。

    参数：
        play_stats: 当前库数据（用于本月游玩时长统计）
        snapshot_dates: 可选日期列表；缺省由 snapshot.list_dates() 提供
        month: YYYY-MM；缺省为本月
        snapshot_loader: (date) -> list[dict]；缺省使用 core.snapshot
        rules: 可选规则 dict
        today: 可选 YYYY-MM-DD（便于测试）
        challenge_record: 已落库的当月挑战记录（title/appids/completed）；
            传入后挑战目标与候选游戏以该记录为准，保证月内稳定
        total_xp_base: 本月之前已累计的 XP；传入后 level/title 按
            「累计 XP」计算（int(sqrt(累计 XP / 100))）。
        account_appids: 可选「当前账号库」的 appid 集合；传入后只统计这些
            appid 的状态变化与搁置时长，避免串用别的账号写下的全局快照。
            缺省为 None，表示不按账号过滤（保持既有纯函数语义）。

    返回：{month, completed_count, resume_count, new_playing_count,
           shelved_hours, xp_breakdown, total_xp, month_xp, cumulative_xp,
           level, title, monthly_challenge, changes}
          total_xp 仍是「本月 XP 增量」原语义；累计值见 cumulative_xp。
    """
    crules = _resolve_challenge_rules(rules)
    today = today or date.today().isoformat()
    month = month or today[:7]
    prev_year, prev_month = map(int, month.split("-"))
    prev_month -= 1
    if prev_month == 0:
        prev_year, prev_month = prev_year - 1, 12
    prev_month_str = f"{prev_year:04d}-{prev_month:02d}"

    if snapshot_loader is None:
        from core import snapshot as _snap  # type: ignore
        snapshot_loader = _snap.load
        if snapshot_dates is None:
            snapshot_dates = _snap.list_dates()
    dates = snapshot_dates or []
    before = _latest_status_map(dates, prev_month_str, snapshot_loader)
    after = _latest_status_map(dates, month, snapshot_loader)
    if account_appids is not None:
        scope = {_to_int(a) for a in account_appids}
        before = {a: r for a, r in before.items() if a in scope}
        after = {a: r for a, r in after.items() if a in scope}

    completed: list[int] = []
    resumed: list[int] = []
    new_playing: list[int] = []
    changes = []

    for appid, row in after.items():
        before_row = before.get(appid)
        after_status = str(row.get("status", ""))
        before_status = str(before_row.get("status", "")) if before_row else "未玩"
        if before_status == "未玩" and after_status == "在玩":
            new_playing.append(appid)
            changes.append(
                {"appid": appid, "from": "未玩", "to": "在玩", "type": "new_playing"}
            )
        elif before_status == "搁置" and after_status == "通关":
            completed.append(appid)
            changes.append(
                {"appid": appid, "from": "搁置", "to": "通关", "type": "complete"}
            )
        elif before_status == "搁置" and after_status == "在玩":
            resumed.append(appid)
            changes.append(
                {"appid": appid, "from": "搁置", "to": "在玩", "type": "resume"}
            )

    xp_complete = len(completed) * crules["complete"]
    xp_resume = len(resumed) * crules["resume_shelved_bonus"]
    xp_new = len(new_playing) * crules["new_playing_bonus"]
    xp = xp_complete + xp_resume + xp_new

    shelved_minutes = 0
    shelved_appids = {a for a, r in before.items() if r.get("status") == "搁置"}
    ps = play_stats or {}
    for appid in shelved_appids:
        shelved_minutes += _to_int((ps.get(appid) or {}).get("playtime", 0))
    shelved_hours = shelved_minutes // 60
    xp_shelved_play = shelved_hours * crules["resume_shelved_per_hour"]
    xp += xp_shelved_play

    challenge_games = sorted(shelved_appids)[: crules["monthly_challenge_game_count"]]
    goal = _pick_goal(month, crules["monthly_challenge_options"])
    if challenge_record:
        goal = str(challenge_record.get("title") or goal)
        recorded = [
            _to_int(a)
            for a in (challenge_record.get("appids") or [])
            if _to_int(a) > 0
        ]
        if recorded:
            challenge_games = sorted(set(recorded))
    progress: dict[str, Any] = {}
    if "通关" in goal:
        needed = 2
        done = sum(1 for a in challenge_games if a in completed)
        progress = {"needed": needed, "done": done, "game_count": len(challenge_games)}
    else:
        needed_minutes = 10 * 60
        done = sum(
            _to_int((ps.get(a) or {}).get("playtime", 0)) for a in challenge_games
        )
        progress = {
            "needed_minutes": needed_minutes,
            "done_minutes": done,
            "game_count": len(challenge_games),
        }
    monthly_challenge_complete = (
        (progress["done"] >= progress["needed"])
        if "needed" in progress
        else progress["done_minutes"] >= progress["needed_minutes"]
    )
    if monthly_challenge_complete:
        xp += crules["monthly_challenge"]

    month_xp = xp
    base = None if total_xp_base is None else max(0, _to_int(total_xp_base))
    cumulative_xp = month_xp if base is None else base + month_xp
    level = int(math.sqrt(cumulative_xp / 100)) if cumulative_xp > 0 else 0
    title = _title_for_level(level, crules["level_titles"])

    return {
        "month": month,
        "completed_count": len(completed),
        "resume_count": len(resumed),
        "new_playing_count": len(new_playing),
        "shelved_hours": shelved_hours,
        "xp_breakdown": {
            "complete": xp_complete,
            "resume": xp_resume,
            "new_playing": xp_new,
            "shelved_play": xp_shelved_play,
            "monthly_challenge": crules["monthly_challenge"]
            if monthly_challenge_complete
            else 0,
        },
        "total_xp": month_xp,
        "month_xp": month_xp,
        "cumulative_xp": cumulative_xp,
        "level": level,
        "title": title,
        "monthly_challenge": {
            "goal": goal,
            "games": [
                {
                    "appid": a,
                    "name": display.display_name(a, ps.get(a) or {}),
                }
                for a in challenge_games
            ],
            "progress": progress,
            "completed": monthly_challenge_complete,
        },
        "changes": changes,
    }


def _title_for_level(level: int, titles: dict[str, str]) -> str:
    for rng, t in titles.items():
        if "-" in rng:
            lo, hi = map(int, rng.split("-"))
            if lo <= level <= hi:
                return t
        elif rng.endswith("+"):
            if level >= int(rng[:-1]):
                return t
    return ""


# ===================== 持久化层（player_xp / challenges） =====================
#
# 记账方案（幂等）：
#   * player_xp(id=1).total_xp 保存**累计** XP（跨月只增不减），level 同步写回；
#   * challenges.xp_awarded 保存该月**已发放**的 XP（每月一条月度挑战记录充当账本）；
#   * ``apply_month_xp`` 只补发 ``max(0, 本月计算值 - 已发放)`` 的正差额，
#     因此同一月份重复运行不会重复加分，月内进度增长也只补差额。
#   * 旧库没有 xp_awarded 列，由 data/schema.py 的幂等迁移补齐。


def _level_for_xp(total_xp: int) -> int:
    """等级 = int(sqrt(累计 XP / 100))（设计文档 10.2）。"""
    total_xp = _to_int(total_xp)
    return int(math.sqrt(total_xp / 100)) if total_xp > 0 else 0


def _connect():
    """打开数据库连接；data.schema.DB_PATH 在调用时解析，便于测试替换。"""
    from data import schema  # 延迟 import，避免纯函数模块强依赖数据层

    return schema.get_connection()


def _record_dict(row: Any) -> dict[str, Any]:
    """challenges 行 -> 统一 dict（appids 解析为 int 列表）。"""
    appids: list[int] = []
    raw = row["appids"]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            appids = [_to_int(a) for a in parsed if _to_int(a) > 0]
    return {
        "id": _to_int(row["id"]),
        "month": str(row["month"] or ""),
        "title": str(row["title"] or ""),
        "appids": appids,
        "completed": bool(_to_int(row["completed"])),
        "completed_date": row["completed_date"],
        "xp_awarded": _to_int(row["xp_awarded"]),
    }


def load_xp() -> dict[str, Any] | None:
    """读取累计 XP（player_xp 单行）；数据库不可用时返回 None。"""
    try:
        conn = _connect()
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT total_xp, level FROM player_xp WHERE id = ?", (XP_ROW_ID,)
        ).fetchone()
        if row is None:
            return {"total_xp": 0, "level": 0}
        total_xp = max(0, _to_int(row["total_xp"]))
        return {"total_xp": total_xp, "level": _level_for_xp(total_xp)}
    except Exception:
        return None
    finally:
        conn.close()


def get_month_record(month: str) -> dict[str, Any] | None:
    """读取某月的挑战记录；库不可用或未生成返回 None。"""
    try:
        conn = _connect()
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT id, month, title, appids, completed, completed_date, xp_awarded "
            "FROM challenges WHERE month = ? ORDER BY id LIMIT 1",
            (str(month),),
        ).fetchone()
        return _record_dict(row) if row is not None else None
    except Exception:
        return None
    finally:
        conn.close()


def load_month_xp(month: str) -> int | None:
    """当月已发放 XP（记账）；无记录或库不可用返回 None。"""
    record = get_month_record(month)
    return None if record is None else int(record["xp_awarded"])


def get_or_create_monthly_challenge(
    month: str,
    goal: str | None = None,
    appids: list[int] | None = None,
) -> dict[str, Any] | None:
    """读取当月挑战记录，缺失则生成并落库（每月唯一一条）。

    goal/appids 只在「新建」或「旧行为空」时写入，保证月内稳定可复现。
    数据库不可用时返回 None。
    """
    month = str(month)
    goal = str(goal or "")
    clean_appids = sorted({_to_int(a) for a in (appids or []) if _to_int(a) > 0})
    appids_json = json.dumps(clean_appids)
    try:
        conn = _connect()
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT id, month, title, appids, completed, completed_date, xp_awarded "
            "FROM challenges WHERE month = ? ORDER BY id LIMIT 1",
            (month,),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO challenges (month, title, appids, completed, completed_date, xp_awarded) "
                "VALUES (?,?,?,?,?,?)",
                (month, goal, appids_json, 0, None, 0),
            )
            conn.commit()
            return get_month_record(month) or {
                "id": cur.lastrowid,
                "month": month,
                "title": goal,
                "appids": clean_appids,
                "completed": False,
                "completed_date": None,
                "xp_awarded": 0,
            }
        record = _record_dict(row)
        if (goal and not record["title"]) or (clean_appids and not record["appids"]):
            conn.execute(
                "UPDATE challenges SET title = ?, appids = ? WHERE id = ?",
                (record["title"] or goal, json.dumps(record["appids"] or clean_appids), record["id"]),
            )
            conn.commit()
            record = get_month_record(month) or record
        return record
    except Exception:
        return None
    finally:
        conn.close()


def apply_month_xp(month: str, month_xp: int) -> dict[str, Any] | None:
    """幂等地把本月 XP 累加进 player_xp，返回 {awarded, total_xp, level, ...}。

    只补发 ``max(0, month_xp - 当月已发放)`` 的差额：同一月份重复运行两次
    第二次 awarded=0（不翻倍）；月内进度增长时只补增量。
    数据库不可用时返回 None。
    """
    month = str(month)
    month_xp = max(0, _to_int(month_xp))
    try:
        conn = _connect()
    except Exception:
        return None
    try:
        conn.execute("INSERT OR IGNORE INTO player_xp (id, total_xp, level) VALUES (?, 0, 0)", (XP_ROW_ID,))
        row = conn.execute(
            "SELECT id, xp_awarded FROM challenges WHERE month = ? ORDER BY id LIMIT 1",
            (month,),
        ).fetchone()
        if row is None:
            cur = conn.execute(
                "INSERT INTO challenges (month, title, appids, completed, completed_date, xp_awarded) "
                "VALUES (?,?,?,?,?,?)",
                (month, "", "[]", 0, None, 0),
            )
            row_id, awarded_before = cur.lastrowid, 0
        else:
            row_id, awarded_before = _to_int(row["id"]), _to_int(row["xp_awarded"])
        delta = max(0, month_xp - awarded_before)
        xp_row = conn.execute(
            "SELECT total_xp FROM player_xp WHERE id = ?", (XP_ROW_ID,)
        ).fetchone()
        total_xp = max(0, _to_int(xp_row["total_xp"]) if xp_row else 0) + delta
        level = _level_for_xp(total_xp)
        conn.execute(
            "UPDATE challenges SET xp_awarded = ? WHERE id = ?",
            (max(awarded_before, month_xp), row_id),
        )
        conn.execute(
            "UPDATE player_xp SET total_xp = ?, level = ? WHERE id = ?",
            (total_xp, level, XP_ROW_ID),
        )
        conn.commit()
        return {
            "month": month,
            "month_xp": month_xp,
            "awarded": delta,
            "awarded_total": max(awarded_before, month_xp),
            "total_xp": total_xp,
            "level": level,
        }
    except Exception:
        return None
    finally:
        conn.close()


def save(
    month: str,
    monthly_challenge: dict[str, Any] | None = None,
    month_xp: int = 0,
    today: str | None = None,
    rules: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """把本月结果落库：月度挑战记录 + 累计 XP + 完成状态。

    返回 {month, awarded, total_xp, level, title, challenge, created}；
    数据库不可用时返回 None（调用方退化为仅计算并打印）。
    """
    month = str(month)
    mc = monthly_challenge or {}
    goal = str(mc.get("goal") or "")
    appids = [g.get("appid") for g in mc.get("games") or [] if isinstance(g, dict)]
    record = get_or_create_monthly_challenge(month, goal, appids)
    if record is None:
        return None
    completed = bool(mc.get("completed"))
    completed_date = record.get("completed_date")
    if completed and not record.get("completed"):
        completed_date = today or date.today().isoformat()
        try:
            conn = _connect()
        except Exception:
            return None
        try:
            conn.execute(
                "UPDATE challenges SET completed = 1, completed_date = ? WHERE id = ?",
                (completed_date, record["id"]),
            )
            conn.commit()
        except Exception:
            return None
        finally:
            conn.close()
        record["completed"] = True
        record["completed_date"] = completed_date
    elif record.get("completed") and not completed_date:
        completed_date = today or date.today().isoformat()

    xp_result = apply_month_xp(month, month_xp)
    if xp_result is None:
        return None
    refreshed = get_month_record(month)
    if refreshed is not None:
        record = refreshed
    crules = _resolve_challenge_rules(rules)
    return {
        "month": month,
        "awarded": xp_result["awarded"],
        "total_xp": xp_result["total_xp"],
        "level": xp_result["level"],
        "title": _title_for_level(xp_result["level"], crules["level_titles"]),
        "challenge": record,
        "completed": bool(record.get("completed")),
        "completed_date": record.get("completed_date") or completed_date,
    }


def run(args: Any) -> int:
    """CLI 入口：计算 → 落库（累计 XP / 月度挑战）→ 打印。

    统计范围按「当前账号库」限定：快照表本身没有账号维度，若不加限制，
    空账号或其它账号会套用别人的全局快照算出「首次游玩 N 款」这类结果。
    这里以传入的 play_stats（该账号的本地 VDF 拥有列表）作为账号范围；
    该账号没有任何游戏时直接提示、不写入全局 XP 账本。
    """
    ps = getattr(args, "play_stats", None) or {}
    today = date.today().isoformat()
    month = getattr(args, "month", None) or today[:7]

    month_label = month
    if not ps:
        print(f"== 清库挑战 · {month_label} ==")
        print("  当前账号没有读取到任何游戏，无法计算挑战（本次不写入 XP/等级）。")
        print("  提示：请用 --account-id <ID> 指定一个有游玩记录的账号。")
        return 0

    account_appids = {_to_int(a) for a in ps}

    base = load_xp()
    db_ok = base is not None
    base_total = int(base["total_xp"]) if base else 0

    def _calc(**extra: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "play_stats": ps,
            "month": month,
            "today": today,
            "total_xp_base": base_total,
            "account_appids": account_appids,
        }
        kwargs.update(extra)
        if not db_ok:
            # 数据层不可用时退化为「仅用当期数据计算」，不触碰快照
            kwargs.setdefault("snapshot_dates", [])
            kwargs.setdefault("snapshot_loader", lambda _d: [])
        return compute_challenge(**kwargs)

    result = _calc()
    mc = result["monthly_challenge"]
    record = None
    if db_ok:
        try:
            record = get_or_create_monthly_challenge(
                month, mc["goal"], [g["appid"] for g in mc["games"]]
            )
        except Exception:
            record = None
    if record:
        result = _calc(challenge_record=record)

    saved = None
    if db_ok and record:
        try:
            saved = save(
                month,
                monthly_challenge=result["monthly_challenge"],
                month_xp=result["month_xp"],
                today=today,
            )
        except Exception:
            saved = None
    if not db_ok or saved is None:
        print("[!] 数据库不可用或写入失败，本轮仅计算不落库（XP/等级不会保存）")

    bd = result["xp_breakdown"]
    print(f"== 清库挑战 · {result['month']} ==")
    print(
        f"  通关 {result['completed_count']} 款 | 回归 {result['resume_count']} 款 | 首次游玩 {result['new_playing_count']} 款"
    )
    print(f"  搁置游玩时长 {result['shelved_hours']}h")
    if saved is not None:
        print(
            f"  XP 本月 +{result['month_xp']}（本次入账 +{saved['awarded']}）  =>  "
            f"累计 {saved['total_xp']} XP，等级 {saved['level']} {saved['title']}"
        )
    else:
        print(
            f"  XP 本月 +{result['month_xp']}  =>  累计 {result['cumulative_xp']} XP，"
            f"等级 {result['level']} {result['title']}（未落库）"
        )
    print(
        f"    构成：完成 {bd['complete']} + 回归 {bd['resume']} + 首次 {bd['new_playing']} + 搁置游玩 {bd['shelved_play']} + 月度 {bd['monthly_challenge']}"
    )
    mcv = result["monthly_challenge"]
    done = bool(saved["completed"]) if saved else bool(mcv["completed"])
    print(f"  月度挑战：{mcv['goal']}  ({'已完成' if done else '进行中'})")
    if record and record.get("completed_date"):
        print(f"    候选 {len(record['appids'])} 款 · 达成于 {record['completed_date']}")
    return 0
