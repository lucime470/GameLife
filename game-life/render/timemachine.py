"""时光机（可视化呈现层 · 模块 6）。

职责：每天展示「去年的今天你在玩什么」，生成单文件 HTML 卡片。
      算法按 04 文档 §8.2：取今天日期，在 snapshots 中寻找去年今日 ±7 天内
      LastPlayed 吻合的游戏；无快照数据则 fallback 到 play_stats；无匹配
      显示空态「一年前的今天，你没有游戏记录」。

数据接口（纯 dict 参数，缺省字段均有安全默认值）::

    {
        "user_name": str,            # 可选，默认「神秘玩家」
        "target_date": str,          # 可选，默认今天 "YYYY-MM-DD"
        "matches": [                 # 可选；缺省时由 snapshots / play_stats 推导
            {
                "name": "艾尔登法环",
                "then_date": "2025-09-18",   # 去年今日 ±7 天内
                "then_hours": 87,            # 当时已玩（小时）
                "now_hours": 145,            # 现在已玩（小时）
            },
        ],
        "snapshots": [...],          # 可选 [{record_date, appid, playtime, last_played, status}]
        "play_stats": {...},         # 可选 {appid: {name?, playtime(分钟), last_played}}
        "games": {...},              # 可选 {appid: {name}}
        "generated_at": str,         # 可选，默认今天
    }

用法::

    from game_life.render import timemachine
    timemachine.generate_timemachine(data)                # 渲染 + 写文件 + 打开浏览器
    timemachine.generate_timemachine(data, open_browser=False, output_path=...)
    python game-life/render/timemachine.py --demo --no-open
"""

from __future__ import annotations

import argparse
import json
import logging
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional, Union

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
DEFAULT_OUTPUT_DIR = PACKAGE_DIR.parent / "output"


# ---------------------------------------------------------------- 游戏封面
#
# 页面图片一律走「产物目录内的 covers/ 相对路径」：由 core.render_bridge
# 把本地已有封面（data/covers 或本机 Steam librarycache，**不联网**）复制过去。
# 取不到封面的游戏保持 cover 为空，模板据此显示占位色块（首字母），绝不裂图。

def _attach_covers(items, out_dir) -> None:
    """就地给条目补上 ``cover``（相对产物目录的图片地址）；无封面则留空。"""
    appids: list = []
    for item in items or ():
        appid = (item or {}).get("appid")
        if appid in (None, "", 0):
            continue
        appids.append(appid)
    if not appids:
        return
    try:
        from core import render_bridge

        meta = render_bridge.prepare_covers_meta(appids, out_dir)
    except Exception as exc:  # 封面不可用不应影响页面生成
        logger.debug("封面不可用，页面显示占位块：%s", exc)
        return
    for item in items or ():
        row = meta.get(str((item or {}).get("appid"))) or {}
        url = row.get("url")
        if url:
            item["cover"] = url
            item["cover_kind"] = str(row.get("kind") or "unknown")

# ---------------------------------------------------------------- 工具函数

def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_num(value: Any, decimals: int = 1) -> str:
    """1247.5 -> "1,247.5"；1247.0 -> "1,247"。"""
    v = _to_float(value)
    s = f"{v:,.{decimals}f}"
    if s.endswith("." + "0" * decimals):
        s = s[: -(decimals + 1)]
    return s


def _normalize_date(value: Any) -> str:
    """把 datetime / date / "YYYY-MM-DD" / unix 秒 归一为 "YYYY-MM-DD"。"""
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if s.isdigit() and len(s) >= 9:
        try:
            return datetime.fromtimestamp(int(s)).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return s
    return s


def _display_date(d: str) -> str:
    """"2025-09-18" -> "2025年9月18日"。"""
    parts = str(d or "").split("-")
    if len(parts) == 3:
        try:
            return f"{int(parts[0])}年{int(parts[1])}月{int(parts[2])}日"
        except ValueError:
            pass
    return str(d or "")
# ------------------------------------------------------------ 匹配算法（04 文档 8.2）

# unix 秒可接受的下界（约 2000-01-01）：挡掉 Steam 写入的 LastPlayed=0/1 这类脏值，
# 避免它们被当作 1970-01-01 静默混进匹配结果。
_MIN_TIMESTAMP = 946684800


def _parse_iso_date(value: Any) -> Optional[date]:
    """严格解析 datetime / date / "YYYY-MM-DD" / unix 秒，无法解析时返回 None。

    与 :func:`_normalize_date` 的区别：畸形输入不再原样透传，而是显式失败（None），
    由调用方安全丢弃或回退到兜底值，避免 ``int()`` 之类的下游崩溃。

    unix 秒做范围校验：``_MIN_TIMESTAMP <= ts <= 现在 + 1 天``。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        # 纯数字一律按 unix 秒处理；过小（脏值）或过大（溢出）均视为无效
        try:
            ts = int(s)
        except (TypeError, ValueError):
            return None
        if not (_MIN_TIMESTAMP <= ts <= datetime.now().timestamp() + 86400):
            return None
        try:
            return datetime.fromtimestamp(ts).date()
        except (OverflowError, OSError, ValueError):
            return None
    # 只取前 10 位做严格校验，顺带容忍 "YYYY-MM-DDTHH:MM:SS" 这类带时间的 ISO 串；
    # 再退一步支持 "YYYY-MM"（按当月 1 日处理）。
    for text, fmt in ((s[:10], "%Y-%m-%d"), (s, "%Y-%m")):
        try:
            return datetime.strptime(text, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _target_window(target: str) -> tuple[date, date]:
    """去年今日 ±7 天的日期窗口；目标日期畸形时回退到今天，绝不抛异常。"""
    t = _parse_iso_date(target) or date.today()
    last_year = t.replace(year=t.year - 1)
    return last_year - timedelta(days=7), last_year + timedelta(days=7)


def _parse_date(d: str):
    try:
        return datetime.strptime(d, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _find_matches(data: Mapping[str, Any]) -> tuple[list[dict], str]:
    """按 snapshots -> play_stats 顺序推导去年今日 ±7 天的匹配，返回 (matches, target)。"""
    target_date = _parse_iso_date(data.get("target_date"))
    if target_date is None and data.get("target_date"):
        logger.debug("timemachine: target_date=%r 无法解析，回退到今天", data.get("target_date"))
    target = (target_date or date.today()).isoformat()
    direct = data.get("matches")
    if direct is not None:
        return ([dict(m) for m in direct if isinstance(m, Mapping)], target)

    lo, hi = _target_window(target)
    play_stats = data.get("play_stats") or {}
    games = data.get("games") or {}

    now_hours: dict[str, float] = {}
    if isinstance(play_stats, Mapping):
        for appid, stat in play_stats.items():
            info = stat if isinstance(stat, Mapping) else {}
            now_hours[str(appid)] = _to_float(info.get("playtime")) / 60.0

    results: list[dict] = []
    seen: set[str] = set()

    def _name_of(appid: Any, stat: Mapping, fallback: str = "") -> str:
        name = stat.get("name")
        if not name and isinstance(games, Mapping):
            info_g = games.get(appid) or games.get(str(appid)) or {}
            name = info_g.get("name", "") if isinstance(info_g, Mapping) else ""
        return str(name or fallback).strip() or "神秘游戏"

    def _try_append(appid: Any, lp: str, minutes: float, fallback_name: str = "") -> None:
        lp_date = _parse_date(lp)
        if not lp_date or not (lo <= lp_date <= hi):
            return
        key = str(appid)
        if key and key in seen:
            return
        if key:
            seen.add(key)
        if not fallback_name:
            fallback_name = _name_of(appid, {}, "")
        results.append(
            {
                "appid": appid,
                "name": fallback_name,
                "then_date": lp,
                "then_hours": minutes / 60.0,
                "now_hours": now_hours.get(key, 0.0),
            }
        )

    snapshots = data.get("snapshots") or []
    if isinstance(snapshots, list):
        for snap in snapshots:
            if not isinstance(snap, Mapping):
                continue
            rows = snap.get("games")
            if rows is None:
                rows = [snap]
            if isinstance(rows, Mapping):
                rows = list(rows.values())
            for g in rows:
                if not isinstance(g, Mapping):
                    continue
                lp_date = _parse_iso_date(g.get("last_played") or snap.get("record_date"))
                if lp_date is None:
                    logger.debug("timemachine: 跳过非法快照日期（appid=%s）", g.get("appid"))
                    continue
                lp = lp_date.isoformat()
                _try_append(g.get("appid"), lp, _to_float(g.get("playtime")))

    if not results and isinstance(play_stats, Mapping):
        for appid, stat in play_stats.items():
            info = stat if isinstance(stat, Mapping) else {}
            lp_date = _parse_iso_date(info.get("last_played"))
            if lp_date is None:
                logger.debug("timemachine: 跳过非法 last_played=%r（appid=%s）", info.get("last_played"), appid)
                continue
            lp = lp_date.isoformat()
            _try_append(appid, lp, _to_float(info.get("playtime")), _name_of(appid, info))

    return results, target
# ------------------------------------------------------------ 数据归一化

def normalize_timemachine_data(data: Mapping[str, Any]) -> dict:
    """把外部传入的 dict 清洗为模板可直接使用的结构。"""
    user_name = str(data.get("user_name") or data.get("name") or "神秘玩家")
    matches, target = _find_matches(data)

    items: list[dict] = []
    for m in matches:
        then_hours = _to_float(m.get("then_hours"))
        now_hours = _to_float(m.get("now_hours"))
        delta = now_hours - then_hours
        if delta >= 0.05:
            delta_text = f"+{_fmt_num(delta)} 小时，这一年"
            delta_cls = "tm-delta up"
        else:
            delta_text = "今年没有再花时间"
            delta_cls = "tm-delta flat"
        then_date = (_parse_iso_date(m.get("then_date")) or _parse_iso_date(target) or date.today()).isoformat()
        items.append(
            {
                "appid": m.get("appid"),
                "name": str(m.get("name") or "神秘游戏"),
                "then_date": then_date,
                "then_date_display": _display_date(then_date),
                "then_hours": then_hours,
                "then_hours_text": _fmt_num(then_hours),
                "now_hours": now_hours,
                "now_hours_text": _fmt_num(now_hours),
                "delta_text": delta_text,
                "delta_cls": delta_cls,
            }
        )

    items.sort(key=lambda x: x["then_date"], reverse=True)
    return {
        "user_name": user_name,
        "target_date": target,
        "target_date_display": _display_date(target),
        "matches": items,
        "has_matches": bool(items),
        "generated_at": str(data.get("generated_at") or date.today().isoformat()),
    }


# ------------------------------------------------------------ 主生成流程

def generate_timemachine(
    data: Mapping[str, Any],
    output_path: Optional[Union[str, Path]] = None,
    open_browser: bool = True,
) -> Path:
    """生成「时光机 · 去年今日」单文件 HTML，返回输出路径。

    :param data: 时光机数据 dict（结构见模块 docstring）
    :param output_path: 输出路径；缺省为 ``game-life/output/timemachine.html``
    :param open_browser: 生成后是否自动用浏览器打开（默认 True）
    """
    payload = normalize_timemachine_data(data)
    out = Path(output_path) if output_path else DEFAULT_OUTPUT_DIR / "timemachine.html"
    out.parent.mkdir(parents=True, exist_ok=True)

    _attach_covers(payload["matches"], out.parent)

    env = _build_env()
    html = env.get_template("timemachine.html").render(payload=payload)
    out.write_text(html, encoding="utf-8")
    logger.info("时光机已生成：%s（去年今日匹配 %d 款）", out, len(payload["matches"]))

    if open_browser:
        webbrowser.open(out.resolve().as_uri())
    return out
def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(("html", "xml")),
    )
    env.filters["h"] = lambda v: _fmt_num(v)
    return env


# ---------------------------------------------------------------- 示例数据

def demo_data() -> dict:
    """示例数据：去年今日 ±7 天内的匹配（含增长与未再玩两种）。"""
    return {
        "user_name": "Neo",
        "target_date": "2026-09-20",
        "matches": [
            {"name": "艾尔登法环", "then_date": "2025-09-18", "then_hours": 87, "now_hours": 145},
            {"name": "群星", "then_date": "2025-09-24", "then_hours": 12.5, "now_hours": 12.5},
            {"name": "星露谷物语", "then_date": "2025-09-13", "then_hours": 3.5, "now_hours": 18},
        ],
    }


# ---------------------------------------------------------------- CLI 入口

def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="timemachine",
        description="生成「时光机 · 去年今日」单文件 HTML（示例：--demo 或 --data data.json）",
    )
    parser.add_argument("--data", type=Path, help="数据 JSON 文件路径（结构见 timemachine.generate_timemachine 文档）")
    parser.add_argument("--demo", action="store_true", help="使用内置示例数据（无真实数据时跑通全流程）")
    parser.add_argument("--output", type=Path, help="输出 HTML 路径（缺省为 game-life/output/timemachine.html）")
    parser.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    args = parser.parse_args(argv)

    if args.data:
        with open(args.data, encoding="utf-8") as f:
            data = json.load(f)
        logger.info("已从 %s 读取数据", args.data)
    else:
        data = demo_data()
        logger.info("未指定 --data，默认使用示例数据")

    out = generate_timemachine(data, output_path=args.output, open_browser=not args.no_open)
    print(f"[timemachine] 已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
