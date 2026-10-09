"""展示层中文化（引擎层共享）：显示归显示、判定归判定。

用户反复要求「游戏名 / 类型都要中文」。渲染层（``core/render_bridge.py``）已单独
处理；引擎层此前完全没有读中文名，于是 CLI 输出仍是英文（类型分布 Action / Indie、
「最常玩 Action」、榜单里的英文游戏名、族谱里的英文成员名）。

本模块只负责**输出给人看**的字段：

    display_name(appid, meta)   官方中文名 -> 本地中文名索引 -> 官方原名 -> "appid N"
    display_genres(meta)        官方中文类型 -> 官方英文类型
    genre_display(genre)        英文类型名 -> 官方中文类型名（引擎文案用）

判定绝不许走这里
----------------
所有**判定 / 匹配 / 查表**必须继续使用官方英文原名，否则会静默改变评分与状态：

  * 免费标记（``price_state`` 认 ``Free to Play``）
  * 高难度标签（``persona._is_hard_game`` 的 ``HARD_TAGS``）
  * 单局时长表（``recommender._session_minutes`` 的 ``rules.json`` 英文 key）
  * 分类规则（``classifier`` 的 ``genre_rules``）
  * 价格三态、候选筛选、打分、排序、库范围

本模块不参与上述任何路径，只做「把英文名换成中文名」的展示替换。

数据来源（全部本地只读、零联网）
--------------------------------
1. 调用方传入的元数据（``core.store_api.cached_metadata`` /
   ``render_bridge.fill_metadata`` 已带 ``name_cn`` / ``genres_cn``）；
2. ``data/cache.db`` 的 ``games`` 表（``name_cn`` / ``genres_cn`` 列）；
3. ``data/names_cn.json``（``core.metadata_cn`` 的抓取缓存，覆盖比 cache.db 更广）；
4. ``data/genre_cn.json``（Steam 官方类型 ID -> 中英名对照表）。

任何一步失败都降级为「没有中文名」，按官方原名输出，绝不抛异常、绝不联网。

分层
----
``engine`` 依赖 ``core`` 是本项目既有约定（``series_tree`` 已 ``from core import
appinfo`` / ``from data import schema``），因此这里允许在函数内部延迟 import；
**不反向依赖 render 层**（render 在 engine 之上）。
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# 与 core.store_api / core.render_bridge 一致的列名（本地库权威中文列）
CN_NAME_KEY = "name_cn"
CN_GENRES_KEY = "genres_cn"

_LOCK = threading.Lock()
_NAME_INDEX: dict[int, str] | None = None
_GENRE_MAP: dict[str, str] | None = None


# ------------------------------------------------------------------ 基础工具

def _as_str_list(raw: Any) -> list[str]:
    """list / JSON 字符串 / 逗号分隔字符串 -> 去空去重列表（保序）。"""
    if isinstance(raw, (list, tuple, set)):
        values = [str(item).strip() for item in raw]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                data = json.loads(text)
            except (ValueError, TypeError):
                data = None
            values = [str(item).strip() for item in data] if isinstance(data, list) else []
        else:
            values = [part.strip() for part in text.split(",")]
    else:
        values = []
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _text(value: Any) -> str:
    """空串 / 纯空白 / None 一律当成「没有」。"""
    return str(value if value is not None else "").strip()


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ 懒加载索引

def _build_name_index() -> dict[int, str]:
    """appid -> 官方中文名（本地只读）。"""
    index: dict[int, str] = {}
    # 1) data/names_cn.json：覆盖最广（cache.db 之外的已确认中文名）
    try:
        from core import metadata_cn

        items = metadata_cn.load_cache().get("items") or {}
        for key, entry in items.items():
            if not isinstance(entry, Mapping):
                continue
            value = _text(entry.get("name_cn"))
            appid = _as_int(key)
            if value and appid is not None:
                index[appid] = value
    except Exception as exc:                      # pragma: no cover - 降级路径
        logger.debug("中文名缓存（names_cn.json）不可用：%s", exc)
    # 2) cache.db 的 name_cn：本地库权威列，覆盖上面的值
    try:
        from core import store_api

        for appid, name in (store_api.cached_names_cn() or {}).items():
            aid = _as_int(appid)
            value = _text(name)
            if aid is not None and value:
                index[aid] = value
    except Exception as exc:                      # pragma: no cover - 降级路径
        logger.debug("cache.db 中文名不可用：%s", exc)
    return index


def _build_genre_map() -> dict[str, str]:
    """官方英文类型名 -> 官方中文类型名（本地只读）。"""
    pairs: dict[str, str] = {}
    # 1) cache.db：逐行按 appid 的 中英类型列表**按位对齐**（数量不等则整行跳过）
    try:
        from data.schema import get_connection

        db = get_connection()
        try:
            rows = db.execute("SELECT genres, genres_cn FROM games").fetchall()
        finally:
            db.close()
        for row in rows:
            en = _as_str_list(row[0])
            cn = _as_str_list(row[1])
            if not en or len(en) != len(cn):
                continue
            for source, target in zip(en, cn):
                if source and target and source not in pairs:
                    pairs[source] = target
    except Exception as exc:                      # pragma: no cover - 降级路径
        logger.debug("cache.db 中文类型表不可用：%s", exc)
    # 2) data/genre_cn.json：官方 ID -> 中英名对照，补上面没有覆盖到的类型
    try:
        from core import store_api

        path = store_api.readonly_data_path("genre_cn.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, value in (data or {}).items():
            if str(key).startswith("_") or not isinstance(value, Mapping):
                continue
            source = _text(value.get("en"))
            target = _text(value.get("cn"))
            if source and target and source not in pairs:
                pairs[source] = target
    except Exception as exc:                      # pragma: no cover - 降级路径
        logger.debug("genre_cn.json 不可用：%s", exc)
    return pairs


def _name_index() -> dict[int, str]:
    global _NAME_INDEX
    if _NAME_INDEX is None:
        with _LOCK:
            if _NAME_INDEX is None:
                try:
                    _NAME_INDEX = _build_name_index()
                except Exception as exc:          # pragma: no cover - 极端降级
                    logger.debug("构建中文名索引失败：%s", exc)
                    _NAME_INDEX = {}
    return _NAME_INDEX


def _genre_map() -> dict[str, str]:
    global _GENRE_MAP
    if _GENRE_MAP is None:
        with _LOCK:
            if _GENRE_MAP is None:
                try:
                    _GENRE_MAP = _build_genre_map()
                except Exception as exc:          # pragma: no cover - 极端降级
                    logger.debug("构建中文类型表失败：%s", exc)
                    _GENRE_MAP = {}
    return _GENRE_MAP


def reset_cache() -> None:
    """清空懒加载缓存（测试用；下一次调用重新读盘）。"""
    global _NAME_INDEX, _GENRE_MAP
    with _LOCK:
        _NAME_INDEX = None
        _GENRE_MAP = None


# ------------------------------------------------------------------ 对外接口

def name_cn_of(appid: Any) -> str:
    """只读：该 appid 的官方中文名；取不到返回 ""。"""
    aid = _as_int(appid)
    if aid is None:
        return ""
    return _name_index().get(aid, "")


def display_name(appid: Any, meta: Mapping | None = None) -> str:
    """展示用游戏名：调用方中文名 -> 本地中文名索引 -> 官方原名 -> "appid N"。

    空串 / 纯空白 / None 一律算「没有」，页面与终端都不会出现空白名字。
    """
    source = meta if isinstance(meta, Mapping) else {}
    provided = _text(source.get(CN_NAME_KEY))
    if provided:
        return provided
    local = name_cn_of(appid)
    if local:
        return local
    original = _text(source.get("name"))
    if original:
        return original
    aid = _as_int(appid)
    return f"appid {aid if aid is not None else appid}"


def lookup_name(appid: Any, meta: Mapping | None = None) -> str:
    """只查中文名（不回落英文）：调用方中文名 -> 本地中文名索引；取不到返回 ""。

    用于「先看本地有没有官方中文名，没有才走原来的命名逻辑」的调用点
    （如族谱：先中文名 -> 商店中文缓存 -> 本机 appinfo -> 英文名）。
    """
    source = meta if isinstance(meta, Mapping) else {}
    provided = _text(source.get(CN_NAME_KEY))
    if provided:
        return provided
    return name_cn_of(appid)


def display_name_from(appid: Any, meta: Mapping | None = None, fallback: str = "") -> str:
    """同 :func:`display_name`，额外接受一个「调用方已有的展示名」兜底。

    用于「调用方已经算好名字（可能来自系列缓存 / 手工表）」的场景：中文名仍然
    优先，其次才是调用方给的名字，最后才 ``appid N``。
    """
    source = dict(meta) if isinstance(meta, Mapping) else {}
    if _text(fallback) and not _text(source.get("name")):
        source["name"] = _text(fallback)
    return display_name(appid, source)


def display_genres(meta: Mapping | None = None) -> list[str]:
    """展示用类型列表：官方中文类型 -> 官方英文类型（中文缺失/与英文相同则用英文）。

    与 ``render_bridge._localized_genres`` 同一口径：中文列表与英文列表**不同**时
    才采用中文，避免同一批数据出现中英两种写法。
    """
    source = meta if isinstance(meta, Mapping) else {}
    english = _as_str_list(source.get("genres"))
    chinese = _as_str_list(source.get(CN_GENRES_KEY))
    if chinese and chinese != english:
        return chinese
    return english


def genre_display(genre: Any) -> str:
    """英文类型名 -> 官方中文类型名；没有对照时原样返回（绝不机翻）。"""
    text = _text(genre)
    if not text:
        return ""
    mapped = _genre_map().get(text)
    if mapped:
        return mapped
    lowered = text.lower()
    for source, target in _genre_map().items():
        if source.lower() == lowered:
            return target
    return text
