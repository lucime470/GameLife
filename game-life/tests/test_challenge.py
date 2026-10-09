"""清库挑战 XP/等级与月度挑战持久化的最小自测（P5）。

运行方式（在 game-life/ 目录下）：
    python tests/test_challenge.py
零依赖，纯标准库；全部使用临时数据库，不污染 data/cache.db。

覆盖：
    1. XP 累计持久化：连续两个月运行后 total_xp 累加、等级按累计 XP 提升；
    2. 同月幂等：同一 month 跑两次不重复加分、挑战记录唯一；
    3. 跨月：新月份产生新记录，上月记录保留；
    4. 月度挑战：goal 与候选游戏多次运行稳定可复现；达成后 completed=1
       且 completed_date 写入；
    5. 容错：数据库连接失败时不抛异常，退化为「仅计算并打印」；
    6. 纯函数：compute_challenge 不读写数据库；
    7. 表迁移：旧库缺 xp_awarded 列时由 init_db 幂等补齐。
"""

import contextlib
import io
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# 让 game-life/ 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.schema as schema  # noqa: E402
from core import snapshot  # noqa: E402
from engine import challenge as ch  # noqa: E402

PLAY_STATS = {
    1: {"name": "Game1", "playtime": 600},
    2: {"name": "Game2", "playtime": 600},
    3: {"name": "Game3", "playtime": 0},
    4: {"name": "Game4", "playtime": 3600},
}

# 历史快照：09 月 1/2 由搁置变通关；10 月 4 号也通关
SNAPSHOTS = {
    "2026-08-15": [
        {"appid": 1, "status": "搁置"},
        {"appid": 2, "status": "搁置"},
        {"appid": 3, "status": "未玩"},
        {"appid": 4, "status": "搁置"},
    ],
    "2026-09-10": [
        {"appid": 1, "status": "通关"},
        {"appid": 2, "status": "通关"},
        {"appid": 3, "status": "搁置"},
        {"appid": 4, "status": "搁置"},
    ],
    "2026-10-05": [
        {"appid": 1, "status": "通关"},
        {"appid": 2, "status": "通关"},
        {"appid": 3, "status": "搁置"},
        {"appid": 4, "status": "通关"},
    ],
}

# 固定挑战目标，避免依赖月份摘要的随机选择结果
RULES_COMPLETE = {"challenge": {"monthly_challenge_options": ["通关 2 款搁置游戏"]}}
RULES_HOURS = {"challenge": {"monthly_challenge_options": ["玩满 10 小时搁置游戏"]}}


def _loader(store):
    return lambda day: store.get(day, [])


@contextlib.contextmanager
def temp_db():
    """把 data.schema.DB_PATH 指向临时文件，结束后清理。"""
    with tempfile.TemporaryDirectory(prefix="gl_challenge_") as tmp:
        original = schema.DB_PATH
        schema.DB_PATH = Path(tmp) / "cache.db"
        try:
            schema.init_db()
            yield schema.DB_PATH
        finally:
            schema.DB_PATH = original


@contextlib.contextmanager
def broken_db():
    """让数据库连接直接失败，用于容错测试。"""
    original = schema.get_connection

    def _boom():
        raise sqlite3.OperationalError("database is locked (simulated)")

    schema.get_connection = _boom
    try:
        yield
    finally:
        schema.get_connection = original


def _compute(month, total_xp_base, rules=None, record=None, today=None):
    return ch.compute_challenge(
        play_stats=PLAY_STATS,
        snapshot_dates=list(SNAPSHOTS),
        snapshot_loader=_loader(SNAPSHOTS),
        month=month,
        today=today or f"{month}-20",
        total_xp_base=total_xp_base,
        rules=rules,
        challenge_record=record,
    )


def _run_month(month, total_xp_base, rules=None):
    """计算 -> 落库，返回 (result, saved)。"""
    result = _compute(month, total_xp_base, rules=rules)
    mc = result["monthly_challenge"]
    record = ch.get_or_create_monthly_challenge(
        month, mc["goal"], [g["appid"] for g in mc["games"]]
    )
    if record:
        result = _compute(month, total_xp_base, rules=rules, record=record)
    saved = ch.save(
        month, result["monthly_challenge"], result["month_xp"],
        today=f"{month}-21", rules=rules,
    )
    return result, saved


def test_xp_accumulates_across_months():
    """连续两个月运行：total_xp 累加，等级按累计 XP 提升。"""
    with temp_db():
        sept, saved_sept = _run_month("2026-09", 0, rules=RULES_COMPLETE)
        # 通关 2 款(+200) + 搁置游玩 80h*5(+400) + 月度挑战(+200)
        assert sept["xp_breakdown"]["complete"] == 200
        assert sept["xp_breakdown"]["shelved_play"] == 400
        assert sept["xp_breakdown"]["monthly_challenge"] == 200
        assert sept["month_xp"] == 800
        assert saved_sept["awarded"] == 800
        assert saved_sept["total_xp"] == 800
        assert saved_sept["level"] == 2  # int(sqrt(800/100))
        assert saved_sept["title"] == "库存清理学徒"
        assert ch.load_xp()["total_xp"] == 800

        # 10 月：4 号搁置转通关(+100) + 搁置游玩 60h(+300)；月度挑战未达成
        octo, saved_octo = _run_month("2026-10", 800, rules=RULES_COMPLETE)
        assert octo["month_xp"] == 400
        assert saved_octo["total_xp"] == 1200
        assert saved_octo["level"] == 3  # int(sqrt(1200/100))
        assert saved_octo["level"] > saved_sept["level"]
        assert ch.load_xp()["total_xp"] == 1200
    print("PASS XP 累计（跨月累加 + 等级按累计 XP 提升）")


def test_same_month_is_idempotent():
    """同一月份跑两次：不重复加分，挑战记录唯一。"""
    with temp_db():
        _, first = _run_month("2026-09", 0, rules=RULES_COMPLETE)
        _, second = _run_month("2026-09", 0, rules=RULES_COMPLETE)
        assert first["awarded"] == 800
        assert second["awarded"] == 0  # 第二次不翻倍
        assert second["total_xp"] == 800
        assert ch.load_xp()["total_xp"] == 800

        db = schema.get_connection()
        try:
            rows = list(db.execute(
                "SELECT month, xp_awarded FROM challenges WHERE month='2026-09'"))
        finally:
            db.close()
        assert len(rows) == 1 and rows[0]["xp_awarded"] == 800

        _, third = _run_month("2026-09", ch.load_xp()["total_xp"], rules=RULES_COMPLETE)
        assert third["awarded"] == 0 and third["total_xp"] == 800
    print("PASS 同月幂等（不翻倍 + 挑战记录唯一）")


def test_cross_month_keeps_history():
    """跨月：新月份产生新记录，上月记录保留。"""
    with temp_db():
        _run_month("2026-09", 0, rules=RULES_COMPLETE)
        _run_month("2026-10", ch.load_xp()["total_xp"], rules=RULES_COMPLETE)
        db = schema.get_connection()
        try:
            rows = {
                r["month"]: (r["xp_awarded"], r["completed"])
                for r in db.execute("SELECT month, xp_awarded, completed FROM challenges")
            }
        finally:
            db.close()
        assert set(rows) == {"2026-09", "2026-10"}
        assert rows["2026-09"] == (800, 1)  # 上月记录与已发放 XP 保留
        assert rows["2026-10"][0] == 400    # 新月份独立记账
    print("PASS 跨月（新记录生成 + 上月保留）")


def test_monthly_challenge_stable_and_completed():
    """月度挑战：goal/候选稳定可复现；达成后 completed=1 且写 completed_date。"""
    with temp_db():
        goals, candidates = set(), set()
        for _ in range(3):
            result = _compute("2026-09", 0, rules=RULES_COMPLETE)
            mc = result["monthly_challenge"]
            goals.add(mc["goal"])
            candidates.add(tuple(g["appid"] for g in mc["games"]))
            ch.get_or_create_monthly_challenge(
                "2026-09", mc["goal"], [g["appid"] for g in mc["games"]])

        # 月内稳定：目标唯一、候选游戏唯一（固定为 [1,2,4]）
        assert goals == {"通关 2 款搁置游戏"}
        assert candidates == {(1, 2, 4)}
        assert ch._pick_goal("2026-09", ["A", "B"]) == ch._pick_goal("2026-09", ["A", "B"])

        db = schema.get_connection()
        try:
            rows = list(db.execute(
                "SELECT id, completed, completed_date FROM challenges WHERE month='2026-09'"))
        finally:
            db.close()
        assert len(rows) == 1 and rows[0]["completed"] == 0
        assert rows[0]["completed_date"] is None

        # 达成：落库后 completed=1、completed_date 写入
        _, saved = _run_month("2026-09", 0, rules=RULES_COMPLETE)
        assert saved["completed"] is True
        assert saved["completed_date"] == "2026-09-21"
        db = schema.get_connection()
        try:
            row = db.execute(
                "SELECT completed, completed_date FROM challenges WHERE month='2026-09'"
            ).fetchone()
        finally:
            db.close()
        assert row["completed"] == 1 and row["completed_date"] == "2026-09-21"

        # 「玩满 10 小时」目标（4 号 60h > 10h）同样可达成
        _, saved_hours = _run_month("2026-09", 0, rules=RULES_HOURS)
        assert saved_hours["completed"] is True
    print("PASS 月度挑战（目标/候选稳定 + 达成写 completed/completed_date）")


def test_run_persists_and_prints_cumulative():
    """run() 编排：计算 -> 落库 -> 打印本月增量与累计 XP/等级。"""
    with temp_db():
        for day, rows in SNAPSHOTS.items():
            snapshot.save(
                {
                    r["appid"]: {
                        "playtime": PLAY_STATS[r["appid"]]["playtime"],
                        "last_played": 0,
                        "status": r["status"],
                    }
                    for r in rows
                },
                record_date=day,
            )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ch.run(SimpleNamespace(play_stats=PLAY_STATS, month="2026-09"))
        out = buf.getvalue()
        assert code == 0
        assert "累计 800 XP" in out and "等级 2" in out
        assert ch.load_xp()["total_xp"] == 800

        # 第二次运行：本次入账 +0，累计不变
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            code2 = ch.run(SimpleNamespace(play_stats=PLAY_STATS, month="2026-09"))
        out2 = buf2.getvalue()
        assert code2 == 0 and "本次入账 +0" in out2
        assert ch.load_xp()["total_xp"] == 800
    print("PASS run() 编排（落库 + 打印本月增量/累计 XP/等级）")


def test_db_failure_is_tolerated():
    """数据库不可用：不抛异常，退化为仅计算并打印。"""
    with broken_db():
        assert ch.load_xp() is None
        assert ch.get_or_create_monthly_challenge("2026-09", "目标", [1]) is None
        assert ch.apply_month_xp("2026-09", 100) is None
        assert ch.save("2026-09", {"goal": "G", "games": []}, 100) is None
        assert ch.load_month_xp("2026-09") is None

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ch.run(SimpleNamespace(play_stats=PLAY_STATS, month="2026-09"))
        out = buf.getvalue()
        assert code == 0
        assert "仅计算不落库" in out
    print("PASS 容错（DB 不可用不抛异常，退化为仅计算打印）")


def test_compute_challenge_does_not_touch_db():
    """compute_challenge 是纯函数：不创建数据库文件。"""
    with tempfile.TemporaryDirectory(prefix="gl_challenge_") as tmp:
        original = schema.DB_PATH
        schema.DB_PATH = Path(tmp) / "cache.db"
        try:
            result = _compute("2026-09", 0, rules=RULES_COMPLETE)
            assert result["month"] == "2026-09"
            assert not schema.DB_PATH.exists()
        finally:
            schema.DB_PATH = original
    print("PASS 纯函数（compute_challenge 不读写数据库）")


def test_legacy_schema_migration_is_idempotent():
    """旧库缺 xp_awarded 列时，init_db 幂等补列。"""
    with tempfile.TemporaryDirectory(prefix="gl_challenge_") as tmp:
        original = schema.DB_PATH
        schema.DB_PATH = Path(tmp) / "legacy.db"
        try:
            db = sqlite3.connect(str(schema.DB_PATH))
            try:
                db.execute(
                    "CREATE TABLE challenges (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "month TEXT, title TEXT, appids TEXT, completed INTEGER DEFAULT 0, "
                    "completed_date TEXT)"
                )
                db.commit()
            finally:
                db.close()
            schema.init_db()
            schema.init_db()  # 幂等：重复调用不报错、不重复加列
            db = schema.get_connection()
            try:
                cols = [r[1] for r in db.execute("PRAGMA table_info(challenges)")]
            finally:
                db.close()
            assert cols.count("xp_awarded") == 1
            assert ch.load_xp()["total_xp"] == 0
            assert ch.apply_month_xp("2026-09", 100)["total_xp"] == 100
        finally:
            schema.DB_PATH = original
    print("PASS 迁移（旧库幂等补 xp_awarded 列）")


def main():
    tests = [
        test_xp_accumulates_across_months,
        test_same_month_is_idempotent,
        test_cross_month_keeps_history,
        test_monthly_challenge_stable_and_completed,
        test_run_persists_and_prints_cumulative,
        test_db_failure_is_tolerated,
        test_compute_challenge_does_not_touch_db,
        test_legacy_schema_migration_is_idempotent,
    ]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 项验证通过")


if __name__ == "__main__":
    main()