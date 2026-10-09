"""游戏族谱（分析引擎层 · 模块 5：series_tree）。

数据来源（权威优先）
--------------------
1. **官方商店 ``franchise=`` 检索为权威主源**（首选）：Valve 官方商店的
   ``search/results?franchise=<系列名>&category1=998`` 返回该系列**全部**在售作品，
   且 ``category1=998`` 已剔除原声 / 礼包 / 季票。实测本机 ``appinfo.vdf`` 里
   《怪物猎人》只有 2 部 ``type=Game`` 的真作品，而官方商店有 7 部以上——这正是
   「系列不全」的根因。抓取结果落盘 ``data/series_official.json``，
   之后**完全离线可用**；这个域名大陆时通时断，取不到网络时静默退化到第 2 条。
2. **本机 ``<Steam>/appcache/appinfo.vdf`` 为离线兜底**：``common.associations`` 中
   ``type == "franchise"`` 是 Valve 官方标注的系列名，由 :mod:`core.appinfo` 解析并
   剔除「其实是公司名」的噪音后提供。它是**客户端缓存**，覆盖面有限（本机 1801 个 app）。
3. **手工表 ``data/series_map.json`` 为补充源**：补官方没有任何标注的作品，
   并在 ``_official_aliases`` 里把官方英文系列名映射到中文展示名。
   三条来源按**同一展示名**归并为一条（同名系列不再重复）。

设计约定
--------
  * 纯函数、纯 Python、可单测；输出纯 dict，终端与 HTML 渲染通用；
  * **只输出 ``owned_count > 0`` 或 ``played_count > 0`` 的系列**（用户既没有、
    也没玩过的系列不再展示）；
  * **单一作品不算系列**：整个系列只有 1 部作品时（如「空洞骑士」曾只列 1 部），
    它是**独立作品**而不是系列，默认不再作为系列展示（``min_titles`` 可调）；
  * **不截断**：返回全部合格系列，折叠/滚动由展示层决定；
  * 输出结构稳定：``series / owned_count / played_count / total_count / completed_count /
    playtime_minutes / playtime_hours / games[]``；``series`` 是**统一的展示名**
    （中文优先，英文 franchise 经 ``_official_aliases`` 映射），保证全局唯一；
    另有可选 ``official_name``（该系列对应的官方英文 franchise 名）；
  * **「拥有」只认 ``core.ownership``（全产品唯一口径）**：``flag == 0`` 才是真拥有；
    「家庭库 / 免费体验但本账号玩过」的成员标成 ``played``（未拥有但玩过）；
    读不到授权票时降级为历史行为「在库里即算拥有」；
  * **只用 ``type == "Game"`` / ``App_`` 的条目**，DLC、原声、季票、组合包与
    试玩 / 性能测试一律不计入系列成员；
  * 排除非游戏条目（appid 7 / 760 / 241100）；
  * 不 import 状态引擎：状态优先用调用方传入的 statuses，缺省走 stats 兜底规则；
  * 三条来源全部不可用时自动退化为空结果，**绝不抛异常、绝不阻塞页面**。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html import unescape
from pathlib import Path
from typing import Any, Iterable

from . import display
from .stats import load_rules, _resolve_statuses, _to_int

# 非游戏条目（Steam 客户端 / 截图 / 壁纸引擎 / 性能工具等），绝不进入族谱。
# 判定口径收敛到 core.non_game（全产品唯一口径）—— 此前这里只有 3 项黑名单、
# 不查 appinfo 类型，与面板/性价比榜的口径并不一致。
from core import non_game  # noqa: E402

NON_GAME_APPIDS = non_game.NON_GAME_APPIDS

# 归属类别常量：与 core.ownership 同源（「拥有」的唯一口径，绝不在这里另立字面量）。
try:  # pragma: no cover - 打包/裁剪环境兜底，正常不会走到 except
    from core.ownership import OWNED as _OWNED
    from core.ownership import PLAYED_SHARED as _PLAYED_SHARED
    from core.ownership import FREE as _FREE
except Exception:
    _OWNED = "owned"
    _PLAYED_SHARED = "played_shared"
    _FREE = "free"

# Valve 偶尔把「发行商计划 / 工作室」标成 franchise，这些不是游戏系列，按名排除
NOISE_SERIES = frozenset({
    "ea play", "ea originals", "playstation studios", "wb games",
    "505 games official", "thunderful publishing", "franchise",
})

# ------------------------------------------------------------------ 权威源：官方商店 franchise 检索
#
# 为什么需要它：本机 ``appinfo.vdf`` 只是**客户端缓存**，只覆盖本机见过的 app。
# 实测《怪物猎人》在该缓存里只有 2 部 ``type=Game`` 的真作品，于是族谱显示 3 部
# 封顶，用户一眼就看出「系列不全」。Valve 官方商店的 ``franchise=<系列名>`` 检索
# 参数返回该系列**全部**在售作品，才是权威目录。
#
# URL 关键参数（实测）：
#   * ``franchise=<系列名>`` —— 该系列全部作品；
#   * ``category1=998`` —— 只要**游戏本体**，自动剔除原声 / 礼包 / 季票；
#   * ``infinite=1&json=1`` —— 一次返回全部（实测 start=50/100 返回同一批，无需分页），
#     响应体是 ``{"total_count": N, "results_html": "..."}``。
#
# 解析约定（实测 HTML 结构）：
#   * 每条结果是一个 ``<a href=...>`` 块；块内 ``data-ds-itemkey="App_<appid>"``
#     才是真**游戏**。组合包 / 季票的 itemkey 是 ``Sub_<subid>`` 且
#     ``data-ds-appid`` 是逗号分隔的多值（如 ``1277400,1461850``），必须靠 itemkey 排除；
#   * 标题在 ``<span class="title">...</span>``，已按 ``l=schinese&cc=CN`` 本地化；
#   * 少数**试玩 / 性能测试 / 原声**条目仍是 ``App_``（如
#     ``MONSTER HUNTER RISE: SUNBREAK DEMO``），按标题关键词剔除。
#
# 这个域名大陆**时通时断**（实测连打 6 次前 4 次超时），所以：重试 → 成功即落盘
# ``data/series_official.json`` → 之后完全离线可用；取不到网络时静默用缓存，
# **绝不抛异常、绝不阻塞页面**。

logger = logging.getLogger("game-life.series_tree")

# --------------------------------------------------------------- 路径解析
#
# 打包（PyInstaller onefile）后 ``__file__`` 落在**临时解包目录** ``sys._MEIPASS``，
# 该目录进程退出即删。权威系列缓存 ``series_official.json`` 若按 ``__file__`` 定位，
# 打包版会「读不到 + 写进去下次就没了」→ 族谱退回覆盖不全的本机数据，
# 复现用户反馈的「系列不全」。因此运行时读写一律走 ``data.schema.data_dir()``
# （它已正确处理 frozen：源码态 = 仓库 data，打包态 = exe 附近的真实 data）。


def _frozen_signature() -> tuple:
    """(frozen, _MEIPASS, executable)：环境切换时让下面的路径缓存失效。"""
    return (
        bool(getattr(sys, "frozen", False)),
        str(getattr(sys, "_MEIPASS", "") or ""),
        str(getattr(sys, "executable", "") or ""),
    )


_DATA_DIR_CACHE: tuple = ()


def _stable_data_dir() -> Path:
    """运行时读写数据的**真实** data 目录（打包态 = exe 附近的 game-life/data）。"""
    global _DATA_DIR_CACHE
    signature = _frozen_signature()
    if _DATA_DIR_CACHE and _DATA_DIR_CACHE[0] == signature:
        return _DATA_DIR_CACHE[1]
    try:
        from data import schema  # 延迟 import：不在 engine 层制造 import 期副作用
        resolved = Path(schema.data_dir())
    except Exception:  # 理论上不会发生；兜底退回源码布局
        resolved = Path(__file__).resolve().parent.parent / "data"
    resolved = Path(resolved)
    _DATA_DIR_CACHE = (signature, resolved)
    return resolved


def _bundle_data_dir() -> Path:
    """随包分发的**只读**静态表所在目录（打包态 = ``_MEIPASS/data``）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "data"
    return Path(__file__).resolve().parent.parent / "data"


def _readonly_data_path(name: str) -> Path:
    """只读静态表定位：先真实 data 目录（用户可自行更新），再回退随包副本。"""
    real = _stable_data_dir() / name
    if real.is_file():
        return real
    bundled = _bundle_data_dir() / name
    if bundled.is_file():
        return bundled
    return real


def series_cache_path() -> Path:
    """权威目录缓存的落盘位置（真实 data 目录下的 ``series_official.json``）。"""
    return _stable_data_dir() / "series_official.json"


def series_map_path() -> Path:
    """手工补充表（只读）的位置：真实 data 目录优先，回退随包副本。"""
    return _readonly_data_path("series_map.json")


# 模块级常量：按 import 期解析，仅供外部只读取用（真实运行中 frozen 早于 import 就位）
SERIES_CACHE_PATH = series_cache_path()
SERIES_MAP_PATH = series_map_path()

STORE_SEARCH_URL = (
    "https://store.steampowered.com/search/results/"
    "?query&start=0&count=100&sort_by=_ASC&infinite=1&json=1&cc=CN&l=schinese"
    "&category1=998&franchise={name}"
)
STORE_TIMEOUT = 15.0
STORE_RETRIES = 10
STORE_RETRY_DELAY = 0.6
STORE_WORKERS = 4
# 后台渐进刷新的单次时间预算（秒）：到点停止，剩下的下次再补。
# 这个域名大陆时通时断，没预算时一轮失败重试（单名最差 15s×10）
# 能把一次后台刷新拖到十几分钟；有预算才能「渐进」而不是「卡死」。
ASYNC_REFRESH_BUDGET_SECONDS = 60.0
CACHE_TTL_DAYS = 30
STORE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GameLife/1.0 (+local)"

_ITEMKEY_RE = re.compile(r'data-ds-itemkey="App_(\d+)"')
_TITLE_TAG_RE = re.compile(r'<span class="title">(.*?)</span>', re.S)
# 这些条目虽然带 App_ itemkey，但不是「作品本体」，不计入系列成员
_NOT_A_TITLE = (
    "demo", "playtest", "试玩", "体验版", "测试版", "benchmark", "性能测试",
    "soundtrack", "原声", "season pass", "季票", "expansion pass",
    "dedicated server", "sdk",
)

# 这些是**真实存在的游戏系列**，但 Valve 在官方商店里**没有给它们建 franchise 页**
# （实测：这些作品详情页的「系列:」区块为空，``franchise=`` 参数会静默退化成全文搜索
# 并返回 0 条）。它们的成员改由本机 appinfo 标注 + 手工表提供，不再浪费联网重试。
NO_STORE_FRANCHISE = frozenset({
    "portal", "counter-strike", "team fortress", "left 4 dead",
    "mount & blade", "command & conquer",
})

_CACHE_WRITE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_REFRESH_THREAD: threading.Thread | None = None


def _cache_file(path=None) -> Path:
    """缓存文件位置：显式传入优先，缺省用真实 data 目录下的权威缓存。"""
    return Path(path) if path else series_cache_path()


def _today_iso() -> str:
    return date.today().isoformat()


def _read_cache_file(path=None) -> dict:
    try:
        with open(_cache_file(path), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _entry_stale(entry: object, *, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    """缓存条目是否该重新抓：无记录 / 日期不可解析 / 超过 ttl_days / **成员为空**。

    「成员为空」也算过期（实测踩过）：早期版本会把「名字不是有效 franchise slug、
    官方返回 0 条」误写成空成员；若把这种条目当成「新鲜」，它会在 ttl 内一直
    挡住重抓，该系列 30 天都拿不到数据。让空成员参与重抓即可自愈。
    """
    if not isinstance(entry, dict):
        return True
    if not (entry.get("members") or []):
        return True
    stamp = str(entry.get("updated") or "").strip()
    try:
        when = date.fromisoformat(stamp[:10])
    except ValueError:
        return True
    return (date.today() - when).days > ttl_days


def load_series_cache(path=None) -> dict[str, list[int]]:
    """只读缓存 ``{官方系列名: [appid, ...]}``；缺失/损坏返回 {}（**不联网**）。"""
    raw = _read_cache_file(path).get("series")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[int]] = {}
    for name, entry in raw.items():
        key = str(name).strip()
        if not key:
            continue
        members = entry.get("members") if isinstance(entry, dict) else entry
        ids: set[int] = set()
        for item in members or []:
            value = item[0] if isinstance(item, (list, tuple)) and item else item
            appid = _as_appid(value)
            if appid > 0 and not non_game.is_non_game(appid):
                ids.add(appid)
        if ids:
            result[key] = sorted(ids)
    return result


def load_series_cache_titles(path=None) -> dict[int, str]:
    """缓存里的**作品标题** ``{appid: 标题}``；只用于给「查不到名字」的作品兜底。"""
    raw = _read_cache_file(path).get("series")
    if not isinstance(raw, dict):
        return {}
    titles: dict[int, str] = {}
    for entry in raw.values():
        members = entry.get("members") if isinstance(entry, dict) else entry
        for item in members or []:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            appid = _as_appid(item[0])
            title = str(item[1] or "").strip()
            if appid > 0 and title and appid not in titles:
                titles[appid] = title
    return titles


def series_cache_age_days(path=None, today: date | None = None) -> float | None:
    """缓存距今多少天；无缓存 / 日期不可解析返回 None。"""
    meta = _read_cache_file(path).get("_meta")
    stamp = str((meta or {}).get("updated") or "").strip() if isinstance(meta, dict) else ""
    if not stamp:
        return None
    try:
        when = date.fromisoformat(stamp[:10])
    except ValueError:
        return None
    return float(((today or date.today()) - when).days)


def series_cache_is_stale(path=None, *, ttl_days: int = CACHE_TTL_DAYS) -> bool:
    """缓存缺失或超过 ``ttl_days`` 天即视为过期。"""
    age = series_cache_age_days(path)
    return age is None or age > ttl_days


def _http_get_json(url: str, *, timeout: float = STORE_TIMEOUT,
                   tries: int = STORE_RETRIES) -> dict | None:
    """带重试的 GET，只用标准库；失败返回 None，**绝不抛异常**。"""
    attempts = max(1, int(tries))
    for index in range(attempts):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": STORE_UA, "Accept-Language": "zh-CN,zh"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", "replace")
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else None
        except Exception:
            if index + 1 < attempts:
                time.sleep(STORE_RETRY_DELAY)
    return None


def parse_franchise_html(results_html: object) -> list[tuple[int, str]]:
    """从官方检索结果的 ``results_html`` 解出**作品本体** ``[(appid, 标题), ...]``。

    只认 ``data-ds-itemkey="App_<appid>"``（组合包是 ``Sub_``），再按标题关键词
    剔除试玩 / 性能测试 / 原声等非作品条目；同一 appid 只保留一次。
    """
    rows: list[tuple[int, str]] = []
    seen: set[int] = set()
    for chunk in str(results_html or "").split("<a href="):
        match = _ITEMKEY_RE.search(chunk)
        if not match:
            continue
        appid = _as_appid(match.group(1))
        if appid <= 0 or appid in seen or non_game.is_non_game(appid):
            continue
        found = _TITLE_TAG_RE.search(chunk)
        title = unescape(re.sub(r"\s+", " ", found.group(1))).strip() if found else ""
        lowered = title.lower()
        if any(bad in lowered for bad in _NOT_A_TITLE):
            continue
        seen.add(appid)
        rows.append((appid, title))
    return rows


def fetch_franchise_members(name: str, *, timeout: float = STORE_TIMEOUT,
                            tries: int = STORE_RETRIES) -> list[tuple[int, str]] | None:
    """联网抓某个官方系列的全部作品；网络不可用返回 None（与「确实一部都没有」区分）。"""
    url = STORE_SEARCH_URL.format(name=urllib.parse.quote(str(name)))
    payload = _http_get_json(url, timeout=timeout, tries=tries)
    if payload is None:
        return None
    return parse_franchise_html(payload.get("results_html"))


def _refresh_targets(names=None) -> list[str]:
    """要抓的系列名：本机官方 franchise 名 + 手工中文系列名反查出的英文名。"""
    if names is not None:
        return sorted({str(item).strip() for item in names if str(item).strip()})
    wanted: set[str] = set()

    def _want(name: object) -> None:
        key = str(name or "").strip()
        lowered = key.lower()
        if not key or lowered in NOISE_SERIES or lowered in NO_STORE_FRANCHISE:
            return
        wanted.add(key)

    try:
        from core import appinfo
        for name in (appinfo.franchise_index(games_only=True) or {}):
            _want(name)
    except Exception:
        pass
    try:
        reverse: dict[str, str] = {}
        for english, chinese in load_official_aliases().items():
            reverse.setdefault(str(chinese).strip(), str(english).strip())
        for entry in load_series_map().values():
            _want(reverse.get(str((entry or {}).get("series") or "").strip()))
    except Exception:
        pass
    return sorted(wanted)


def _write_cache_file(path, series: dict, *, updated: str | None = None,
                      partial: bool = False) -> None:
    """写缓存（原子）。

    ``updated`` 传 ``None`` = 写今天（完整刷新）；传其它值（含空串）=原样保留，
    用于「被时间预算截断」的半截刷新——它不能把未抓全的缓存伪造成「新鲜」，
    否则后台渐进刷新（:func:`maybe_refresh_async`）会以为不用补而跳过。
    ``partial=True`` 会在 ``_meta`` 里留一个可查的标记。
    """
    meta = {
        "purpose": "官方商店 franchise 检索结果的本地缓存（游戏族谱的权威成员目录）。",
        "source": ("https://store.steampowered.com/search/results/"
                   "?franchise=<系列名>&category1=998&infinite=1&json=1&cc=CN&l=schinese"),
        "parsing": ("只取 data-ds-itemkey=\"App_<appid>\" 的真作品；"
                    "剔除 Sub_ 组合包与试玩/原声/性能测试等条目。"),
        "fallback": ("取不到网络时 engine.series_tree 退回本机 appinfo.vdf 的 "
                     "官方 franchise 标注（离线兜底，覆盖不全）。"),
        "updated": _today_iso() if updated is None else str(updated),
        "ttl_days": CACHE_TTL_DAYS,
    }
    if partial:
        meta["partial"] = True
        meta["partial_note"] = ("上次刷新在时间预算内被截断，"
                                "还有系列没抓到；下次刷新会继续补。")
    payload = {"_meta": meta, "series": series}
    target = _cache_file(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _CACHE_WRITE_LOCK:
            tmp = target.with_name(target.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
            tmp.replace(target)
    except OSError as exc:  # 缓存写不进去也不该影响主流程
        logger.warning("系列缓存写入失败：%s", exc)


def _known_members(name: str) -> set[int]:
    """本机 appinfo 里该系列的成员（用于校验联网结果是否真是这个系列）。"""
    try:
        from core import appinfo
        return {_as_appid(a) for a in (appinfo.franchise_index().get(name) or ())}
    except Exception:
        return set()


def _fetch_verified(name: str, *, timeout: float, tries: int,
                    known: set[int] | None = None) -> list[tuple[int, str]] | None:
    """抓一个系列并**校验**结果；不可信时返回 None（与「确实没有作品」区分）。

    校验为什么必要（实测）：``franchise=<名字>`` 参数只在名字确实是 Valve 官方
    franchise slug 时生效；名字对不上时官方**静默退化成全文搜索**，返回一批
    风马牛不相及的游戏（实测 ``franchise=Portal`` 返回 6 个 Portal 主题的独立小游戏，
    ``franchise=Counter-Strike`` 直接返回 0 条）。因此：
      * 返回 0 条 —— 不写入缓存（否则会把「名字无效」误当成「这个系列没有作品」）；
      * 已知成员（本机 appinfo 的同名系列成员）一个都没出现在结果里 —— 同样不可信。
    两种情况下都退回本机 appinfo 的离线标注。
    """
    rows = fetch_franchise_members(name, timeout=timeout, tries=tries)
    if rows is None:
        return None
    if not rows:
        return None
    if known:
        got = {appid for appid, _ in rows}
        if not (known & got):
            return None
    return rows


def refresh_series_cache(names=None, *, path=None, force: bool = False,
                         workers: int = STORE_WORKERS, timeout: float = STORE_TIMEOUT,
                         tries: int = STORE_RETRIES, rounds: int = 2,
                         deadline_seconds: float | None = None,
                         on_progress=None) -> dict:
    """刷新权威系列目录缓存，返回
    ``{"updated", "fetched", "kept", "failed", "unverified", "deferred"}``。

    ``names`` 缺省取「本机官方 franchise 名 + 手工表反查英文名」；未过期且
    ``force=False`` 时跳过，避免无谓联网。这个域名大陆时通时断，所以：

      * 分 ``rounds`` 轮抓取，只重试上一轮失败的（多一点耐心，少留空洞）；
      * 抓取失败的系列**保留缓存原值**，绝不写入空结果；
      * 结果校验不通过的（见 :func:`_fetch_verified`）记入 ``unverified``，
        由离线 appinfo 兜底，不污染缓存。

    ``deadline_seconds``（可选）：整个阶段的**总时间预算**。到点立刻不再开新批次
    （在飞的请求最多再等一个 ``timeout``），本轮已经抓到的照常写进缓存，
    没轮到的系列记进 ``deferred`` 留给下次刷新（见 :func:`maybe_refresh_async` 与
    ``webapp.api`` 的自动补齐）。被截断时缓存不会被伪造成「新鲜」
    （``_meta.updated`` 保留原值，无原值则留空），并记 ``_meta.partial = True``。
    """
    cache_payload = _read_cache_file(path)
    stored = cache_payload.get("series")
    stored = dict(stored) if isinstance(stored, dict) else {}
    previous_meta = cache_payload.get("_meta")
    previous_updated = (previous_meta or {}).get("updated") if isinstance(previous_meta, dict) else None

    targets = _refresh_targets(names)
    pending = [n for n in targets if force or _entry_stale(stored.get(n))]
    fetched: list[str] = []
    failed: list[str] = []
    unverified: list[str] = []
    deferred: list[str] = []

    try:
        budget = float(deadline_seconds) if deadline_seconds is not None else None
    except (TypeError, ValueError):
        budget = None
    started = time.monotonic()

    def _out_of_time() -> bool:
        return budget is not None and (time.monotonic() - started) >= budget

    try:
        batch_size = max(1, int(workers))
    except (TypeError, ValueError):
        batch_size = STORE_WORKERS

    queue = list(pending)
    for round_index in range(max(1, int(rounds))):
        if not queue:
            break
        retry: list[str] = []
        # 按批取：每批开始前先看预算，到点就不再发新请求（总耗时≈预算+一个 timeout）。
        while queue:
            if _out_of_time():
                deferred.extend(queue)
                queue = []
                break
            batch, queue = queue[:batch_size], queue[batch_size:]
            known_by_name = {name: _known_members(name) for name in batch}
            with ThreadPoolExecutor(max_workers=max(1, min(batch_size, len(batch)))) as pool:
                results = pool.map(
                    lambda item: (item, _fetch_verified(
                        item, timeout=timeout, tries=tries, known=known_by_name.get(item))),
                    batch,
                )
                for name, rows in results:
                    if rows is None:
                        retry.append(name)
                        continue
                    stored[name] = {
                        "updated": _today_iso(),
                        "total_count": len(rows),
                        "members": [[appid, title] for appid, title in rows],
                    }
                    fetched.append(name)
                    if on_progress:
                        on_progress(name, len(rows))
        queue = retry

    # 走到这里还剩下的：要么网络一直不通（failed），要么名字不是有效 franchise slug
    # （unverified，实测如 Portal / Counter-Strike / Mount & Blade）。预算用完了就不再多打一次探测请求。
    for name in list(queue):
        if _out_of_time():
            deferred.append(name)
            continue
        rows = fetch_franchise_members(name, timeout=timeout, tries=1)
        if rows is not None and not rows:
            unverified.append(name)      # 官方明确返回 0 条 -> 名字无效
        else:
            failed.append(name)          # 网络问题或校验不通过

    # 目标里本轮未处理的（缓存仍新鲜的）作为 kept 回报
    kept = [n for n in targets if n not in pending]
    partial = bool(deferred)
    # 缓存里已存在、但本轮不在目标里的系列（如游戏已从本机库移除）保持原样，
    # 避免"目标缩小"把历史缓存洗掉；抓取失败的系列同样保留旧值。
    _write_cache_file(
        path, stored,
        updated=(previous_updated if partial and previous_updated else ("" if partial else None)),
        partial=partial)
    return {"updated": _today_iso(), "fetched": fetched, "kept": kept,
            "failed": failed, "unverified": unverified, "deferred": deferred}


def _network_disabled() -> bool:
    """测试 / 离线环境不自动联网：显式开关，或正跑在 pytest 里。"""
    if os.environ.get("GAMELIFE_SERIES_NO_NETWORK"):
        return True
    return "pytest" in sys.modules


def maybe_refresh_async(names=None, **kwargs) -> bool:
    """缓存过期时**后台**刷新一次（每进程最多一个守护线程），立即返回、不阻塞页面。

    单次预算 ``ASYNC_REFRESH_BUDGET_SECONDS``：到点就停，缓存保持「未完成」，
    下次渲染系关系图时接着补——这就是「渐进式刷新」，而不是一次性打满网络。
    """
    kwargs.setdefault("deadline_seconds", ASYNC_REFRESH_BUDGET_SECONDS)
    global _REFRESH_THREAD
    if _network_disabled() or not series_cache_is_stale():
        return False
    with _REFRESH_LOCK:
        if _REFRESH_THREAD is not None and _REFRESH_THREAD.is_alive():
            return False
        thread = threading.Thread(
            target=_background_refresh, args=(names, kwargs),
            name="series-cache-refresh", daemon=True)
        _REFRESH_THREAD = thread
        thread.start()
        return True


def _background_refresh(names, kwargs) -> None:
    try:
        refresh_series_cache(names, **kwargs)
    except Exception:  # 后台任务绝不能让面板崩掉
        logger.debug("系列缓存后台刷新失败", exc_info=True)


def _as_appid(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def load_series_map(path: str | Path | None = None) -> dict[int, dict[str, str]]:
    """读取**手工补充**系列映射：dict[int(appid), {series, name}]；缺失/损坏返回 {}。"""
    config_path = Path(path) if path else series_map_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[int, dict[str, str]] = {}
    for key, value in data.items():
        if str(key).startswith("_") or not isinstance(value, dict):
            continue
        appid = _as_appid(key)
        if appid <= 0:
            continue
        result[appid] = {
            "series": str(value.get("series", "")).strip(),
            "name": str(value.get("name", f"appid {appid}")).strip(),
        }
    return result


def load_official_aliases(path: str | Path | None = None) -> dict[str, str]:
    """读取手工表里的 ``_official_aliases``：{官方英文系列名: 中文显示名}。"""
    config_path = Path(path) if path else series_map_path()
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("_official_aliases") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip(): str(v).strip()
            for k, v in raw.items() if str(k).strip() and str(v).strip()}


def merge_series_catalogs(*catalogs: dict | None) -> dict[str, list[int]]:
    """把多份系列目录按**系列名并集**合并成一份 ``{系列名: [appid, ...]}``。

    纯函数、不联网、可单测。用于把「官方商店缓存」（权威、覆盖全）与
    「本机 appinfo 标注」（离线兜底、覆盖窄但含已下架作品）合到一起：
    同一个系列名取成员并集，非游戏条目（``core.non_game``）一律剔除，空系列不进结果。
    """
    merged: dict[str, set[int]] = {}
    for catalog in catalogs:
        if not isinstance(catalog, dict):
            continue
        for name, appids in catalog.items():
            key = str(name or "").strip()
            if not key:
                continue
            bucket = merged.setdefault(key, set())
            for raw in appids or ():
                appid = _as_appid(raw)
                if appid > 0 and not non_game.is_non_game(appid):
                    bucket.add(appid)
    return {name: sorted(ids) for name, ids in merged.items() if ids}


def official_series(steam_path: str | None = None, *, games_only: bool = True,
                    local: dict | None = None, cache: dict | None = None) -> dict[str, list[int]]:
    """官方系列目录 ``{系列名: [appid, ...]}``（**权威优先**）；都不可用返回 {}。

    合并两份来源（见 :func:`merge_series_catalogs`）：
      1. ``data/series_official.json``——官方商店 ``franchise=`` 检索的落盘缓存，
         覆盖该系列**全部在售**作品，是权威主源；
      2. 本机 ``appinfo.vdf`` 的官方 franchise 标注——离线兜底，覆盖面窄
         （本机 1801 个 app），但能补上商店已下架的老作品（实测 ``Devil May Cry``）。
    任何一侧缺失都不影响另一侧，绝不抛异常。``local`` / ``cache`` 供测试注入。
    """
    if cache is None:
        cache = load_series_cache()
        # 无缓存或缓存过期时后台静默刷新：守护线程、立即返回、绝不阻塞页面；
        # 正在跑 pytest 或设了 GAMELIFE_SERIES_NO_NETWORK 时自动跳过（不出网）。
        maybe_refresh_async()
    if local is None:
        try:
            from core import appinfo
            local = appinfo.franchise_index(steam_path, games_only=games_only) or {}
        except Exception:
            local = {}
    return merge_series_catalogs(local, cache)


def _title_key(text: object) -> str:
    """把名称归一化成可比较的紧凑形式（用于判断「作品名 == 系列名」）。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(text or "").lower())


def _merge_identical_franchises(
    official: dict[str, list[int]],
    game_names: dict[int, str] | None = None,
) -> dict[str, str]:
    """把「appid 集合完全相同」的官方 franchise 归并成一条。

    官方目录里偶尔会把系列名与作品名标成两条 franchise（如 ``Kingdom`` 与
    ``Kingdom Two Crowns``，或 ``The Elder Scrolls`` 与 ``Skyrim``），集合相同即同一个
    系列的两种叫法。代表名选取顺序（依次比较）：
      1. 「不是作品名」优先：名称与其任一成员作品名完全相同者，是作品名而非系列名；
      2. 「是作品名开头」优先：真系列名往往出现在成员标题的开头（``The Elder Scrolls``
         之于 "The Elder Scrolls V: Skyrim"），而具体作品名不会；
      3. 更短的名字优先（避免选到啰嗦的公司全称）；
      4. 最后按字典序，保证结果稳定。

    返回 ``{被归并的名称: 代表名称}``；无需归并时返回 {}。
    """
    by_set: dict[frozenset[int], list[str]] = {}
    for name, appids in official.items():
        members = frozenset(a for a in appids if not non_game.is_non_game(a))
        if not members:
            continue
        by_set.setdefault(members, []).append(name)

    rename: dict[str, str] = {}
    for members, names in by_set.items():
        if len(names) < 2:
            continue
        titles = [_title_key((game_names or {}).get(a, "")) for a in members]
        title_set = {t for t in titles if t}

        def rank(name: str):
            key = _title_key(name)
            is_work = key in title_set                      # 名称本身就是一个作品名
            anchors = any(t.startswith(key) and t != key for t in titles)  # 是作品名的开头
            return (1 if is_work else 0, 0 if anchors else 1, len(name), name)

        representative = min(names, key=rank)
        for name in names:
            if name != representative:
                rename[name] = representative
    return rename


def _compose_groups(
    official: dict[str, list[int]],
    manual: dict[int, dict[str, str]],
    aliases_cfg: dict[str, str],
    game_names: dict[int, str] | None = None,
) -> tuple[dict[str, dict[int, str]], dict[str, str]]:
    """合成系列分组：官方 franchise 为主源、手工表为补充源，并按**统一展示名**归并同名系列。

    为什么要归并：官方 franchise 是英文名（``Devil May Cry``），手工表键是中文名（``鬼泣``），
    同一个系列在两边都有时会生成两条；另外官方目录里还可能同时出现系列名（``Kingdom``）与
    作品名（``Kingdom Two Crowns``）两条 franchise。因此这里以「展示名」为分组键，
    映射到同一展示名的来源一律合并成一条。

    展示名取值规则（中文优先）：
      * 官方英文名 -> ``aliases_cfg``（``_official_aliases``）里的中文别名，缺失则保留英文；
      * 手工系列中文名 -> ``aliases_cfg`` 里的别名，或它唯一归属的官方系列之展示名，否则用自身；
      * 无法确定中文名的保留英文，但结果展示名保证唯一。

    参数 ``game_names``：``{appid: 展示名}``，用于识别「作品名当系列名」的官方 franchise。

    返回 ``(groups, official_names)``：
      * ``groups``：``{展示名: {appid: 手工展示名}}``（同 appid 天然去重）；
      * ``official_names``：``{展示名: 官方英文系列名}``（仅当与展示名不同时记录，供展示层参考）。
    """
    # 先归并「集合完全相同的官方 franchise」（系列名 vs 作品名），再按展示名分组
    rename = _merge_identical_franchises(official, game_names)
    official = {rename.get(name, name): appids for name, appids in official.items()} if rename else official

    appid_official: dict[int, set[str]] = {}
    for name, appids in official.items():
        for appid in appids:
            appid_official.setdefault(appid, set()).add(name)

    manual_series: dict[str, list[int]] = {}
    for appid, entry in manual.items():
        series_name = str(entry.get("series", "")).strip()
        if appid > 0 and series_name:
            manual_series.setdefault(series_name, []).append(appid)

    aliases_cfg = {k: str(v).strip() for k, v in (aliases_cfg or {}).items() if k and str(v).strip()}

    def official_display(name: str) -> str:
        """官方英文系列名 -> 展示名（中文优先）。"""
        return aliases_cfg.get(name) or name

    # 手工系列 -> 它唯一归属的官方系列（仅当所有已覆盖成员指向同一个官方系列时才认定）
    auto_alias: dict[str, str] = {}
    for manual_name, members in manual_series.items():
        covered = [a for a in members if a in appid_official]
        if not covered:
            continue
        common = set.intersection(*[appid_official[a] for a in covered])
        if len(common) == 1:
            candidate = next(iter(common))
            if candidate != manual_name:
                auto_alias[manual_name] = candidate

    def manual_display(name: str) -> str:
        """手工系列中文名 -> 展示名（优先跟随它所属的官方系列）。"""
        mapped = auto_alias.get(name)
        if mapped:
            return official_display(mapped)
        return aliases_cfg.get(name) or name

    groups: dict[str, dict[int, str]] = {}
    official_names: dict[str, str] = {}

    # 1) 官方系列（主源）：收录该系列全部作品（含本机未拥有的，用来表达"还差哪几部"）
    for name, appids in official.items():
        display = official_display(name)
        bucket = groups.setdefault(display, {})
        for appid in appids:
            if non_game.is_non_game(appid):
                continue
            bucket.setdefault(appid, "")
        if display != name:
            official_names.setdefault(display, name)

    # 2) 手工系列（补充源）：并入统一展示名对应的分组
    #    只接收「官方没有标注」或「官方标注的正是同一展示名」的成员，
    #    这样既能补齐官方缺的作品，又不会把同一 appid 塞进别的系列造成跨系列重复。
    for manual_name, members in manual_series.items():
        display = manual_display(manual_name)
        target = groups.setdefault(display, {})
        for appid in members:
            if non_game.is_non_game(appid):
                continue
            covered_by = {official_display(oname) for oname in appid_official.get(appid, ())}
            if covered_by and display not in covered_by:
                continue
            mapped = manual[appid].get("name", "")
            if not target.get(appid):
                target[appid] = mapped

    return groups, official_names


def _resolve_ownership_kinds(
    steam_path: str | None,
    account_id: str | None,
    appids: Iterable[int],
    playtimes: dict,
    ownership_map: dict | None = None,
) -> dict[int, str] | None:
    """``appid -> 归属类别``（见 :mod:`core.ownership`）；读不到授权票返回 ``None``（降级）。

    * ``ownership_map`` 显式传入（测试/离线）时直接用它，不再碰文件；
    * 没有 ``steam_path`` / ``account_id`` 时返回 ``None``（铁律：不猜、判空比误判更糟）；
    * ``ticket_flags`` 返回 ``None``（vdf 读不到 / 合流库的 ``all`` 不是真实账号目录）
      时返回 ``None`` —— 否则 ``classify`` 会把整批判成 ``free``，等于谎报「都没有」；
    * ``core.ownership`` 不可用或任何异常一律返回 ``None``；
    * 判定范围只有**系列成员** appid，逐条出网/读盘的操作一次也不做。

    ``playtimes`` 是 ``{appid: 分钟}``，决定「家庭共享 / 免费体验」是「玩过」还是「没玩过」
    （与 ``engine.stats`` / ``engine.classifier`` 的「已玩」同口径 ``playtime > 0``）。
    """
    if ownership_map is not None:
        return {_as_appid(k): str(v) for k, v in ownership_map.items()}
    path = str(steam_path or "").strip()
    account = str(account_id or "").strip()
    if not path or not account:
        return None
    try:
        from core import ownership

        # 合流库（account_id="all"）：它不是真实账号目录，票要**逐成员账号**取并取并集
        # （一款游戏只要任一成员真正拥有，在合流库里就算「拥有」）。
        member_ids = _merge_member_ids(path, account)
        if member_ids is not None:
            return _classify_union(path, member_ids, appids, playtimes, ownership)

        # 先探一次票面：读不到就是**信息不足**，必须降级（返回 None）而不是让 classify
        # 把整批判成 free —— 那等于谎报「这些游戏都不属于你」。
        if ownership.ticket_flags(path, account) is None:
            logger.info("族谱读不到授权票（%s / %s），降级为「在库里即拥有」", path, account)
            return None
        return ownership.classify(path, account, list(appids), playtimes)
    except Exception as exc:  # 缺模块 / 坏环境 / 读盘失败：降级为「在库里即拥有」
        logger.warning("族谱归属判定失败，已降级为「在库里即拥有」：%s", exc)
        return None


def _merge_member_ids(steam_path: str, account_id: str) -> list[str] | None:
    """若 ``account_id`` 是合流库 id 则返回成员账号列表；否则 ``None``（普通账号）。"""
    try:
        from core import merge

        if not merge.is_merge_id(account_id):
            return None
        return [str(item) for item in merge.resolve_member_ids(steam_path) if str(item).strip()]
    except Exception as exc:  # 合流模块不可用 / 读不到 accounts.json：按普通账号处理
        logger.debug("合流成员解析失败（按普通账号处理）：%s", exc)
        return None


def _classify_union(
    steam_path: str,
    account_ids: list[str],
    appids: Iterable[int],
    playtimes: dict,
    ownership: Any,
) -> dict[int, str] | None:
    """逐成员账号判定后取**并集**（任一成员 ``OWNED`` 即算拥有）；全部读不到返回 ``None``。"""
    ids = list(appids)
    merged: dict[int, str] = {}
    available = False
    for member in account_ids:
        try:
            if ownership.ticket_flags(steam_path, member) is None:
                continue                        # 这个成员读不到票：跳过，但不算「信息不足」
            available = True
            kinds = ownership.classify(steam_path, member, ids, playtimes)
        except Exception as exc:
            logger.debug("合流成员 %s 归属判定失败（跳过）：%s", member, exc)
            continue
        for appid, kind in kinds.items():
            # 优先级：真拥有 > 其余（玩过 / 免费等由上层按 playtime 判定）
            if appid not in merged or kind == _OWNED:
                merged[appid] = kind
    return merged if available else None


def _authoritative_owned_appids(
    steam_path: str | None, account_id: str | None
) -> "frozenset[int] | None":
    """**权威「确实拥有」集合**（T22 线）；读不到票面 / 拿不到清单返回 ``None``（降级）。

    来源是 :func:`core.ownership.authoritative_owned_appids`：票面 ``flag == 0`` ∪
    客户端 ``AccountOwnedApps`` ∪ **Steam 客户端实时家庭清单**。合流库取成员并集
    （任一成员真拥有即算拥有，与 :func:`_classify_union` 同一口径）。

    拿到它之后，三态判定就不再需要「免费 / 在库里即拥有」这类兜底 —— 这正是
    用户反馈的「命令与征服 / 杀手系列把家庭库算成自有」的根治办法。
    """
    path = str(steam_path or "").strip()
    account = str(account_id or "").strip()
    if not path or not account:
        return None
    try:
        from core import ownership

        member_ids = _merge_member_ids(path, account)
        if member_ids is not None:
            merged: set[int] = set()
            available = False
            for member in member_ids:
                owned = ownership.authoritative_owned_appids(path, member)
                if owned is not None:
                    available = True
                    merged.update(owned)
            return frozenset(merged) if available else None
        return ownership.authoritative_owned_appids(path, account)
    except Exception as exc:                    # 缺模块 / 读盘失败：降级为历史行为
        logger.debug("权威归属读取失败（降级）：%s", exc)
        return None


def compute_series_tree(
    play_stats: dict,
    games: dict,
    series_map: dict | None = None,
    statuses: dict | None = None,
    rules: dict[str, Any] | None = None,
    today: int | None = None,
    steam_path: str | None = None,
    official: dict[str, list[int]] | None = None,
    aliases: dict[str, str] | None = None,
    min_titles: int = 2,
    account_id: str | None = None,
    ownership_map: dict | None = None,
) -> dict:
    """按系列聚合收藏完成度（官方 franchise 为主 + 手工表补充）。

    参数：
        play_stats: dict[appid, {playtime: 分钟, last_played: unix 秒}]
        games:      dict[appid, {name, ...}]
        series_map: 可选**手工补充**映射（缺省读 data/series_map.json）
        statuses:   可选 dict[appid, 状态名]
        rules:      可选规则 dict（缺省自动读 rules.json）
        today:      可选 unix 时间戳（便于测试）
        steam_path: 可选 Steam 安装目录（读取官方 appinfo 系列用）
        official:   可注入官方系列目录 ``{系列名: [appid]}``（测试/离线用）
        aliases:    可注入 ``{中文系列名: 官方英文系列名}``（测试用）
        min_titles: 少于这么多部作品的「系列」不展示（缺省 2；用户明确反馈
                    「1/1 就是没有系列、就是单独的作品」，故单一作品不算系列）
        account_id: 可选 Steam 账号 id（与 ``steam_path`` 一起用于判定归属）
        ownership_map: 可注入 ``{appid: 归属类别}``（测试用，类别见 core.ownership）

    「拥有」的唯一口径（用户反馈修正）
    ----------------------------------
    历史上这里把「在库里」当成「已拥有」，而库是**家庭共享过滤后**的集合，于是
    「家庭共享但玩过」的游戏被错标成「已拥有」、真正拥有却从没启动过的游戏反倒被
    标成「未拥有」。现在改为复用 :mod:`core.ownership`（全产品唯一口径），三态：

      * ``owned``  **确实拥有** —— ``core.ownership`` 判为 ``OWNED``（授权票
        ``flag == 0`` / 客户端自有清单 / 库索引补全），**或**判为 ``FREE`` 且条目
        确实在库里（免费游戏本来就没有授权票，在库里就是自己的一份）；
      * ``played`` **未拥有但本账号玩过**（家庭共享，``playtime > 0``）；
      * 其余为「未拥有」。

    注意 ``core.ownership`` 是**五类**（``OWNED`` / ``PLAYED_SHARED`` / ``SHARED`` /
    ``FREE`` / ``TRACE``），这里只渲染成三态：``FREE`` 与 ``OWNED`` 同属「保留」，
    所以在库里时同样算「已拥有」；``TRACE``（残留痕迹）与 ``SHARED``（共享且没玩过）
    都落在「未拥有」。

    读不到授权票（没给 ``steam_path`` / ``account_id``、或票面读不出来）时**降级**为
    历史行为「在库里即算拥有」，绝不因为信息不足把用户的库判空。

    展示层的两条硬规则
    ------------------
      * ``owned_count == 0`` 且 ``played_count == 0`` 的系列**不展示**（用户既没有、
        也没玩过的系列与他无关）；只玩过（家庭库）的系列照常展示；
      * ``total_count < min_titles`` 的系列**不展示**（只有 1 部的其实是独立作品）。
    """
    rules = rules or load_rules()
    today_ts = _to_int(today) or int(time.time())
    play_stats = play_stats or {}
    games = games or {}
    library = {a for a in (set(play_stats) | set(games)) if not non_game.is_non_game(a)}
    status_of = _resolve_statuses(play_stats, statuses, rules, today_ts)
    playtime_minutes = {
        appid: _to_int((play_stats.get(appid) or {}).get("playtime", 0))
        for appid in play_stats
    }

    if official is None:
        official = official_series(steam_path)
    official = official or {}

    manual = series_map if isinstance(series_map, dict) else load_series_map()
    manual = {
        _as_appid(k): {"series": str((v or {}).get("series", "")).strip(),
                       "name": str((v or {}).get("name", "")).strip()}
        for k, v in (manual or {}).items()
        if not str(k).startswith("_") and isinstance(v, dict) and _as_appid(k) > 0
    }

    if aliases is None:
        aliases = load_official_aliases()

    # 展示名先行备好：既要给系列成员命名，也用于识别「作品名当系列名」的官方 franchise
    all_appids = {appid for bucket in official.values() for appid in bucket}
    all_appids.update(manual)
    names: dict[int, str] = {}
    # 本地官方中文名优先（含本机未拥有、但库里已缓存中文名的系列成员）
    for appid in all_appids:
        value = display.lookup_name(appid, games.get(appid) or {})
        if value:
            names[appid] = value
    for appid in all_appids:
        if appid in names:
            continue
        value = str((games.get(appid) or {}).get("name") or "").strip()
        if value:
            names[appid] = value
    for appid, entry in manual.items():
        if appid not in names and entry.get("name"):
            names[appid] = entry["name"]
    missing = [a for a in all_appids if a not in names]
    if missing:
        # 商店缓存里的标题按 schinese 抓的（如「全面战争：三国」），优先于英文名
        cached_titles = load_series_cache_titles()
        for appid in missing:
            value = str(cached_titles.get(appid) or "").strip()
            if value:
                names[appid] = value
        still = [a for a in missing if a not in names]
        if still:
            try:
                from core import appinfo
                names.update(appinfo.app_names(still, steam_path))
            except Exception:
                pass

    groups, official_names = _compose_groups(official, manual, aliases or {}, names)

    # 归属判定：只对**系列成员** appid 判定一次（`core.ownership` 是唯一口径；
    # 读不到授权票时返回 None -> 退回「在库里即拥有」的历史行为）。
    member_appids = {appid for bucket in groups.values() for appid in bucket}
    kinds = _resolve_ownership_kinds(
        steam_path, account_id, member_appids, playtime_minutes, ownership_map
    )
    # T22 权威归属：拿到时三态只认它（不再需要任何「免费 / 在库里即拥有」的兜底）。
    # 显式注入 ownership_map（测试 / 离线）时绝不额外碰实时源。
    authoritative = (None if ownership_map is not None
                     else _authoritative_owned_appids(steam_path, account_id))

    series_list: list[dict] = []
    singletons_hidden = 0
    for series in sorted(groups):
        if series.strip().lower() in NOISE_SERIES:
            continue
        entries = sorted(groups[series].items())
        # 单一作品不是「系列」（用户明确反馈「1/1 就是单独的作品」）：只有 1 部时
        # 直接不展示，避免族谱里出现「1/1」这种等于没系列的条目。
        if len(entries) < max(1, int(min_titles)):
            if entries:
                singletons_hidden += 1
            continue
        games_rows = []
        total_playtime = 0
        completed = 0
        for appid, mapped_name in entries:
            if authoritative is not None:
                # T22 权威口径：**真拥有**才算「已拥有」（与玩没玩过无关）；
                # 未拥有但本账号玩过 -> 「玩过（家庭库）」；其余 -> 「未拥有」。
                owned = appid in authoritative
                played = (not owned) and _to_int(playtime_minutes.get(appid, 0)) > 0
            elif kinds is None:                     # 降级：沿用「在库里即拥有」
                owned = appid in library
                played = False
            elif kinds.get(appid) == _OWNED:
                owned = True                        # 确实拥有（授权票 flag == 0）
                played = False
            elif kinds.get(appid) == _FREE and appid in library:
                # 免费游戏 / 免费开玩（``core.ownership`` 的 ``FREE`` 类）**只要在库里
                # 就是本账号正儿八经的一份**，与「家庭共享但玩过」不是一回事。
                #
                # 历史 bug（用户反馈）：这个分支过去把 ``kinds != OWNED`` 一律当「未真拥有」，
                # 于是神之浩劫 / 饥荒 / Dota 霸业这类免费游戏只要玩过就被标成
                # 「玩过（家庭库）」——它们其实没有授权票（免费游戏本来就没有），
                # 但确实在自己库里。判据用「在库里」（``play_stats | games`` 且非非游戏条目）
                # 与 ``OWNED`` 分支互斥，家庭共享条目走下面 else 分支不受影响。
                owned = True
                played = False
            else:
                # 未真拥有：只要本账号真玩过（playtime > 0，与全产品「已玩」同口径）
                # 就标成「玩过」——覆盖家庭共享与免费体验两类。
                owned = False
                played = _to_int(playtime_minutes.get(appid, 0)) > 0
            active = owned or played
            minutes = playtime_minutes.get(appid, 0) if active else 0
            status = status_of.get(appid, "") if active else ""
            if active:
                total_playtime += minutes
                if status == "通关":
                    completed += 1
            games_rows.append({
                "appid": appid,
                # 中文名（本地库 / 商店缓存 / appinfo）优先，最后才是 appid N
                "name": names.get(appid) or display.lookup_name(appid)
                        or mapped_name or f"appid {appid}",
                "owned": owned,
                "played": played,
                "status": status,
                "playtime_minutes": minutes,
                "playtime_hours": round(minutes / 60, 2),
            })
        owned_count = sum(1 for g in games_rows if g["owned"])
        played_count = sum(1 for g in games_rows if g["played"])
        if owned_count <= 0 and played_count <= 0:
            continue  # 用户既没拥有、也没玩过的系列不展示
        item = {
            "series": series,
            "owned_count": owned_count,
            "played_count": played_count,
            "total_count": len(games_rows),
            "completed_count": completed,
            "playtime_minutes": total_playtime,
            "playtime_hours": round(total_playtime / 60, 2),
            "games": games_rows,
        }
        official_name = official_names.get(series)
        if official_name and official_name != series:
            item["official_name"] = official_name
        series_list.append(item)

    series_list.sort(key=lambda s: (-s["playtime_minutes"], -s["owned_count"], s["series"]))
    return {
        "series_count": len(series_list),
        "total_titles": sum(s["total_count"] for s in series_list),
        "series": series_list,
        # 诊断用：被当成「独立作品」而不再展示的单作品条目数（应为 0，见验收）
        "singletons_hidden": singletons_hidden,
        "single_title_series": sum(1 for s in series_list if s["total_count"] == 1),
        # 诊断用：一部都没真拥有、只靠「玩过（家庭库）」进榜的系列数
        "unowned_series": sum(1 for s in series_list if s["owned_count"] == 0),
    }


def _safe_print(text: str) -> None:
    """CLI 输出兜底：控制台编码（如 Windows GBK）装不下的字符（™ / ® 等）降级为 ?，
    避免 cmd_family 直接抛 UnicodeEncodeError 崩掉。"""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def run(args: Any) -> int:
    """CLI 入口（供 main.py 调用）：args 需含 play_stats/games，
    可选 series_map/statuses/steam_path，输出族谱并返回退出码。"""
    result = compute_series_tree(
        getattr(args, "play_stats", None) or {},
        getattr(args, "games", None) or {},
        series_map=getattr(args, "series_map", None),
        statuses=getattr(args, "statuses", None),
        steam_path=getattr(args, "steam_path", None),
        account_id=getattr(args, "account_id", None),
    )
    series = result["series"]
    if not series:
        _safe_print("未匹配到任何你拥有或玩过的系列（官方 franchise 与手工补充均为空）。")
        return 0
    owned_series = sum(1 for s in series if s["owned_count"] > 0)
    played_only = len(series) - owned_series
    head = f"你拥有 {owned_series} 个系列"
    if played_only:
        head += f"；另有 {played_only} 个系列你未拥有、但在家庭库里玩过"
    _safe_print(head + "：")
    for s in series:
        suffix = f"（{s['official_name']}）" if s.get("official_name") else ""
        _safe_print(
            f"{s['series']}{suffix}：{s['owned_count']}/{s['total_count']} 部，"
            f"通关 {s['completed_count']} 部，总时 {s['playtime_hours']}h"
        )
        for g in s["games"]:
            if g["owned"]:
                # 真拥有但没状态（可能从没启动过）：如实说「未玩」，别谎报「已玩」
                mark = "通关" if g["status"] == "通关" else (
                    g["status"] or ("已玩" if g["playtime_minutes"] else "未玩")
                )
                _safe_print(f"  +-- {g['name']}  [已拥有  {g['playtime_hours']}h  {mark}]")
            elif g.get("played"):
                mark = "通关" if g["status"] == "通关" else (g["status"] or "已玩")
                _safe_print(f"  +-- {g['name']}  [玩过（家庭库）  {g['playtime_hours']}h  {mark}]")
            else:
                _safe_print(f"  +-- {g['name']}  [未拥有]")
    return 0
