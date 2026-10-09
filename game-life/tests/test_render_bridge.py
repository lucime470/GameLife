"""真实数据桥接最小自测（core/render_bridge.py）。

运行方式（仓库根目录）：
    python game-life/tests/test_render_bridge.py
零额外依赖（标准库 + 既有 requirements.txt），完全离线、不联网：
    - 不读取/写入真实 Steam 目录，不抓取 Store 元数据；
    - 需要数据库的用例统一指向临时文件，绝不污染 data/cache.db。

覆盖：
    1. 分钟 -> 小时换算（含取整与非法输入兜底）；
    2. 库范围口径：games_meta 里多出的 appid 不得扩大「我的库」
       （复现并锁死 174 vs 177 这类缓存虚高问题）；
    3. 缺 genre 兜底为「未知」、缺 name 兜底为「appid N」；
    4. 空库 / 空快照 / 无元数据下 6 类 payload 都能装配且不崩；
    5. 脏时间戳（LastPlayed=0/1）不落成 1970 日期；
    6. 快照过滤：与当期 last_played 一致的当日快照不会重复进入时间轴；
    7. 各页面 payload 落到渲染层 normalize 后结构可用（真跑一遍渲染函数）；
    8. --demo 路径不受影响（渲染模块内置示例数据仍可生成 HTML）。
"""

import contextlib
import re
import sys
import tempfile
from pathlib import Path

# 让 game-life/ 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.schema as schema  # noqa: E402
from core import render_bridge as rb  # noqa: E402
from render import annual_report, archaeologist, checkup, graveyard, starmap, timemachine  # noqa: E402


@contextlib.contextmanager
def temp_db():
    """把 data.schema.DB_PATH 指向临时文件，结束后清理（绝不碰真实 cache.db）。"""
    with tempfile.TemporaryDirectory(prefix="gl_bridge_", ignore_cleanup_errors=True) as tmp:
        original = schema.DB_PATH
        schema.DB_PATH = Path(tmp) / "cache.db"
        try:
            schema.init_db()
            yield schema.DB_PATH
        finally:
            schema.DB_PATH = original


# 展示名回落测试用的合成 appid：刻意挑**任何来源都查不到**的号段，
# 这样「缺名字 -> appid N」的回落逻辑可以独立验证，不会被本地中文名表/官方
# appinfo 里真实游戏的中文名盖掉（真实 appid 现在会解析出权威中文名，那是另一批用例）。
GHOST_A = 77700001        # 库里没有它的名字与类型
GHOST_B = 77700002        # 同上，且连英文名都没有


def _sample_library():
    """两份 appid 的迷你库：一份有完整元数据，一份缺失（用于兜底断言）。"""
    play_stats = {
        GHOST_A: {"playtime": 150, "last_played": 1690000000},   # 2.5 小时
        GHOST_B: {"playtime": 0, "last_played": 0},              # 未玩
    }
    games_meta = {
        GHOST_A: {"name": "ELDEN RING", "genres": ["Action", "RPG"], "price": 29800},
        GHOST_B: {},                                             # 缺 name / genre
    }
    return play_stats, games_meta


def test_minutes_to_hours():
    assert rb.minutes_to_hours(60) == 1.0
    assert rb.minutes_to_hours(150) == 2.5
    assert rb.minutes_to_hours(0) == 0.0
    assert rb.minutes_to_hours(None) == 0.0
    assert rb.minutes_to_hours("abc") == 0.0
    assert rb.minutes_to_hours(10000) == 166.67
    print("  [1] 分钟 -> 小时换算 OK")


def test_library_scope_excludes_meta_only_appids():
    """games_meta 里多出的 appid（缓存残留）绝不能进入「我的库」。"""
    play_stats = {480: {"playtime": 60, "last_played": 1690000000}}
    games_meta = {
        480: {"name": "Spacewar", "genres": ["Action"]},
        570: {"name": "Dota 2", "genres": ["Strategy"]},   # 不在库里
        99999: {"name": "Ghost", "genres": ["Indie"]},     # 不在库里
    }
    report = rb.build_report(play_stats, games_meta, None)
    assert report["stats"]["total_games"] == 1, report["stats"]["total_games"]
    assert report["user"]["total_games"] == 1
    assert rb.library_appids(play_stats) == [480]
    assert list(rb.build_library(play_stats, games_meta, None)["games"]) == ["480"]
    assert rb.build_checkup(play_stats, games_meta, None)["stats"]["total_games"] == 1
    # 类型聚合里也不能出现那两个多出来的 appid 的类型
    genre_names = {row["name"] for row in report["genres"]}
    assert "Strategy" not in genre_names, genre_names
    print("  [2] 库范围以 play_stats 为准，元数据缓存不扩大库 OK")


def test_missing_genre_and_name_fallback():
    play_stats, games_meta = _sample_library()
    games = rb.build_library(play_stats, games_meta, None)["games"]
    # 缺 name -> "appid N"；缺 genre -> "未知"
    assert games[str(GHOST_B)]["name"] == f"appid {GHOST_B}", games[str(GHOST_B)]
    assert games[str(GHOST_B)]["genre"] == rb.UNKNOWN_GENRE
    assert games[str(GHOST_A)]["name"] == "ELDEN RING"
    assert games[str(GHOST_A)]["genre"] == "Action"
    # 时长单位为小时
    assert games[str(GHOST_A)]["playtime"] == 2.5
    assert games[str(GHOST_B)]["playtime"] == 0.0

    # 显式传入分类，避免依赖「无状态数据时的兜底推断」，让断言确定
    classified = {
        GHOST_A: {"status": "在玩", "status_name": "在玩"},
        GHOST_B: {"status": "未玩", "status_name": "未玩"},
    }
    report = rb.build_report(play_stats, games_meta, classified)
    assert report["top_games"][0]["name"] == "ELDEN RING"
    assert report["top_games"][0]["status"] == "在玩"
    assert report["stats"]["total_hours"] == 2.5
    assert report["stats"]["total_games"] == 2
    assert report["stats"]["backlog_rate"] == 0.5  # 未玩 1/2
    assert {row["name"] for row in report["genres"]} == {"Action", "RPG", rb.UNKNOWN_GENRE}
    print("  [3] 缺 name -> appid N / 缺 genre -> 未知 OK")


def test_empty_library_never_crashes():
    for kind in rb.RENDER_KINDS:
        payload = rb.build_payload(kind, {}, {}, None, snapshots=[], user_name="空库")
        assert isinstance(payload, dict) and payload, kind
    # 空库的 report 也应给出 0 而不是崩
    report = rb.build_report({}, {}, None)
    assert report["stats"]["total_games"] == 0
    assert report["stats"]["total_hours"] == 0
    # 未知 kind 明确报错
    try:
        rb.build_payload("nope", {}, {}, None)
    except ValueError:
        pass
    else:
        raise AssertionError("未知页面类型应抛 ValueError")
    print("  [4] 空库 / 未知类型不崩 OK")


def test_dirty_timestamps_are_dropped():
    play_stats = {
        2: {"playtime": 60, "last_played": 1},        # Steam 缺失值
        3: {"playtime": 60, "last_played": 0},
        4: {"playtime": 60, "last_played": -5},
        5: {"playtime": 60, "last_played": None},
        6: {"playtime": 60, "last_played": "abc"},
        7: {"playtime": 60, "last_played": 1690000000},  # 合法
    }
    games = rb.build_library(play_stats, None, None)["games"]
    for appid in ("2", "3", "4", "5", "6"):
        assert games[appid]["last_played"] == "", (appid, games[appid])
    assert games["7"]["last_played"] == "2023-07-22", games["7"]["last_played"]
    print("  [5] 脏时间戳不落成 1970 日期 OK")


def test_snapshots_filtering_and_payload_shape():
    play_stats = {GHOST_A: {"playtime": 150, "last_played": 1690000000}}
    games_meta = {GHOST_A: {"name": "ELDEN RING", "genres": ["Action"]}}
    rows = [
        # 与当期 last_played 同日 -> 视为当期数据，应被过滤掉
        {"record_date": "2023-07-22", "appid": GHOST_A, "playtime": 150, "last_played": 1690000000, "status": "搁置"},
        # 更早的历史 -> 保留
        {"record_date": "2022-01-01", "appid": GHOST_A, "playtime": 60, "last_played": 1640000000, "status": "在玩"},
        # 非本库 appid -> 丢弃
        {"record_date": "2022-01-01", "appid": 999, "playtime": 1, "last_played": 1640000000, "status": "未玩"},
    ]
    kept = rb.history_snapshots(rows, play_stats, games_meta)
    assert len(kept) == 1, kept
    assert kept[0]["appid"] == GHOST_A
    assert kept[0]["name"] == "ELDEN RING"
    assert kept[0]["last_played"] == "2021-12-20"

    timeline = rb.build_timeline(play_stats, games_meta, rows, user_name="测试")
    assert timeline["user_name"] == "测试"
    assert timeline["play_stats"][str(GHOST_A)]["name"] == "ELDEN RING"
    assert timeline["play_stats"][str(GHOST_A)]["playtime"] == 150  # 保持分钟
    # today 保留全部本库快照行（渲染层按 appid 去重）
    today = rb.build_today(play_stats, games_meta, rows, user_name="测试")
    assert {row["appid"] for row in today["snapshots"]} == {GHOST_A}
    print("  [6] 快照过滤 / timeline & today payload 结构 OK")


def test_checkup_ratios_are_fractions():
    play_stats = {
        1: {"playtime": 600, "last_played": 1690000000},   # 10h 通关
        2: {"playtime": 0, "last_played": 0},              # 未玩
        3: {"playtime": 60, "last_played": 1600000000},    # 1h 搁置
    }
    games_meta = {1: {"name": "A", "genres": ["Action"]}, 2: {"name": "B", "genres": ["Indie"]}, 3: {"name": "C", "genres": ["Action"]}}
    classified = {
        1: {"status": "通关", "status_name": "已通关"},
        2: {"status": "未玩", "status_name": "未玩"},
        3: {"status": "搁置", "status_name": "搁置"},
    }
    payload = rb.build_checkup(play_stats, games_meta, classified)
    stats = payload["stats"]
    assert stats["total_games"] == 3
    assert stats["total_playtime_hours"] == 11.0
    for key in ("dust_rate", "played_ratio"):
        assert 0.0 <= stats[key] <= 1.0, (key, stats[key])
    assert abs(stats["dust_rate"] - round(2 / 3 * 100, 1) / 100) < 1e-6   # 未玩 + 搁置 = 2/3
    # 通过率已废弃（依赖不可用的通关判定、恒为 0 的假指标），payload 不得再携带
    assert "completion_rate" not in stats
    assert abs(stats["played_ratio"] - round(2 / 3 * 100, 1) / 100) < 1e-6
    assert stats["shelved_count"] == 1
    assert stats["unplayed_count"] == 1
    assert payload["top_game"]["name"] == "A"
    assert payload["top_game"]["hours"] == 10.0
    print("  [7] checkup 比率为 0~1 小数 OK")


def test_payloads_survive_render_normalizers():
    """把桥接产物交给真实渲染层的 normalize，确保字段名对得上。"""
    play_stats, games_meta = _sample_library()
    classified = {
        GHOST_A: {"status": "在玩", "status_name": "在玩"},
        GHOST_B: {"status": "未玩", "status_name": "未玩"},
    }
    report = annual_report.normalize_data(rb.build_report(play_stats, games_meta, classified, user_name="玩家"))
    assert report["user"]["name"] == "玩家"
    assert report["stats"]["total_games"] == 2
    assert report["stats"]["total_hours"] == 2.5
    # 通关数是恒为 0 的假指标（依赖不可用的通关判定），payload 不得再携带
    assert "completed_games" not in rb.build_report(play_stats, games_meta, classified)["stats"]
    assert "completed_games" not in report["stats"]
    assert report["top_games"][0]["hours"] == 2.5
    assert report["top_games"][0]["status"] == "在玩"
    assert report["genres"][0]["share"] > 0

    grave = graveyard.normalize_graveyard_data(rb.build_library(play_stats, games_meta, classified, user_name="玩家"))
    assert grave["user_name"] == "玩家"
    assert grave["stats"]["unburied"] == 1        # GHOST_B 未玩 -> 未入土
    assert len(grave["games"]) == 0               # GHOST_A 在玩 -> 既不入墓园也不入未入土

    star = starmap.normalize_starmap_data(rb.build_library(play_stats, games_meta, classified, user_name="玩家"))
    assert len(star["stars"]) == 2
    assert star["stats"]["playing"] == 1
    assert star["stats"]["unplayed"] == 1
    # 「通关」状态已废弃（依赖不可用数据、恒为 0）：图例/统计不得再出现
    assert "completed" not in star["stats"]
    assert all(s["status"] != "completed" for s in star["stars"])
    star_names = {s["name"] for s in star["stars"]}
    assert star_names == {"ELDEN RING", f"appid {GHOST_B}"}

    tlp = archaeologist.normalize_archaeologist_data(
        rb.build_timeline(play_stats, games_meta, [], user_name="玩家")
    )
    assert tlp["user_name"] == "玩家"
    assert tlp["stats"]["total"] >= 1
    assert any(ev for year in tlp["year_groups"] for mg in year["month_groups"] for ev in mg["events"])

    tmk = timemachine.normalize_timemachine_data(
        rb.build_today(play_stats, games_meta, [], user_name="玩家", target_date="2023-07-22")
    )
    assert tmk["user_name"] == "玩家"
    assert tmk["target_date"] == "2023-07-22"

    ck = checkup.normalize_checkup_data(rb.build_checkup(play_stats, games_meta, classified, user_name="玩家"))
    assert ck["user_name"] == "玩家"
    assert ck["total_games"] == 2
    assert ck["total_hours"] == 2.5
    assert ck["top_genres"]

    print("  [8] 6 类 payload 均通过渲染层 normalize OK")


def test_demo_path_still_works():
    """--demo / 无参数路径不受桥接影响：渲染模块内置示例数据仍可生成 HTML。"""
    for module, generator, demo in (
        (annual_report, annual_report.generate_report, annual_report.demo_data),
        (graveyard, graveyard.generate_graveyard, graveyard.demo_data),
        (starmap, starmap.generate_starmap, starmap.demo_data),
        (archaeologist, archaeologist.generate_timeline, archaeologist.demo_data),
        (timemachine, timemachine.generate_timemachine, timemachine.demo_data),
        (checkup, checkup.generate_checkup, checkup.demo_data),
    ):
        data = demo()
        assert isinstance(data, dict) and data, module.__name__
        normalized = {
            annual_report: annual_report.normalize_data,
            graveyard: graveyard.normalize_graveyard_data,
            starmap: starmap.normalize_starmap_data,
            archaeologist: archaeologist.normalize_archaeologist_data,
            timemachine: timemachine.normalize_timemachine_data,
            checkup: checkup.normalize_checkup_data,
        }[module](data)
        assert isinstance(normalized, dict) and normalized, module.__name__
    print("  [9] --demo 示例数据路径不受影响 OK")


def test_metadata_cache_readonly():
    """缓存读取是只读的：临时库写入后能被 fill_metadata 读到，且不联网。"""
    play_stats = {480: {"playtime": 60, "last_played": 1690000000}}
    with temp_db():
        db = schema.get_connection()
        try:
            db.execute(
                "INSERT OR REPLACE INTO games (appid, name, genres, categories, developers, price, release_date, last_updated) VALUES (?,?,?,?,?,?,?,?)",
                (480, "Spacewar", '["Action"]', "[]", '["Valve"]', 0, "2007", "2026-09-23"),
            )
            db.execute(
                "INSERT OR REPLACE INTO games (appid, name, genres, categories, developers, price, release_date, last_updated) VALUES (?,?,?,?,?,?,?,?)",
                (570, "Dota 2", '["Strategy"]', "[]", '["Valve"]', 0, "2013", "2026-09-23"),
            )
            db.commit()
        finally:
            db.close()
        merged = rb.fill_metadata({}, play_stats)
        assert 480 in merged and merged[480]["name"] == "Spacewar"
        assert merged[480]["genres"] == ["Action"]
        assert 570 not in merged  # 不在库里的缓存 appid 被排除
        # 显式传入的元数据优先于缓存
        merged2 = rb.fill_metadata({480: {"name": "Override"}}, play_stats)
        assert merged2[480]["name"] == "Override"
        assert merged2[480]["genres"] == ["Action"]  # 缓存补齐缺失字段
    print("  [10] 元数据缓存只读兜底 / 不扩大库范围 OK")




# ---------------------------------------------------------------- 游戏封面

def _png_bytes(width: int = 460, height: int = 215) -> bytes:
    """最小合法 PNG（仅文件头，够魔数识别即可）。"""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\x00\x00\x00" * width
    idat = zlib.compress(raw)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def test_payload_carries_appid_for_every_page():
    """6 类 payload 都必须带上游戏标识（appid），否则页面无从配图。"""
    play_stats, games_meta = _sample_library()
    classified = {
        GHOST_A: {"status": "在玩", "status_name": "在玩"},
        GHOST_B: {"status": "未玩", "status_name": "未玩"},
    }
    lib = rb.build_library(play_stats, games_meta, classified)
    assert lib["games"][str(GHOST_A)]["appid"] == GHOST_A
    assert lib["games"][str(GHOST_B)]["appid"] == GHOST_B

    report = rb.build_report(play_stats, games_meta, classified)
    assert [g["appid"] for g in report["top_games"]] == [GHOST_A, GHOST_B]

    timeline = rb.build_timeline(play_stats, games_meta, [], user_name="x")
    assert timeline["play_stats"][str(GHOST_A)]["appid"] == GHOST_A
    assert timeline["games"][str(GHOST_B)]["appid"] == GHOST_B

    today = rb.build_today(play_stats, games_meta, [], user_name="x")
    assert today["games"][str(GHOST_A)]["appid"] == GHOST_A

    checkup = rb.build_checkup(play_stats, games_meta, classified)
    assert checkup["top_game"]["appid"] == GHOST_A

    import json
    for payload in (lib, report, timeline, today, checkup):
        json.dumps(payload)  # Path 等非 JSON 类型不得混进来
    print("  [11] 6 类 payload 均携带 appid 且可 JSON 序列化 OK")


def test_materialize_covers_copies_and_returns_relative_urls():
    """封面复制到产物目录并返回相对地址；缺失/坏文件静默跳过。"""
    with tempfile.TemporaryDirectory(prefix="gl_cov_") as tmp:
        tmp_path = Path(tmp)
        out_dir = tmp_path / "output"
        src_dir = tmp_path / "src"
        src_dir.mkdir(parents=True)
        good = src_dir / "111.jpg"
        good.write_bytes(_png_bytes())
        bad = src_dir / "222.jpg"  # 不存在

        urls = rb.materialize_covers({"111": str(good), "222": str(bad)}, out_dir)
        assert urls == {"111": "covers/111.png"}, urls  # 按魔数改成 .png
        assert (out_dir / "covers" / "111.png").is_file()

        # 再跑一次：已存在且大小一致 -> 复用，不报错
        urls2 = rb.materialize_covers({"111": str(good)}, out_dir)
        assert urls2 == {"111": "covers/111.png"}

        # 空输入 / 坏输入一律安全返回
        assert rb.materialize_covers({}, out_dir) == {}
        assert rb.materialize_covers(None, out_dir) == {}
    print("  [12] 封面复制 -> 相对地址 OK（缺文件安全跳过）")


def test_pages_render_img_when_cover_available_and_placeholder_when_missing():
    """有封面出 <img>；无封面出占位块；两者都不裂图、不抛异常。"""
    import re

    img_re = re.compile(r'<img\b[^>]*?\bsrc\s*=\s*"([^"]*)"', re.I)
    ph_re = re.compile(r"gl-cover-ph", re.I)

    play_stats = {
        900001: {"playtime": 600, "last_played": 1690000000},  # 2023-07-22，有封面
        900002: {"playtime": 120, "last_played": 1689827200},  # 2023-07-20，无封面
    }
    games_meta = {
        900001: {"name": "有封面游戏", "genres": ["Action"], "price": 1000},
        900002: {"name": "无封面游戏", "genres": ["Indie"], "price": 0},
    }
    classified = {
        900001: {"status": "搁置", "status_name": "搁置"},
        900002: {"status": "未玩", "status_name": "未玩"},
    }

    with tempfile.TemporaryDirectory(prefix="gl_pages_") as tmp:
        tmp_path = Path(tmp)
        cover_src = tmp_path / "src" / "900001.jpg"
        cover_src.parent.mkdir(parents=True)
        cover_src.write_bytes(_png_bytes())

        pages = (
            ("report", annual_report.generate_report, "annual_report_2026.html"),
            ("graveyard", graveyard.generate_graveyard, "graveyard.html"),
            ("starmap", starmap.generate_starmap, "starmap.html"),
            ("timeline", archaeologist.generate_timeline, "timeline.html"),
            ("today", timemachine.generate_timemachine, "timemachine.html"),
            ("checkup", checkup.generate_checkup, "checkup.html"),
        )
        for kind, generator, filename in pages:
            out = tmp_path / filename
            payload = rb.build_payload(
                kind, play_stats, games_meta, classified=classified, snapshots=[], user_name="测试"
            )
            if kind == "today":
                # 时光机看「一年前」：target=2024-07-22 -> 窗口 2023-07-15..07-29，
                # 固定它可避开「今天」漂移，保证两款游戏都落在匹配里
                payload["target_date"] = "2024-07-22"
            # 用打桩的本地封面替换真实查找（离线、可复现）
            original = rb.resolve_covers
            rb.resolve_covers = lambda appids, steam_path=None, _s=str(cover_src): {
                "900001": _s
            }
            try:
                generator(payload, output_path=out, open_browser=False)
            finally:
                rb.resolve_covers = original

            html = out.read_text(encoding="utf-8")
            srcs = img_re.findall(html)
            broken = [s for s in srcs if s and not (out.parent / s).is_file()]
            placeholders = ph_re.findall(html)
            assert srcs, f"{kind}: 应至少渲染一张封面 <img>"
            assert not broken, f"{kind}: 引用了不存在的图片 {broken}"
            assert placeholders, f"{kind}: 无封面游戏应降级为占位块"
            # appid 必须真的出现在产物里（此前 timeline 连 appid 都没有）
            assert "900001" in html or "900002" in html, kind
    print("  [13] 6 页均渲染 <img>（有图出图 / 无图占位），无裂图 OK")


def test_cover_pipeline_never_breaks_pages_without_artwork():
    """封面模块不可用时页面照样生成（只是没有图片）。"""
    import core as core_pkg

    play_stats, games_meta = _sample_library()
    saved_mod = sys.modules.get("core.artwork")
    saved_attr = getattr(core_pkg, "artwork", None)
    if saved_attr is not None:
        delattr(core_pkg, "artwork")
    sys.modules["core.artwork"] = None
    try:
        assert rb.resolve_covers([1245620]) == {}
    finally:
        if saved_mod is None:
            sys.modules.pop("core.artwork", None)
        else:
            sys.modules["core.artwork"] = saved_mod
        if saved_attr is not None:
            core_pkg.artwork = saved_attr

    with tempfile.TemporaryDirectory(prefix="gl_noart_") as tmp:
        payload = rb.build_payload("graveyard", play_stats, games_meta, classified=None, user_name="x")
        out = graveyard.generate_graveyard(payload, output_path=Path(tmp) / "g.html", open_browser=False)
        assert out.is_file()
    print("  [14] 封面模块缺失时页面仍可生成 OK")


def test_prepare_covers_meta_reports_orientation():
    """封面朝向按**实际图片尺寸**判定，供模板决定是否裁切。"""
    with tempfile.TemporaryDirectory(prefix="gl_meta_") as tmp:
        tmp_path = Path(tmp)
        out_dir = tmp_path / "output"
        src = tmp_path / "src"
        src.mkdir(parents=True)
        portrait = src / "1.jpg"      # 300x450 -> portrait
        landscape = src / "2.jpg"     # 460x215 -> landscape
        portrait.write_bytes(_png_bytes(300, 450))
        landscape.write_bytes(_png_bytes(460, 215))

        original = rb.resolve_covers
        rb.resolve_covers = lambda appids, steam_path=None, _p=str(portrait), _l=str(landscape): {
            "111": _p, "222": _l
        }
        try:
            meta = rb.prepare_covers_meta(["111", "222"], out_dir)
        finally:
            rb.resolve_covers = original

        assert meta["111"]["kind"] == "portrait", meta["111"]
        assert meta["222"]["kind"] == "landscape", meta["222"]
        for row in meta.values():
            assert (out_dir / row["url"]).is_file(), row
        # 空输入安全
        assert rb.prepare_covers_meta([], out_dir) == {}
    print("  [15] prepare_covers_meta 朝向判定正确 OK")


def test_landscape_cover_is_not_cropped_in_pages():
    """横版封面必须带 is-landscape（模板据此用 contain，不裁切）。"""
    import re

    play_stats = {900003: {"playtime": 600, "last_played": 1690000000}}
    games_meta = {900003: {"name": "横版封面游戏", "genres": ["Action"], "price": 1000}}
    classified = {900003: {"status": "搁置", "status_name": "搁置"}}

    with tempfile.TemporaryDirectory(prefix="gl_land_") as tmp:
        tmp_path = Path(tmp)
        src = tmp_path / "src" / "900003.jpg"
        src.parent.mkdir(parents=True)
        src.write_bytes(_png_bytes(460, 215))

        original = rb.resolve_covers
        rb.resolve_covers = lambda appids, steam_path=None, _s=str(src): {"900003": _s}
        try:
            for kind, generator, filename in (
                ("graveyard", graveyard.generate_graveyard, "graveyard.html"),
                ("timeline", archaeologist.generate_timeline, "timeline.html"),
                ("starmap", starmap.generate_starmap, "starmap.html"),
                ("checkup", checkup.generate_checkup, "checkup.html"),
                ("report", annual_report.generate_report, "annual_report_2026.html"),
            ):
                out = tmp_path / ("land_" + filename)
                payload = rb.build_payload(
                    kind, play_stats, games_meta, classified=classified, snapshots=[], user_name="测试"
                )
                if kind == "timeline":
                    payload["generated_at"] = "2026-09-23"
                generator(payload, output_path=out, open_browser=False)
                html = out.read_text(encoding="utf-8")
                hits = re.findall(r'<img[^>]*class="[^"]*is-landscape[^"]*"', html)
                assert hits, f"{kind}: 横版封面应带 is-landscape，避免被裁成细条"
        finally:
            rb.resolve_covers = original
    print("  [16] 横版封面在页面里不被裁切（is-landscape）OK")


# ------------------------------------------------- 中文名 / 中文类型（T1）

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _cn_library():
    """带官方中文列的迷你库：一款有中文、一款只有英文、一款都没有。"""
    play_stats = {
        730: {"playtime": 600, "last_played": 1690000000},
        220: {"playtime": 120, "last_played": 1690000000},
        999: {"playtime": 30, "last_played": 0},
    }
    games_meta = {
        730: {"name": "Counter-Strike 2", "name_cn": "反恐精英：全球攻势",
              "genres": ["Action", "Free to Play"], "genres_cn": ["动作", "免费开玩"],
              "price": 0},
        220: {"name": "Half-Life 2", "name_cn": "",
              "genres": ["Action"], "genres_cn": [], "price": 6800},
        999: {"name": "", "name_cn": "   "},          # 全是空白 -> 按「没有」处理
    }
    return play_stats, games_meta


def test_game_name_prefers_official_chinese_name():
    """game_name：官方中文名优先 -> 官方原名 -> 才回落 appid N；空白一律算没有。"""
    assert rb.game_name(730, {"name": "Counter-Strike 2", "name_cn": "反恐精英：全球攻势"}) == "反恐精英：全球攻势"
    # 中文列缺失 / 空白 / None -> 用官方原名，绝不渲染空白
    for blank in ("", "   ", None, "\t"):
        assert rb.game_name(220, {"name": "Half-Life 2", "name_cn": blank}) == "Half-Life 2", repr(blank)
    assert rb.game_name(220, {"name": "Half-Life 2"}) == "Half-Life 2"
    # 都没有才回落 appid N（保留历史行为）——用索引里查不到的合成 appid，
    # 否则会被本地中文名表/官方 appinfo 里真实游戏的中文名接住（见下一条用例）
    for meta in ({"name": "", "name_cn": ""}, {}, None, "坏数据"):
        assert rb.game_name(GHOST_A, meta) == f"appid {GHOST_A}", repr(meta)
    print("  [17] game_name 官方中文名优先 / 空白算缺失 / appid N 兜底 OK")


# 样本中文名：注入到 metadata_cn.name_cn_for，避免依赖开发机的 data/names_cn.json。
# 这些 appid -> 中文名来自 Steam 官方本地化 / 社区通行译名，仅作夹具用。
_SAMPLE_CN = {
    410110: "12比6好",
    2321470: "深岩银河：幸存者",
    1086940: "博德之门3",
    594650: "猎杀：对决 1896",
    2344520: "《暗黑破坏神® IV》",
}


@contextlib.contextmanager
def sample_chinese_names():
    """把 metadata_cn.name_cn_for 打桩成上面这张样本表（离线、可复现）。"""
    from core import metadata_cn

    original = metadata_cn.name_cn_for
    metadata_cn.name_cn_for = lambda appid, *a, **k: _SAMPLE_CN.get(rb._to_int(appid))
    try:
        yield
    finally:
        metadata_cn.name_cn_for = original


def test_game_name_falls_back_to_authoritative_chinese_tables():
    """meta 缺中文列时，继续查两张本地权威中文表（names_cn.json → 本机 appinfo）。

    用户反馈「六个可视化界面里的游戏名还是全英文」的根因：六页只读 cache.db 的
    ``name_cn`` 列，漏了这两级来源，而面板列表两边都查 —— 于是同一个库
    面板显示中文、六页显示英文甚至 ``appid N``。
    """
    with sample_chinese_names():
        # 1) 只有英文名，但中文名表里有 -> 显示中文
        assert rb.game_name(410110, {"name": "12 is Better Than 6"}) == "12比6好"
        # 2) 中文名表与 appinfo 都有（官方本地化名）-> 显示官方中文名
        assert rb.game_name(2321470, {"name": "Deep Rock Galactic: Survivor"}) == "深岩银河：幸存者"
        assert rb.game_name(1086940, {"name": "Baldur's Gate 3"}) == "博德之门3"
        # 3) meta 一个字都没有（完全缺名）也应拿到中文名，而不是 appid N
        assert rb.game_name(594650, {}) == "猎杀：对决 1896"
        assert rb.game_name(2344520) == "《暗黑破坏神® IV》"
        # 4) 本地库缓存的 name_cn 仍是最高优先级（数据层权威列）
        assert rb.game_name(2321470, {"name_cn": "本地列的中文"}) == "本地列的中文"
    # 5) 绝不臆造：两张表都查不到 -> 保持官方原名，连原名都没有才 appid N
    assert rb.game_name(GHOST_A, {"name": "Some English Name"}) == "Some English Name"
    assert rb.game_name(GHOST_A, {}) == f"appid {GHOST_A}"
    # 6) 非法 appid 不炸
    assert rb.game_name(None, {"name": "X"}) == "X"
    assert rb.game_name("abc", {"name": "X"}) == "X"
    print("  [17b] game_name 回落权威中文表（names_cn → appinfo）/ 不臆造 OK")


def test_game_genre_prefers_official_chinese_genres():
    """game_genre / genre_hours：官方中文类型优先 -> 英文 -> 未知。"""
    assert rb.game_genre({"genres": ["Action"], "genres_cn": ["动作"]}) == "动作"
    assert rb._localized_genres({"genres": ["Action"], "genres_cn": ["动作", "免费开玩"]}) == ["动作", "免费开玩"]
    # 中文列缺失/空白 -> 回落英文
    assert rb.game_genre({"genres": ["Action"], "genres_cn": []}) == "Action"
    assert rb.game_genre({"genres": ["Action"], "genres_cn": ["", "  "]}) == "Action"
    assert rb.game_genre({"genres": ["Action"]}) == "Action"
    # 都没有 -> 未知
    assert rb.game_genre({}) == rb.UNKNOWN_GENRE
    assert rb.game_genre(None) == rb.UNKNOWN_GENRE
    # 中英同名（VRChat 这类官方中文名就是原文）不硬改成中文列表
    assert rb._localized_genres({"genres": ["Action"], "genres_cn": ["Action"]}) == ["Action"]
    # 判定用英文列表不受影响（免费标记仍然认 "Free to Play"）
    assert rb._as_genre_list({"genres": ["Free to Play"], "genres_cn": ["免费开玩"]}) == ["Free to Play"]

    play_stats, games_meta = _cn_library()
    hours = rb.genre_hours(play_stats, games_meta)
    assert hours["动作"] == 10.0 and hours["免费开玩"] == 10.0, hours   # 730 有官方中文类型
    assert hours["Action"] == 2.0, hours        # 220 没有官方中文类型 -> 保留英文
    assert hours[rb.UNKNOWN_GENRE] == 0.5, hours  # 999 没有类型 -> 未知
    print("  [18] game_genre / genre_hours 官方中文类型优先 OK")


def test_cached_metadata_exposes_official_chinese_columns():
    """load_cached_metadata / fill_metadata 透出 name_cn / genres_cn，且不扩大库范围。"""
    play_stats = {480: {"playtime": 60, "last_played": 1690000000}}
    with temp_db():
        db = schema.get_connection()
        try:
            db.execute(
                "INSERT OR REPLACE INTO games (appid, name, name_cn, genres, genres_cn, categories,"
                " developers, price, release_date, last_updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (480, "Spacewar", "太空大战", '["Action"]', '["动作"]', "[]", '["Valve"]', 0, "2007", "2026-09-24"),
            )
            db.execute(
                "INSERT OR REPLACE INTO games (appid, name, name_cn, genres, genres_cn, categories,"
                " developers, price, release_date, last_updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (570, "Dota 2", "Dota 2 刀塔", '["Strategy"]', '["策略"]', "[]", '["Valve"]', 0, "2013", "2026-09-24"),
            )
            db.commit()
        finally:
            db.close()

        cached = rb.load_cached_metadata([480])
        assert cached[480]["name_cn"] == "太空大战", cached[480]
        assert cached[480]["genres_cn"] == ["动作"], cached[480]
        assert cached[480]["name"] == "Spacewar"          # 英文列保持原样
        assert cached[480]["genres"] == ["Action"]
        assert 570 not in cached                          # 限定 appid 生效

        merged = rb.fill_metadata({}, play_stats)
        assert set(merged) == {480}, merged               # 库范围不因缓存扩大
        assert merged[480]["name_cn"] == "太空大战"
        assert rb.game_name(480, merged[480]) == "太空大战"
        assert rb.game_genre(merged[480]) == "动作"

        # 网络侧若带来脏中文列，不得改写本地官方列
        dirty = rb.fill_metadata({480: {"name_cn": "编造的名字", "genres_cn": ["伪类型"]}}, play_stats)
        assert dirty[480]["name_cn"] == "太空大战", dirty[480]
        assert rb.game_genre(dirty[480]) == "动作"
        # 本地已有官方中文名 -> 外部传入不得覆盖（幂等：已有中文名不覆盖）
        idem = rb.fill_metadata({570: {"name_cn": "刀塔 2"}}, {570: {"playtime": 1}})
        assert idem[570]["name_cn"] == "Dota 2 刀塔", idem[570]
        # 本地确实没有中文列时，才接受外部提供的（仍限定库内 appid）
        synced = rb.fill_metadata({999: {"name_cn": "外部补的中文"}}, {999: {"playtime": 1}})
        assert synced[999]["name_cn"] == "外部补的中文"
        assert set(synced) == {999}
    print("  [19] 缓存中文列只读透出 / 不扩大库范围 / 不被网络脏值覆盖 OK")


def test_common_style_helpers_degrade_safely():
    """_as_genre_list 兼容 list / JSON / 逗号串，且去空去重。"""
    assert rb._as_genre_list({"genres": '["Action", "RPG"]'}) == ["Action", "RPG"]
    assert rb._as_genre_list({"genres": "Action, RPG"}) == ["Action", "RPG"]
    assert rb._as_genre_list({"genres": ["Action", "Action", ""]}) == ["Action"]
    assert rb._as_genre_list({"genres": None}) == []
    assert rb._as_genre_list(None) == []
    assert rb._as_str_list('["动作"]') == ["动作"]
    assert rb._as_str_list("") == []
    print("  [20] 类型列表解析兼容 list/JSON/逗号串并去重 OK")


def test_payloads_use_official_chinese_names():
    """六类 payload 里的展示名/类型都优先中文，且不出现空白名。"""
    play_stats, games_meta = _cn_library()
    classified = {
        730: {"status": "在玩", "status_name": "在玩"},
        220: {"status": "搁置", "status_name": "搁置"},
        999: {"status": "未玩", "status_name": "未玩"},
    }

    lib = rb.build_library(play_stats, games_meta, classified, user_name="玩家")
    games = lib["games"]
    assert games["730"]["name"] == "反恐精英：全球攻势", games["730"]
    assert games["730"]["genre"] == "动作"
    assert games["220"]["name"] == "Half-Life 2"       # 无中文 -> 英文，不是空白
    assert games["999"]["name"] == "appid 999"         # 全空白 -> appid N
    assert all(str(g["name"]).strip() for g in games.values())

    report = rb.build_report(play_stats, games_meta, classified, user_name="玩家")
    assert report["top_games"][0]["name"] == "反恐精英：全球攻势"
    assert report["top_games"][0]["genre"] == "动作"
    # 730 有官方中文类型 -> 中文；220 只有英文 -> 保留英文
    # （report 的类型榜只取前 3，0.5h 的「未知」排在榜外，另用 genre_hours 断言）
    assert {row["name"] for row in report["genres"]} == {"动作", "免费开玩", "Action"}
    assert rb.genre_hours(play_stats, games_meta)[rb.UNKNOWN_GENRE] == 0.5
    # 人格摘要里的类型名也被本地官方中文替换
    assert "Action" not in report["persona"]["summary"], report["persona"]["summary"]

    timeline = rb.build_timeline(play_stats, games_meta, [], user_name="玩家")
    assert timeline["play_stats"]["730"]["name"] == "反恐精英：全球攻势"
    assert timeline["games"]["730"]["name"] == "反恐精英：全球攻势"

    today = rb.build_today(play_stats, games_meta, [], user_name="玩家")
    assert today["games"]["730"]["name"] == "反恐精英：全球攻势"

    checkup = rb.build_checkup(play_stats, games_meta, classified, user_name="玩家")
    assert checkup["top_game"]["name"] == "反恐精英：全球攻势"
    top_genre_names = [row["name"] for row in checkup["stats"]["top_genres"]]
    assert top_genre_names[0] in ("动作", "免费开玩"), top_genre_names
    assert set(top_genre_names) == {"动作", "免费开玩", "Action", rb.UNKNOWN_GENRE}, top_genre_names
    print("  [21] 六类 payload 展示名/类型中文优先、无空白名 OK")


def test_persona_summary_genre_localization_is_evidence_based():
    """摘要里的英文类型只在本地库有官方中文对照时替换，否则原样保留。"""
    metas = {1: {"genres": ["Action", "RPG"], "genres_cn": ["动作", "角色扮演"]}}
    text = "最常玩 Action，整体更接近「死忠粉型」。"
    assert rb._localize_genre_text(text, metas) == "最常玩 动作，整体更接近「死忠粉型」。"
    # 没有中文对照 -> 原样保留（绝不机翻）
    assert rb._localize_genre_text(text, {1: {"genres": ["Action"]}}) == text
    # 中英数量对不上 -> 整条跳过，不张冠李戴
    assert rb._localize_genre_text(text, {1: {"genres": ["Action", "RPG"], "genres_cn": ["动作"]}}) == text
    assert rb._localize_genre_text("", metas) == ""
    assert rb._localize_genre_text(None, metas) == ""
    print("  [22] 摘要类型名按本地官方中英对照替换 / 无对照不机翻 OK")


def test_all_pages_render_chinese_names_without_blank():
    """真跑六页渲染：有官方中文名的游戏在页面里显示中文名，且不出现空白名。"""
    from datetime import datetime, timezone

    play_stats, games_meta = _cn_library()
    classified = {a: {"status": "搁置", "status_name": "搁置"} for a in play_stats}
    # 时间轴 / 时光机只收录有 last_played 的游戏；给三款都放进「去年今日」窗口
    then = int(datetime(2022, 7, 24, 12, tzinfo=timezone.utc).timestamp())
    for appid in play_stats:
        play_stats[appid] = dict(play_stats[appid], last_played=then)

    with tempfile.TemporaryDirectory(prefix="gl_cn_") as tmp:
        out_dir = Path(tmp)
        builders = {
            "report": lambda: rb.build_report(play_stats, games_meta, classified, user_name="玩家"),
            "graveyard": lambda: rb.build_library(play_stats, games_meta, classified, user_name="玩家"),
            "starmap": lambda: rb.build_library(play_stats, games_meta, classified, user_name="玩家"),
            "timeline": lambda: rb.build_timeline(play_stats, games_meta, [], user_name="玩家"),
            "today": lambda: rb.build_today(play_stats, games_meta, [], user_name="玩家",
                                            target_date="2023-07-24"),
            "checkup": lambda: rb.build_checkup(play_stats, games_meta, classified, user_name="玩家"),
        }
        generators = {
            "report": annual_report.generate_report,
            "graveyard": graveyard.generate_graveyard,
            "starmap": starmap.generate_starmap,
            "timeline": archaeologist.generate_timeline,
            "today": timemachine.generate_timemachine,
            "checkup": checkup.generate_checkup,
        }
        for kind, generator in generators.items():
            out = out_dir / (kind + ".html")
            generator(builders[kind](), output_path=out, open_browser=False)
            html = out.read_text(encoding="utf-8")
            assert "反恐精英：全球攻势" in html, kind + ": 页面没有中文游戏名"
            # 只查模板 Jinja 直出的名字容器（starmap 的 tt-name 由 JS 运行时填充，排除）
            for container in ("game-name", "tomb-name", "unburied-name", "sc-name", "chk-forgot-name"):
                blanks = re.findall(r'class="%s">\s*</' % container, html)
                assert not blanks, kind + ": 出现空白名字段 %r" % (blanks,)

    # 缺名游戏必须回落 "appid N"，绝不渲染空白
    fallback = rb.build_library(play_stats, games_meta, classified, user_name="玩家")
    assert fallback["games"]["999"]["name"] == "appid 999", fallback["games"]["999"]
    print("  [23] 六页渲染：中文名出现、缺名回落 appid N、无空白名 OK")


def main() -> None:
    tests = [
        test_minutes_to_hours,
        test_library_scope_excludes_meta_only_appids,
        test_missing_genre_and_name_fallback,
        test_empty_library_never_crashes,
        test_dirty_timestamps_are_dropped,
        test_snapshots_filtering_and_payload_shape,
        test_checkup_ratios_are_fractions,
        test_payloads_survive_render_normalizers,
        test_demo_path_still_works,
        test_metadata_cache_readonly,
        test_payload_carries_appid_for_every_page,
        test_materialize_covers_copies_and_returns_relative_urls,
        test_pages_render_img_when_cover_available_and_placeholder_when_missing,
        test_cover_pipeline_never_breaks_pages_without_artwork,
        test_prepare_covers_meta_reports_orientation,
        test_landscape_cover_is_not_cropped_in_pages,
        test_game_name_prefers_official_chinese_name,
        test_game_name_falls_back_to_authoritative_chinese_tables,
        test_game_genre_prefers_official_chinese_genres,
        test_cached_metadata_exposes_official_chinese_columns,
        test_common_style_helpers_degrade_safely,
        test_payloads_use_official_chinese_names,
        test_persona_summary_genre_localization_is_evidence_based,
        test_all_pages_render_chinese_names_without_blank,
    ]
    for t in tests:
        t()
    print(f"\n全部 {len(tests)} 项验证通过")


if __name__ == "__main__":
    main()