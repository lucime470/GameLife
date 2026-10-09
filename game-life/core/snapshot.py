"""历史快照维护。

每次 run 完成后写入当日快照，同天多次运行覆盖当天数据；
供时光机/考古/挑战系统查询历史游玩数据。
"""
from datetime import date, datetime, timedelta

from data.schema import get_connection, init_db


# unix 秒可接受的下界（约 2000-01-01）：Steam 的 localconfig.vdf 里存在
# LastPlayed=0/1 这类缺失值，若直接换算会被当成 1970-01-01 的"真实游玩日期"
# 写进快照库，污染时光机/考古的年份跨度与统计，这里与渲染层
# （render/archaeologist.py、render/timemachine.py）保持同一常量与语义。
_MIN_TIMESTAMP = 946684800
# 上界留出 1 天余量，容忍时钟漂移/未来时区写入。
_MAX_TIMESTAMP_SKEW = 86400


def _today() -> str:
    return date.today().isoformat()


def _day_from_ts(ts) -> str:
    """"last_played" 的 unix 秒 -> "YYYY-MM-DD"；无效返回 ""。

    unix 秒做范围校验：_MIN_TIMESTAMP <= ts <= 现在 + 1 天。
    区间外的输入一律视为无效并丢弃，例如 Steam 写入的 LastPlayed=0/1
    这类缺失值（否则会变成 1970-01-01 的脏快照），以及极大值、
    None、非数字字符串等。
    """
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if not (_MIN_TIMESTAMP <= ts <= datetime.now().timestamp() + _MAX_TIMESTAMP_SKEW):
        return ""
    try:
        return datetime.fromtimestamp(ts).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def save(games: dict, record_date: str | None = None) -> None:
    """将当前游戏列表写入快照表，同一天多次调用只保留最新一次。

    games: {appid: {playtime, last_played, status?}}
    """
    today = record_date or _today()
    init_db()
    db = get_connection()
    try:
        db.execute("DELETE FROM snapshots WHERE record_date = ?", (today,))
        for appid, g in games.items():
            db.execute(
                "INSERT INTO snapshots (record_date, appid, playtime, last_played, status) VALUES (?,?,?,?,?)",
                (
                    today,
                    appid,
                    int(g.get("playtime", 0)),
                    int(g.get("last_played", 0)),
                    str(g.get("status", "")),
                ),
            )
        db.commit()
    finally:
        db.close()


def load(record_date: str | None = None) -> list[dict]:
    """按日期拉取快照行，缺参数则读今天。"""
    today = record_date or _today()
    init_db()
    db = get_connection()
    try:
        rows = db.execute(
            "SELECT record_date, appid, playtime, last_played, status FROM snapshots WHERE record_date = ? ORDER BY appid",
            (today,),
        ).fetchall()
        return [dict(zip(r.keys(), r)) for r in rows]
    finally:
        db.close()


def list_dates() -> list[str]:
    """返回有快照的日期列表（按日期倒序）。"""
    init_db()
    db = get_connection()
    try:
        return [r[0] for r in db.execute(
            "SELECT DISTINCT record_date FROM snapshots ORDER BY record_date DESC"
        ).fetchall()]
    finally:
        db.close()


def diff(appid: int, date_a: str, date_b: str) -> dict | None:
    """对比同一款游戏在两个日期之间的游玩变化，缺记录返回 None。"""
    init_db()
    db = get_connection()
    try:
        rows = db.execute(
            "SELECT record_date, playtime, last_played, status FROM snapshots WHERE appid = ? AND record_date IN (?,?)",
            (appid, date_a, date_b),
        ).fetchall()
    finally:
        db.close()
    by_date = {}
    for r in rows:
        d = dict(zip(r.keys(), r))
        by_date[d["record_date"]] = d
    if date_a not in by_date or date_b not in by_date:
        return None
    a, b = by_date[date_a], by_date[date_b]
    return {
        "appid": appid,
        "date_a": date_a,
        "date_b": date_b,
        "playtime_delta": b["playtime"] - a["playtime"],
        "last_played_a": a["last_played"],
        "last_played_b": b["last_played"],
        "status_a": a["status"],
        "status_b": b["status"],
    }


def _existing_dates(db, dates: set) -> set:
    """返回给定日期中已经在 snapshots 里有行的那些（一次查询）。"""
    if not dates:
        return set()
    placeholders = ",".join("?" for _ in dates)
    rows = db.execute(
        f"SELECT DISTINCT record_date FROM snapshots WHERE record_date IN ({placeholders})",
        tuple(dates),
    ).fetchall()
    return {r[0] for r in rows}


def backfill(rows_by_date: dict, only_missing: bool = True, dry_run: bool = False) -> dict:
    """按日期批量回填历史快照：只补缺、默认不覆盖、绝不触碰今天。

    用于解决时光机/考古时间轴上线初期没有历史数据的冷启动问题。

    参数
    ----
    rows_by_date : {"YYYY-MM-DD": {appid: {playtime, last_played, status?}}}
        每个日期一行快照集合，结构同 ``save`` 的 games。
    only_missing : 默认 True —— 该日期若已存在任意快照行，则整体跳过、绝不覆盖；
        显式传 False 才允许覆盖该日期既有行（沿用 save 的先删后插语义）。
    dry_run      : True 时只统计、不写库（用于预览）。

    行为约定
    --------
    - **不触碰今天**：今天的快照由 ``run``/``save`` 写入，这里一律跳过，
      保证当日数据原样保留。
    - 写库前先 ``init_db()``，只使用 ``data/schema.py`` 已有的 snapshots 表结构
      （record_date, appid, playtime, last_played, status），不新增表或列。

    重要（推断值）
    --------------
    历史 playtime 无法精确还原，传入的 playtime 是「以当前已知数据作为该日期的
    近似值」，属于**推断值**，并非当时的真实时长；last_played 同理取当前值。
    随着后续 ``run`` 累积，真实历史会逐渐替换这些近似快照。

    返回可打印小结：``{"dates": n, "rows": m, "skipped": k}``。
    """
    today = _today()
    init_db()
    db = get_connection()
    stats = {"dates": 0, "rows": 0, "skipped": 0}
    try:
        pending = {
            str(d): games
            for d, games in (rows_by_date or {}).items()
            if games and str(d) != today  # 今天绝不触碰
        }
        stats["skipped"] = sum(1 for d in (rows_by_date or {}) if str(d) == today)

        existing = _existing_dates(db, set(pending))
        for record_date, games in pending.items():
            if record_date in existing and only_missing:
                stats["skipped"] += 1
                continue
            if record_date in existing:
                stats["dates"] += 1
                if not dry_run:
                    db.execute("DELETE FROM snapshots WHERE record_date = ?", (record_date,))
            else:
                stats["dates"] += 1
            for appid, g in games.items():
                g = g or {}
                stats["rows"] += 1
                if dry_run:
                    continue
                db.execute(
                    "INSERT INTO snapshots (record_date, appid, playtime, last_played, status) VALUES (?,?,?,?,?)",
                    (
                        record_date,
                        appid,
                        int(g.get("playtime", 0)),
                        int(g.get("last_played", 0)),
                        str(g.get("status", "")),
                    ),
                )
        if not dry_run:
            db.commit()
    finally:
        db.close()
    return stats


def backfill_from_play_stats(
    play_stats: dict,
    classified: dict | None = None,
    only_missing: bool = True,
    min_days_ago: int = 1,
    dry_run: bool = False,
) -> dict:
    """按 ``last_played`` 的日期，为每款游戏生成一条历史快照（近似推断）。

    - play_stats  : {appid: {playtime, last_played}}，来自 ``run``/``_load_library``。
    - classified  : classifier.classify_games 的输出；提供时用其内部状态键
      （``info["status"]``，如 未玩/在玩/搁置/沉迷/通关）作为快照 status，
      与 ``cmd_run`` 写入的 snapshot_games 结构保持一致。
    - min_days_ago: 只回填距今至少 N 天的记录（默认 1，天然避免覆盖今天；
      设为 0 时今天仍会被 backfill 拒绝，今天永远不写）。
    - only_missing/dry_run: 透传给 ``backfill``，语义一致。

    注意：历史 playtime 无法精确还原，写入的是「以当前已知数据作为该日期的
    近似值」，属于**推断值**（详见 ``backfill`` 文档）。

    返回 ``backfill`` 的小结：``{"dates": n, "rows": m, "skipped": k}``。
    """
    threshold = date.today() - timedelta(days=max(int(min_days_ago), 0))
    rows_by_date: dict = {}
    skipped_recent = 0
    for appid, raw in (play_stats or {}).items():
        raw = raw or {}
        day = _day_from_ts(raw.get("last_played"))
        if not day:
            continue
        try:
            played = date.fromisoformat(day)
        except ValueError:
            continue
        if played > threshold:
            skipped_recent += 1
            continue
        info = (classified or {}).get(appid) or {}
        status = info.get("status") or raw.get("status") or ""
        rows_by_date.setdefault(day, {})[appid] = {
            "playtime": int(raw.get("playtime", 0)),
            "last_played": int(raw.get("last_played", 0)),
            "status": status,
        }
    stats = backfill(rows_by_date, only_missing=only_missing, dry_run=dry_run)
    stats["skipped_recent"] = skipped_recent
    return stats
