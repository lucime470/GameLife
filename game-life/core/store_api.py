# -*- coding: utf-8 -*-
"""Store 元数据获取（大陆网络无加速器可用）。

数据来源（按优先级合并，逐字段取第一个非空值）：
    1. 本机 Steam ``appcache/appinfo.vdf``（离线、Steam 官方数据）——英文名、官方中文名、封面文件名；
    2. ``api.steamcmd.net``（Steam 官方 appinfo 的公开镜像，大陆直连可达）——
       英文名、多语言官方名、类型 ID、开发商、发行商、发布日期、封面；
    3. ``store.steamchina.com``（蒸汽平台官方商店 API，大陆直连可达）——官方中文名、中文类型、价格、发行日期、标签；
    4. ``store.steampowered.com``（官方商店 API，**大陆时通时断**）——元数据只做一次短超时机会性尝试；
       **价格**则走独立的 ``price_for`` / ``backfill_prices`` 路径（较长超时、独立熔断），
       与「缺名字才顺带取」彻底解耦，保证已有名字的游戏也会取价。

⚠️ ``appdetails`` 响应的**外层 key 可能是错的**（CDN 边缘实测：请求 582010 返回的外层 key
是 ``1390430``，而内层 ``data.steam_appid`` 才是 582010；5/5 命中）。**取值必须认内层**——
见 ``_appdetails_node``。历史实现只认外层 key，这正是「全库价格 0/135」的直接根因。

⚠️ 该域名在大陆还会出现**分钟级整体阻断窗口**（所有候选 IP 一起超时，而同期其它站点正常）。
所以取价必须「温柔 + 可续做」：appid 之间随机间隔、单轮有预算、拿到即落库、
失败只做**分钟级冷却**（不是 3 天负缓存）。

价格语义（三态，务必遵守）
--------------------------
    * **有价**：``price > 0``（单位：分）；
    * **免费**：``is_free`` 为真（``price`` 通常为 0）；
    * **未知**：既不免费也没有正价格——**绝不能当作免费**，也不参与性价比排序。

    历史实现把「缺失」记为 ``price = 0``，导致 179 款游戏里 174 款被误判为免费。
    现改为：取不到价格时保留 ``is_free`` 为空且 ``price`` 不为正数，由上层按「未知」处理。

对外契约（与旧版兼容，调用方无需修改）::

    fetch_metadata(appids) -> {appid: {
        "name": str, "genres": [str], "categories": [str],
        "developers": [str], "price": int, "release_date": str,
        # 以下为新增字段（旧调用方忽略即可）
        "name_cn": str, "genres_cn": [str], "publisher": str,
        "header_image": str, "source": str,
    }}

约束：
    * ``genres`` 仍是 **英文** Steam 类型名（分析引擎 / rules.json 的 genre_rules 依赖英文标签）；
      中文类型放在 ``genres_cn``，仅供显示，不参与判定。
    * 任何失败都降级为缺字段，**绝不抛异常**；单款联网预算 <= 15s，绝不 hang。
    * 增量缓存沿用 ``games`` 表（新增列由 data/schema.py 幂等迁移补齐）。
"""

from __future__ import annotations

import http.client
import shutil
import json
import os
import logging
import random
import re
import socket
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

try:  # requests 缺失时降级为「只能用本地数据」
    import requests
except ImportError:  # pragma: no cover
    requests = None

from data.schema import _is_fresh, get_connection, init_db

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


# ------------------------------------------------------------------ 运行时路径
# P1：onefile 打包后 ``__file__`` 落在 ``sys._MEIPASS``（进程退出即删），
# 写进去的汇率缓存 / 价格审计 / 百科缓存全部「看不见也留不住」。
# 因此**运行时读写**的数据文件一律由 ``data.schema.data_dir()`` 解析
# （源码模式 = game-life/data；打包模式 = exe 旁的真实 data/），
# 只读静态表先查真实目录、查不到再回退打包进 bundle 的副本。

def stable_data_dir() -> Path:
    """运行时读写目录（与 cache.db 同目录，兼容 PyInstaller onefile）。"""
    try:
        from data.schema import data_dir as _data_dir

        return Path(_data_dir())
    except Exception:                     # pragma: no cover - 极端降级
        return APP_ROOT / "data"


def readonly_data_path(name: str) -> Path:
    """只读静态表（genre_cn.json / name_cn_extra.json …）的位置。

    真实 data 目录优先；打包运行下那里可能还没有这些表，就把打包进 bundle
    的副本**铺设**过去再返回真实路径（内容只在首次缺失时复制，之后只读）。
    两份都没有时返回 bundle 路径（调用方的降级分支会按「文件不存在」处理）。
    """
    real = stable_data_dir() / name
    if real.is_file():
        return real
    bundled = APP_ROOT / "data" / name
    if not bundled.is_file():
        return bundled
    try:
        real.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundled, real)
        return real
    except Exception:
        return bundled


def price_audit_path() -> Path:
    """价格审计留档（**写入**文件，绝不能写进退出即删的 _MEIPASS）。"""
    return stable_data_dir() / "price_audit.json"

logger = logging.getLogger("game-life.store_api")

# ------------------------------------------------------------------ 配置

URL_STEAMCMD = "https://api.steamcmd.net/v1/info/{appid}"
URL_STEAMCHINA = "https://store.steamchina.com/api/appdetails?appids={appid}&l=schinese"
URL_OFFICIAL = "https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese"
# 带区域参数的官方接口：``cc=CN`` 直接返回人民币，**不必自己乘汇率**（最准）
URL_OFFICIAL_CC = "https://store.steampowered.com/api/appdetails?appids={appid}&cc={cc}&l=schinese"

# 价格 / 中文名 / 封面的**国区主源**（小黑盒，无需登录/签名/密钥；见文件末尾「小黑盒」一节）
URL_XIAOHEIHE = "https://api.xiaoheihe.cn/game/get_game_infos/pc/?appids={appids}"
XHH_BATCH_SIZE = 40            # 接口一次最多吞 40 个 appid（超了会被截断，必须分批）
XHH_BATCH_INTERVAL = 0.5       # 批与批之间的礼貌间隔（秒）
XHH_TIMEOUT = 8.0
XHH_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://www.xiaoheihe.cn/",
    "Accept": "application/json",
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GameLife/1.0 (+local)"

TIMEOUT_STEAMCMD = 8.0
TIMEOUT_STEAMCHINA = 8.0
TIMEOUT_OFFICIAL = 4.0         # 官方商店「元数据」尝试的超时（短，失败即熔断）
TIMEOUT_OFFICIAL_PRICE = 8.0   # 官方商店「价格」单次请求超时（短超时 + 快速重试，绝不死等）
PER_APP_BUDGET = 15.0          # 单款游戏联网总预算（秒）
PRICE_WORKERS = 1              # 取价线程数：**串行**。appdetails 要温柔，靠全局节流控速
DEFAULT_WORKERS = 6            # 并发线程数（对同一个镜像仍保持礼貌间隔）
REQUEST_INTERVAL = 0.15        # 每个工作线程的请求间隔（秒）

CACHE_TTL_DAYS = 30            # 命中元数据的缓存有效期
MISS_TTL_HOURS = 6             # 「什么都没抓到」的负缓存有效期（**短 TTL**，不是 3 天）

# ---------------------------------------------------------------- 价格：重试 / 熔断 / 汇率
#
# 用户要求「不同账号国家不同，价格都要统一换算成人民币」。落地顺序：
#   1) 先按 ``cc=CN`` 取价——官方**直接返回人民币**，没有任何汇率误差；
#   2) 拿不到再用账号所在区域取价，再按**当日汇率**换算成人民币；
#   3) 最后用蒸汽平台（人民币）。
#
# 大陆网络下 store.steampowered.com 时通时断（实测：同一 URL 连打 10 次全超时），
# 因此**绝不能一次失败就永久放弃**：
#   * 失败会重试多轮（见 PRICE_RETRY_ATTEMPTS），并做 DNS 污染兜底（DoH + 多 IP 直连）；
#   * 连续失败达阈值只做**冷却式熔断**，冷却期满自动恢复尝试。
PRICE_RETRY_ATTEMPTS = 2       # 单款取价「同一端点」的快速重试次数（网络层还含直连兜底）
PRICE_RETRY_SLEEP = 0.6        # 重试基础间隔（秒），逐次线性退避
PRICE_COOLDOWN_SECONDS = 900.0 # 取价源整体不可达后的冷却（15 分钟自动恢复，绝不是 3 天）
PRICE_INTERVAL_RANGE = (1.5, 2.0)      # 相邻两次 appdetails 请求的**全局**随机间隔（秒）
PRICE_ROUNDS = 4                       # 一批 appid 的默认扫描轮数（拿到即落库，剩下的下轮再试）
PRICE_ROUND_BUDGET_SECONDS = 90.0      # 单轮硬预算（到点停止，剩余留给下一轮 / 下次启动）
DEFAULT_REGION = "CN"          # 取不到账号区域时的兜底区域

# ------------------------------------------------------------------ 内置类型表

# Steam 官方类型 ID 对照表（英文 | 中文）。
#
# 权威来源（2026-09-23 实测核对）：
#   * Valve 官方商店 API ``appdetails`` 的 ``genres[].id``（逐款实测）；
#   * 本机 Steam 客户端官方本地化 ``steamui/localization/shared_schinese-json.js`` 的
#     ``Sale_default_label_35..44`` = 官方 ID 顺序表（35..44 =>
#     Action/Adventure/Casual/Indie/MMO/Racing/RPG/Simulation/Sports/Strategy）；
#   * 与本机 ``appinfo.vdf`` 中该 ID 对应作品的类型交叉验证。
#
# ⚠️ 旧实现把 4/23 **互换**（Indie<->Casual）、9/18/25/28/29 记错，且 50-60 段整段缺失，
#    于是库里冒出「类型 29」「类型 57」这类占位。下面为修正后的权威值。
STEAM_GENRES: dict[int, tuple[str, str]] = {
    1: ("Action", "动作"),
    2: ("Strategy", "策略"),
    3: ("RPG", "角色扮演"),
    4: ("Casual", "休闲"),
    9: ("Racing", "竞速"),
    18: ("Sports", "体育"),
    23: ("Indie", "独立"),
    25: ("Adventure", "冒险"),
    28: ("Simulation", "模拟"),
    29: ("Massively Multiplayer", "大型多人在线"),
    37: ("Free to Play", "免费开玩"),
    50: ("Accounting", "会计"),
    51: ("Animation & Modeling", "动画制作和建模"),
    52: ("Audio Production", "音频制作"),
    53: ("Design & Illustration", "设计和插画"),
    54: ("Education", "教育"),
    55: ("Photo Editing", "照片编辑"),
    56: ("Software Training", "软件培训"),
    57: ("Utilities", "实用工具"),
    58: ("Video Production", "视频制作"),
    59: ("Web Publishing", "网络出版"),
    60: ("Game Development", "游戏开发"),
    70: ("Early Access", "抢先体验"),
}

# 软件/工具类 ID（非游戏类型）：面板可据此标注「非游戏类型」
SOFTWARE_GENRE_IDS = frozenset({50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60})

# 运行期额外补全（data/genre_cn.json，权威缓存表；缺失/损坏时忽略）
_GENRE_EXTRA: dict[int, tuple[str, str]] = {}
_GENRE_EXTRA_LOADED = False


def _load_genre_table() -> None:
    """把 ``data/genre_cn.json`` 合并进 ``STEAM_GENRES``（幂等；任何异常都忽略）。"""
    global _GENRE_EXTRA_LOADED
    if _GENRE_EXTRA_LOADED:
        return
    _GENRE_EXTRA_LOADED = True
    try:
        path = readonly_data_path("genre_cn.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if str(key).startswith("_") or not isinstance(value, dict):
            continue
        try:
            gid = int(str(key).strip())
        except (TypeError, ValueError):
            continue
        en = str(value.get("en") or "").strip()
        cn = str(value.get("cn") or "").strip()
        if en and cn:
            _GENRE_EXTRA[gid] = (en, cn)
    STEAM_GENRES.update(_GENRE_EXTRA)


def _cn_to_en_genre() -> dict[str, str]:
    """中文类型名 -> 英文（蒸汽平台返回中文字符串，需要映射回英文以保持契约）。"""
    _load_genre_table()
    return {cn: en for en, cn in STEAM_GENRES.values() if en and en != "?"}

# ``price_source`` 必须在这里：它由 ``_price_from_official`` 带回，
# 经 ``_merge`` 复制进 record 后才能在 ``_save`` 落库。此前漏掉它，
# 导致「真实补齐流程」里 price_source 永远是空串（source 却含 +price）。
_FIELDS = ("name", "name_cn", "genres", "genres_cn", "categories",
           "developers", "publisher", "price", "is_free", "release_date",
           "header_image", "price_source")

_LOCK = threading.Lock()
_EXTRA_CACHE: dict | None = None
_OFFICIAL_BLOCKED = False       # 官方商店「元数据」首次失败后置位，本进程不再尝试
_OFFICIAL_PRICE_BLOCKED = False  # 官方商店「价格」路径的独立熔断（与元数据互不影响）
_OFFICIAL_PRICE_FAILS = 0        # 价格路径连续不可达次数（阈值后冷却式熔断）
MAX_PRICE_SOURCE_FAILS = 3       # 国际站连续失败这么多次后暂停（冷却期满自动恢复）
_PRICE_BLOCKED_AT = 0.0          # 熔断发生时刻（用于冷却到期自动恢复）
_OFFICIAL_BLOCKED_AT = 0.0       # 元数据路径熔断时刻（同样冷却式恢复）
OFFICIAL_COOLDOWN_SECONDS = 300.0
_REGION_MEMO: dict = {}          # 账号区域缓存（读 Steam 日志很贵，别每款都读）
_ACTIVE_REGION: str | None = None  # 本次批量取价使用的区域（None = 自动探测账号区域）
_APPINFO_WARNED = False
_PRICE_COLUMN_READY = False     # games.is_free 列是否已确保存在（幂等迁移）


# ------------------------------------------------------------------ 小工具

def _as_list(value: Any, sep: str = ";") -> list[str]:
    """把 str / list / None 统一成去掉空项的字符串列表。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    if sep and sep in text:
        return [part.strip() for part in text.split(sep) if part.strip()]
    return [text]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_appid(appid: Any) -> int | None:
    try:
        return int(appid)
    except (TypeError, ValueError):
        return None


def price_state(record: Mapping | None) -> str:
    """把一条记录的价格判成三态之一："paid" / "free" / "unknown"。

    - ``is_free`` 为真 -> free（这是**唯一**的免费信号，绝不再用 price=0 推断）；
    - ``price`` 为正整数 -> paid；
    - 其余（0 / 空 / 缺失）-> unknown。
    """
    if not record:
        return "unknown"
    if record.get("is_free") in (True, 1, "1", "true", "True"):
        return "free"
    return "paid" if _as_int(record.get("price"), 0) > 0 else "unknown"


def _has_price(record: Mapping | None) -> bool:
    """是否已经拿到了可用价格（有价或确认为免费）。"""
    return price_state(record) != "unknown"


def _ensure_price_column() -> None:
    """幂等补齐 ``games.is_free`` / ``games.miss_at`` 列。

    ``data/schema.py`` 负责集中建表（T7 起 ``miss_at`` 也进了那边的迁移表），
    这里沿用 schema 同款「PRAGMA 判列 -> 缺列才 ALTER」做法兜底，
    保证「三态价格」与「负缓存时间戳」都能持久化。任何失败都吞掉。
    """
    global _PRICE_COLUMN_READY
    if _PRICE_COLUMN_READY:
        return
    try:
        init_db()
        db = get_connection()
        try:
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(games)").fetchall()}
            if columns and "is_free" not in columns:
                db.execute("ALTER TABLE games ADD COLUMN is_free INTEGER")
            if columns and "miss_at" not in columns:
                db.execute("ALTER TABLE games ADD COLUMN miss_at TEXT")
            if columns and "price_source" not in columns:
                db.execute("ALTER TABLE games ADD COLUMN price_source TEXT")
            db.commit()
        finally:
            db.close()
        _PRICE_COLUMN_READY = True
    except Exception as exc:
        logger.info("补齐 games.is_free / miss_at 列失败（忽略）：%s", exc)


def genre_label(genre_id: Any, lang: str = "en") -> str:
    """官方类型 ID -> 可读名（lang='en'|'cn'）。

    查不到的名称返回空串，**绝不返回「类型 <id>」这种占位**
    （用户明确要求：不确定的宁可保留官方英文名/原文，也不要无意义占位）。
    """
    gid = _as_int(genre_id, -1)
    if gid < 0:
        return ""
    _load_genre_table()
    pair = STEAM_GENRES.get(gid)
    if not pair:
        return ""
    en, cn = pair
    if not en:
        return ""
    if lang.startswith(("cn", "zh", "sc", "tc")):
        return cn or en          # 中文缺失时退回官方英文名，仍可读
    return en


def _genre_names(raw: Any) -> tuple[list[str], list[str]]:
    """appinfo 的 {"0": "1", "1": "3"} / 官方商店的 [{"id":1,"description":..}] -> (英文, 中文)。"""
    ids: list[int] = []
    direct_cn: list[str] = []

    if isinstance(raw, Mapping):
        for value in raw.values():
            for part in str(value).split(","):
                part = part.strip()
                if part.isdigit():
                    ids.append(int(part))
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, Mapping):
                desc = str(item.get("description") or "").strip()
                if desc:
                    direct_cn.append(desc)
                gid = item.get("id")
                if gid is not None and str(gid).strip().isdigit():
                    ids.append(int(str(gid)))
            elif str(item).strip().isdigit():
                ids.append(int(str(item)))
    elif isinstance(raw, str) and raw.strip():
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))

    en: list[str] = []
    cn: list[str] = []
    seen_en: set[str] = set()
    for gid in ids:
        name_en = genre_label(gid, "en")
        name_cn = genre_label(gid, "cn")
        if name_en and name_en not in seen_en:
            seen_en.add(name_en)
            en.append(name_en)
            cn.append(name_cn)
    for desc in direct_cn:
        mapped = _cn_to_en_genre().get(desc)
        if mapped and mapped not in seen_en:
            seen_en.add(mapped)
            en.append(mapped)
            cn.append(desc)
        elif not mapped and desc not in cn:
            cn.append(desc)
    return en, cn


_LEGACY_GENRE_RE = re.compile(r"^\u7c7b\u578b\s*(\d+)$")


def normalize_genre_tokens(tokens: Iterable[str], lang: str = "en") -> list[str]:
    """把历史脏值（``类型 51``）按**权威表**重新解析成可读名；其余原样保留并去重。

    旧版类型表缺失 50-60 段，导致落库了「类型 51」这类占位。这里逐条回填，
    避免用户被迫重抓；解析不出的占位**直接丢弃**（宁可少一个标签，也不留无意义占位）。
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in tokens or ():
        text = str(raw or "").strip()
        if not text:
            continue
        match = _LEGACY_GENRE_RE.match(text)
        if match:
            text = genre_label(int(match.group(1)), lang).strip()
            if not text:
                continue          # 解析不出的占位直接丢掉
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def remap_legacy_genres(quiet: bool = True) -> int:
    """把 ``games`` 表里遗留的「类型 NN」占位按权威表重写。返回修正的行数。

    幂等、绝不抛异常；只动含占位的行，其余行不碰。
    """
    changed = 0
    try:
        _ensure_price_column()
        init_db()
        db = get_connection()
    except Exception as exc:
        if not quiet:
            logger.info("打开缓存库失败（跳过类型回填）：%s", exc)
        return 0
    try:
        rows = db.execute("SELECT appid, genres, genres_cn FROM games").fetchall()
        for appid, genres_raw, genres_cn_raw in rows:
            genres = _json_list(genres_raw)
            genres_cn = _json_list(genres_cn_raw)
            if not any(_LEGACY_GENRE_RE.match(t) for t in genres + genres_cn):
                continue
            new_en = normalize_genre_tokens(genres, "en")
            new_cn = normalize_genre_tokens(genres_cn, "cn")
            db.execute(
                "UPDATE games SET genres=?, genres_cn=? WHERE appid=?",
                (json.dumps(new_en, ensure_ascii=False),
                 json.dumps(new_cn, ensure_ascii=False), int(appid)),
            )
            changed += 1
        if changed:
            db.commit()
    except Exception as exc:
        logger.info("类型占位回填失败（忽略）：%s", exc)
    finally:
        try:
            db.close()
        except Exception:
            pass
    return changed


def _pick_localized(localized: Any, order: Iterable[str]) -> str:
    if not isinstance(localized, Mapping):
        return ""
    for code in order:
        value = localized.get(code)
        if isinstance(value, Mapping):      # 少数条目是多语言套多语言
            value = _pick_localized(value, order)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _format_release(value: Any) -> str:
    """把 appinfo 的 unix 时间戳 / 官方商店的 "Apr 13, 2015" 统一成 YYYY-MM-DD。"""
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if text.isdigit():
        ts = _as_int(text, 0)
        if ts > 0:
            try:
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                return ""
        return ""
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%d %b, %Y", "%Y年%m月%d日", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _merge(target: dict, source: Mapping | None, origin: str, sources: list[str]) -> None:
    """逐字段取第一个非空值（target 已有的值不覆盖）。"""
    if not source:
        return
    used = False
    for field in _FIELDS:
        if field not in source:
            continue
        value = source[field]
        if value in (None, "", [], 0):
            continue
        current = target.get(field)
        if current in (None, "", [], 0):
            target[field] = value
            used = True
    if used and origin not in sources:
        sources.append(origin)


# ---------------------------------------------------------------- 小黑盒（国区主源）
#
# T9 用户拍板的主源（主 agent 已独立复核）：无需登录 / 签名 / 密钥。
#   * **批量 40 款/次**（超了截断），批间隔 >=0.5s；单款 0.3s，191 款全量 5 批 / 约 6 秒；
#   * 返回 ``result.base_infos[]``，每条含 ``steam_appid``（**权威，必须校验**）、
#     ``name``（中文名）、``image``（封面 URL）、``price.{initial,current,...}``（**单位「元」字符串**）、
#     ``is_free``；
#   * 价格与官方国区**逐分一致**（实测 Half-Life 2 ¥42 = 4200 分）；
#   * 局限：``genres`` **恒为空** → 类型仍走官方商店 / 蒸汽平台 / 本地 appinfo；
#     少量 appid 它没有（本机库实测 13 款）→ **必须回落官方商店**，绝不当成 0 元或免费。


def _fen_from_yuan(value: Any) -> int:
    """「元」字符串 -> 分。**必须用 Decimal**：``11.6 * 100`` 用浮点会得到 1159.99…。

    兼容 ``"42"`` / ``"42.00"`` / ``"¥ 42.00"`` / ``"42元"``；空值或非法一律返回 0。
    """
    text = str(value if value is not None else "").strip().replace(",", "").replace("，", "")
    if not text:
        return 0
    match = re.search(r"(?<![\d.\-])\d+(?:\.\d+)?", text)
    if not match:
        return 0
    try:
        yuan = Decimal(match.group(0))
    except InvalidOperation:
        return 0
    if yuan <= 0:
        return 0
    return int((yuan * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _xhh_record(info: Mapping) -> dict:
    """把小黑盒的一条 ``base_info`` 变成我们的记录字段。

    * 免费**只认 ``is_free`` 字段**（沿用三态语义：取不到就是「未知」，绝不因为
      ``price.current == 0`` 就当成免费）；
    * 价格优先 ``price.initial``（原价），没有才用 ``price.current``（折后价）；
    * 只有真正带回了信息才返回内容。
    """
    price = info.get("price") if isinstance(info.get("price"), Mapping) else {}
    fen = _fen_from_yuan(price.get("initial")) or _fen_from_yuan(price.get("current"))
    free = info.get("is_free") in (True, 1, "1", "true", "True")

    out: dict = {}
    name = str(info.get("name") or "").strip()
    if name:
        out["name_cn"] = name
    image = str(info.get("image") or "").strip()
    if image.startswith(("http://", "https://")):
        out["header_image"] = image
    if free:
        out["price"] = 0
        out["is_free"] = True
        out["price_source"] = "xiaoheihe"
    elif fen > 0:
        out["price"] = fen
        out["price_source"] = "xiaoheihe"
    return out


def xiaoheihe_batch(appids: Iterable, session=None, sleep_between: bool = True) -> dict[int, dict]:
    """小黑盒批量取数（40 款/次）。**任何失败都静默降级为「这批没有」**，永不抛异常。

    返回 ``{appid: {"name_cn", "header_image", "price"(分), "is_free", "price_source"}}``，
    只含**内层 ``steam_appid`` 校验通过**的条目（宁可没有，也绝不把别的游戏写进来）。
    """
    wanted: list[int] = []
    for raw in appids or []:
        aid = _as_appid(raw)
        if aid is not None and aid not in wanted:
            wanted.append(aid)
    if not wanted or requests is None:
        return {}

    getter = session.get if session is not None else requests.get
    out: dict[int, dict] = {}
    chunks = [wanted[i:i + XHH_BATCH_SIZE] for i in range(0, len(wanted), XHH_BATCH_SIZE)]
    for index, chunk in enumerate(chunks):
        if index and sleep_between:
            time.sleep(XHH_BATCH_INTERVAL)
        url = URL_XIAOHEIHE.format(appids=",".join(str(a) for a in chunk))
        try:
            resp = getter(url, timeout=XHH_TIMEOUT, headers=XHH_HEADERS)
            status = getattr(resp, "status_code", 200)
            if status != 200:
                logger.info("小黑盒返回 HTTP %s，本批 %d 款跳过", status, len(chunk))
                continue
            payload = resp.json()
        except Exception as exc:
            logger.info("小黑盒取数失败（本批 %d 款，已静默跳过）：%s", len(chunk), exc)
            continue
        result = payload.get("result") if isinstance(payload, Mapping) else None
        infos = result.get("base_infos") if isinstance(result, Mapping) else None
        if not isinstance(infos, (list, tuple)):
            logger.info("小黑盒响应结构不认识（本批 %d 款跳过）", len(chunk))
            continue
        for info in infos:
            if not isinstance(info, Mapping):
                continue
            aid = _as_appid(info.get("steam_appid"))
            # **内层 steam_appid 必须校验**：不在本批请求里的一律丢弃（绝不写错价）
            if aid is None or aid not in chunk:
                continue
            record = _xhh_record(info)
            if record:
                out[aid] = record
    return out


def _xiaoheihe_one(appid: int, session=None) -> dict:
    """单款小黑盒取数（价格优先级第一档；0.3 秒级）。"""
    return xiaoheihe_batch([appid], session=session, sleep_between=False).get(appid) or {}


# ------------------------------------------------------------------ 数据源

def _record_appid(value: Any) -> int | None:
    """从一条商店/镜像记录里读出「它自述的 appid」（读不到返回 None）。

    三种真实形状：
      * ``appdetails`` : ``{"success": true, "data": {"steam_appid": 582010, ...}}``
      * ``steamcmd``   : ``{"appid": 582010, "common": {...}}``
      * 打桩/异常形状   : 什么都没有 -> None
    """
    if not isinstance(value, Mapping):
        return None
    data = value.get("data")
    if isinstance(data, Mapping):
        inner = _as_appid(data.get("steam_appid"))
        if inner is not None:
            return inner
    for key in ("steam_appid", "appid"):
        inner = _as_appid(value.get(key))
        if inner is not None:
            return inner
    return None


def _appdetails_node(payload: Any, appid: int) -> dict:
    """从商店响应里取出**属于 appid** 的那条记录（**绝不轻信外层 key**）。

    背景（2026-09-26 实测 5/5 命中）：CDN 边缘返回的 JSON **外层 key 是错的**
    （请求 582010 返回的外层 key 是 1390430），但内层 ``data.steam_appid`` 完全正确。
    历史实现只认外层 key ``payload.get(str(appid))`` → 永远取不到价，
    这就是「全库价格 0/135」的直接根因。

    规则（**宁可返回 {}，也绝不把别的游戏的价格写进本游戏**）：
      1. 外层 key ``str(appid)`` 命中，且它自述的 appid 与请求**一致**（或没自述 appid）→ 返回；
      2. 否则遍历所有记录，取「自述 appid == 请求 appid」的那条；
      3. 自述 appid **缺失**、且整个响应只有一条记录 → 也接受（记 warning：外层 key 不对）；
      4. 自述 appid **存在但与请求不一致** → **拒绝**（哪怕只有一条记录）；
      5. 都不满足 → ``{}``。
    """
    target = _as_appid(appid)
    if not isinstance(payload, Mapping) or target is None:
        return {}

    direct = payload.get(str(target))
    if isinstance(direct, Mapping):
        inner = _record_appid(direct)
        if inner is None or inner == target:
            return dict(direct)
        logger.warning("appdetails 外层 key %s 与内层 appid %s 不一致（请求 %s），拒绝使用",
                       target, inner, target)

    records = [value for value in payload.values() if isinstance(value, Mapping)]
    for value in records:
        if _record_appid(value) == target:
            return dict(value)

    if len(records) == 1:
        value = records[0]
        if _record_appid(value) is None:
            logger.warning("appdetails 外层 key %s 与请求不一致，且响应没有内层 appid 可校验，"
                           "按「只有一条记录」接受", list(payload.keys()))
            return dict(value)
    return {}

def _from_appinfo(appid: int, steam_path: str | None = None) -> dict:
    """本机 appinfo.vdf（离线）。取不到时返回 {}。"""
    global _APPINFO_WARNED
    try:
        from core import appinfo as appinfo_mod
    except Exception as exc:
        if not _APPINFO_WARNED:
            logger.info("本地 appinfo 模块不可用：%s", exc)
            _APPINFO_WARNED = True
        return {}
    try:
        entry = appinfo_mod.app_metadata(appid, steam_path)
    except Exception as exc:
        logger.info("读取本地 appinfo 失败（appid=%s）：%s", appid, exc)
        return {}
    if not entry:
        return {}

    localized = entry.get("name_localized") or {}
    header = entry.get("header_image") or {}
    out = {
        "name": str(entry.get("name") or "").strip(),
        "name_cn": _pick_localized(localized, ("schinese", "sc_schinese", "tchinese")),
        "header_image": _pick_localized(header, ("schinese", "sc_schinese", "english", "tchinese")),
    }
    return {k: v for k, v in out.items() if v}


def _from_steamcmd(appid: int, session=None) -> dict:
    """Steam 官方 appinfo 的公开镜像（大陆直连可用）。"""
    if requests is None:
        return {}
    url = URL_STEAMCMD.format(appid=appid)
    getter = session or requests
    try:
        resp = getter.get(url, timeout=TIMEOUT_STEAMCMD, headers={"User-Agent": UA})
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        logger.info("steamcmd.net 获取失败（appid=%s）：%s", appid, exc)
        return {}

    # 顺手加固：同样不轻信外层 key，按记录自述的 appid 取值
    node = _appdetails_node((payload or {}).get("data") or {}, appid)
    common = node.get("common") or {}
    extended = node.get("extended") or {}
    if not isinstance(common, Mapping):
        return {}

    name = str(common.get("name") or "").strip()
    localized = common.get("name_localized") or {}
    name_cn = _pick_localized(localized, ("schinese", "sc_schinese", "tchinese"))

    genres_en, genres_cn = _genre_names(common.get("genres"))
    release = _format_release(common.get("original_release_date") or common.get("release_date"))
    header = common.get("header_image")
    header_name = _pick_localized(header, ("schinese", "english")) if isinstance(header, Mapping) else str(header or "")

    out = {
        "name": name,
        "name_cn": name_cn,
        "genres": genres_en,
        "genres_cn": genres_cn,
        "developers": _as_list(extended.get("developer")),
        "publisher": "、".join(_as_list(extended.get("publisher"))),
        "release_date": release,
        "header_image": header_name,
    }
    return {k: v for k, v in out.items() if v not in (None, "", [], 0)}


def _from_official_store(appid: int, url: str, timeout: float, lang: str, session=None,
                         price_source: str = "") -> tuple[dict, bool]:
    """官方商店 appdetails（蒸汽平台 / store.steampowered.com）。返回 (数据, 是否连通)。

    传输走 ``_store_json``：常规请求失败时会用 DoH 解析 + IP 直连兜底，
    解决大陆环境下 store.steampowered.com 因 DNS 污染而「时通时断」的问题。
    """
    payload, reachable = _store_json(url, timeout, session=session, attempts=2)
    if not payload:
        if not reachable:
            logger.info("商店接口不可用（%s）", urlsplit(str(url)).netloc)
        return {}, reachable

    node = _appdetails_node(payload, appid)
    if not isinstance(node, Mapping) or not node.get("success"):
        return {}, True
    data = node.get("data") or {}
    if not isinstance(data, Mapping):
        return {}, True

    genres_en, genres_cn = _genre_names(data.get("genres"))
    overview = data.get("price_overview") or {}
    price = _as_int(overview.get("initial"), 0) or _as_int(overview.get("final"), 0)
    is_free = bool(data.get("is_free")) if data.get("is_free") is not None else None
    out = {
        "name": str(data.get("name") or "").strip(),
        "genres": genres_en,
        "genres_cn": genres_cn,
        "categories": [str(c.get("description") or "") for c in (data.get("categories") or [])
                       if isinstance(c, Mapping) and str(c.get("description") or "").strip()],
        "developers": _as_list(data.get("developers")),
        "publisher": "、".join(_as_list(data.get("publishers"))),
        "price": price if price > 0 else None,
        "is_free": True if is_free else None,
        "release_date": _format_release((data.get("release_date") or {}).get("date")),
        "header_image": str(data.get("header_image") or ""),
    }
    if out.get("price") or out.get("is_free"):
        # 元数据这条路也会带回价格 / 免费（如蒸汽平台的 appdetails），
        # 那就必须如实标注它来自哪家商店——否则这几行会出现「有价无来源」。
        # （没有价格/免费信息的行不写，保持「取不到 = 未知」语义）
        out["price_source"] = price_source
    if lang.startswith(("cn", "zh", "sc", "tc")):
        # 该接口按 l= 返回本地化名称与类型，这里的中文要落到中文列
        Chinese = out.pop("name")
        out["name_cn"] = Chinese
        out.pop("genres", None)
    return {k: v for k, v in out.items() if v not in (None, "", [], 0)}, True


def _from_steamchina(appid: int, session=None) -> dict:
    """蒸汽平台官方商店（大陆直连可用）。"""
    data, _ = _from_official_store(
        appid, URL_STEAMCHINA.format(appid=appid), TIMEOUT_STEAMCHINA, "cn", session,
        price_source="steamchina",
    )
    return data


def _from_official(appid: int, session=None) -> dict:
    """官方商店（大陆被墙）；只尝试一次，失败后本进程不再试。"""
    global _OFFICIAL_BLOCKED, _OFFICIAL_BLOCKED_AT
    if _OFFICIAL_BLOCKED:
        if time.time() - _OFFICIAL_BLOCKED_AT < OFFICIAL_COOLDOWN_SECONDS:
            return {}
        with _LOCK:                       # 冷却期满：自动恢复尝试（别永久放弃）
            _OFFICIAL_BLOCKED = False
    data, reachable = _from_official_store(
        # 带 cc=CN：官方会按中国区返回**官方简体中文名**（如 GTA V 传承版）
        appid, URL_OFFICIAL_CC.format(appid=appid, cc=DEFAULT_REGION),
        TIMEOUT_OFFICIAL, "cn", session,
        price_source="steampowered",
    )
    if not reachable:
        with _LOCK:
            _OFFICIAL_BLOCKED = True
            _OFFICIAL_BLOCKED_AT = time.time()
        logger.info("官方商店 store.steampowered.com 暂不可达（大陆网络常见），%.0f 秒后重试",
                    OFFICIAL_COOLDOWN_SECONDS)
        return {}
    return data


def set_active_region(region: str | None) -> None:
    """指定「按哪个区域取价」（None = 自动探测账号区域）。批量取价前设置一次即可。"""
    global _ACTIVE_REGION
    _ACTIVE_REGION = str(region or "").strip().upper() or None


def account_region(steam_path: str | None = None) -> str:
    """账号所在区域（Steam 客户端日志里的 ``COUNTRY=xx``），取不到返回 ""。

    每个账号的国家可能不同，这决定「按哪个区域取价」。只读日志、绝不写；
    结果按天缓存（读大日志不便宜）。任何异常都降级为 ""。
    """
    key = str(steam_path or "")
    today = date.today().isoformat()
    memo = _REGION_MEMO.get(key)
    if memo and memo[1] == today:
        return memo[0]
    region = ""
    try:
        root = Path(steam_path) if steam_path else None
        if root is None:                             # 没给路径就自己找（只读）
            try:
                from core import locator

                found = locator.find_steam_path()
                root = Path(found) if found else None
            except Exception:
                root = None
        candidates = [root / "logs" / "webhelper_js.txt"] if root else []
        if not candidates:
            return ""
        for path in candidates:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            matches = re.findall(r"COUNTRY=([A-Za-z]{2})", text)
            if matches:
                region = str(matches[-1]).upper()      # 取最近一次启动记录
                break
    except Exception as exc:
        logger.info("读取账号区域失败（忽略，按默认区域）：%s", exc)
    _REGION_MEMO[key] = (region, today)
    return region


def _price_from_official(appid: int, session=None) -> dict:
    """独立价格路径：取价并**统一为人民币**。

    顺序（T9 起「小黑盒」是国区人民币实价主源，官方商店只做兜底）：
      0. **小黑盒** —— 国区人民币实价，40 款/次批量、单款 0.3 秒，与官方国区逐分一致；
      1. 官方国际站 ``cc=CN`` —— 直接返回人民币，最准；
      2. 官方国际站 ``cc=<账号区域>`` —— 账号在别国时按当日汇率换算成人民币；
      3. 蒸汽平台 —— 人民币。

    与元数据路径**完全解耦**（更长超时、独立熔断）。返回 ``{}`` 或
    ``{"price": <人民币分>, "is_free": True, "price_source": "..."}``；
    拿不到价格绝不返回 ``price=0`` 的假免费。官方那条走网络失败会**重试 + DoH 直连兜底**，
    连续失败只做冷却式熔断（冷却期满自动恢复）。
    """
    global _OFFICIAL_PRICE_BLOCKED

    # 0) 小黑盒（国区人民币实价，最快最准；它没有的 appid 自动落到下面的官方路径）
    data = _xiaoheihe_one(appid, session)
    if _has_price(data):
        return data

    should_try_official = _price_source_available()
    if not should_try_official:
        # 熔断冷却期内：仍试蒸汽平台（人民币，另一台服务器，不受国际站影响）
        data = _from_store_price(
            appid, URL_STEAMCHINA.format(appid=appid), TIMEOUT_STEAMCHINA, session
        ) or {}
        if data:
            data.setdefault("price_source", "steamchina")
        return data

    failed = False
    # 1) 区域 = 人民币：直接取人民币，不需要换算
    for code in ("CN",):
        data, reachable = _from_store_price(
            appid, URL_OFFICIAL_CC.format(appid=appid, cc=code),
            TIMEOUT_OFFICIAL_PRICE, session, report=True,
        )
        if data:
            with _LOCK:
                globals()["_OFFICIAL_PRICE_FAILS"] = 0
            data.setdefault("price_source", "steampowered")
            return data
        if not reachable:
            failed = True

    # 2) 账号区域（非人民币时按当日汇率换算成人民币）
    home = str(_ACTIVE_REGION or account_region()).strip().upper()
    if home and home != "CN" and not failed:
        data, reachable = _from_store_price(
            appid, URL_OFFICIAL_CC.format(appid=appid, cc=home),
            TIMEOUT_OFFICIAL_PRICE, session, report=True,
        )
        if data:
            with _LOCK:
                globals()["_OFFICIAL_PRICE_FAILS"] = 0
            data.setdefault("price_source", "steampowered")
            return data
        if not reachable:
            failed = True

    # 3) 蒸汽平台（人民币）
    data = _from_store_price(
        appid, URL_STEAMCHINA.format(appid=appid), TIMEOUT_STEAMCHINA, session
    )
    if data:
        with _LOCK:
            globals()["_OFFICIAL_PRICE_FAILS"] = 0
        data.setdefault("price_source", "steamchina")
        return data

    if failed:
        _note_price_fail()
    return {}


def _note_price_fail() -> None:
    """记一次价格源不可达；连续失败达阈值后**冷却式**熔断（冷却期满自动恢复）。

    历史实现是「一次失败就永久放弃」，这正是「价格全是 0」的根因之一：
    大陆网络下 store.steampowered.com 本身就是时通时断，必须允许它恢复。
    """
    global _OFFICIAL_PRICE_BLOCKED, _OFFICIAL_PRICE_FAILS, _PRICE_BLOCKED_AT
    with _LOCK:
        _OFFICIAL_PRICE_FAILS += 1
        if _OFFICIAL_PRICE_FAILS >= MAX_PRICE_SOURCE_FAILS and not _OFFICIAL_PRICE_BLOCKED:
            _OFFICIAL_PRICE_BLOCKED = True
            _PRICE_BLOCKED_AT = time.time()
            logger.info("官方商店价格接口连续 %d 次不可达，暂停 %.0f 秒后自动重试",
                        _OFFICIAL_PRICE_FAILS, PRICE_COOLDOWN_SECONDS)


def _price_source_available() -> bool:
    """价格源现在可用吗？熔断后冷却期满会**自动恢复**（返回 True 并清零计数）。"""
    global _OFFICIAL_PRICE_BLOCKED, _OFFICIAL_PRICE_FAILS, _PRICE_BLOCKED_AT
    with _LOCK:
        if not _OFFICIAL_PRICE_BLOCKED:
            return True
        waited = time.time() - _PRICE_BLOCKED_AT
        if waited < PRICE_COOLDOWN_SECONDS:
            return False
        _OFFICIAL_PRICE_BLOCKED = False
        _OFFICIAL_PRICE_FAILS = 0
        logger.info("价格源冷却结束（%.0f 秒），重新尝试官方商店", waited)
        return True


# ---------------------------------------------------------------- 网络：DNS 污染兜底
#
# 大陆环境下本地 DNS 对 store.steampowered.com 会返回**被污染、连不通的 IP**
# （实测：同一 URL 连打 10 次全部超时；换用**校验过的** DoH IP 直连则 200）。
# 这里做两级兜底，任何一步失败都只是「降级」，绝不抛异常：
#   1. 常规请求（系统 DNS）——大多数时候够用；
#   2. 全部 DoH 取并集 + **逐个真实请求校验** + 直连（TLS 仍按真实域名做 SNI/证书）+ 可用 IP 复用。
#
# ⚠️ 历史实现有两个坑（T7c 实测定位，是「价格全 0」的第一根因）：
#   * ``DOH_ENDPOINTS`` 把**阿里**排第一位，而阿里对该域名返回的是**污染 IP**
#     （104.244.46.165，Twitter 段，必然超时）；
#   * ``_doh_ips``「拿到第一个非空结果就 return」→ 永远钉在这个死 IP 上（白等 22.7 秒）。
# 现在：候选池 = 所有 DoH 的 IP ∪ 系统 DNS 的 IP，**每个候选都用真实小请求校验**，
# 通过才钉住；校验失败进短时黑名单，换下一个，绝不在死 IP 上耗完超时预算。

DOH_ENDPOINTS = (
    "https://doh.pub/dns-query?name={host}&type=A",        # 腾讯（实测唯一给出可连 IP 的一家）
    "https://1.12.12.12/dns-query?name={host}&type=A",     # 腾讯 DNSPod
    "https://doh.360.cn/resolve?name={host}&type=A",       # 360 DNS
    "https://dns.alidns.com/resolve?name={host}&type=A",   # 阿里：对该域名返回被污染 IP，排最后
    "https://223.5.5.5/resolve?name={host}&type=A",
    "https://223.6.6.6/resolve?name={host}&type=A",
)

_PINNED_IPS: dict[str, tuple[str, float]] = {}   # host -> (IP, 记录时刻)
_PINNED_LOCK = threading.Lock()
PINNED_TTL = 900.0        # 直连 IP 复用窗口（秒）
DOH_TIMEOUT = 5.0
IP_CHECK_TIMEOUT = 4.0            # 候选 IP 校验（真实小请求）的单次超时
IP_BLACKLIST_SECONDS = 300.0      # 校验失败的 IP 进短时黑名单（别反复在死 IP 上耗预算）
HOST_COOLDOWN_SECONDS = 900.0     # 所有候选 IP 都不通 -> 该主机整体冷却（分钟级阻断窗口）
_IP_BLACKLIST: dict[str, float] = {}   # ip -> 解禁时刻
_HOST_DOWN: dict[str, float] = {}      # host -> 冷却结束时刻
_IP_CHECK_LOG: dict[str, str] = {}     # ip -> 最近一次校验失败原因（排障/验收用）

# 路由健康度：某主机「常规 DNS 路线」最近失败过 -> 后续优先走直连，
# 避免每款游戏都先白等一轮超时（污染环境下这是最大的时间浪费）。
_BAD_ROUTE: dict[str, float] = {}
ROUTE_PENALTY_SECONDS = 180.0
PRIMARY_ATTEMPT_TIMEOUT = 5.0    # 常规路线单次超时上限（直连兜底可用完整超时）
_CANDIDATE_CACHE: dict[str, tuple[list, float]] = {}   # host -> (候选 IP, 记录时刻)
CANDIDATE_TTL = 300.0            # 候选 IP 复用窗口（DoH 不便宜，别每款都问）
_CANDIDATE_EMPTY_TTL = 60.0      # 解析不出来的负缓存（别连续猛问同一批 DoH）

# ---- 跳进程的「历史验证成功 IP」（T11） ---------------------------------------
#
# 只落盘**校验通过过**的 IP（带时间戳）：下次 DNS 解析到的 IP 全不可用时，
# 可以拿历史的成功 IP 再试一次——依然要过 ``_validate_ip`` 那一关（历史 IP 也会失效）。
# 这只是**进程内的地址选择**：绝不改系统 hosts、不装代理、不起本地转发服务。
_IP_HISTORY: dict[str, list[list]] = {}      # host -> [[ip, ts], ...]（新在前）
_IP_HISTORY_LOADED = False
_IP_HISTORY_LOCK = threading.Lock()
IP_HISTORY_TTL = 30 * 86400.0                # 历史 IP 最多留 30 天
IP_HISTORY_MAX = 8                           # 每台主机最多记这么多条
IP_HISTORY_NAME = "pinned_ips.json"          # 与 cache.db 同目录（跟着一起备份）


def ip_history_path() -> Path:
    """历史成功 IP 的落盘位置（**写入**文件，别写进退出即删的 _MEIPASS）。"""
    return stable_data_dir() / IP_HISTORY_NAME


def _load_ip_history() -> dict[str, list[list]]:
    """惰性读盘（只读一次）并按 TTL 剪枝；坏文件 / 读不到一律当空表。"""
    global _IP_HISTORY_LOADED
    with _IP_HISTORY_LOCK:
        if _IP_HISTORY_LOADED:
            return _IP_HISTORY
        _IP_HISTORY_LOADED = True
    data = _read_json_file(ip_history_path()) or {}
    hosts = data.get("hosts") if isinstance(data, Mapping) else None
    if not isinstance(hosts, Mapping):
        return _IP_HISTORY
    limit = time.time() - IP_HISTORY_TTL
    loaded: dict[str, list[list]] = {}
    for host, items in hosts.items():
        keep: list[list] = []
        if isinstance(items, list):
            for pair in items:
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    continue
                ip = str(pair[0] or "").strip()
                try:
                    ts = float(pair[1])
                except (TypeError, ValueError):
                    continue
                if ip and ts >= limit:
                    keep.append([ip, ts])
        if keep:
            loaded[str(host)] = keep[:IP_HISTORY_MAX]
    with _IP_HISTORY_LOCK:
        _IP_HISTORY.update(loaded)
    return _IP_HISTORY


def _history_ips(host: str) -> list[str]:
    """该主机历史上验证成功过的 IP（新在前；只作候选，还要逐个校验）。"""
    items = _load_ip_history().get(str(host)) or []
    return _dedup(str(pair[0]) for pair in items)


def _remember_ip(host: str, ip: str) -> None:
    """把校验通过的 IP 记进内存并**落盘**（失败静默，绝不影响主流程）。"""
    host, ip = str(host), str(ip)
    if not host or not ip:
        return
    _load_ip_history()                       # 先确保已读盘（避免在持有锁时反向获取）
    now = time.time()
    with _IP_HISTORY_LOCK:
        items = [pair for pair in _IP_HISTORY.get(host, []) if str(pair[0]) != ip]
        items.insert(0, [ip, now])
        _IP_HISTORY[host] = items[:IP_HISTORY_MAX]
        snapshot = {key: [list(pair) for pair in value]
                    for key, value in _IP_HISTORY.items()}
    _write_json_atomic(ip_history_path(), {
        "version": 1,
        "updated_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "hosts": snapshot,
    })


def ip_history_status() -> dict:
    """当前跳进程的历史成功 IP（验收/排障用，只读）。"""
    return {str(host): _history_ips(host) for host in _load_ip_history()}

# ---------------------------------------------------------------- 网络出口策略
#
# **永远直连**（T9 用户拍板）。不做任何「本机代理自动探测 / 自动切换」——
# 探测逻辑既没必要（用户要的是直连）又会引入不确定性。requests 一律不传 proxies。


def _dedup(values: Iterable) -> list[str]:
    """保序去重（并丢掉空值）。"""
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _doh_ips(host: str, timeout: float = DOH_TIMEOUT) -> list[str]:
    """把**所有** DoH 提供方的解析结果取并集（保序：按 DOH_ENDPOINTS 的优先级）。

    历史实现「拿到第一个非空结果就 return」，而阿里对该域名返回污染 IP
    → 永远钉在死 IP 上。这里一律取并集，由 ``_pick_pinned_ip`` 逐个真实校验。
    """
    headers = {"User-Agent": UA, "Accept": "application/dns-json"}
    out: list[str] = []
    for template in DOH_ENDPOINTS:
        url = template.format(host=host)
        payload = None
        try:
            if requests is not None:
                resp = requests.get(url, timeout=timeout, headers=headers)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
            else:                                    # 没有 requests 时退回标准库
                import urllib.request

                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as handle:
                    payload = json.loads(handle.read().decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        answers = payload.get("Answer")
        ips = []
        if isinstance(answers, (list, tuple)):
            for item in answers:
                if isinstance(item, Mapping) and _as_int(item.get("type"), 0) == 1:
                    ips.append(item.get("data"))
        out.extend(ips)
    return _dedup(out)


def _system_ips(host: str) -> list[str]:
    """系统 DNS 的解析结果（可能被污染，仅作候选之一）。"""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except Exception:
        return []
    return _dedup(info[4][0] for info in infos if len(info) > 4)


def _candidate_ips(host: str, use_cache: bool = True) -> list[str]:
    """候选 IP：**全部 DoH 的并集**（按优先级）在前，系统 DNS 兜底在后。

    只是「候选」，能不能用要由 ``_pick_pinned_ip`` 逐个真实校验决定。
    结果带 TTL 缓存（含负缓存），别每款游戏都把 6 个 DoH 问一遍。
    """
    now = time.time()
    if use_cache:
        with _PINNED_LOCK:
            cached = _CANDIDATE_CACHE.get(host)
        if cached:
            ips, stamp = cached
            ttl = CANDIDATE_TTL if ips else _CANDIDATE_EMPTY_TTL
            if now - stamp < ttl:
                return list(ips)
    # 历史验证成功过的 IP 放最后（新解析的优先；都不行时才拿历史的再试一次）
    ips = _dedup(_doh_ips(host) + _system_ips(host) + _history_ips(host))
    with _PINNED_LOCK:
        _CANDIDATE_CACHE[host] = (list(ips), now)
    return ips


def _ip_blacklisted(ip: str) -> bool:
    """该 IP 是否还在短时黑名单里（校验失败过就别再试）。"""
    with _PINNED_LOCK:
        until = _IP_BLACKLIST.get(str(ip))
    return bool(until) and time.time() < until


def _blacklist_ip(ip: str, reason: str = "") -> None:
    with _PINNED_LOCK:
        _IP_BLACKLIST[str(ip)] = time.time() + IP_BLACKLIST_SECONDS
        if reason:
            _IP_CHECK_LOG[str(ip)] = str(reason)[:120]


def _host_cooling(host: str) -> bool:
    """该主机是否处于「整体阻断」冷却期（所有候选 IP 都不通后的分钟级冷却）。"""
    with _PINNED_LOCK:
        until = _HOST_DOWN.get(host)
    return bool(until) and time.time() < until


def _note_host_down(host: str) -> None:
    with _PINNED_LOCK:
        _HOST_DOWN[host] = time.time() + HOST_COOLDOWN_SECONDS


def _validate_ip(host: str, ip: str, timeout: float) -> bool:
    """用**一次真实小请求**校验这个 IP 是否真能连上该主机；通过才允许钉住。

    校验失败（超时 / TLS 失败 / 连接被拒）→ 记短时黑名单，换下一个候选。
    """
    try:
        _get_via_ip(host, "/", ip, min(timeout, IP_CHECK_TIMEOUT))
        return True
    except Exception as exc:
        _blacklist_ip(ip, type(exc).__name__)
        logger.debug("候选 IP 校验失败（%s -> %s）：%s", host, ip, exc)
        return False


class _PinnedHTTPS(http.client.HTTPSConnection):
    """把连接钉到指定 IP，但 TLS 仍用真实域名做 SNI 与证书校验。"""

    def __init__(self, host: str, ip: str, port: int = 443,
                 timeout: float = 10.0, context=None) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pin_ip = ip

    def connect(self) -> None:                       # pragma: no cover - 真实网络
        sock = socket.create_connection((self._pin_ip, self.port), self.timeout)
        context = getattr(self, "_context", None) or ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self.host)


def _get_via_ip(host: str, path: str, ip: str, timeout: float):
    """经指定 IP 发一次 GET，返回 (status, body)。异常向上抛，由调用方决定降级。"""
    conn = _PinnedHTTPS(host, ip, timeout=timeout)
    try:
        conn.request("GET", path, headers={
            "User-Agent": UA,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "close",
        })
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _pick_pinned_ip(host: str, timeout: float) -> str | None:
    """挑一个**真正连得通**的 IP 并缓存（候选池 = 全部 DoH ∪ 系统 DNS）。

    逐个候选做真实小请求校验：通过的才钉住；失败的进短时黑名单换下一个。
    全部候选都不通 → 认为该主机处于「分钟级阻断窗口」，整体冷却
    （``HOST_COOLDOWN_SECONDS``）后自动再试，绝不在死 IP 上耗完超时预算。
    """
    with _PINNED_LOCK:
        cached = _PINNED_IPS.get(host)
    if cached and time.time() - cached[1] < PINNED_TTL:
        return cached[0]
    checked = False
    for ip in _candidate_ips(host):
        if _ip_blacklisted(ip):
            continue
        checked = True
        if _validate_ip(host, ip, timeout):
            with _PINNED_LOCK:
                _PINNED_IPS[host] = (ip, time.time())
                _HOST_DOWN.pop(host, None)
            _remember_ip(host, ip)            # 跳进程记住「这个 IP 曾经连通过」
            logger.debug("直连 IP 校验通过：%s -> %s", host, ip)
            return ip
    with _PINNED_LOCK:
        _PINNED_IPS.pop(host, None)
    if checked:
        _note_host_down(host)
        logger.info("%s 的候选 IP 全部校验失败（分钟级阻断窗口），%.0f 秒内不再重试直连",
                    host, HOST_COOLDOWN_SECONDS)
    return None


def pinned_ip_status() -> dict:
    """当前缓存的「主机 -> 可用 IP」（调试/验收用；只读）。"""
    with _PINNED_LOCK:
        return {host: ip for host, (ip, _ts) in _PINNED_IPS.items()}


def _is_real_session(session: Any) -> bool:
    """调用方给的是**真实** requests.Session 吗？

    打桩会话（测试用）不能触发直连兜底——否则单元测试会真的发网络请求。
    """
    if session is None or requests is None:
        return False
    try:
        return isinstance(session, requests.sessions.Session)
    except Exception:
        return False


def _route_is_bad(host: str) -> bool:
    """该主机的常规路线最近是否失败过（失败过就优先直连，别每次都白等超时）。"""
    with _PINNED_LOCK:
        when = _BAD_ROUTE.get(host)
    return bool(when) and (time.time() - when) < ROUTE_PENALTY_SECONDS


def _note_route(host: str, ok: bool) -> None:
    """记录常规路线健康度（成功即清零，失败才开始计罚）。"""
    with _PINNED_LOCK:
        if ok:
            _BAD_ROUTE.pop(host, None)
        else:
            _BAD_ROUTE[host] = time.time()


def reset_routes() -> None:
    """清空路由健康度与直连缓存（测试/排障用）。

    注意：这里只清**内存**里的历史 IP，落盘的 ``pinned_ips.json`` 不动。
    """
    global _IP_HISTORY_LOADED
    with _PINNED_LOCK:
        _BAD_ROUTE.clear()
        _PINNED_IPS.clear()
        _CANDIDATE_CACHE.clear()
        _IP_BLACKLIST.clear()
        _HOST_DOWN.clear()
        _IP_HISTORY.clear()
        _IP_HISTORY_LOADED = False


def route_status() -> dict:
    """当前路由状态（验收/排障用，只读）。"""
    with _PINNED_LOCK:
        now = time.time()
        return {
            "bad_routes": dict(_BAD_ROUTE),
            "pinned": {h: v[0] for h, v in _PINNED_IPS.items()},
            "host_cooling": {h: round(until - now, 1) for h, until in _HOST_DOWN.items()
                             if until > now},
            "ip_blacklist": {ip: round(until - now, 1) for ip, until in _IP_BLACKLIST.items()
                             if until > now},
            "ip_check_log": dict(_IP_CHECK_LOG),
        }


def _store_json(url: str, timeout: float, session=None,
                attempts: int = 2) -> tuple[dict | None, bool]:
    """请求商店 JSON，返回 ``(payload, reachable)``。

    * ``session`` 由调用方提供时视为**受控会话**（测试打桩），只用它、不做直连兜底；
    * ``session=None`` 时启用两级兜底：常规请求（含重试，**有本机代理就用**）→
      DoH 解析 + **逐个校验过的** IP 直连；
    * 该主机已被判定「候选 IP 全不通」（分钟级阻断窗口）时直接快速返回，
      **绝不白等一整个超时预算**；
    * ``reachable=False`` 表示**网络层不通**（供熔断判断）；拿到任何响应都算 reachable。
    """
    parts = urlsplit(str(url))
    host = parts.netloc
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    allow_pinned = session is None or _is_real_session(session)

    # 分钟级整体阻断窗口：已知所有候选 IP 都不通 → 直接放弃，别耗预算
    if allow_pinned and _host_cooling(host):
        return None, False

    # 已知常规路线被污染：直接走直连，省掉一轮必超时的等待
    prefer_pinned = allow_pinned and _route_is_bad(host)

    if not prefer_pinned:
        if session is not None or requests is not None:
            getter = session.get if session is not None else requests.get
            # 要留出直连兜底的时间预算，常规路线不能把超时吃满
            primary_timeout = timeout if not allow_pinned else min(timeout, PRIMARY_ATTEMPT_TIMEOUT)
            total_attempts = max(1, _as_int(attempts, 1) or 1)
            for attempt in range(total_attempts):
                try:
                    resp = getter(url, timeout=primary_timeout, headers=headers)
                    resp.raise_for_status()
                    payload = resp.json()
                    if isinstance(payload, Mapping):
                        _note_route(host, True)
                        return dict(payload), True
                except Exception as exc:
                    logger.debug("商店请求失败（%s 第 %d 次）：%s", host, attempt + 1, exc)
                if attempt + 1 < total_attempts:
                    time.sleep(PRICE_RETRY_SLEEP)
            if allow_pinned:
                _note_route(host, False)               # 常规路线失败：接下来优先直连
                logger.info("常规线路访问 %s 失败，改用直连兜底", host)

    if not allow_pinned:
        return None, False

    rounds = max(1, _as_int(attempts, 1) or 1)
    for round_index in range(rounds):                  # 直连重试：每轮重新挑 IP
        ip = _pick_pinned_ip(host, timeout)
        if not ip:
            return None, False
        try:
            status, body = _get_via_ip(host, path, ip, timeout)
        except Exception as exc:
            with _PINNED_LOCK:
                _PINNED_IPS.pop(host, None)           # 这个 IP 失效了，下轮重挑
            _blacklist_ip(ip, type(exc).__name__)      # 并记短时黑名单，别反复踩
            logger.debug("直连 %s(%s) 失败：%s", host, ip, exc)
            if round_index + 1 < rounds:
                time.sleep(PRICE_RETRY_SLEEP)
            continue
        if 200 <= status < 300:
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except (ValueError, UnicodeDecodeError):
                return None, True
            if isinstance(payload, Mapping):
                return dict(payload), True
        return None, True                              # 连上了，只是内容不对
    return None, False


# ---------------------------------------------------------------- 汇率：统一人民币
#
# 用户要求「不同账号国家不同，价格都要换算成人民币」。汇率只用于**非人民币区域**的
# 兜底换算；``cc=CN`` 的官方接口直接返回人民币，不需要任何换算。
#
# 三个源互相兜底（都实测大陆可直连）：
#   1. 中国货币网（chinamoney）——官方人民币中间价，最权威；
#   2. frankfurter.app（欧洲央行参考汇率）；
#   3. open.er-api.com。
# 汇率按天缓存（``data/fx_rates.json``），当天不再联网；离线时用最近一次成功结果。

FX_CACHE_NAME = "fx_rates.json"
FX_TTL_SECONDS = 6 * 3600          # 同一天内复用（含当天重复调用）
CHINAMONEY_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
FRANKFURTER_URL = "https://api.frankfurter.app/latest?from=USD"
ERAPI_URL = "https://open.er-api.com/v6/latest/USD"

_FX_MEMO: dict | None = None       # {"date": str, "source": str, "rates": {CCY: cny_per_unit}}


def fx_cache_path() -> Path:
    return stable_data_dir() / FX_CACHE_NAME


def _read_json_file(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    return data if isinstance(data, Mapping) else None


def _write_json_atomic(path: Path, payload: Mapping) -> None:
    """原子写 JSON（写临时文件再替换），失败静默。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logger.info("写入 %s 失败（忽略）：%s", path.name, exc)


def _rates_from_chinamoney(payload: Mapping) -> dict:
    """中国货币网 -> {币种: 1 单位该币种折合多少人民币}。

    该源同时给「外币/人民币」（如 ``100JPY/CNY``）与「人民币/外币」（如 ``CNY/KRW``）
    两种方向，**都要认**：前者直接换算（注意 ``100`` 这类标价单位），后者取倒数。
    """
    rates: dict[str, float] = {}
    records = payload.get("records") if isinstance(payload, Mapping) else None
    if not isinstance(records, (list, tuple)):
        return rates
    for record in records:
        if not isinstance(record, Mapping):
            continue
        pair = str(record.get("vrtEName") or "").strip().upper()
        if "/" not in pair:
            continue
        left, right = pair.split("/", 1)
        try:
            price = float(str(record.get("price") or "").replace(",", ""))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        def _split(text: str) -> tuple[str, float]:
            digits = "".join(ch for ch in text if ch.isdigit())
            scale = float(digits) if digits else 1.0
            code = text[len(digits):] if digits else text
            return code, (scale if scale > 0 else 1.0)

        if right == "CNY":                           # 外币/人民币：直接得 cny_per_unit
            code, scale = _split(left)
            if code:
                rates[code] = price / scale
        elif left == "CNY":                          # 人民币/外币：取倒数
            code, scale = _split(right)
            if code:
                rates[code] = scale / price
    return rates


def _rates_from_usd_base(payload: Mapping) -> dict:
    """以 USD 为基准的汇率表（frankfurter / er-api）-> {币种: cny_per_unit}。"""
    rates = payload.get("rates") if isinstance(payload, Mapping) else None
    if not isinstance(rates, Mapping):
        return {}
    try:
        cny = float(rates.get("CNY"))
    except (TypeError, ValueError):
        return {}
    if cny <= 0:
        return {}
    out: dict[str, float] = {"USD": cny}
    for code, value in rates.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        code = str(code or "").upper()
        if number > 0 and code:
            out[code] = cny / number
    out["CNY"] = 1.0
    return out


def _fetch_fx_online() -> dict:
    """联网取一次汇率：**三个源合并**（先到的源优先，后面的补缺），全部失败返回 {}。

    合并的意义：中国货币网最权威但只覆盖 25 个币种，某些区域（如阿根廷比索）只有
    国际源才有；反过来国际源覆盖人民币之外的交叉盘不如中间价准。
    """
    providers = (
        ("chinamoney", CHINAMONEY_URL, _rates_from_chinamoney),
        ("frankfurter", FRANKFURTER_URL, _rates_from_usd_base),
        ("er-api", ERAPI_URL, _rates_from_usd_base),
    )
    merged: dict[str, float] = {}
    used: list[str] = []
    for name, url, parser in providers:
        payload, _reachable = _store_json(url, 8.0, session=None, attempts=1)
        if not payload:
            continue
        try:
            rates = parser(payload)
        except Exception as exc:
            logger.info("解析 %s 汇率失败：%s", name, exc)
            continue
        if rates:
            used.append(name)
            for code, value in rates.items():
                merged.setdefault(str(code).upper(), float(value))
    if not merged:
        return {}
    merged["CNY"] = 1.0
    return {"date": date.today().isoformat(), "source": "+".join(used), "rates": merged}


def fx_snapshot(force: bool = False) -> dict:
    """当日汇率快照（按天缓存，离线时退回最近一次成功结果）。"""
    global _FX_MEMO
    today = date.today().isoformat()
    if not force and _FX_MEMO and _FX_MEMO.get("date") == today:
        return _FX_MEMO

    disk = _read_json_file(fx_cache_path()) or {}
    if not force and disk.get("date") == today and isinstance(disk.get("rates"), Mapping):
        _FX_MEMO = dict(disk)
        return _FX_MEMO

    fresh = _fetch_fx_online()
    if fresh:
        _FX_MEMO = fresh
        _write_json_atomic(fx_cache_path(), fresh)
        return _FX_MEMO

    if isinstance(disk.get("rates"), Mapping) and disk.get("rates"):
        logger.info("汇率联网失败，沿用 %s 的缓存（%s）", disk.get("date"), disk.get("source"))
        _FX_MEMO = dict(disk)
        return _FX_MEMO
    return {"date": today, "source": "none", "rates": {"CNY": 1.0}}


def fx_rate(currency: Any) -> tuple[float, str, str]:
    """1 单位 ``currency`` 折合多少人民币 -> (汇率, 来源, 日期)。查不到返回 (0.0, '', '')。"""
    code = str(currency or "").strip().upper()
    if not code:
        return 0.0, "", ""
    if code == "CNY":
        return 1.0, "direct", date.today().isoformat()
    snapshot = fx_snapshot()
    try:
        rate = float((snapshot.get("rates") or {}).get(code))
    except (TypeError, ValueError):
        return 0.0, "", ""
    if rate <= 0:
        return 0.0, "", ""
    return rate, str(snapshot.get("source") or ""), str(snapshot.get("date") or "")


def to_cny_fen(amount: Any, currency: Any) -> dict:
    """把某个币种的**分**金额换算成**人民币分**。

    返回 ``{"cny_fen", "currency", "amount", "rate", "rate_source", "rate_date", "converted"}``；
    汇率缺失时 ``cny_fen`` 为 0 且 ``converted=False``（调用方据此保留「价格未知」）。
    """
    value = _as_int(amount, 0)
    code = str(currency or "").strip().upper() or "CNY"
    if value <= 0:
        return {"cny_fen": 0, "currency": code, "amount": value,
                "rate": 0.0, "rate_source": "", "rate_date": "", "converted": False}
    if code == "CNY":
        return {"cny_fen": value, "currency": "CNY", "amount": value,
                "rate": 1.0, "rate_source": "direct", "rate_date": date.today().isoformat(),
                "converted": False}
    rate, source, stamp = fx_rate(code)
    if rate <= 0:
        return {"cny_fen": 0, "currency": code, "amount": value,
                "rate": 0.0, "rate_source": "", "rate_date": "", "converted": False}
    return {"cny_fen": int(round(value * rate)), "currency": code, "amount": value,
            "rate": rate, "rate_source": source, "rate_date": stamp, "converted": True}


PRICE_AUDIT_ENABLED = os.environ.get("GAMELIFE_PRICE_AUDIT", "") not in ("", "0", "false", "False")


def set_price_audit(enabled: bool) -> None:
    """打开/关闭价格留档（默认关闭，避免日常运行反复写盘）。"""
    global PRICE_AUDIT_ENABLED
    PRICE_AUDIT_ENABLED = bool(enabled)


_PRICE_SOURCE_STATS: dict = {}


def _note_price_source(currency: str) -> None:
    """统计「本次取到的人民币价格来自哪种原币种」（验收/日志用）。"""
    code = str(currency or "CNY").upper()
    with _LOCK:
        _PRICE_SOURCE_STATS[code] = _PRICE_SOURCE_STATS.get(code, 0) + 1


def price_source_stats() -> dict:
    """取价来源统计（只读）。"""
    with _LOCK:
        return dict(_PRICE_SOURCE_STATS)


def reset_price_source_stats() -> None:
    with _LOCK:
        _PRICE_SOURCE_STATS.clear()


def _record_price_audit(appid: int, info: Mapping) -> None:
    """把「原币种/原金额/汇率/来源」留档到 data/price_audit.json（便于核对，不参与计算）。

    默认关闭（``GAMELIFE_PRICE_AUDIT=1`` 或 ``set_price_audit(True)`` 打开）；
    失败一律忽略，绝不影响取价。
    """
    if not info or not PRICE_AUDIT_ENABLED:
        return
    path = price_audit_path()
    table = _read_json_file(path) or {}
    table[str(appid)] = {
        "currency": str(info.get("currency") or "CNY"),
        "amount_fen": _as_int(info.get("amount"), 0),
        "cny_fen": _as_int(info.get("cny_fen"), 0),
        "rate": info.get("rate"),
        "rate_source": str(info.get("rate_source") or ""),
        "rate_date": str(info.get("rate_date") or ""),
        "method": "fx" if info.get("converted") else ("direct_cny" if str(info.get("currency") or "CNY") == "CNY" else "unknown"),
        "updated": date.today().isoformat(),
    }
    _write_json_atomic(path, table)


def _from_store_price(appid: int, url: str, timeout: float, session=None,
                      report: bool = False):
    """请求单个商店 appdetails，抽取价格并**统一换算成人民币分**。

    返回 ``{}`` 或 ``{"price": <人民币分>, "is_free": True}``（``report=True`` 时
    额外返回 reachable 供熔断判断）。规则：

      * ``currency == "CNY"`` —— 官方直接给的就是人民币，原样使用，**零汇率误差**；
      * 其它币种（账号在美区/日区等）—— 按**当日汇率**换算成人民币；
      * 汇率取不到时返回 ``{}``，宁可「价格未知」也不写一个错的数字；
      * ``success`` 但无 ``price_overview``（未上架/区域不可售）→ ``{}``，绝不伪造成免费。
    """
    def _ret(payload, reachable):
        return (payload, reachable) if report else payload

    # 短超时 + 同一端点快速重试 2 次（网络层还含「校验过的 IP 直连」兜底）
    payload, reachable = _store_json(url, timeout, session=session,
                                     attempts=PRICE_RETRY_ATTEMPTS)
    if not payload:
        if not reachable:
            logger.info("商店价格接口不可用（%s）", urlsplit(str(url)).netloc)
        return _ret({}, reachable)

    node = _appdetails_node(payload, appid)
    if not isinstance(node, Mapping) or not node.get("success"):
        return _ret({}, True)          # 该游戏不在这家商店，不算故障
    data = node.get("data") or {}
    if not isinstance(data, Mapping):
        return _ret({}, True)
    overview = data.get("price_overview") or {}
    raw = _as_int(overview.get("initial"), 0) or _as_int(overview.get("final"), 0)
    currency = str(overview.get("currency") or "CNY").strip().upper() or "CNY"
    if raw > 0:
        if currency == "CNY":
            _record_price_audit(appid, {"currency": "CNY", "amount": raw, "cny_fen": raw,
                                        "rate": 1.0, "rate_source": "direct", "rate_date": "",
                                        "converted": False})
            _note_price_source("CNY")
            return _ret({"price": raw}, True)
        converted = to_cny_fen(raw, currency)
        if converted["cny_fen"] > 0:
            _record_price_audit(appid, converted)
            _note_price_source(currency)
            return _ret({"price": converted["cny_fen"]}, True)
        logger.info("价格换算失败（appid=%s，%s %s）：无可用汇率，按「未知」处理",
                    appid, currency, raw)
        return _ret({}, True)
    if data.get("is_free"):
        return _ret({"price": 0, "is_free": True}, True)
    return _ret({}, True)              # success 但无价格（未上架 / 区域不可售）


def price_for(appid: Any, steam_path: str | None = None, session=None,
              region: str | None = None) -> dict:
    """**只取价格**（独立于名字/类型的抓取路径），结果统一为**人民币分**。

    返回 ``{"price": int, "is_free": bool|None}``：拿到价或有免费标记时 ``price`` 为
    人民币分，否则 ``{}``。永远不抛异常，单款超时有界。
    """
    aid = _as_appid(appid)
    if aid is None:
        return {}
    if region is not None:
        set_active_region(region)
    if session is not None:
        return _price_from_official(aid, session)
    if requests is None:                          # 缺 requests 依赖：无法取价，降级为空
        return {}
    try:
        with requests.Session() as own:
            return _price_from_official(aid, own)
    except Exception as exc:                     # Session 创建失败等极端情况
        logger.info("取价失败（appid=%s）：%s", aid, exc)
        return {}


def backfill_prices(
    appids: Iterable,
    steam_path: str | None = None,
    workers: int = PRICE_WORKERS,
    force: bool = False,
    progress: Callable[[int, int, Mapping], None] | None = None,
    quiet: bool = False,
    region: str | None = None,
    rounds: int = 1,
    round_budget: float | None = None,
    on_round: Callable[[int, int, Mapping], None] | None = None,
) -> dict:
    """独立的**价格回填**路径：给一批 appid 取价并写回 ``games.price`` / ``is_free``。

    价格统一为**人民币分**（``cc=CN`` 直取，或按账号区域取价后按当日汇率换算）。

    与 ``fetch_metadata`` 的差别：
      * 不看「是否已有名字」，只要有 appid 就会尝试取价；
      * 默认跳过「已经拿到价格（有价或确认免费）」的行，只补未知；
      * 单款异常不影响整批。

    「温柔 + 可续做」（T7d 要求，``appdetails`` 不支持一次问多款，只能一款一次）：
      * **全局节流**：相邻两次请求之间随机停 ``PRICE_INTERVAL_RANGE`` 秒（与线程数无关，
        线程数再多也不会猛打——高频访问会触发该域名的分钟级整体阻断）；
      * ``rounds``：同一批 appid 扫多轮，**拿到即落库**，没拿到的下一轮再试；
      * ``round_budget``：单轮硬预算（秒），到点停止，剩下的留给下一轮 / 下次启动；
      * ``on_round(round_index, rounds, {"success", "attempts", "seconds", "deferred"})``：
        每轮的成功率回调（可观测，别只报一轮）。

    返回 ``{appid: {"price": int, "is_free": bool|None}}``（仅含本次实际取到价格的项）。
    """
    wanted: list[int] = []
    for raw in appids or []:
        aid = _as_appid(raw)
        if aid is not None and aid not in wanted:
            wanted.append(aid)
    if not wanted:
        return {}

    if not force:
        known = _cached_rows(wanted)
        wanted = [aid for aid in wanted if not _has_price(known.get(aid))]

    if not wanted:
        if not quiet:
            print("价格回填：所有游戏都已有价格（或确认为免费），无需抓取")
        return {}

    total = len(wanted)
    result: dict[int, dict] = {}
    done_set: set[int] = set()
    max_rounds = max(1, _as_int(rounds, 1) or 1)
    budget = None if round_budget is None else max(1.0, float(round_budget))

    if region is not None:                       # 显式指定区域（否则自动探测账号区域）
        set_active_region(region)

    if not quiet:
        print(f"价格回填：正在为 {total} 款游戏取价（温柔抓取：相邻请求间隔 "
              f"{PRICE_INTERVAL_RANGE[0]:.1f}~{PRICE_INTERVAL_RANGE[1]:.1f} 秒，最多 {max_rounds} 轮）...")

    def _work(aid: int, deadline: float | None) -> tuple[int, dict, bool]:
        """取一款的价；到点（deadline）就跳过（返回 skipped=True，留给下一轮）。"""
        if deadline is not None and time.monotonic() >= deadline:
            return aid, {}, True
        _pace_price_request()                    # 全局节流：与线程数无关
        if deadline is not None and time.monotonic() >= deadline:
            return aid, {}, True
        data = {}
        if requests is not None:
            try:
                with requests.Session() as session:
                    data = _price_from_official(aid, session)
            except Exception as exc:             # 单款异常绝不能中断整批
                logger.warning("取价异常（appid=%s）：%s", aid, exc)
                data = {}
        if data:
            _save_price(aid, data)               # **拿到即落库**（进度可累积）
        return aid, data or {}, False

    pending = list(wanted)
    for round_index in range(1, max_rounds + 1):
        if not pending:
            break
        started = time.monotonic()
        deadline = (started + budget) if budget is not None else None
        pool_size = max(1, min(_as_int(workers, PRICE_WORKERS) or PRICE_WORKERS, len(pending)))
        if pool_size == 1 or len(pending) == 1:
            triples = [_work(aid, deadline) for aid in pending]
        else:
            triples = []
            with ThreadPoolExecutor(max_workers=pool_size) as pool:
                futures = [pool.submit(_work, aid, deadline) for aid in pending]
                for future in as_completed(futures):
                    try:
                        triples.append(future.result())
                    except Exception as exc:
                        logger.warning("取价线程异常：%s", exc)

        got_this_round: list[int] = []
        skipped: list[int] = []
        for aid, data, was_skipped in triples:
            if was_skipped:
                skipped.append(aid)
                continue
            done_set.add(aid)
            if data:
                result[aid] = dict(data)
                got_this_round.append(aid)
            if progress is not None:
                try:
                    progress(len(done_set), total, {"appid": aid, **data})
                except Exception:
                    pass
        seconds = round(time.monotonic() - started, 1)
        stats = {"success": len(got_this_round), "attempts": len(triples) - len(skipped),
                 "seconds": seconds, "deferred": len(skipped), "round": round_index,
                 "rounds": max_rounds}
        if not quiet:
            print(f"价格回填 第 {round_index}/{max_rounds} 轮：成功 {stats['success']}/"
                  f"{stats['attempts']}（累计 {len(result)}/{total}）耗时 {seconds}s"
                  + (f"，{len(skipped)} 款留给下一轮" if skipped else ""))
        if on_round is not None:
            try:
                on_round(round_index, max_rounds, stats)
            except Exception:
                pass
        # 下一轮只重试「这一轮真的问过、但没拿到」的（到点跳过的也算，因为它们还没被问过）
        pending = [aid for aid in pending if aid not in result]

    if not quiet:
        print(f"价格回填完成：{len(result)}/{total} 款取到价格"
              + ("" if result else "（本次未取到新价格）")
              + (f"，剩 {len(pending)} 款留给下次" if pending else ""))
    return result


_PRICE_PACE_LOCK = threading.Lock()
_PRICE_LAST_REQUEST = 0.0


def _pace_price_request() -> None:
    """**全局**节流：相邻两次取价请求之间随机停 1.5~2.0 秒。

    与线程数无关（多线程也会在这里排队），因为官方商店在大陆是按主机限流的：
    猛打会触发「分钟级整体阻断窗口」。温柔一点，慢但拿得到。
    """
    global _PRICE_LAST_REQUEST
    with _PRICE_PACE_LOCK:
        now = time.monotonic()
        wait = _PRICE_LAST_REQUEST + random.uniform(*PRICE_INTERVAL_RANGE) - now
        if wait > 0:
            time.sleep(wait)
        _PRICE_LAST_REQUEST = time.monotonic()


def _save_price(appid: int, data: Mapping) -> None:
    """只更新价格相关列（不动其它字段，不整行覆盖）。"""
    _ensure_price_column()
    price = _as_int(data.get("price"), 0)
    is_free = data.get("is_free")
    # 来源如实记录：新值带来源就写，没带就保留旧值（不覆盖成空）
    source = str(data.get("price_source") or "").strip()
    with _LOCK:
        init_db()
        db = get_connection()
        try:
            db.execute(
                "UPDATE games SET price=?, is_free=?, last_updated=?,"
                " price_source=COALESCE(NULLIF(?, ''), price_source) WHERE appid=?",
                (price, 1 if is_free else (0 if is_free is False else None),
                 date.today().isoformat(), source, int(appid)),
            )
            db.commit()
        except Exception as exc:
            logger.warning("写入价格失败（appid=%s）：%s", appid, exc)
        finally:
            db.close()


def _extra_map() -> dict:
    """人工校对补充表（data/name_cn_extra.json），只读一次。"""
    global _EXTRA_CACHE
    if _EXTRA_CACHE is None:
        table: dict = {}
        try:
            import json as _json

            path = readonly_data_path("name_cn_extra.json")
            if path.is_file():
                raw = _json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, Mapping):
                    table = {str(k): str(v) for k, v in raw.items()
                             if not str(k).startswith("_") and str(v).strip()}
        except Exception as exc:
            logger.info("读取中文名补充表失败：%s", exc)
        _EXTRA_CACHE = table
    return _EXTRA_CACHE


def _from_extra(appid: int) -> dict:
    """人工校对补充表（最后兜底，不覆盖官方中文名）。"""
    name = str(_extra_map().get(str(appid)) or "").strip()
    return {"name_cn": name} if name else {}


def _from_baike(appid: int, name_en: str) -> dict:
    """百度百科补名（仅当官方与补充表都拿不到时）。"""
    try:
        from core import name_cn_baike
    except Exception:
        return {}
    try:
        value = name_cn_baike.lookup(appid, name_en)
    except Exception as exc:
        logger.info("百科补名失败（appid=%s）：%s", appid, exc)
        return {}
    return {"name_cn": value} if value else {}


# ------------------------------------------------------------------ 单款聚合

def empty_record(appid: Any) -> dict:
    """一条空记录（字段齐全，便于前端稳定渲染）。"""
    return {
        "appid": _as_appid(appid),
        "name": "", "name_cn": "", "genres": [], "genres_cn": [],
        "categories": [], "developers": [], "publisher": "",
        "price": 0, "is_free": None, "price_source": "", "release_date": "",
        "header_image": "", "source": "",
    }


def fetch_one(appid: Any, steam_path: str | None = None, session=None,
              with_mirror: bool = True, with_price: bool = True) -> dict:
    """抓取单款游戏的元数据（多源合并）。永不抛异常。

    ``with_price=True``（默认）时，无论是否已有名字都会走**独立价格路径**取价，
    修复了历史上「已有名字就永远不取价」的问题（面板性价比榜空榜的根因）。
    """
    aid = _as_appid(appid)
    record = empty_record(aid)
    if aid is None:
        return record

    sources: list[str] = []
    _merge(record, _from_appinfo(aid, steam_path), "appinfo", sources)
    if with_mirror:
        _merge(record, _from_steamcmd(aid, session), "steamcmd", sources)
    if not record.get("name_cn") or not record.get("genres_cn"):
        _merge(record, _from_steamchina(aid, session), "steamchina", sources)
    if not record.get("name") or not record.get("genres"):
        _merge(record, _from_official(aid, session), "steampowered", sources)
    if not record.get("name_cn"):
        _merge(record, _from_extra(aid), "extra", sources)
    if not record.get("name_cn") and str(record.get("name") or "").strip():
        _merge(record, _from_baike(aid, str(record.get("name") or "")), "baike", sources)
    if with_price:
        _merge(record, _price_from_official(aid, session), "price", sources)
    record["source"] = "+".join(sources)
    return record


# ------------------------------------------------------------------ 缓存读写

def _cached_rows(appids: Iterable[int]) -> dict[int, dict]:
    init_db()
    db = get_connection()
    try:
        _ensure_price_column()
        rows = db.execute(
            "SELECT appid, name, genres, categories, developers, price, release_date,"
            " last_updated, name_cn, genres_cn, header_image, publisher, source, is_free,"
            " miss_at, price_source FROM games"
        ).fetchall()
    except Exception:
        return {}
    finally:
        db.close()

    wanted = set()
    for appid in appids:
        aid = _as_appid(appid)
        if aid is not None:
            wanted.add(aid)

    result: dict[int, dict] = {}
    for row in rows:
        aid = _as_int(row[0], -1)
        if aid not in wanted:
            continue
        result[aid] = {
            "appid": aid,
            "name": row[1] or "",
            "genres": _json_list(row[2]),
            "categories": _json_list(row[3]),
            "developers": _json_list(row[4]),
            "price": _as_int(row[5], 0),
            "is_free": (True if _as_int(row[13], 0) else None) if len(row) > 13 else None,
            "release_date": row[6] or "",
            "last_updated": row[7] or "",
            "name_cn": (row[8] or "") if len(row) > 8 else "",
            "genres_cn": _json_list(row[9]) if len(row) > 9 else [],
            "header_image": (row[10] or "") if len(row) > 10 else "",
            "publisher": (row[11] or "") if len(row) > 11 else "",
            "source": (row[12] or "") if len(row) > 12 else "",
            "miss_at": (row[14] or "") if len(row) > 14 else "",
            "price_source": (row[15] or "") if len(row) > 15 else "",
        }
    return result


def _json_col(value: Any) -> str:
    """列表 -> 库里存的 JSON 字符串。**空列表一律写成空串**（不是 ``"[]"``）。

    UPSERT 用 ``COALESCE(NULLIF(excluded.X,''), games.X)`` 实现「新来源缺字段时不擦旧值」，
    而 ``"[]"`` 是非空字符串，会被误判为「有新值」，从而把库里已有的类型/开发商擦空。
    """
    items = _json_list(value)
    return json.dumps(items, ensure_ascii=False) if items else ""


def _json_list(raw: Any) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if str(x).strip()]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(data, list):
        return [str(x) for x in data if str(x).strip()]
    return []


def _row_usable(row: Mapping | None) -> bool:
    """缓存里的这一行还能用吗（有名字或中文名，且在有效期内）。

    「什么都没抓到」的负缓存行**只认短 TTL**（``MISS_TTL_HOURS``）：一行如果
    没有 ``miss_at`` 时间戳（T7 之前写下的历史负缓存），视为**立即失效**——
    这样修好解析 bug 后，老行下一次扫描就会被自动重试，不用等 3 天。
    """
    if not row:
        return False
    fresh = _is_fresh(str(row.get("last_updated") or ""))
    if not fresh:
        return False
    if str(row.get("source") or "") == "miss":
        return _within_hours(str(row.get("miss_at") or ""), MISS_TTL_HOURS)
    if not str(row.get("name") or "").strip() and not str(row.get("name_cn") or "").strip():
        return False
    return _within_days(str(row.get("last_updated") or ""), CACHE_TTL_DAYS)


def _within_hours(stamp: str, hours: float) -> bool:
    """时间戳（``%Y-%m-%dT%H:%M:%S``）是否在最近 ``hours`` 小时内。

    解析不了（历史行没有时间戳 / 格式坏）→ False，即**立即失效、下次重试**。
    """
    try:
        stored = datetime.strptime(str(stamp or ""), "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return False
    try:
        limit = max(60.0, float(hours) * 3600.0)
    except (TypeError, ValueError):
        limit = MISS_TTL_HOURS * 3600.0
    return (datetime.now() - stored).total_seconds() < limit


def _within_days(last_updated: str, days: int) -> bool:
    try:
        stored = datetime.strptime(last_updated, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return (date.today() - stored).days < max(1, int(days))


def _save(record: Mapping) -> None:
    """UPSERT 一行元数据（按 appid 单行 upsert，绝不全量覆盖表，不读内存快照）。"""
    aid = _as_appid(record.get("appid"))
    if aid is None:
        return
    _ensure_price_column()
    payload = {
        "name": str(record.get("name") or ""),
        # ⚠️ 列表列必须把「空列表」写成空串而不是 ``"[]"``：
        # UPSERT 用 ``COALESCE(NULLIF(excluded.genres,''), games.genres)`` 保护旧值，
        # 而 ``"[]"`` 是**非空字符串**，会被当成「有新值」从而把库里已有的类型擦成空。
        # （T9 实测踩到过：官方先写类型、小黑盒后写中文名 -> 类型被擦掉。）
        "genres": _json_col(record.get("genres")),
        "categories": _json_col(record.get("categories")),
        "developers": _json_col(record.get("developers")),
        "price": _as_int(record.get("price"), 0),
        "release_date": str(record.get("release_date") or ""),
        "name_cn": str(record.get("name_cn") or ""),
        # ⚠️ 必须用 ``_json_col``：空列表要写成**空串**而不是 ``"[]"``。
        # 这里曾经直接 ``json.dumps``，于是 ``"[]"`` 被 UPSERT 当成「有新值」，
        # 把库里已有的**中文类型擦成空**（T11 实测复现：一次断网补齐就把
        # genres_cn 从 [动作, 角色扮演] 变成 []）。
        "genres_cn": _json_col(record.get("genres_cn")),
        "header_image": str(record.get("header_image") or ""),
        "publisher": str(record.get("publisher") or ""),
        "source": str(record.get("source") or "") or "miss",
        "price_source": str(record.get("price_source") or ""),
    }
    is_free = record.get("is_free")
    is_free_col = 1 if is_free else (0 if is_free is False else None)
    # 负缓存时间戳：只有「什么都没抓到」的行才写。
    # 历史行没有这个字段 -> _row_usable 视为立即失效 -> 自动重试（一次性失效，不必等 3 天）。
    miss_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S") if payload["source"] == "miss" else None
    with _LOCK:
        init_db()
        db = get_connection()
        try:
            db.execute(
                "INSERT INTO games (appid, name, genres, categories, developers, price, release_date,"
                " last_updated, name_cn, genres_cn, header_image, publisher, source, is_free, miss_at,"
                " price_source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(appid) DO UPDATE SET"
                # 文本列一律「非空才覆盖」：新来源缺字段时**绝不把已有的名字/类型/封面上擦掉**
                # （小黑盒只带中文名与封面 URL，官方来源只带英文名与类型，两边必须能叠加）
                " name=COALESCE(NULLIF(excluded.name,''), games.name),"
                " genres=COALESCE(NULLIF(excluded.genres,''), games.genres),"
                " categories=COALESCE(NULLIF(excluded.categories,''), games.categories),"
                " developers=COALESCE(NULLIF(excluded.developers,''), games.developers),"
                " release_date=COALESCE(NULLIF(excluded.release_date,''), games.release_date),"
                " name_cn=COALESCE(NULLIF(excluded.name_cn,''), games.name_cn),"
                " genres_cn=COALESCE(NULLIF(excluded.genres_cn,''), games.genres_cn),"
                " header_image=COALESCE(NULLIF(excluded.header_image,''), games.header_image),"
                " publisher=COALESCE(NULLIF(excluded.publisher,''), games.publisher),"
                " last_updated=excluded.last_updated, source=excluded.source,"
                " miss_at=excluded.miss_at,"
                # 价格三态：只有新值带来「有价/免费」信息时才覆盖，避免把已知价格改回 NULL
                " is_free=COALESCE(excluded.is_free, games.is_free),"
                " price_source=COALESCE(NULLIF(excluded.price_source,''), games.price_source),"
                " price=CASE WHEN excluded.price > 0 THEN excluded.price"
                "            WHEN excluded.is_free = 1 THEN 0"
                "            ELSE games.price END",
                (aid, payload["name"], payload["genres"], payload["categories"], payload["developers"],
                 payload["price"], payload["release_date"], date.today().isoformat(), payload["name_cn"],
                 payload["genres_cn"], payload["header_image"], payload["publisher"], payload["source"],
                 is_free_col, miss_at, payload["price_source"]),
            )
            db.commit()
        except Exception as exc:
            logger.warning("写入元数据缓存失败（appid=%s）：%s", aid, exc)
        finally:
            db.close()


# ------------------------------------------------------------------ 对外主入口

def invalidate_cache(appids=None) -> None:
    """抓取/写入成功后通知只读缓存失效（面板复用同一进程时也看得到新数据）。

    ``appids=None`` 表示整表失效；给 appid 时只失效这些行。任何异常都忽略。
    """
    try:
        keys = None if appids is None else [int(a) for a in appids]
    except (TypeError, ValueError):
        keys = None
    try:
        refresh = globals().get("_invalidate_rows")
        if callable(refresh):
            refresh(keys)
    except Exception:
        pass
    try:
        hook = globals().get("_CACHE_HOOK")
        if callable(hook):
            hook(keys)
    except Exception:
        pass


def fetch_metadata(
    appids: list,
    steam_path: str | None = None,
    workers: int = DEFAULT_WORKERS,
    progress: Callable[[int, int, Mapping], None] | None = None,
    force: bool = False,
    quiet: bool = False,
    with_price: bool = True,
    with_xiaoheihe: bool = True,
) -> dict:
    """抓取（并缓存）给定 appid 列表的元数据。

    - 命中缓存且在有效期内的直接返回，不发请求；
    - 其余并发抓取（默认 6 线程），逐个写库并回调 progress(done, total, record)；
    - ``with_xiaoheihe=True``（默认，T9）：抓完再跑一遍**小黑盒批量补数**（40 款/次），
      补中文名 / 封面 URL / 价格（只填空缺，绝不覆盖已有值），国区人民币实价；
    - ``with_price=True``（默认）：再跑一遍**独立价格回填**（``backfill_prices``），
      给「已有名字但价格未知」的游戏补价——历史上这些游戏因为「缺名字才取」的把门
      永远不会取价（性价比榜空榜的根因）；
    - 返回 **所有** 请求的 appid -> 记录（含缓存命中项），失败项为空字段，绝不抛异常。
    """
    wanted: list[int] = []
    for appid in appids or []:
        aid = _as_appid(appid)
        if aid is not None and aid not in wanted:
            wanted.append(aid)
    if not wanted:
        return {}

    cached = {} if force else _cached_rows(wanted)
    result: dict[int, dict] = {}
    need: list[int] = []
    for aid in wanted:
        row = cached.get(aid)
        if _row_usable(row):
            result[aid] = dict(row)
        else:
            need.append(aid)

    if not need:
        if with_xiaoheihe:
            _xiaoheihe_pass(result, wanted, quiet)
        if with_price:
            _price_pass(result, wanted, steam_path, workers, quiet)
        return result

    if not quiet:
        print(f"正在抓取 {len(need)} 款游戏的元数据（本地 appinfo + 官方镜像，无需加速器）...")

    total = len(need)
    done = 0
    failed: list[int] = []

    def _work(aid: int) -> dict:
        # with_price 必须**透传**：调用方把价格拆到独立阶段（with_price=False）时，
        # 元数据阶段就不该顺带取价（否则每个 appid 多打一次价格请求，还抢在
        # 价格阶段之前把价格写死，让独立阶段的进度与多轮兜底形同虚设）。
        if requests is not None:
            with requests.Session() as session:
                record = fetch_one(aid, steam_path, session=session, with_price=with_price)
        else:
            record = fetch_one(aid, steam_path, session=None, with_mirror=False,
                              with_price=with_price)
        time.sleep(REQUEST_INTERVAL)
        return record

    pool_size = max(1, min(_as_int(workers, DEFAULT_WORKERS) or DEFAULT_WORKERS, total))
    if pool_size == 1 or total == 1:
        for aid in need:
            record = _work(aid)
            _finish(record, result, failed, progress, done, total)
            done += 1
            _tick(done, total, record, quiet)
    else:
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            futures = {pool.submit(_work, aid): aid for aid in need}
            for future in as_completed(futures):
                aid = futures[future]
                try:
                    record = future.result()
                except Exception as exc:            # 线程内异常也不能中断整体
                    logger.warning("抓取 %s 时出错：%s", aid, exc)
                    record = {"appid": aid, "source": ""}
                _finish(record, result, failed, progress, done, total)
                done += 1
                _tick(done, total, record, quiet)

    if failed and not quiet:
        print(f"  {len(failed)} 款未抓到元数据（已记负缓存，{MISS_TTL_HOURS} 小时内不重复请求）")
    if with_xiaoheihe:
        _xiaoheihe_pass(result, wanted, quiet)
    if with_price:
        _price_pass(result, wanted, steam_path, workers, quiet)
    return result


def _price_pass(result: dict, wanted: list, steam_path: str | None,
                workers: int, quiet: bool) -> None:
    """对「价格未知」的 appid 跑独立价格回填，并把结果并回 result（原地）。

    这是「元数据」与「价格」两条路径解耦后，保证已有名字的游戏也会取价的关键一步。
    委托给 ``backfill_prices``（全局节流，线程数只影响排队不影响猛打的速率）。
    """
    try:
        priced = backfill_prices(wanted, steam_path=steam_path,
                                 workers=PRICE_WORKERS, quiet=quiet)
    except Exception as exc:
        logger.warning("价格回填失败（已忽略）：%s", exc)
        return
    if not priced:
        return
    for aid, data in priced.items():
        record = result.setdefault(aid, empty_record(aid))
        if data.get("price"):
            record["price"] = _as_int(data.get("price"), 0)
        if data.get("is_free"):
            record["is_free"] = True
        if data.get("price_source"):
            record["price_source"] = str(data.get("price_source"))
        source = str(record.get("source") or "")
        if "price" not in source.split("+"):
            record["source"] = (source + "+price") if source else "price"


def _xiaoheihe_pass(result: dict, appids: list, quiet: bool) -> int:
    """小黑盒批量补「中文名 / 封面 URL / 价格」：**只填空缺，绝不覆盖已有值**。

    跑在元数据抓取**之后**：官方 / 镜像拿不到的字段由它补（它的 ``genres`` 恒为空，
    类型仍来自官方商店 / 蒸汽平台 / 本地 appinfo）。返回真正写入的行数；
    任何异常都吞掉——小黑盒挂了也绝不能拖垮补齐流程。
    """
    try:
        found = xiaoheihe_batch(appids)
    except Exception as exc:
        logger.info("小黑盒批量补数失败（已忽略）：%s", exc)
        return 0
    if not found:
        return 0

    wanted_ids = set()
    for raw in appids or []:
        aid = _as_appid(raw)
        if aid is not None:
            wanted_ids.add(aid)

    touched = 0
    for aid, data in found.items():
        if wanted_ids and aid not in wanted_ids:     # 只处理本次请求范围内的 appid
            continue
        record = result.get(aid)
        merged = dict(record) if isinstance(record, Mapping) else {"appid": aid}
        merged.setdefault("appid", aid)
        changed = False

        if not str(merged.get("name_cn") or "").strip() and data.get("name_cn"):
            merged["name_cn"] = data["name_cn"]
            changed = True
        if not str(merged.get("header_image") or "").strip() and data.get("header_image"):
            merged["header_image"] = data["header_image"]
            changed = True
        if not _has_price(merged) and _has_price(data):
            if data.get("is_free"):
                merged["is_free"] = True
                merged["price"] = 0
            elif _as_int(data.get("price"), 0) > 0:
                merged["price"] = _as_int(data.get("price"), 0)
            merged["price_source"] = str(data.get("price_source") or "xiaoheihe")
            changed = True
        if not changed:
            continue

        source = str(merged.get("source") or "")
        parts = [part for part in source.split("+") if part]
        if "xiaoheihe" not in parts:
            parts.append("xiaoheihe")
        merged["source"] = "+".join(parts)
        _save(merged)
        result[aid] = merged
        touched += 1
    if touched and not quiet:
        print(f"小黑盒补数：{touched} 款的中文名 / 封面 / 价格已入库（命中 {len(found)}/{len(appids)}）")
    return touched


def _finish(record: Mapping, result: dict, failed: list, progress, done: int, total: int) -> None:
    aid = _as_appid(record.get("appid"))
    if aid is not None:
        result[aid] = dict(record)
    if not str(record.get("name") or "").strip() and not str(record.get("name_cn") or "").strip():
        failed.append(aid)
    _save(record)
    if progress is not None:
        try:
            progress(done, total, record)
        except Exception:
            pass


def _tick(done: int, total: int, record: Mapping, quiet: bool) -> None:
    if quiet:
        return
    if done % 10 == 0 or done == total:
        print(f"进度: {done}/{total}")


def cached_names_cn() -> dict:
    """只读缓存里的「appid -> 中文名」映射（不联网），供面板快速取用。"""
    init_db()
    out: dict[int, str] = {}
    db = get_connection()
    try:
        rows = db.execute("SELECT appid, name_cn FROM games").fetchall()
    except Exception:
        return {}
    finally:
        db.close()
    for row in rows:
        aid = _as_int(row[0], -1)
        name = str(row[1] or "").strip()
        if aid >= 0 and name:
            out[aid] = name
    return out


def cached_metadata(appids: Iterable) -> dict:
    """只读缓存（不联网）。"""
    return _cached_rows(appids)


def clear_official_block() -> None:
    """测试用：重置"官方商店不可达"标记。"""
    global _OFFICIAL_BLOCKED
    with _LOCK:
        _OFFICIAL_BLOCKED = False


def official_blocked() -> bool:
    return _OFFICIAL_BLOCKED
