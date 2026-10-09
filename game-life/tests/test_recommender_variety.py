"""推荐引擎回归：结果随条件变化 / 剔除非游戏条目 / 统计移除通过率。

运行方式（在 game-life/ 目录下）：
    python tests/test_recommender_variety.py
零依赖，纯标准库。
"""

import sys
import time
from pathlib import Path

# 让 game-life/ 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import recommender, stats  # noqa: E402

NOW = int(time.time())
DAY = 86400

MOODS = ("随便", "轻松", "挑战", "沉浸")
TIMES = ("不限", "30m", "2h")
NON_GAME_APPIDS = {7, 760, 241100}


def _fake_library():
    """构造 12 款风格各异的游戏，覆盖「短会话/长流程/高难度/陪玩」等口味。"""
    games = {
        900001: {"name": "休闲消除", "genres": ["Casual", "Puzzle"], "playtime": 60,
                 "last_played": NOW - 2 * DAY, "session_minutes": 20},
        900002: {"name": "独立小品", "genres": ["Indie", "Casual"], "playtime": 200,
                 "last_played": NOW - 5 * DAY, "session_minutes": 35},
        900003: {"name": "硬核动作", "genres": ["Action", "Hardcore"], "playtime": 4000,
                 "last_played": NOW - 3 * DAY, "session_minutes": 120},
        900004: {"name": "魂类受苦", "genres": ["Action", "Souls-like"], "playtime": 6000,
                 "last_played": NOW - DAY, "session_minutes": 150},
        900005: {"name": "剧情巨制", "genres": ["RPG", "Story Rich"], "playtime": 3000,
                 "last_played": NOW - 6 * DAY, "session_minutes": 180},
        900006: {"name": "大世界漫游", "genres": ["RPG", "Open World"], "playtime": 2500,
                 "last_played": NOW - 9 * DAY, "session_minutes": 160},
        # 真实库里混进来的非游戏条目（黑名单 appid）
        7: {"name": "Steam Client", "genres": [], "playtime": 0, "last_played": 0},
        760: {"name": "Steam Screenshots", "genres": [], "playtime": 0, "last_played": 0},
        241100: {"name": "Steam Input Configs", "genres": [], "playtime": 0, "last_played": 0},
        900010: {"name": "音游抽卡", "genres": ["Casual", "Free to Play"], "playtime": 400,
                 "last_played": NOW - 4 * DAY, "session_minutes": 25},
        900011: {"name": "像素经营", "genres": ["Indie", "Simulation"], "playtime": 500,
                 "last_played": NOW - 700 * DAY, "session_minutes": 45},
        900012: {"name": "策略长局", "genres": ["Strategy"], "playtime": 1200,
                 "last_played": NOW - 20 * DAY, "session_minutes": 200},
    }
    play_stats = {
        appid: {"playtime": meta["playtime"], "last_played": meta["last_played"]}
        for appid, meta in games.items()
    }
    return play_stats, games


def _rec(play_stats, games, **kwargs):
    """统一入口：显式注入空 type_index，脱离本机 appinfo 数据，结果可复现。"""
    kwargs.setdefault("statuses", {})
    kwargs.setdefault("today", NOW)
    kwargs.setdefault("type_index", {})
    return recommender.recommend(play_stats, games, **kwargs)


def test_mood_and_time_change_results():
    """12 组 mood x time 组合至少产出 6 组互不相同的 top5（原本只有 1 组）。"""
    play_stats, games = _fake_library()
    seen: dict[tuple, list[tuple[str, str]]] = {}
    for mood in MOODS:
        for time_left in TIMES:
            result = _rec(play_stats, games, mood=mood, time_left=time_left, top_k=5)
            names = tuple(item["name"] for item in result["recommendations"])
            assert len(names) == 5, (mood, time_left, names)
            seen.setdefault(names, []).append((mood, time_left))
    assert len(seen) >= 6, f"只有 {len(seen)} 组不同 top5：{list(seen)}"
    print(f"  [1] 12 组组合产出 {len(seen)} 组不同 top5 OK")


def test_mood_preferences_are_consistent():
    """轻松偏短会话休闲、挑战偏高难动作、沉浸偏剧情长流程。"""
    play_stats, games = _fake_library()

    relax = _rec(play_stats, games, mood="轻松", time_left="30m", top_k=3)
    names = [item["name"] for item in relax["recommendations"]]
    assert "硬核动作" not in names and "魂类受苦" not in names, names

    hard = _rec(play_stats, games, mood="挑战", time_left="2h", top_k=3)
    hard_names = [item["name"] for item in hard["recommendations"]]
    assert {"硬核动作", "魂类受苦"} & set(hard_names), hard_names

    deep = _rec(play_stats, games, mood="沉浸", time_left="2h", top_k=3)
    deep_names = [item["name"] for item in deep["recommendations"]]
    assert {"剧情巨制", "大世界漫游"} & set(deep_names), deep_names
    print("  [2] 轻松/挑战/沉浸 的口味偏好生效 OK")


def test_time_fit_penalises_overlong_sessions():
    """时间维度真实参与打分：短局加分、长局在短时段下被降权。"""
    play_stats, games = _fake_library()

    def scores(time_left: str) -> dict[str, float]:
        result = _rec(play_stats, games, mood="随便", time_left=time_left, top_k=12)
        return {item["name"]: item["score"] for item in result["recommendations"]}

    free_scores = scores("不限")
    short_scores = scores("30m")
    for name in ("剧情巨制", "策略长局", "魂类受苦"):
        assert short_scores[name] < free_scores[name], (name, short_scores[name], free_scores[name])
    # 短会话的休闲游戏则因「时长刚好」拿到加分
    assert short_scores["休闲消除"] > free_scores["休闲消除"]
    print("  [3] 时间维度对长局降权、对短局加分 OK")


def test_non_game_entries_excluded():
    """Steam 客户端 / 截图 / 输入配置等非游戏条目不进推荐，也不进候选池。"""
    play_stats, games = _fake_library()
    for mood in MOODS:
        result = _rec(play_stats, games, mood=mood, top_k=len(games))
        appids = {item["appid"] for item in result["recommendations"]}
        assert not (appids & NON_GAME_APPIDS), (mood, appids & NON_GAME_APPIDS)
    # 12 款里剔掉 3 款非游戏 -> 候选 9 款
    result = _rec(play_stats, games, top_k=len(games))
    assert result["candidate_count"] == 9, result["candidate_count"]

    # type 字段非 game 的条目同样剔除
    typed = {1: {"name": "合法游戏", "genres": ["Action"], "type": "game"},
             2: {"name": "某个 DLC", "genres": ["Action"], "type": "dlc"},
             3: {"name": "某个工具", "genres": ["Action"], "type": "tool"}}
    stats_in = {appid: {"playtime": 10, "last_played": 0} for appid in typed}
    typed_hits = _rec(stats_in, typed, top_k=9)
    assert [item["appid"] for item in typed_hits["recommendations"]] == [1]

    # 条目自身无 type 字段时，本机 appinfo 类型索引同样把关
    plain = {1: {"name": "合法游戏", "genres": ["Action"]},
             4: {"name": "某个工具", "genres": ["Action"]}}
    plain_stats = {appid: {"playtime": 10, "last_played": 0} for appid in plain}
    assert _rec(plain_stats, plain, top_k=9)["candidate_count"] == 2       # 类型未知 → 放行
    indexed = _rec(plain_stats, plain, top_k=9, type_index={1: "Game", 4: "Tool"})
    assert [item["appid"] for item in indexed["recommendations"]] == [1]
    print("  [4] 非游戏条目剔除 OK")


def test_reason_matches_hit_keys():
    """推荐理由必须与实际命中的加分项一致，不能"说一套做一套"。"""
    play_stats, games = _fake_library()
    result = _rec(play_stats, games, mood="轻松", time_left="30m", genre="Casual",
                  top_k=12)
    for item in result["recommendations"]:
        assert isinstance(item["reason"], str) and item["reason"]
    # 指定类型时，命中的游戏理由里必须真带上类型名
    matched = [item for item in result["recommendations"] if "Casual" in item["reason"]]
    assert matched, [item["reason"] for item in result["recommendations"]]
    # 「时长刚好合适」只应出现在单局确实放得下的游戏上
    for item in result["recommendations"]:
        if "时长刚好合适" in item["reason"]:
            assert item["score"] > 0
    print("  [5] 理由与命中项一致 OK")


def test_results_are_deterministic_within_a_day():
    """同一天同一条件结果稳定（扰动由确定性种子生成，不是随机数）。"""
    play_stats, games = _fake_library()
    first = _rec(play_stats, games, mood="挑战", time_left="2h", top_k=5)
    second = _rec(play_stats, games, mood="挑战", time_left="2h", top_k=5)
    assert [i["appid"] for i in first["recommendations"]] == [
        i["appid"] for i in second["recommendations"]
    ]
    print("  [6] 同日同条件结果确定 OK")


def test_stats_drops_completion_rate():
    """统计输出不再包含恒为 0 的 completion_rate，改用 played_ratio。"""
    play_stats, games = _fake_library()
    stat = stats.compute_stats(play_stats, games, statuses={}, today=NOW)
    assert "completion_rate" not in stat
    assert "played_ratio" in stat and stat["played_ratio"] > 0
    assert "played_count" in stat and "unplayed_count" in stat
    print("  [7] 统计移除通过率、保留已玩占比 OK")


def main():
    tests = [
        test_mood_and_time_change_results,
        test_mood_preferences_are_consistent,
        test_time_fit_penalises_overlong_sessions,
        test_non_game_entries_excluded,
        test_reason_matches_hit_keys,
        test_results_are_deterministic_within_a_day,
        test_stats_drops_completion_rate,
    ]
    for test in tests:
        test()
    print(f"\n全部 {len(tests)} 组推荐/统计回归验证通过")


if __name__ == "__main__":
    main()