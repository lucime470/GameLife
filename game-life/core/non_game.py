"""非游戏条目判定（**全产品唯一口径**）。

Steam 会把 **Steam Client / Steam 截图 / 手柄配置 / 壁纸引擎 / 性能测试工具**
这类条目也写进 ``localconfig.vdf``，于是它们混进「我的库」。此前每个模块各自
维护一份黑名单（``webapp/api.py`` / ``core/render_bridge.py`` /
``engine/value_ranking.py`` / ``engine/series_tree.py``），口径不一致，导致同一个库
在不同界面显示不同的游戏数（面板顶部 174、游戏明细 166），性价比榜里躺着
「壁纸引擎」，反向愿望单里冒出「实用工具 / 照片编辑」这类软件类型。

本模块把判定**收敛成一处**，其余模块一律改调这里。

为什么放在 core 层
------------------
``core`` 是最底层，``engine`` 与 ``webapp`` 都依赖它（既有约定：
``engine/series_tree.py`` 已 ``from core import appinfo``）。反过来放到
``webapp.api`` 就会让 core 依赖上层、形成 import 环；放到 ``engine`` 则让
``core.render_bridge`` 依赖 engine 的具体实现（render_bridge 对 engine 的 import
全部是函数内延迟 import，就是为避开这一点）。因此唯一口径必须落在 core。

判定规则（逐字沿用既有共识，不新发明）
--------------------------------------
1. 黑名单 appid：``{7, 760, 241100}``（Steam 客户端本体 / 截图 / 输入配置）。
   这三项连 appinfo 都不必查，直接命中。
2. 本机 ``data/appinfo_index.json`` 的条目 ``type`` 落在
   ``NON_GAME_TYPES`` 里（大小写不敏感）。
3. **类型未知（查不到 appinfo / type 为空）一律当游戏放行** —— 铁律，
   绝不误杀真游戏。合成数据、未入库的新游戏都靠这条兜底。

只读本地、绝不联网；读不到一律降级为「未知」并放行。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# Steam 客户端本体 / 截图 / 输入配置：无需查 appinfo，直接判非游戏
NON_GAME_APPIDS = frozenset({7, 760, 241100})

# appinfo 里这些 type 不是游戏（Config / Tool / Application 等）。
# 与 webapp/api.py:_NON_GAME_TYPES 和 core/render_bridge.py:NON_GAME_TYPES 的并集一致。
NON_GAME_TYPES = frozenset({
    "config",
    "tool",
    "application",
    "adtool",
    "hardware",
    "series",
    "video",
    "plugin",
    "guide",
    "driver",
    "movie",
})

_LOCK = threading.Lock()
# appid -> 是否非游戏。appinfo 索引在进程内不变，缓存避免逐页重复读 JSON。
_MEMO: dict[int, bool] = {}


def _app_type(appid: int) -> str:
    """本机 appinfo 里的条目 type（小写）；取不到返回空串（视为未知）。"""
    try:
        from core import appinfo

        return str((appinfo.app_metadata(appid) or {}).get("type") or "").strip().lower()
    except Exception as exc:                      # 读不到类型不影响主流程
        logger.debug("读取 appinfo 类型失败（%s）：%s", appid, exc)
        return ""


def is_non_game(appid: Any) -> bool:
    """是否非游戏条目（黑名单 appid 或本机 appinfo 里的非游戏 type）。

    **类型未知时一律当游戏**，绝不误伤真游戏。非法输入（None / 非数字）也返回 False。
    """
    try:
        aid = int(appid)
    except (TypeError, ValueError):
        return False
    if aid in NON_GAME_APPIDS:
        return True
    hit = _MEMO.get(aid)
    if hit is not None:
        return hit
    result = _app_type(aid) in NON_GAME_TYPES
    with _LOCK:
        _MEMO[aid] = result
    return result


def exclude_non_games(play_stats: Mapping) -> dict:
    """滤掉非游戏条目，返回**新** dict（不动入参、不删数据，纯展示层过滤）。

    键原样保留（库侧既有 int 键也有 str 键），上层按需自行转换。
    非 int 键保持不动（它们本就进不了库范围）。
    """
    result: dict = {}
    for key, value in (play_stats or {}).items():
        try:
            appid = int(key)
        except (TypeError, ValueError):
            result[key] = value
            continue
        if not is_non_game(appid):
            result[key] = value
    return result


def reset_cache() -> None:
    """清空判定缓存（测试用；下一次调用重新读 appinfo）。"""
    with _LOCK:
        _MEMO.clear()