"""状态引擎 classifier 的基础验证（零依赖，纯标准库）。

运行方式（在 game-life/ 目录下）：
    python tests/test_classifier.py

steam_writer 写回用例依赖 vdf/psutil，缺失时自动跳过。
"""

import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

# 让 game-life/ 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import classifier as c  # noqa: E402

TODAY = date(2026, 9, 19)
DAY = timedelta(days=1)


def _ts(days_ago: int) -> int:
    """生成 unix 时间戳：今天往前推 days_ago 天。"""
    base = datetime(TODAY.year, TODAY.month, TODAY.day)
    return int((base - timedelta(days=days_ago)).timestamp())


def test_not_played():
    assert c.classify_game(0, _ts(0), today=TODAY) == c.STATUS_NOT_PLAYED
    # 时长 0 优先于一切
    assert c.classify_game(0, _ts(0), hltb_time=10, today=TODAY) == c.STATUS_NOT_PLAYED
    print("PASS 未玩")


def test_addicted():
    assert c.classify_game(3000, today=TODAY) == c.STATUS_ADDICTED
    assert c.classify_game(99999, today=TODAY) == c.STATUS_ADDICTED
    # 优先级：沉迷 > 通关（即使 HLTB 也满足）
    assert c.classify_game(5000, hltb_time=5000, today=TODAY) == c.STATUS_ADDICTED
    print("PASS 沉迷")


def test_completed():
    hltb = 1200  # 20 小时
    assert c.classify_game(960, hltb_time=hltb, today=TODAY) == c.STATUS_COMPLETED
    assert c.classify_game(959, hltb_time=hltb, today=TODAY) != c.STATUS_COMPLETED
    print("PASS 通关")


def test_hltb_skip():
    # 无 HLTB 数据：优雅跳过通关判定，落到 在玩/搁置
    assert c.classify_game(100, _ts(0), hltb_time=None, today=TODAY) == c.STATUS_PLAYING
    assert c.classify_game(100, _ts(0), hltb_time=0, today=TODAY) == c.STATUS_PLAYING
    print("PASS HLTB 跳过")


def test_playing_vs_backlog():
    assert c.classify_game(100, _ts(0), today=TODAY) == c.STATUS_PLAYING
    assert c.classify_game(100, _ts(30), today=TODAY) == c.STATUS_PLAYING   # 正好 30 天
    assert c.classify_game(100, _ts(31), today=TODAY) == c.STATUS_BACKLOG
    # 无最近游玩记录 -> 搁置
    assert c.classify_game(100, None, today=TODAY) == c.STATUS_BACKLOG
    assert c.classify_game(100, 0, today=TODAY) == c.STATUS_BACKLOG
    print("PASS 在玩/搁置")


def test_custom_rules():
    # 阈值可配置
    r1 = {"playtime": {"addicted_min": 600}}
    assert c.classify_game(1000, today=TODAY, rules=r1) == c.STATUS_ADDICTED

    r2 = {"playtime": {"recent_days": 7}}
    assert c.classify_game(100, _ts(10), today=TODAY, rules=r2) == c.STATUS_BACKLOG
    r3 = {"playtime": {"recent_days": 60}}
    assert c.classify_game(100, _ts(40), today=TODAY, rules=r3) == c.STATUS_PLAYING

    r4 = {"playtime": {"hltb_ratio": 0.5}}
    assert c.classify_game(600, hltb_time=1200, today=TODAY, rules=r4) == c.STATUS_COMPLETED
    print("PASS 自定义规则")


def test_status_display():
    assert c.status_display(c.STATUS_COMPLETED) == "已通关"
    assert c.status_display(c.STATUS_PLAYING) == "在玩"
    assert c.status_display("未知键") == "未知键"
    r = {"status_names": {c.STATUS_COMPLETED: "毕业"}}
    assert c.status_display(c.STATUS_COMPLETED, rules=r) == "毕业"
    print("PASS 展示名映射")


def test_categories():
    games = {
        10: {"playtime": 100, "last_played": _ts(0),
             "genres": ["Action"], "tags": ["Multiplayer"]},
        20: {"playtime": 100, "last_played": _ts(0),
             "genres": ["Casual"], "tags": ["Free to Play"]},
    }
    result = c.classify_games(games, today=TODAY)
    assert result[10]["genre_categories"] == ["动作"]
    assert result[10]["tag_categories"] == ["多人"]
    # ignore_tags 过滤 + 无匹配 -> 空
    assert result[20]["genre_categories"] == []
    assert result[20]["tag_categories"] == []
    assert result[10]["status_name"] == "在玩"
    print("PASS 类型/标签分类")


def test_classify_games_with_hltb():
    games = {7: {"playtime": 2000, "last_played": _ts(100)}}
    hltb = {7: {"main": 2400}}  # 2000 >= 2400*0.8=1920 -> 通关
    result = c.classify_games(games, hltb_data=hltb, today=TODAY)
    assert result[7]["status"] == c.STATUS_COMPLETED
    # 无 HLTB -> 落回 搁置
    result2 = c.classify_games(games, today=TODAY)
    assert result2[7]["status"] == c.STATUS_BACKLOG
    print("PASS 批量 + HLTB 接入")


def test_load_rules():
    # 文件不存在 -> 默认规则
    assert c.load_rules("不存在的路径.json") == c.DEFAULT_RULES
    # 部分覆盖 -> 深合并
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as f:
        f.write('{"playtime": {"recent_days": 60}}')
        tmp = f.name
    try:
        rules = c.load_rules(tmp)
        assert rules["playtime"]["recent_days"] == 60
        assert rules["playtime"]["addicted_min"] == 3000  # 默认值保留
    finally:
        Path(tmp).unlink()
    print("PASS rules.json 加载")


def test_rules_json_integration():
    """rules.json 两种布局（顶层 / categories 容器）都生效，用户配置不被静默忽略。"""
    rules = c.load_rules()
    # categories 容器应被提升到顶层
    assert "categories" not in rules
    # 实际 rules.json 的整表配置整体替换默认表（独立游戏，而非默认的 独立）
    assert "独立游戏" in rules["genre_rules"]
    assert "独立" not in rules["genre_rules"]
    assert c.categorize_genres(["Indie"], rules) == ["独立游戏"]
    assert c.status_display(c.STATUS_COMPLETED, rules) == "已通关"
    # playtime 标量仍按需覆盖，缺省项保留默认值
    assert rules["playtime"]["addicted_min"] == 3000
    # 顶层布局（02 文档写法）同样生效
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as f:
        f.write('{"status_names": {"通关": "毕业"}, "genre_rules": {"独立游戏": ["Indie"]}, '
                '"tag_rules": {"肉鸽": ["Roguelike"]}, "ignore_tags": []}')
        tmp = f.name
    try:
        flat = c.load_rules(tmp)
        assert flat["tag_rules"] == {"肉鸽": ["Roguelike"]}
        assert c.categorize_tags(["Roguelike"], flat) == ["肉鸽"]
        assert c.status_display(c.STATUS_COMPLETED, flat) == "毕业"
    finally:
        Path(tmp).unlink()
    print("PASS rules.json 双布局集成")


def test_steam_writer():
    """steam_writer 写回：中止路径/备份/合并保留/幂等/原子替换；缺 vdf/psutil 则跳过。"""
    try:
        import vdf  # noqa: F401
        from core import steam_writer as sw
    except ImportError as exc:
        print(f"SKIP steam_writer（缺少依赖: {exc}）")
        return

    import shutil
    import types

    root = Path(tempfile.mkdtemp(prefix="gl_steam_"))
    acct = "12345"
    shared = root / "userdata" / acct / "7" / "remote" / "sharedconfig.vdf"
    tmp_path = shared.with_suffix(".vdf.tmp")
    backup_path = shared.with_suffix(".bak")
    initial = {
        "UserRoamingConfigStore": {"Software": {"Valve": {"Steam": {"Apps": {
            "730": {"tags": {"0": "FPS"}},
            "200": {"tags": {"0": "RPG"}},
        }}}}}
    }
    try:
        shared.parent.mkdir(parents=True, exist_ok=True)
        with open(shared, "w", encoding="utf-8-sig") as fh:
            vdf.dump(initial, fh)
        original_bytes = shared.read_bytes()

        # 1) 合并纯函数：追加去重，保持顺序
        assert sw.merge_tags({"在玩"}, ["在玩", "FPS"]) == ["在玩", "FPS"]

        # 2) Steam 运行 -> 中止（mock psutil.process_iter 返回 steam.exe）
        class _FakeProc:
            def __init__(self, info):
                self.info = info

        fake_psutil = types.ModuleType("psutil")
        fake_psutil.process_iter = lambda attrs=None: [
            _FakeProc({"name": "explorer.exe"}), _FakeProc({"name": "Steam.exe"})]
        fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
        fake_psutil.AccessDenied = type("AccessDenied", (Exception,), {})
        saved_psutil = sys.modules.get("psutil")
        sys.modules["psutil"] = fake_psutil
        try:
            assert sw.is_steam_running() is True
            try:
                sw.write_categories(str(root), acct, {730: ["在玩"]})
                raise AssertionError("Steam 运行时应中止写回")
            except sw.SteamRunningError:
                pass
            assert not backup_path.exists()  # 中止时不得产生备份
        finally:
            sys.modules.pop("psutil", None)
            if saved_psutil is not None:
                sys.modules["psutil"] = saved_psutil

        # 3) 正常写回：备份生成 + 现有分类保留 + 追加去重 + 原子替换（无残留临时文件）
        orig_is = sw.is_steam_running
        sw.is_steam_running = lambda *a, **k: False
        try:
            returned = sw.write_categories(str(root), acct, {730: ["在玩"]})
            assert str(backup_path) == returned
            assert backup_path.exists()
            assert backup_path.read_bytes() == original_bytes  # 备份=写前原文件
            with open(shared, "r", encoding="utf-8-sig") as fh:
                data = vdf.load(fh)
            apps = data["UserRoamingConfigStore"]["Software"]["Valve"]["Steam"]["Apps"]
            # 现有 FPS 保留 + 追加 在玩，去重排序
            assert sorted(apps["730"]["tags"].values()) == ["FPS", "在玩"]
            assert sorted(apps["200"]["tags"].values()) == ["RPG"]  # 未涉及 appid 保留
            assert not tmp_path.exists()  # 原子替换后临时文件被清理

            # 4) 幂等：再跑一次结果不变
            sw.write_categories(str(root), acct, {730: ["在玩"]})
            with open(shared, "r", encoding="utf-8-sig") as fh:
                data2 = vdf.load(fh)
            apps2 = data2["UserRoamingConfigStore"]["Software"]["Valve"]["Steam"]["Apps"]
            assert sorted(apps2["730"]["tags"].values()) == ["FPS", "在玩"]
            assert not tmp_path.exists()
        finally:
            sw.is_steam_running = orig_is
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("PASS steam_writer 备份/合并保留/幂等/中止/原子替换")



# === 第四轮：边界 + HLTB 表联调 ===
#
# 说明：这两个用例早先直接读**开发机 data/cache.db 的 hltb 表**（那里恰好有
# 220 Half-Life 2 = 1200 分钟）。那属于本机缓存、不进仓库，换台机器就是空表
# -> 直接 TypeError。现在改成在**临时数据库**里种入同样的样本行，
# 断言完全等价，且跑在任何机器上都自给自足、绝不碰真实 cache.db。

import contextlib
import tempfile as _tempfile


@contextlib.contextmanager
def _temp_hltb_db(rows):
    """把 data.schema.DB_PATH 指向临时库并种入 hltb 样本行（用完即删）。"""
    import data.schema as _schema
    original = _schema.DB_PATH
    with _tempfile.TemporaryDirectory(prefix="gl_hltb_") as tmp:
        _schema.DB_PATH = Path(tmp) / "cache.db"
        try:
            _schema.init_db()
            db = _schema.get_connection()
            try:
                for appid, name, main in rows:
                    db.execute(
                        "INSERT OR REPLACE INTO hltb (appid, name, main, extra, completionist, last_updated)"
                        " VALUES (?,?,?,?,?,?)",
                        (appid, name, main, None, None, "2026-01-01"),
                    )
                db.commit()
            finally:
                db.close()
            yield
        finally:
            _schema.DB_PATH = original


def test_hltb_boundary_from_db():
    """用 hltb 表（临时库里的同款样本行）验证通关判定四种情形。"""
    from data.schema import get_connection
    with _temp_hltb_db([(220, "Half-Life 2", 1200.0)]):
        db = get_connection()
        try:
            row = dict(db.execute(
                "SELECT appid, name, main FROM hltb WHERE appid=?", (220,)).fetchone())
            hltb_main = row["main"]           # 1200 min (Half-Life 2)
        finally:
            db.close()

        # 1) 正好 = 0.8 倍 -> 通关
        assert c.classify_game(int(hltb_main * 0.8), hltb_time=hltb_main, today=TODAY) == c.STATUS_COMPLETED
        # 2) 略低于 0.8 倍 -> 非通关（落到在玩）
        assert c.classify_game(int(hltb_main * 0.8) - 1, hltb_time=hltb_main, today=TODAY) != c.STATUS_COMPLETED
    # 3) main=0 优雅跳过通关判定
    assert c.classify_game(99999, hltb_time=0, today=TODAY) == c.STATUS_ADDICTED
    # 4) main=None 跳过通关判定
    assert c.classify_game(99999, hltb_time=None, today=TODAY) == c.STATUS_ADDICTED
    # 5) 无 HLTB 条目（appid 99999 查不到）
    assert c.classify_game(100, _ts(0), hltb_time=None, today=TODAY) == c.STATUS_PLAYING
    print("PASS HLTB 边界（临时表样本数据）")


def test_hltb_real_table_scenarios():
    """读取 hltb 表（临时库样本行）与 games 合成数据，逐条验证 classifier 输出。"""
    from data.schema import get_connection
    seed = [
        (220, "Half-Life 2", 1200.0),
        (730, "Counter-Strike 2", 1500.0),
        (274190, "Broforce", 600.0),
    ]
    with _temp_hltb_db(seed):
        db = get_connection()
        try:
            rows = db.execute("SELECT appid, main FROM hltb WHERE main IS NOT NULL").fetchall()
        finally:
            db.close()
        games = {}
        hltb_data = {}
        for r in rows:
            appid = r["appid"]
            main = r["main"]
            if not main or main <= 0:
                continue  # main=0/None 时通关判定被跳过，不参与本场景
            hltb_data[appid] = {"main": main}
            games[appid] = {"playtime": int(main * 0.8), "last_played": _ts(0)}
        assert hltb_data, "临时库应有样本行"
        result = c.classify_games(games, hltb_data=hltb_data, today=TODAY)
        for appid, info in result.items():
            assert info["status"] == c.STATUS_COMPLETED, (appid, info["status"], hltb_data[appid])
        # 缺失条目跳过通关
        games[99999] = {"playtime": 100, "last_played": _ts(0)}
        result2 = c.classify_games(games, hltb_data=hltb_data, today=TODAY)
        assert result2[99999]["status"] == c.STATUS_PLAYING
    print("PASS HLTB 表全量场景")


def test_future_last_played():
    """未来时间戳（last_played > today）应判为在玩。"""
    future = int(datetime(TODAY.year, TODAY.month, TODAY.day).timestamp()) + 86400 * 10
    assert c.classify_game(100, future, today=TODAY) == c.STATUS_PLAYING
    assert c.classify_game(5000, future, today=TODAY) == c.STATUS_ADDICTED
    print("PASS 未来时间戳")


def test_huge_playtime_over_10000h():
    """超长时长 >10000h = 600001 min，始终沉迷。"""
    assert c.classify_game(600001, today=TODAY) == c.STATUS_ADDICTED
    assert c.classify_game(1000000, today=TODAY) == c.STATUS_ADDICTED
    print("PASS 超长时长")


def test_negative_and_missing_fields():
    """负值 / 缺字段安全判定。"""
    assert c.classify_game(-100, today=TODAY) == c.STATUS_NOT_PLAYED
    assert c.classify_game(-100, _ts(0), today=TODAY) == c.STATUS_NOT_PLAYED
    result = c.classify_games({888: {}}, today=TODAY)
    assert result[888]["status"] == c.STATUS_NOT_PLAYED
    print("PASS 负值/缺字段")


def test_addicted_over_completed_with_hltb():
    """沉迷与通关优先级冲突：时长≥沉迷阈值时，即使满足通关条件仍判沉迷。"""
    assert c.classify_game(5000, hltb_time=5000, today=TODAY) == c.STATUS_ADDICTED
    assert c.classify_game(3000, hltb_time=3000, today=TODAY) == c.STATUS_ADDICTED
    assert c.classify_game(2401, hltb_time=3000, today=TODAY) == c.STATUS_COMPLETED
    print("PASS 沉迷/通关优先级")


def main():
    tests = [
        test_not_played, test_addicted, test_completed, test_hltb_skip,
        test_playing_vs_backlog, test_custom_rules, test_status_display,
        test_categories, test_classify_games_with_hltb, test_load_rules,
        test_rules_json_integration, test_steam_writer,
        test_hltb_boundary_from_db, test_hltb_real_table_scenarios,
        test_future_last_played, test_huge_playtime_over_10000h,
        test_negative_and_missing_fields, test_addicted_over_completed_with_hltb,
    ]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 组验证通过")


if __name__ == "__main__":
    main()
