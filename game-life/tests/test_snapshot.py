"""历史快照维护与回填的最小自测（core/snapshot.py）。

运行方式（仓库根目录）：
    python game-life/tests/test_snapshot.py
零依赖，纯标准库；全部使用临时数据库，不污染 data/cache.db。

覆盖：
    1. _day_from_ts 边界：过小(0/1/-5)/过大/None/非数字一律返回 ""，
       合法 unix 秒换算为 "YYYY-MM-DD"，未来 3 天的时间戳被上界拦下；
    2. backfill_from_play_stats 基本能力：两条不同历史日期分别落到对应日期，
       playtime/last_played/status 与输入一致（status 取自 classified 内部状态键）；
    3. 幂等：连续两次回填行数/日期数不变，第二次 skipped 增加、rows 为 0；
    4. 不覆盖（只补缺）语义：既有快照原样保留、不整日替换；
       only_missing=False（CLI --overwrite）时该日期才被整日覆盖；
    5. 绝不触碰今天：今天的快照行回填前后逐字段一致，输入里带今天也不写入；
    6. dry_run=True 不写入任何行；
    7. min_days_ago：0 时今天仍不写入；较大的值时较新记录被跳过且计入 skipped_recent；
    8. 脏数据防护：LastPlayed=0/1 这类缺失值不落库，不产生 1970-01-01 脏快照，
       混合输入只落合法那条；
    9. 回填产物可被可视化层使用：装配成 archaeologist 输入后能产出事件；
   10. 回归 save/load/list_dates/diff 的基本行为。
"""

import contextlib
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

# 让 game-life/ 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.schema as schema  # noqa: E402
from core import snapshot  # noqa: E402
from render import archaeologist  # noqa: E402


@contextlib.contextmanager
def temp_db():
    """把 data.schema.DB_PATH 指向临时文件，结束后清理（绝不碰真实 cache.db）。"""
    with tempfile.TemporaryDirectory(prefix="gl_snapshot_") as tmp:
        original = schema.DB_PATH
        schema.DB_PATH = Path(tmp) / "cache.db"
        try:
            schema.init_db()
            yield schema.DB_PATH
        finally:
            schema.DB_PATH = original


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _ts_at(day: str) -> int:
    """该日期当地中午的 unix 秒，避免边界上的日期漂移。"""
    return int(datetime.combine(date.fromisoformat(day), time(12, 0)).timestamp())


def _today_ts() -> int:
    return _ts_at(date.today().isoformat())


def _count_rows() -> int:
    db = schema.get_connection()
    try:
        return db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    finally:
        db.close()


def _row_id(appid: int, day: str):
    db = schema.get_connection()
    try:
        row = db.execute(
            "SELECT id FROM snapshots WHERE appid = ? AND record_date = ?", (appid, day)
        ).fetchone()
        return row["id"] if row else None
    finally:
        db.close()


def test_day_from_ts_bounds():
    """_day_from_ts 边界：脏值/超界一律 ""，合法值给出正确日期。"""
    # 下界/上界常量语义（与渲染层保持一致）
    assert snapshot._MIN_TIMESTAMP == 946684800
    assert snapshot._MAX_TIMESTAMP_SKEW == 86400

    # 无效输入：Steam 的 LastPlayed 缺失值、负数、极大值、None、非数字
    assert snapshot._day_from_ts(1) == ""
    assert snapshot._day_from_ts(0) == ""
    assert snapshot._day_from_ts(-5) == ""
    assert snapshot._day_from_ts(99999999999999) == ""
    assert snapshot._day_from_ts(None) == ""
    assert snapshot._day_from_ts("abc") == ""

    # 下界：下界前 1 秒无效，恰好等于下界有效（2000-01-01）
    assert snapshot._day_from_ts(snapshot._MIN_TIMESTAMP - 1) == ""
    assert snapshot._day_from_ts(snapshot._MIN_TIMESTAMP) == "2000-01-01"

    # 合法历史值
    assert snapshot._day_from_ts(1757000000) == "2025-09-04"

    # 当前时间戳合法（当天）
    now_ts = int(datetime.now().timestamp())
    assert snapshot._day_from_ts(now_ts) == date.today().isoformat()
    assert snapshot._day_from_ts(_today_ts()) == date.today().isoformat()

    # 上界生效：未来 3 天的时间戳/超大未来值返回 ""
    future_3d = int((datetime.now() + timedelta(days=3)).timestamp())
    assert snapshot._day_from_ts(future_3d) == ""
    assert snapshot._day_from_ts(int((datetime.now() + timedelta(days=3650)).timestamp())) == ""
    print("PASS _day_from_ts（下界/上界/脏值/合法值/未来时间戳）")


def test_backfill_places_records_by_date():
    """回填基本能力：按 last_played 落到对应历史日期，字段与输入一致。"""
    with temp_db():
        day_a, day_b = _days_ago(300), _days_ago(200)
        ts_a, ts_b = _ts_at(day_a), _ts_at(day_b)
        play_stats = {
            "101": {"playtime": 6000, "last_played": ts_a},
            "202": {"playtime": 120, "last_played": ts_b},
        }
        classified = {
            "101": {"status": "通关", "status_name": "已通关"},
            "202": {"status": "搁置", "status_name": "搁置"},
        }

        stats = snapshot.backfill_from_play_stats(play_stats, classified=classified)
        assert stats["dates"] == 2
        assert stats["rows"] == 2
        assert stats["skipped"] == 0
        assert stats["skipped_recent"] == 0

        dates = snapshot.list_dates()
        assert day_a in dates and day_b in dates

        rows_a = snapshot.load(day_a)
        rows_b = snapshot.load(day_b)
        assert len(rows_a) == 1 and len(rows_b) == 1
        row_a, row_b = rows_a[0], rows_b[0]
        assert row_a["record_date"] == day_a and row_a["appid"] == 101
        assert row_a["playtime"] == 6000 and row_a["last_played"] == ts_a
        assert row_a["status"] == "通关"  # 取自 classified 的内部状态键
        assert row_b["record_date"] == day_b and row_b["appid"] == 202
        assert row_b["playtime"] == 120 and row_b["last_played"] == ts_b
        assert row_b["status"] == "搁置"
        # 反向确认：没有跨日期串行
        assert [r["appid"] for r in snapshot.load(day_a)] == [101]
        assert [r["appid"] for r in snapshot.load(day_b)] == [202]
    print("PASS 回填基本能力（按日期落库 + playtime/last_played/status 一致）")


def test_backfill_is_idempotent():
    """幂等：连续两次回填，总行数/日期数不变，第二次 skipped 增加且 rows 为 0。"""
    with temp_db():
        day = _days_ago(300)
        ts = _ts_at(day)
        play_stats = {
            "1": {"playtime": 100, "last_played": ts},
            "2": {"playtime": 200, "last_played": ts},
        }

        first = snapshot.backfill_from_play_stats(play_stats)
        assert first["dates"] == 1 and first["rows"] == 2 and first["skipped"] == 0
        rows_after_first = _count_rows()
        dates_after_first = snapshot.list_dates()
        assert rows_after_first == 2 and dates_after_first == [day]

        second = snapshot.backfill_from_play_stats(play_stats)
        assert second["rows"] == 0
        assert second["dates"] == 0
        assert second["skipped"] == 1
        assert second["skipped"] > first["skipped"]

        assert _count_rows() == rows_after_first
        assert snapshot.list_dates() == dates_after_first
    print("PASS 幂等（二次回填 rows=0、skipped 增加、行数/日期数不变）")


def test_backfill_does_not_overwrite_existing_date():
    """不覆盖：既有日期整体跳过；only_missing=False（--overwrite）时才整日覆盖。"""
    with temp_db():
        day = _days_ago(300)
        ts = _ts_at(day)
        # 先用 save 写入一条自己的历史快照
        snapshot.save(
            {"999": {"playtime": 12345, "last_played": ts, "status": "在玩"}},
            record_date=day,
        )
        before = snapshot.load(day)
        assert len(before) == 1 and before[0]["appid"] == 999
        before_id = _row_id(999, day)

        # 只补缺：整日跳过，999 原样保留
        stats = snapshot.backfill_from_play_stats(
            {"777": {"playtime": 60, "last_played": ts}}
        )
        assert stats["dates"] == 0 and stats["rows"] == 0 and stats["skipped"] == 1
        rows = snapshot.load(day)
        assert len(rows) == 1
        assert rows[0]["appid"] == 999
        assert rows[0]["playtime"] == 12345
        assert rows[0]["last_played"] == ts
        assert rows[0]["status"] == "在玩"
        assert _row_id(999, day) == before_id  # 未被删除重建，即未整日替换

        # only_missing=False（对应 CLI --overwrite）：整日覆盖，999 被替换
        stats2 = snapshot.backfill_from_play_stats(
            {"777": {"playtime": 60, "last_played": ts}}, only_missing=False
        )
        assert stats2["dates"] == 1 and stats2["rows"] == 1
        rows2 = snapshot.load(day)
        assert len(rows2) == 1
        assert rows2[0]["appid"] == 777 and rows2[0]["playtime"] == 60
        assert all(r["appid"] != 999 for r in rows2)
    print("PASS 不覆盖（only_missing=True 保留 999 / False 整日覆盖）")


def test_backfill_never_touches_today():
    """绝不触碰今天：今天的快照行回填前后逐字段一致，输入带今天也不写入。"""
    with temp_db():
        today = snapshot._today()
        ts_today = _today_ts()
        snapshot.save(
            {"1": {"playtime": 500, "last_played": ts_today, "status": "在玩"}},
            record_date=today,
        )
        before_today = snapshot.load()
        assert len(before_today) == 1
        before_rows = _count_rows()

        play_stats = {
            # 今天：输入值与已存快照不同，用于证明没有被覆写
            "1": {"playtime": 99999, "last_played": ts_today},
            "2": {"playtime": 300, "last_played": _ts_at(_days_ago(300))},
        }
        stats = snapshot.backfill_from_play_stats(
            play_stats,
            classified={"1": {"status": "沉迷"}, "2": {"status": "通关"}},
            min_days_ago=0,
        )

        after_today = snapshot.load()
        assert after_today == before_today  # 逐字段完全一致
        assert after_today[0]["playtime"] == 500
        assert after_today[0]["status"] == "在玩"
        assert stats["skipped"] == 1  # 今天那条被跳过
        assert stats["dates"] == 1 and stats["rows"] == 1  # 只有历史那条落库
        assert _count_rows() == before_rows + 1
        assert snapshot.list_dates() == [today, _days_ago(300)]
    print("PASS 不碰今天（回填前后逐字段一致，今天永不写入）")


def test_backfill_dry_run_writes_nothing():
    """dry_run=True：只统计不写库，前后总行数一致。"""
    with temp_db():
        day_a, day_b = _days_ago(300), _days_ago(200)
        play_stats = {
            "1": {"playtime": 100, "last_played": _ts_at(day_a)},
            "2": {"playtime": 200, "last_played": _ts_at(day_b)},
        }
        before = _count_rows()
        assert before == 0

        stats = snapshot.backfill_from_play_stats(play_stats, dry_run=True)
        assert stats["dates"] == 2 and stats["rows"] == 2  # 统计照常给出
        assert _count_rows() == before
        assert snapshot.list_dates() == []
        assert snapshot.load(day_a) == [] and snapshot.load(day_b) == []

        # dry_run 不影响后续真实回填
        stats2 = snapshot.backfill_from_play_stats(play_stats)
        assert stats2["rows"] == 2 and stats2["skipped"] == 0
        assert _count_rows() == 2
    print("PASS dry_run（只统计不落库，前后行数一致）")


def test_backfill_min_days_ago():
    """min_days_ago：0 时今天仍不写入；较大值时较新记录计入 skipped_recent。"""
    with temp_db():
        today_ts = _today_ts()
        recent_day, old_day = _days_ago(2), _days_ago(400)
        recent_ts, old_ts = _ts_at(recent_day), _ts_at(old_day)

        # min_days_ago=0：今天仍被 backfill 拒绝（今天永远不写）
        stats0 = snapshot.backfill_from_play_stats(
            {"1": {"playtime": 10, "last_played": today_ts}}, min_days_ago=0
        )
        assert stats0["dates"] == 0 and stats0["rows"] == 0
        assert stats0["skipped"] == 1 and stats0["skipped_recent"] == 0
        assert snapshot.load() == [] and snapshot.list_dates() == []

        # min_days_ago=0：非今天的记录照常写入
        stats1 = snapshot.backfill_from_play_stats(
            {"1": {"playtime": 10, "last_played": recent_ts}}, min_days_ago=0
        )
        assert stats1["dates"] == 1 and stats1["rows"] == 1
        assert stats1["skipped_recent"] == 0
        assert snapshot.list_dates() == [recent_day]

    # 另起一个干净临时库，验证较大的 min_days_ago
    with temp_db():
        recent_day, old_day = _days_ago(2), _days_ago(400)
        recent_ts, old_ts = _ts_at(recent_day), _ts_at(old_day)

        # min_days_ago=1000：2 天前与 400 天前的记录都在 1000 天内 -> 全部跳过
        stats_big = snapshot.backfill_from_play_stats(
            {
                "1": {"playtime": 10, "last_played": recent_ts},
                "2": {"playtime": 20, "last_played": old_ts},
            },
            min_days_ago=1000,
        )
        assert stats_big["skipped_recent"] == 2
        assert stats_big["dates"] == 0 and stats_big["rows"] == 0
        assert stats_big["skipped"] == 0
        assert snapshot.list_dates() == []

        # 同一个 400 天前记录：默认 min_days_ago=1 时可以写入
        stats_default = snapshot.backfill_from_play_stats(
            {"2": {"playtime": 20, "last_played": old_ts}}
        )
        assert stats_default["skipped_recent"] == 0
        assert stats_default["dates"] == 1 and stats_default["rows"] == 1
        assert snapshot.list_dates() == [old_day]

        # 2 天前记录（> 默认 1 天）同样可写入
        stats_recent = snapshot.backfill_from_play_stats(
            {"1": {"playtime": 10, "last_played": recent_ts}}
        )
        assert stats_recent["dates"] == 1 and stats_recent["rows"] == 1
        assert snapshot.list_dates() == [recent_day, old_day]
    print("PASS min_days_ago（0 不写今天 / 较大值计入 skipped_recent）")


def test_backfill_rejects_dirty_timestamps():
    """脏数据防护：LastPlayed=0/1 不落库，不产生 1970-01-01 脏快照。"""
    with temp_db():
        stats = snapshot.backfill_from_play_stats(
            {"999": {"playtime": 0, "last_played": 1}},
            classified={"999": {"status": "未玩"}},
            min_days_ago=0,
        )
        assert stats["dates"] == 0
        assert stats["rows"] == 0
        assert stats["skipped"] == 0 and stats["skipped_recent"] == 0
        dates = snapshot.list_dates()
        assert "1970-01-01" not in dates
        assert dates == []

        # 其它脏值同样不落库
        dirty = {
            "998": {"playtime": 0, "last_played": 0},
            "997": {"playtime": 0, "last_played": None},
            "996": {"playtime": 0, "last_played": "abc"},
            "995": {"playtime": 0, "last_played": 99999999999999},
        }
        assert snapshot.backfill_from_play_stats(dirty, min_days_ago=0)["rows"] == 0
        assert snapshot.list_dates() == []

        # 混合输入：脏记录 + 400 天前合法记录，只有合法那条落库
        good_day = _days_ago(400)
        mixed = {
            "999": {"playtime": 0, "last_played": 1},
            "555": {"playtime": 300, "last_played": _ts_at(good_day)},
        }
        stats2 = snapshot.backfill_from_play_stats(mixed, min_days_ago=0)
        assert stats2["dates"] == 1 and stats2["rows"] == 1
        dates2 = snapshot.list_dates()
        assert dates2 == [good_day] and "1970-01-01" not in dates2
        rows = snapshot.load(good_day)
        assert len(rows) == 1 and rows[0]["appid"] == 555

        # 空输入 / None 不应报错
        assert snapshot.backfill_from_play_stats({})["rows"] == 0
        assert snapshot.backfill_from_play_stats(None)["rows"] == 0
    print("PASS 脏数据防护（0/1/None/非数字/极大值不落库，无 1970-01-01）")


def test_backfilled_snapshots_feed_archaeologist():
    """回填产物可被可视化层使用：装配成 archaeologist 输入后能产出事件。"""
    with temp_db():
        day_a, day_b = _days_ago(300), _days_ago(200)
        snapshot.backfill_from_play_stats(
            {
                "101": {"playtime": 6000, "last_played": _ts_at(day_a)},
                "202": {"playtime": 120, "last_played": _ts_at(day_b)},
            },
            classified={"101": {"status": "通关"}, "202": {"status": "搁置"}},
        )

        snapshots = []
        for day in snapshot.list_dates():
            for row in snapshot.load(day):
                # 快照表没有 name 列，可视化层按约定需要外部补 name
                snapshots.append({**row, "name": f"Game{row['appid']}"})
        assert len(snapshots) == 2
        assert all(s["name"] for s in snapshots)

        payload = archaeologist.normalize_archaeologist_data({"snapshots": snapshots})
        events = [
            ev
            for year_group in payload["year_groups"]
            for month_group in year_group["month_groups"]
            for ev in month_group["events"]
        ]
        assert len(events) > 0
        assert payload["stats"]["total"] == len(events)
        assert {ev["name"] for ev in events} == {"Game101", "Game202"}
        assert payload["stats"]["years"] >= 1
    print("PASS 可视化层可用（回填快照 -> archaeologist 事件数 > 0）")


def test_save_load_list_dates_diff_baseline():
    """回归 save/load/list_dates/diff 的基本行为。"""
    with temp_db():
        day_a, day_b = _days_ago(300), _days_ago(200)
        ts_a, ts_b = _ts_at(day_a), _ts_at(day_b)

        # save 同一天两次调用只保留最新（先删后插，行数不翻倍）
        snapshot.save(
            {
                "1": {"playtime": 100, "last_played": ts_a, "status": "在玩"},
                "2": {"playtime": 50, "last_played": ts_a, "status": "搁置"},
            },
            record_date=day_a,
        )
        assert len(snapshot.load(day_a)) == 2
        snapshot.save(
            {"1": {"playtime": 160, "last_played": ts_a, "status": "在玩"}},
            record_date=day_a,
        )
        rows_a = snapshot.load(day_a)
        assert len(rows_a) == 1 and rows_a[0]["appid"] == 1
        assert rows_a[0]["playtime"] == 160

        snapshot.save(
            {"1": {"playtime": 300, "last_played": ts_b, "status": "通关"}},
            record_date=day_b,
        )

        # list_dates 按日期倒序（较新的 day_b 在前）
        assert snapshot.list_dates() == [day_b, day_a]

        # diff：同一 appid 两个日期的 playtime_delta
        d = snapshot.diff(1, day_a, day_b)
        assert d is not None
        assert d["appid"] == 1
        assert d["playtime_delta"] == 300 - 160
        assert d["date_a"] == day_a and d["date_b"] == day_b
        assert d["status_a"] == "在玩" and d["status_b"] == "通关"

        # 缺失记录返回 None
        assert snapshot.diff(2, day_a, day_b) is None        # appid 2 只在 day_a 有
        assert snapshot.diff(1, day_a, "1999-01-01") is None  # 不存在的日期
        assert snapshot.diff(4242, day_a, day_b) is None      # 不存在的 appid

        # 没有快照的日期 load 返回空
        assert snapshot.load(_days_ago(50)) == []
    print("PASS 回归 save/load/list_dates/diff（同日覆盖、delta、缺失返回 None）")


def main():
    tests = [
        test_day_from_ts_bounds,
        test_backfill_places_records_by_date,
        test_backfill_is_idempotent,
        test_backfill_does_not_overwrite_existing_date,
        test_backfill_never_touches_today,
        test_backfill_dry_run_writes_nothing,
        test_backfill_min_days_ago,
        test_backfill_rejects_dirty_timestamps,
        test_backfilled_snapshots_feed_archaeologist,
        test_save_load_list_dates_diff_baseline,
    ]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 项验证通过")


if __name__ == "__main__":
    main()
