"""状态引擎 -> 分析引擎链路最小集成测试。

链路：classifier -> stats -> recommender -> persona（全部用假数据）。
运行方式（在 game-life/ 目录下）：
    python tests/test_engine_integration.py
"""

import sys
from datetime import date, datetime
from pathlib import Path

# 让 game-life/ 可被 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import classifier, persona, recommender, stats  # noqa: E402

TODAY = date(2026, 9, 19)
TODAY_TS = int(datetime(TODAY.year, TODAY.month, TODAY.day).timestamp())
DAY = 86400


def _ts(days_ago: int) -> int:
    """基准日往前推 days_ago 天的 unix 时间戳。"""
    return TODAY_TS - days_ago * DAY


def _build_fake_data():
    """构造 5 款游戏的假数据，覆盖 5 种状态。"""
    play_stats = {
        1: {"playtime": 500, "last_played": _ts(10)},    # 在玩
        2: {"playtime": 0, "last_played": 0},            # 未玩
        3: {"playtime": 5000, "last_played": _ts(3)},    # 沉迷
        4: {"playtime": 2500, "last_played": _ts(120)},  # 通关（2500 >= 3000*0.8）
        5: {"playtime": 20, "last_played": _ts(200)},    # 搁置
    }
    games = {
        # classify_games 从 games 条目读取 playtime / last_played
        1: {"name": "游戏A", "genres": ["Action", "RPG"], "playtime": 500,
            "last_played": _ts(10)},
        2: {"name": "游戏B", "genres": ["Indie"], "playtime": 0, "last_played": 0},
        3: {"name": "游戏C", "genres": ["Action"], "playtime": 5000,
            "last_played": _ts(3)},
        4: {"name": "游戏D", "genres": ["RPG"], "playtime": 2500, "last_played": _ts(120),
            "hltb_main": 3000},
        5: {"name": "游戏E", "genres": ["Indie", "RPG"], "playtime": 20,
            "last_played": _ts(200),
            "categories": ["Story Rich"]},
    }
    hltb_data = {4: {"main": 3000}}
    return play_stats, games, hltb_data


def test_classifier_to_statuses():
    """classify_games 输出字段齐全，to_statuses 映射为分析引擎约定的 dict。"""
    _, games, hltb_data = _build_fake_data()
    classified = classifier.classify_games(games, hltb_data=hltb_data, today=TODAY)
    expected_fields = {"status", "status_name", "genre_categories", "tag_categories"}
    assert expected_fields <= set(classified[1])
    # 5 种状态各命中一款
    statuses = classifier.to_statuses(classified)
    assert statuses == {1: "在玩", 2: "未玩", 3: "沉迷", 4: "通关", 5: "搁置"}
    # 展示名映射：通关 -> 已通关
    assert classifier.to_statuses(classified, key="status_name")[4] == "已通关"
    print("PASS classifier -> statuses 映射")


def test_full_chain():
    """classifier -> stats -> recommender -> persona 全链路可跑通。"""
    play_stats, games, hltb_data = _build_fake_data()
    classified = classifier.classify_games(games, hltb_data=hltb_data, today=TODAY)
    statuses = classifier.to_statuses(classified)

    # 1) stats：状态分布落桶正确；已移除恒为 0 的 completion_rate，改看可计算的已玩占比
    stat = stats.compute_stats(play_stats, games, statuses=statuses, today=TODAY_TS)
    assert stat["total_games"] == 5
    assert stat["status_counts"]["在玩"] == 1
    assert stat["status_counts"]["沉迷"] == 1
    assert stat["status_counts"]["通关"] == 1
    assert "completion_rate" not in stat          # 假指标已彻底移除
    assert stat["played_ratio"] == 80.0           # 5 款里 4 款玩过

    # 2) recommender：候选池、输出结构与状态透传
    rec = recommender.recommend(
        play_stats, games, mood="随便", time_left="不限",
        statuses=statuses, today=TODAY_TS, top_k=5,
    )
    assert rec["candidate_count"] == 5
    assert len(rec["recommendations"]) == 5
    assert {"mood", "time_left", "genre", "candidate_count", "recommendations"} <= set(rec)
    by_appid = {item["appid"]: item for item in rec["recommendations"]}
    assert by_appid[1]["status"] == "在玩"
    assert by_appid[2]["status"] == "未玩"
    for item in rec["recommendations"]:
        assert {"appid", "name", "status", "score", "reason"} <= set(item)
        assert isinstance(item["reason"], str) and item["reason"]

    # 3) persona：复用 stats 结果，输出五维画像
    result = persona.compute_persona(
        play_stats, games, stats_result=stat, statuses=statuses,
        library_age_years=2.0, today=TODAY_TS,
    )
    assert set(result["dimensions"]) == {
        "purchase", "execution", "focus", "breadth", "courage"
    }
    for value in result["dimensions"].values():
        assert 0.0 <= value <= 100.0
    assert isinstance(result["labels"], list) and result["labels"]
    assert isinstance(result["primary_label"], str) and result["primary_label"]
    assert isinstance(result["summary"], str) and result["summary"]
    print("PASS classifier -> stats -> recommender -> persona 全链路")



def test_classify_output_contract_with_real_hltb():
    """验证 classifier 输出字段与 stats/recommender/persona 约定一致。"""
    from data.schema import init_db, get_connection
    init_db()
    db = get_connection()
    try:
        rows = db.execute("SELECT appid, name, main FROM hltb WHERE main IS NOT NULL AND main > 0 LIMIT 3").fetchall()
    finally:
        db.close()
    games = {}
    hltb_data = {}
    play_stats = {}
    names = {}
    for r in rows:
        appid = r["appid"]
        main = r["main"]
        name = r["name"]
        hltb_data[appid] = {"main": main}
        games[appid] = {"name": name, "genres": ["Action"], "playtime": int(main*0.8), "last_played": _ts(0)}
        play_stats[appid] = {"playtime": int(main*0.8), "last_played": _ts(0)}
        names[appid] = name
    classified = classifier.classify_games(games, hltb_data=hltb_data, today=TODAY)
    # 字段齐全
    expected_fields = {"status", "status_name", "genre_categories", "tag_categories"}
    for appid, info in classified.items():
        assert expected_fields <= set(info), (appid, info.keys())
    # 状态值合法
    valid_statuses = {classifier.STATUS_NOT_PLAYED, classifier.STATUS_PLAYING,
                      classifier.STATUS_BACKLOG, classifier.STATUS_ADDICTED, classifier.STATUS_COMPLETED}
    assert all(info["status"] in valid_statuses for info in classified.values())
    # 通关
    statuses = classifier.to_statuses(classified)
    assert all(statuses[a] == classifier.STATUS_COMPLETED for a in classified)
    # 展示名
    assert all(info["status_name"] == "已通关" for info in classified.values())
    # stats 读取 statuses 按键匹配
    stat = stats.compute_stats(play_stats, games, statuses=statuses, today=TODAY_TS)
    assert stat["status_counts"].get("通关", 0) == len(classified)
    # recommender 透传 status（需 play_stats 含 appid 才能解析 statuses）
    rec = recommender.recommend(play_stats, games, statuses=statuses, today=TODAY_TS, top_k=len(classified))
    assert all(item["status"] == classifier.STATUS_COMPLETED for item in rec["recommendations"])
    # persona 可用 statuses
    persona_out = persona.compute_persona({}, {}, stats_result=stat, statuses=statuses, today=TODAY_TS)
    assert "dimensions" in persona_out and "labels" in persona_out
    print("PASS 输出字段与下游约定一致")


def test_classifier_boundary_integration():
    """边界/异常场景在集成层下的表现。"""
    classified = classifier.classify_games({
        1: {"name": "F", "genres": [], "playtime": 100, "last_played": _ts(40)},
    }, today=TODAY)
    assert classified[1]["status"] == classifier.STATUS_BACKLOG
    # future ts
    future = TODAY_TS + 86400 * 5
    classified2 = classifier.classify_games({
        1: {"name": "F2", "genres": [], "playtime": 100, "last_played": future},
    }, today=TODAY)
    assert classified2[1]["status"] == classifier.STATUS_PLAYING
    # huge
    classified3 = classifier.classify_games({
        1: {"name": "Huge", "genres": [], "playtime": 600001},
    }, today=TODAY)
    assert classified3[1]["status"] == classifier.STATUS_ADDICTED
    print("PASS 边界集成")



def test_stats_library_scope_ignores_meta_only_appids():
    """库范围以 play_stats 为准：元数据缓存里多出的 appid 不得虚高总数。"""
    play_stats = {1: {"playtime": 600, "last_played": _ts(5)}}
    games = {
        1: {"name": "A", "genres": ["Action"]},
        570: {"name": "Dota 2", "genres": ["Strategy"]},   # 不在库里（缓存残留）
        99999: {"name": "Ghost", "genres": ["Indie"]},     # 不在库里
    }
    stat = stats.compute_stats(play_stats, games, today=TODAY_TS)
    assert stat["total_games"] == 1, stat["total_games"]
    assert stat["avg_playtime_hours"] == 10.0
    types = {row["name"] for row in stat["top_genres"]}
    assert "Strategy" not in types and "Indie" not in types, types
    assert stats.library_scope(play_stats, games) == [1]
    # play_stats 为空时才退回 games（兼容「只有元数据、无时长记录」场景）
    assert stats.library_scope({}, games) == [1, 570, 99999]
    # play_stats 存在时必须完全忽略 games 的多余键
    assert stats.library_scope({9: {"playtime": 1}}, games) == [9]
    print("PASS 库范围以 play_stats 为准（元数据缓存不虚高总数）")


def main():
    for test in (test_classifier_to_statuses, test_full_chain,
                  test_classify_output_contract_with_real_hltb,
                  test_classifier_boundary_integration,
                  test_stats_library_scope_ignores_meta_only_appids):
        test()
    print("\n全部 4 组集成验证通过")


if __name__ == "__main__":
    main()
