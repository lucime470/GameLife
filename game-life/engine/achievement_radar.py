"""成就冲刺雷达（分析引擎层 · 模块 6：achievement_radar）。

依赖 core/achievements.py（已实现，只 import 不改）。
按已解锁/总数、剩余成就数排序，找出距全成就最近的游戏。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import display
from .stats import _to_int

RULES_PATH = Path(__file__).resolve().parent.parent / "rules.json"

# 完成度分桶阈值（设计文档 8.2）
_BUCKET_A = 90  # >= 90%
_BUCKET_B = 70  # >= 70%


def load_rules(path: str | Path | None = None) -> dict[str, Any]:
    """加载 rules.json；缺失/损坏时返回空 dict（不抛异常）。"""
    config_path = Path(path) if path else RULES_PATH
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def compute_achievement_radar(
    achievement_data: dict,
    games: dict | None = None,
    playtime_minutes: dict[int, int] | None = None,
) -> dict[str, Any]:
    """成就冲刺候选榜。

    参数：
        achievement_data: dict[appid, {achieved, total, completion_rate}]
        games:            dict[appid, {name, ...}]
        playtime_minutes: dict[appid, 分钟]

    返回：{"candidates": [...], "summary": {...}}
          candidates 按分桶(>=90 / 70-89 / <70) + 剩余数 + 预计时长升序；
          **已 100%（剩余 0 个）的不进候选榜** —— 无需冲刺，不再显示。
    """
    achievement_data = achievement_data or {}
    games = games or {}
    playtime_minutes = playtime_minutes or {}
    candidates = []
    no_data = 0
    completed = 0
    bucket_a = bucket_b = 0
    for appid, info in achievement_data.items():
        if not isinstance(info, dict):
            continue
        total = _to_int(info.get("total", 0))
        raw_achieved = info.get("achieved")
        if total <= 0 or raw_achieved is None:
            no_data += 1
            continue
        achieved = _to_int(raw_achieved)
        remaining = total - achieved
        if remaining <= 0:
            # 已 100% 全成就（剩余 0 个）：没有可冲刺的成就，不进候选榜。
            completed += 1
            continue
        rate = info.get("completion_rate")
        if rate is None:
            rate = round(achieved / total * 100, 2)
        minutes = _to_int(playtime_minutes.get(appid, 0))
        avg_per_ach = minutes / total if total else 0
        est_hours = round(remaining * avg_per_ach / 60, 2) if avg_per_ach > 0 else 0.0
        if rate >= _BUCKET_A:
            bucket = 0
            bucket_a += 1
        elif rate >= _BUCKET_B:
            bucket = 1
            bucket_b += 1
        else:
            bucket = 2
        name = display.display_name(appid, games.get(appid) or {})
        candidates.append(
            {
                "appid": appid,
                "name": name,
                "rate": rate,
                "achieved": achieved,
                "total": total,
                "remaining": remaining,
                "estimated_hours": est_hours,
                "_bucket": bucket,
            }
        )
    candidates.sort(key=lambda x: (x["_bucket"], x["remaining"], x["estimated_hours"]))
    for c in candidates:
        c.pop("_bucket")
    return {
        "candidates": candidates,
        "summary": {
            "total": len(candidates) + completed + no_data,  # 扫描到的条目（含无数据）
            "with_data": len(candidates) + completed,        # 有可用成就数据的条目
            "incomplete": len(candidates),                   # 有数据且未 100%（候选榜）
            "completed": completed,                          # 有数据且已 100%（不进候选榜）
            "no_data": no_data,                              # 无可用成就数据
            "bucket_90_plus": bucket_a,
            "bucket_70_plus": bucket_b,
        },
    }


def run(args: Any) -> int:
    """CLI 入口：args.achievement_data(dict) 或 args.data(JSON 路径)，可选 args.games/playtime_minutes。"""
    data = getattr(args, "achievement_data", None)
    if not data and getattr(args, "data", None):
        try:
            with open(args.data, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[!] 成就数据读取失败：{exc}")
            return 1
    if not isinstance(data, dict):
        print("用法：radar 模块需要 achievement_data(dict) 或 --data JSON 路径")
        return 1
    games = getattr(args, "games", None) or {}
    pt = getattr(args, "playtime_minutes", None) or {}
    result = compute_achievement_radar(data, games=games, playtime_minutes=pt)
    summary = result["summary"]
    print(
        f"== 成就冲刺雷达（{summary['incomplete']} 款可冲刺 / "
        f"{summary['with_data']} 款有数据 / {summary['total']} 款扫描；"
        f"{summary['completed']} 款已 100% 全成就） =="
    )
    for i, c in enumerate(result["candidates"], 1):
        print(
            f"  {i:>2}. {c['name']:<20} 完成度 {c['rate']:>6.2f}%  "
            f"剩余 {c['remaining']:>3}  预估 {c['estimated_hours']:.1f}h"
        )
    if not result["candidates"]:
        if summary["completed"]:
            print("  有成就数据的游戏都已 100% 全成就，没有需要冲刺的")
        else:
            print("  暂无成就数据（建议先运行 stats 采集）")
    return 0
