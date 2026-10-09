"""真实数据桥接：把「库数据」转成渲染层各页面要求的 payload。

职责
----
`main.py` 的 6 个可视化命令（report / graveyard / starmap / timeline /
today / checkup）原本只支持 `--demo` / `--data <JSON>`，没有任何通道把真实
Steam 库喂进去。本模块负责把 `_load_library()` 产出的
``play_stats``（分钟 / unix 秒）、``games_meta``、``classified``
（含 ``status`` / ``status_name``）转换为各渲染模块 docstring 里约定的
payload 结构，字段名照抄渲染层约定，**不做任何渲染层语义改动**。

单位换算
--------
库侧 ``play_stats.playtime`` 是**分钟**，渲染层的 ``playtime`` / ``hours``
一律是**小时**，这里统一由 :func:`minutes_to_hours` 换算。

库范围口径（重要）
------------------
状态/统计引擎把库定义为 ``set(play_stats) | set(games)`` 的并集，而
``core/store_api.fetch_metadata`` 会把整个 ``games`` 缓存表都返回，缓存里
可能存在**本账号并没有**的 appid（实测主库 174 款被算成 177 款）。
本模块一律以 ``play_stats``（localconfig.vdf 里真实拥有）为基准界定「我的库」，
``games_meta`` 只用于**补充** name / genres / price，绝不扩大库范围。

中文化（展示层）
----------------
本地 ``games`` 缓存表带官方 ``name_cn`` / ``genres_cn`` 两列，本模块**只把它们用于展示**：
``game_name`` / ``game_genre`` 中文优先、缺失回落官方原文、最后才 ``appid N`` / ``未知``。
判定逻辑（免费标记、高难度标签等）仍走官方英文名，避免中英两套数据互相打架；
中文列缺失、或与英文同名（``VRChat`` / ``PICO PARK``）时保持原文，页面显示英文而不是空白。
本模块绝不为中文名联网。

分层约定
--------
本模块位于 core 层之上、渲染层之下；对 engine 层（stats / persona /
value_ranking）的 import 全部延迟到函数内部，engine 不可用时降级为内置
简化统计，保证 CLI 不中断（与 main.py 的延迟 import 约定一致）。

非游戏条目（展示层过滤）
------------------------
Steam 会把 **Steam Client / 壁纸引擎 / 性能测试工具** 这类非游戏条目也写进
``localconfig.vdf``，于是它们混进「我的库」。面板的「游戏明细」「推荐」「性价比榜」
「族谱」都已经过滤掉它们，但 6 张可视化页面曾经漏了，用户会看到「Steam Client」的墓碑、
「壁纸引擎」的星星、时间轴里「你开始玩《EALink》」。本模块在六页唯一共同入口
:func:`build_payload` 上按与面板一致的判定（:func:`is_non_game`）剔除非游戏条目，
**只过滤展示**、不删数据。

六页**一律**过滤（含 report / checkup）：历史上只看护 graveyard / starmap /
timeline / today 四页，理由是「report / checkup 不逐款列库、实测本就不含非游戏
条目」—— 这个理由是**错的**，它们的 total_games 与类型时长都含壁纸引擎这类条目。
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

from core import non_game
from core.snapshot import _day_from_ts as day_from_ts

logger = logging.getLogger(__name__)

DEFAULT_USER_NAME = "神秘玩家"
UNKNOWN_GENRE = "未知"
# 本模块内部使用的展示字段名（本地库列名同名，便于直接透传）
CN_NAME_KEY = "name_cn"
CN_GENRES_KEY = "genres_cn"

# 人格维度顺序与中文名（与 engine/persona.py 的 DIMENSION_WEIGHTS 对应）
DIMENSION_ORDER = ("purchase", "execution", "focus", "breadth", "courage")
DIMENSION_NAMES = {
    "purchase": "购买力",
    "execution": "执行力",
    "focus": "专注度",
    "breadth": "口味广度",
    "courage": "勇气值",
}

# 渲染层（graveyard / starmap）使用的内部状态键
STATUS_UNPLAYED = "未玩"
STATUS_PLAYING = "在玩"
STATUS_SHELVED = "搁置"
STATUS_ADDICTED = "沉迷"

# 与 engine/stats.py 默认规则一致：沉迷阈值 3000 分钟（50 小时）
ADDICTED_MIN_MINUTES = 3000

RENDER_KINDS = ("report", "graveyard", "starmap", "timeline", "today", "checkup")

# 六页**一律**剔除非游戏条目（见文件头「非游戏条目」一节）。
# 历史上只看护 graveyard / starmap / timeline / today 四页，理由是「report / checkup
# 不逐款列库、实测本就不含非游戏条目」—— 这个理由是**错的**：report 的 total_games
# 与 genres 时长、checkup 的 total_games / total_playtime_hours / top_genres 都含
# 壁纸引擎这类条目的时长（实测 174 款 / 2174.47h，剔除后 166 款 / 2139.67h）。
LIBRARY_DETAIL_KINDS = frozenset(RENDER_KINDS)


# --------------------------------------------------------------- 非游戏条目
#
# 判定口径已收敛到 :mod:`core.non_game`（全产品唯一口径）：
#   * 黑名单 appid       —— {7, 760, 241100}
#   * appinfo type 集合  —— NON_GAME_TYPES
#   * **类型未知一律当游戏放行**（铁律，绝不误杀真游戏）
#
# 本模块只做薄转发，不再自己维护一份黑名单/类型集合（历史上这里与 webapp/api.py
# 各有一份同名同值副本，正是「同一个库在不同界面显示不同数字」的根源）。
# 为什么不直接 ``from webapp.api import is_non_game``：core 是 webapp 的下游，
# 反向 import 会让 core 依赖上层。core.non_game 与 core.appinfo 同层，不产生环。
NON_GAME_APPIDS = non_game.NON_GAME_APPIDS
NON_GAME_TYPES = non_game.NON_GAME_TYPES
NON_GAME_MEMO = non_game._MEMO


def is_non_game(appid: Any) -> bool:
    """薄转发到 :func:`core.non_game.is_non_game`（调用时取用，便于统一打桩）。"""
    return non_game.is_non_game(appid)


def exclude_non_games(play_stats: Mapping) -> dict:
    """薄转发到 :func:`core.non_game.exclude_non_games`。"""
    return non_game.exclude_non_games(play_stats)


# ------------------------------------------------------------------ 基础工具

def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def minutes_to_hours(minutes: Any, decimals: int = 2) -> float:
    """分钟 -> 小时（渲染层统一使用小时）。"""
    return round(_to_int(minutes) / 60.0, decimals)


def library_appids(play_stats: Mapping) -> list[int]:
    """库范围 = play_stats 的 keys（VDF 真实拥有），排序后返回。"""
    appids: set[int] = set()
    for appid in (play_stats or {}):
        try:
            appids.add(int(appid))
        except (TypeError, ValueError):
            continue
    return sorted(appids)


# ------------------------------------------------------------------- 封面
#
# 页面只需要「相对产物 HTML 的图片地址」：这里把本地已有的封面（本工具缓存
# data/covers 或本机 Steam librarycache）**复制**到产物目录下的 covers/ 子目录，
# 再返回 covers/<file> 这样的相对地址。
# 好处：产物是自包含的——整个 output/ 拷到别的机器、直接双击打开都能看到图，
# 不依赖本地 Web 服务，也不写 Steam 目录、不联网。

COVER_SUBDIR = "covers"

# 魔数 -> 落盘扩展名（Steam 缓存里很多图没有后缀，只能按魔数认）
_KIND_EXT = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "gif": ".gif",
    "bmp": ".bmp",
}


def resolve_covers(appids, steam_path: Optional[str] = None) -> dict[str, str]:
    """本地解析封面（**绝不联网**）：返回 {str(appid): 绝对路径}。

    复用 :func:`core.artwork.cover_for_panel`（先本工具缓存、再本机 Steam
    librarycache）。取不到的 appid **不会**出现在结果里，页面据此降级为占位块。
    artwork 不可用时返回空 dict，页面照常生成（只是没有图片）。
    """
    try:
        from core import artwork
    except Exception as exc:  # 依赖缺失不应让整页失败
        logger.warning("封面模块不可用，页面将不含游戏图片：%s", exc)
        return {}

    if not steam_path:
        try:
            from core import locator

            steam_path = locator.find_steam_path()
        except Exception as exc:
            logger.debug("定位 Steam 目录失败（不影响出图，仅少一部分封面）：%s", exc)
            steam_path = None

    covers: dict[str, str] = {}
    for raw in appids or ():
        try:
            aid = int(raw)
        except (TypeError, ValueError):
            continue
        if non_game.is_non_game(aid):
            # 非游戏条目不复制封面（展示层过滤；不删 data/covers 里的既有文件）
            continue
        try:
            # 竖版优先：库里绝大多数是 300x450 竖版海报，用横版会把它裁成细条
            picked = artwork.panel_cover(aid, steam_path)
            path = picked.get("path") if picked.get("ok") else None
            if path is None:                       # 竖版能力不可用时退回历史行为
                path = artwork.cover_for_panel(aid, steam_path)
        except Exception as exc:  # 单款失败不影响整页
            logger.debug("解析封面失败（appid=%s）：%s", aid, exc)
            path = None
        if path is not None:
            covers[str(aid)] = str(path)
    return covers


def prepare_covers(appids, out_dir, steam_path: Optional[str] = None) -> dict[str, str]:
    """一步到位：本地解析封面 + 复制到 ``<out_dir>/covers``，返回 {appid: 相对 url}。"""
    return materialize_covers(resolve_covers(appids, steam_path), out_dir)


def prepare_covers_meta(appids, out_dir, steam_path: Optional[str] = None) -> dict[str, dict]:
    """同 :func:`prepare_covers`，但额外给出封面朝向，供模板决定是否裁切。

    返回 ``{str(appid): {"url": "covers/x.jpg", "kind": "portrait"|"landscape"|"unknown"}}``。
    朝向取自**实际要显示的那张图**（按魔数读尺寸），不靠猜测。
    任何异常都降级为空 dict / 部分结果，绝不抛。
    """
    try:
        paths = resolve_covers(appids, steam_path)
    except Exception as exc:
        logger.debug("解析封面失败：%s", exc)
        return {}
    urls = materialize_covers(paths, out_dir)
    try:
        from core import artwork
    except Exception:
        return {aid: {"url": url, "kind": "unknown"} for aid, url in urls.items()}

    meta: dict[str, dict] = {}
    for aid, url in urls.items():
        kind = "unknown"
        source = paths.get(aid)
        if source:
            try:
                kind = str(artwork.cover_kind(source) or "unknown")
            except Exception:
                kind = "unknown"
        meta[aid] = {"url": url, "kind": kind}
    return meta


def _cover_ext(path: Path) -> str:
    """按魔数（而非后缀）决定落盘扩展名；认不出时退回 .jpg。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return ".jpg"
    try:
        from core import artwork

        kind = artwork.image_kind_bytes(head)
    except Exception:
        kind = None
    return _KIND_EXT.get(kind or "", ".jpg")


def materialize_covers(covers, out_dir, subdir: str = COVER_SUBDIR) -> dict[str, str]:
    """把封面复制到 ``<out_dir>/<subdir>/``，返回 {str(appid): "<subdir>/<file>"}。

    * 目标文件已存在且大小一致时直接复用（重复生成不反复拷贝，秒级）；
    * 单款失败只跳过该款（结果里没有它，页面显示占位块），**绝不抛异常**；
    * 返回的相对地址可直接写进 HTML，双击本地打开即可显示。
    """
    urls: dict[str, str] = {}
    if not covers:
        return urls
    try:
        target_dir = Path(out_dir) / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning("创建封面目录失败，页面将退回占位块：%s", exc)
        return urls

    for raw_appid, raw_path in (covers or {}).items():
        appid = str(raw_appid)
        try:
            src = Path(str(raw_path))
            if not src.is_file():
                continue
            dest = target_dir / f"{appid}{_cover_ext(src)}"
            if not (dest.is_file() and dest.stat().st_size == src.stat().st_size):
                tmp = dest.with_name(dest.name + ".tmp")
                shutil.copyfile(src, tmp)
                os.replace(tmp, dest)
            urls[appid] = f"{subdir}/{dest.name}"
        except Exception as exc:
            logger.debug("复制封面失败（appid=%s）：%s", appid, exc)
            continue
    return urls


def meta_map(play_stats: Mapping, games_meta: Optional[Mapping]) -> dict[int, dict]:
    """只保留库内 appid 的元数据；缓存里多出的 appid 一律丢弃。"""
    source = games_meta or {}
    result: dict[int, dict] = {}
    for appid in library_appids(play_stats):
        meta = source.get(appid)
        if meta is None:
            meta = source.get(str(appid))
        result[appid] = dict(meta) if isinstance(meta, Mapping) else {}
    return result


def _is_blank(value: Any) -> bool:
    """空串 / 纯空白 / None 一律按「没有」处理（页面绝不渲染空白字段）。"""
    return not str(value if value is not None else "").strip()


def _looks_chinese(value: Any) -> bool:
    """这段文本是否**确实**含中文（汉字 / 假名 / 谚文）。

    复用 :func:`core.metadata_cn.looks_chinese` 的判定，绝不另写一套：只有来源里
    确实是中文才拿它当「中文名」，否则一律当作没有（英文名冒充中文名是历史缺陷）。
    """
    try:
        from core import metadata_cn

        return bool(metadata_cn.looks_chinese(value))
    except Exception as exc:                      # pragma: no cover - 极端降级
        logger.debug("中文判定不可用：%s", exc)
        return False


def _authoritative_cn_name(appid: Any) -> str:
    """本地两张权威中文表：``core.metadata_cn``（names_cn.json）→ 本机 appinfo。

    **只读本地、绝不联网**；只接受确实是中文的值（见 :func:`_looks_chinese`）。
    与面板 ``webapp.api._name_cn()`` 的优先级保持一致，别再各自排一套顺序。
    """
    aid = _to_int(appid)
    if aid <= 0:
        return ""
    try:
        from core import metadata_cn

        value = str(metadata_cn.name_cn_for(aid) or "").strip()
        if value and _looks_chinese(value):
            return value
    except Exception as exc:                      # pragma: no cover - 降级路径
        logger.debug("中文名缓存不可用（appid=%s）：%s", aid, exc)
    try:
        from core import appinfo

        for lang in ("schinese", "tchinese"):
            value = str(appinfo.localized_name(aid, lang) or "").strip()
            if value and _looks_chinese(value):
                return value
    except Exception as exc:                      # pragma: no cover - 降级路径
        logger.debug("本机 appinfo 中文名不可用（appid=%s）：%s", aid, exc)
    return ""


def _is_placeholder_name(value: Any) -> bool:
    """``appid N`` 这种占位名：说明上游也没拿到真名字，不能当成游戏名采信。

    实测（账号 10000004 的 219990 Grim Dawn）：``engine.stats`` 的 ``most_forgotten``
    用 ``engine.display.display_name`` 兜底时会产出 ``appid 219990``，
    ``build_checkup`` 再把它当成 ``meta["name"]`` 传回来 —— 于是页面显示占位符，
    而本机 appinfo 里其实有官方原名。这里把占位名当作「没有名字」，继续往下查。
    """
    text = str(value if value is not None else "").strip()
    if not text.startswith("appid "):
        return False
    return text[len("appid "):].strip().isdigit()


def _authoritative_original_name(appid: Any) -> str:
    """本机 appinfo 里的**官方原名**（英文）。

    库里有些条目（缓存没抓到、或只有 appid）在 ``games_meta`` 里连 ``name`` 都没有，
    于是页面只能显示 ``appid N``。本机 appinfo 是权威来源，拿它补上官方原名，
    比占位符有用得多。**只读本地、绝不联网**；取不到返回 ""。
    """
    aid = _to_int(appid)
    if aid <= 0:
        return ""
    try:
        from core import appinfo

        return str((appinfo.app_metadata(aid) or {}).get("name") or "").strip()
    except Exception as exc:                      # pragma: no cover - 降级路径
        logger.debug("本机 appinfo 原名不可用（appid=%s）：%s", aid, exc)
        return ""


def _localized_name(appid: Any, meta: Optional[Mapping] = None) -> str:
    """展示用游戏名：本地库官方中文名优先，缺失再查两张权威中文表，最后回落官方原名。

    优先级（与面板 ``webapp.api._name_cn()`` 一致）：
      1. 本地缓存列 ``games.name_cn``（调用方传入的 meta）；
      2. ``core.metadata_cn``（``data/names_cn.json`` 已确认中文名）；
      3. 本机 appinfo 的官方本地化名（schinese → tchinese）；
      4. 官方原名 ``name``。

    ``name_cn`` 列同样按「确实是中文」把关（见 :func:`_looks_chinese`）：商店在
    ``l=schinese`` 下没有本地化名时会**原样返回英文名**，历史上这类值被当成中文名写进
    缓存，既显示成英文、又**遮蔽**了后面真正的权威中文名。那种值在这里不算中文名，
    继续往下查表，最终回落到官方原名 —— 绝不臆造、绝不用英文冒充中文。
    """
    if not isinstance(meta, Mapping):
        meta = {}
    value = str(meta.get(CN_NAME_KEY) if meta.get(CN_NAME_KEY) is not None else "").strip()
    if value and _looks_chinese(value):
        return value
    value = _authoritative_cn_name(appid)
    if value:
        return value
    value = str(meta.get("name") if meta.get("name") is not None else "").strip()
    if value and not _is_placeholder_name(value):
        return value
    # 连原名都没有（或只有 appid N 占位）：用本机 appinfo 的官方原名兜底，
    # 最后才退到 appid N
    return _authoritative_original_name(appid)


def game_name(appid: Any, meta: Optional[Mapping] = None) -> str:
    """游戏名兜底：**官方中文名优先** → 官方原名 → 都没有才退回 "appid N"。

    中文名来源见 :func:`_localized_name`（本地缓存列 → 中文名表 → 本机 appinfo）。
    缺名字时仍退回 ``appid N``（页面绝不因缺名字而崩），且绝不渲染空白名。
    """
    return _localized_name(appid, meta) or f"appid {_to_int(appid)}"


def _json_list(raw: Any) -> list[str]:
    """把 games 缓存表里的 JSON 字符串列（genres/developers）解析为列表。"""
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if str(item).strip()]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        import json

        data = json.loads(raw)
    except (ValueError, TypeError):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(data, list):
        return [str(item) for item in data if str(item).strip()]
    return []


def load_cached_metadata(appids: Optional[Mapping] = None) -> dict[int, dict]:
    """只读本地 ``games`` 元数据缓存（不联网、不写库）；失败时返回 {}。

    本机对 store.steampowered.com 的访问不稳定（连接被拒 / 429），缓存是
    离线情况下拿到真实游戏名与类型的主要来源。
    """
    try:
        from data.schema import get_connection
    except ImportError as exc:
        logger.warning("元数据缓存不可用：%s", exc)
        return {}
    wanted = {int(a) for a in appids} if appids is not None else None
    result: dict[int, dict] = {}
    db = None
    try:
        db = get_connection()
        for row in db.execute(
            "SELECT appid, name, name_cn, genres, genres_cn, developers, price, release_date"
            " FROM games"
        ):
            appid = _to_int(row["appid"])
            if wanted is not None and appid not in wanted:
                continue
            name = str(row["name"] or "").strip()
            if not name:
                continue
            result[appid] = {
                "name": name,
                CN_NAME_KEY: str(row["name_cn"] or "").strip(),
                "genres": _json_list(row["genres"]),
                CN_GENRES_KEY: _json_list(row["genres_cn"]),
                "developers": _json_list(row["developers"]),
                "price": _to_int(row["price"]),
                "release_date": str(row["release_date"] or ""),
            }
    except Exception as exc:
        logger.warning("读取元数据缓存失败，已降级：%s", exc)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return result


def fill_metadata(
    games_meta: Optional[Mapping],
    play_stats: Mapping,
) -> dict[int, dict]:
    """用本地缓存补底元数据：网络抓取的字段优先，缺失字段回落到缓存。

    返回值严格限定在 play_stats（真实库）范围内。
    """
    cached = load_cached_metadata(library_appids(play_stats))
    provided = games_meta or {}
    merged: dict[int, dict] = {}
    for appid in library_appids(play_stats):
        entry = dict(cached.get(appid) or {})
        source = provided.get(appid)
        if source is None:
            source = provided.get(str(appid))
        if isinstance(source, Mapping):
            for key, value in source.items():
                if key in (CN_NAME_KEY, CN_GENRES_KEY):
                    # 中文展示列只由本地缓存提供；网络侧同名脏值不得改写页面显示名
                    if str(value if value is not None else "").strip() and not entry.get(key):
                        entry[key] = value
                elif key == "genres":
                    values = _as_genre_list({key: value})
                    if values:
                        entry[key] = values
                elif value not in (None, "", 0):
                    entry[key] = value
        merged[appid] = entry
    return merged


def _as_str_list(raw: Any) -> list[str]:
    """list / JSON 字符串 / 逗号分隔字符串 -> 去空去重列表。"""
    if isinstance(raw, (list, tuple, set)):
        values = [str(part).strip() for part in raw]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            values = _json_list(text)
        else:
            values = [part.strip() for part in text.split(",")]
    else:
        values = []
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def _as_genre_list(meta: Optional[Mapping]) -> list[str]:
    return _as_str_list((meta or {}).get("genres"))


def _localized_genres(meta: Optional[Mapping]) -> list[str]:
    """展示用类型：本地库 ``genres_cn`` 优先 → 官方英文 ``genres``。

    仅当中文列表与英文列表**不同**时才改用中文：``genres_cn`` 缺失、为空，
    或与英文同名时保持原文，避免同一批数据出现中英两种写法。
    判定逻辑（免费标记、高难度标签）仍走 :func:`_as_genre_list` 的官方英文名。
    """
    if not isinstance(meta, Mapping):
        return []
    en = _as_genre_list(meta)
    cn = _as_str_list(meta.get(CN_GENRES_KEY))
    if cn and cn != en:
        return cn
    return en


def game_genre(meta: Optional[Mapping] = None) -> str:
    """主类型兜底：**官方中文类型优先**，缺类型返回 "未知"。"""
    genres = _localized_genres(meta)
    return genres[0] if genres else UNKNOWN_GENRE


def _display_name(
    appid: Any,
    classified: Optional[Mapping],
    play_stats: Optional[Mapping] = None,
) -> str:
    """面向页面的状态展示名（已通关 / 在玩 …）；缺失时按游玩时长降级。"""
    info = (classified or {}).get(appid) or {}
    if isinstance(info, Mapping):
        name = str(info.get("status_name") or "").strip()
        if name:
            return name
    return status_key(appid, classified, play_stats)


def status_key(appid: Any, classified: Optional[Mapping], play_stats: Optional[Mapping]) -> str:
    """状态内部键（未玩/在玩/搁置/沉迷/通关），供 graveyard / starmap 使用。

    classified 缺失时按游玩时长降级推断（不判「通关」，与 stats 兜底口径一致）。
    """
    info = (classified or {}).get(appid) or {}
    if isinstance(info, Mapping):
        status = str(info.get("status") or "").strip()
        if status:
            return status
    minutes = _to_int(((play_stats or {}).get(appid) or {}).get("playtime", 0))
    if minutes <= 0:
        return STATUS_UNPLAYED
    if minutes >= ADDICTED_MIN_MINUTES:
        return STATUS_ADDICTED
    return STATUS_SHELVED


def _last_played_day(play_stats: Optional[Mapping], appid: Any) -> str:
    """last_played（unix 秒）-> "YYYY-MM-DD"；脏值（LastPlayed=0/1 等）返回 ""。"""
    return day_from_ts(((play_stats or {}).get(appid) or {}).get("last_played"))


def _statuses_map(play_stats: Mapping, classified: Optional[Mapping]) -> dict:
    """渲染 payload 用的内部状态映射（classified 缺失时逐条降级推断）。"""
    return {appid: status_key(appid, classified, play_stats) for appid in library_appids(play_stats)}


# --------------------------------------------------------------- 通用统计口径

def genre_hours(play_stats: Mapping, games_meta: Optional[Mapping]) -> dict[str, float]:
    """按类型聚合游玩小时数（一款游戏计入它的每个类型）。"""
    metas = meta_map(play_stats, games_meta)
    result: dict[str, float] = {}
    for appid in library_appids(play_stats):
        hours = minutes_to_hours((play_stats.get(appid) or {}).get("playtime", 0))
        genres = _localized_genres(metas.get(appid)) or [UNKNOWN_GENRE]
        for genre in genres:
            result[genre] = round(result.get(genre, 0.0) + hours, 2)
    return result


def library_games(
    play_stats: Mapping,
    games_meta: Optional[Mapping] = None,
    classified: Optional[Mapping] = None,
) -> dict[str, dict]:
    """graveyard / starmap 共用的 games 映射（playtime 单位为小时）。"""
    metas = meta_map(play_stats, games_meta)
    games: dict[str, dict] = {}
    for appid in library_appids(play_stats):
        meta = metas.get(appid) or {}
        games[str(appid)] = {
            "appid": appid,
            "name": game_name(appid, meta),
            "status": status_key(appid, classified, play_stats),
            "playtime": minutes_to_hours((play_stats.get(appid) or {}).get("playtime", 0)),
            "last_played": _last_played_day(play_stats, appid),
            "genre": game_genre(meta),
        }
    return games


def _top_games(
    play_stats: Mapping,
    games_meta: Optional[Mapping],
    classified: Optional[Mapping],
    limit: int = 3,
) -> list[dict]:
    metas = meta_map(play_stats, games_meta)
    ranked = sorted(
        library_appids(play_stats),
        key=lambda appid: -_to_int((play_stats.get(appid) or {}).get("playtime", 0)),
    )
    result: list[dict] = []
    for index, appid in enumerate(ranked[:limit]):
        meta = metas.get(appid) or {}
        result.append(
            {
                "appid": appid,
                "name": game_name(appid, meta),
                "hours": minutes_to_hours((play_stats.get(appid) or {}).get("playtime", 0)),
                "genre": game_genre(meta),
                "status": _display_name(appid, classified, play_stats),
                "rank": index + 1,
            }
        )
    return result


def _genres_payload(play_stats: Mapping, games_meta: Optional[Mapping], limit: int = 3) -> list[dict]:
    hours_map = genre_hours(play_stats, games_meta)
    total_hours = sum(hours_map.values()) or 1.0
    rows = sorted(hours_map.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [
        {"name": name, "hours": hours, "share": round(min(1.0, hours / total_hours), 4)}
        for name, hours in rows
    ]


def _genre_cn_map(metas: Mapping) -> dict[str, str]:
    """由本地库的官方中英类型对生成 英文 -> 中文 映射（仅用于展示替换）。

    中文取自本地 ``genres_cn`` 官方列，不是机翻；中英数量对不上就整条跳过，
    宁可少替换，也不张冠李戴。
    """
    pairs: dict[str, str] = {}
    for meta in (metas or {}).values():
        if not isinstance(meta, Mapping):
            continue
        en = _as_genre_list(meta)
        cn = _as_str_list(meta.get(CN_GENRES_KEY))
        if not en or len(en) != len(cn):
            continue
        for source, target in zip(en, cn):
            if source and target and source != target:
                pairs.setdefault(source, target)
    return pairs


def _localize_genre_text(text: Any, metas: Mapping) -> str:
    """把引擎文案里的英文类型名换成官方中文（本地库有对应中文时才换）。

    引擎（persona）按官方英文名生成摘要，模板直接展示；这里只做展示层替换，
    没有对应中文时原样保留（绝不机翻、绝不臆造）。
    """
    result = str(text if text is not None else "")
    if not result:
        return ""
    for source, target in sorted(_genre_cn_map(metas).items(), key=lambda kv: -len(kv[0])):
        if source in result:
            result = result.replace(source, target)
    return result


def _persona_payload(play_stats: Mapping, games_meta: Optional[Mapping], statuses: Mapping) -> dict:
    """人格画像；engine.persona 不可用时降级为空维度（模板有安全默认值）。"""
    metas = meta_map(play_stats, games_meta)
    try:
        from engine.persona import compute_persona
    except ImportError as exc:
        logger.warning("人格引擎不可用，报告将省略人格维度：%s", exc)
        return {"tag": "", "summary": "", "dimensions": []}
    try:
        result = compute_persona(play_stats, metas, statuses=dict(statuses))
    except Exception as exc:
        logger.warning("人格画像计算失败，已降级：%s", exc)
        return {"tag": "", "summary": "", "dimensions": []}
    dimensions = result.get("dimensions") or {}
    return {
        "tag": str(result.get("primary_label") or ""),
        "summary": _localize_genre_text(result.get("summary"), metas),
        "dimensions": [
            {"name": DIMENSION_NAMES[key], "value": int(round(_to_float(dimensions.get(key, 0.0))))}
            for key in DIMENSION_ORDER
            if key in dimensions
        ],
    }


def _value_picks(play_stats: Mapping, games_meta: Optional[Mapping], limit: int = 3) -> list[dict]:
    """最性价比（每小时单价最低）；engine.value_ranking 不可用时留空。"""
    metas = meta_map(play_stats, games_meta)
    try:
        from engine.value_ranking import compute_value_ranking
    except ImportError as exc:
        logger.warning("性价比引擎不可用，报告将省略性价比榜：%s", exc)
        return []
    try:
        result = compute_value_ranking(play_stats, metas, top_n=limit)
    except Exception as exc:
        logger.warning("性价比计算失败，已降级：%s", exc)
        return []
    picks: list[dict] = []
    for item in (result.get("value_top") or [])[:limit]:
        appid = _to_int(item.get("appid"))
        picks.append(
            {
                "appid": appid,
                "name": game_name(appid, metas.get(appid) or {"name": item.get("name")}),
                "price": _to_float(item.get("price_yuan")),
                "hours": _to_float(item.get("playtime_hours")),
                "cost_per_hour": _to_float(item.get("cost_per_hour")),
            }
        )
    return picks


def _fallback_stats(play_stats: Mapping, statuses: Mapping) -> dict:
    """engine.stats 不可用时的简化统计（保证页面不空白、不崩溃）。"""
    total_games = len(library_appids(play_stats))
    total_minutes = sum(
        _to_int((play_stats.get(appid) or {}).get("playtime", 0))
        for appid in library_appids(play_stats)
    )
    counts: dict[str, int] = {}
    for status in statuses.values():
        counts[status] = counts.get(status, 0) + 1
    played = total_games - counts.get(STATUS_UNPLAYED, 0)
    dust = counts.get(STATUS_UNPLAYED, 0) + counts.get(STATUS_SHELVED, 0)
    percent = lambda part: round(part / total_games * 100, 1) if total_games else 0.0
    return {
        "total_games": total_games,
        "total_playtime_minutes": total_minutes,
        "total_playtime_hours": round(total_minutes / 60, 2),
        "avg_playtime_hours": round(total_minutes / 60 / total_games, 2) if total_games else 0.0,
        "status_counts": counts,
        "dust_rate": percent(dust),
        "played_ratio": percent(played),
        "unplayed_count": counts.get(STATUS_UNPLAYED, 0),
        "top_genres": [],
        "most_forgotten": [],
    }


def _stats_result(play_stats: Mapping, games_meta: Optional[Mapping], statuses: Mapping) -> dict:
    try:
        from engine.stats import compute_stats
    except ImportError as exc:
        logger.warning("统计引擎不可用，使用内置简化统计：%s", exc)
        return _fallback_stats(play_stats, statuses)
    try:
        return compute_stats(play_stats, meta_map(play_stats, games_meta), statuses=dict(statuses))
    except Exception as exc:
        logger.warning("统计计算失败，使用内置简化统计：%s", exc)
        return _fallback_stats(play_stats, statuses)


# ------------------------------------------------------------- 各页面 payload

def build_report(
    play_stats: Mapping,
    games_meta: Optional[Mapping] = None,
    classified: Optional[Mapping] = None,
    user_name: str = DEFAULT_USER_NAME,
    year: Optional[int] = None,
    generated_at: Optional[str] = None,
) -> dict:
    """年度报告 payload（render/annual_report.generate_report）。"""
    statuses = _statuses_map(play_stats, classified)
    stats = _stats_result(play_stats, games_meta, statuses)
    total_hours = _to_float(stats.get("total_playtime_hours"))
    total_games = _to_int(stats.get("total_games"))
    year_value = _to_int(year, date.today().year) or date.today().year
    return {
        "user": {"name": user_name, "year": year_value, "total_games": total_games},
        "stats": {
            "total_hours": total_hours,
            "total_games": total_games,
            "backlog_rate": round(_to_float(stats.get("dust_rate")) / 100.0, 4),
        },
        "genres": _genres_payload(play_stats, games_meta),
        "top_games": _top_games(play_stats, games_meta, classified),
        "persona": _persona_payload(play_stats, games_meta, statuses),
        "value_picks": _value_picks(play_stats, games_meta),
        "year_summary": (
            f"{year_value} 年，你的库里有 {total_games} 款游戏，累计游玩 {total_hours:,.1f} 小时"
            "（全库累计口径）。每一个数字背后，都是一段真实度过的时间。"
        ),
        "generated_at": generated_at or date.today().isoformat(),
    }


def build_library(
    play_stats: Mapping,
    games_meta: Optional[Mapping] = None,
    classified: Optional[Mapping] = None,
    user_name: str = DEFAULT_USER_NAME,
    generated_at: Optional[str] = None,
) -> dict:
    """graveyard / starmap 共用的 payload（user_name + games，时长单位小时）。"""
    return {
        "user_name": user_name,
        "games": library_games(play_stats, games_meta, classified),
        "generated_at": generated_at or date.today().isoformat(),
    }


def _named_stats(
    play_stats: Mapping,
    games_meta: Optional[Mapping],
) -> dict[str, dict]:
    """play_stats 补上游戏名（渲染层需要 name 才能生成事件/匹配）。"""
    metas = meta_map(play_stats, games_meta)
    return {
        str(appid): {
            "appid": appid,
            "name": game_name(appid, metas.get(appid) or {}),
            "playtime": _to_int((play_stats.get(appid) or {}).get("playtime", 0)),
            "last_played": _to_int((play_stats.get(appid) or {}).get("last_played", 0)),
        }
        for appid in library_appids(play_stats)
    }


def history_snapshots(
    snapshots: Optional[list],
    play_stats: Mapping,
    games_meta: Optional[Mapping] = None,
) -> list[dict]:
    """过滤出「历史」快照行，避免与 play_stats 的当期记录重复。

    快照行本身没有游戏名，这里补上 name；只保留 last_played 日期与当前
    play_stats 不一致的行（当日快照与当期数据一致，重复事件会被时间轴渲染两次）。
    """
    metas = meta_map(play_stats, games_meta)
    rows: list[dict] = []
    for row in snapshots or []:
        if not isinstance(row, Mapping):
            continue
        appid = _to_int(row.get("appid"))
        if appid not in metas:
            continue
        day = day_from_ts(row.get("last_played"))
        if not day or day == _last_played_day(play_stats, appid):
            continue
        rows.append(
            {
                "record_date": str(row.get("record_date") or ""),
                "appid": appid,
                "name": game_name(appid, metas.get(appid) or {}),
                "playtime": _to_int(row.get("playtime")),
                "last_played": day,
                "status": str(row.get("status") or ""),
            }
        )
    return rows


def build_timeline(
    play_stats: Mapping,
    games_meta: Optional[Mapping] = None,
    snapshots: Optional[list] = None,
    user_name: str = DEFAULT_USER_NAME,
    generated_at: Optional[str] = None,
) -> dict:
    """考古时间轴 payload（render/archaeologist.generate_timeline）。

    play_stats 的 playtime 保持**分钟**（渲染层会自行换算为小时），
    last_played 保持 unix 秒（渲染层按 unix 秒解析并做脏值防护）。
    """
    metas = meta_map(play_stats, games_meta)
    return {
        "user_name": user_name,
        "play_stats": _named_stats(play_stats, games_meta),
        "games": {str(appid): {"appid": appid, "name": game_name(appid, meta)} for appid, meta in metas.items()},
        "snapshots": history_snapshots(snapshots, play_stats, games_meta),
        "generated_at": generated_at or date.today().isoformat(),
    }


def build_today(
    play_stats: Mapping,
    games_meta: Optional[Mapping] = None,
    snapshots: Optional[list] = None,
    user_name: str = DEFAULT_USER_NAME,
    target_date: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> dict:
    """时光机 payload（render/timemachine.generate_timemachine）。

    快照行补上游戏名后原样传入（渲染层按 appid 去重，不会重复计数）。
    """
    metas = meta_map(play_stats, games_meta)
    rows: list[dict] = []
    for row in snapshots or []:
        if not isinstance(row, Mapping):
            continue
        appid = _to_int(row.get("appid"))
        if appid not in metas:
            continue
        rows.append(
            {
                "record_date": str(row.get("record_date") or ""),
                "appid": appid,
                "name": game_name(appid, metas.get(appid) or {}),
                "playtime": _to_int(row.get("playtime")),
                "last_played": _to_int(row.get("last_played")),
                "status": str(row.get("status") or ""),
            }
        )
    payload = {
        "user_name": user_name,
        "play_stats": _named_stats(play_stats, games_meta),
        "games": {str(appid): {"appid": appid, "name": game_name(appid, meta)} for appid, meta in metas.items()},
        "snapshots": rows,
        "generated_at": generated_at or date.today().isoformat(),
    }
    if target_date:
        payload["target_date"] = str(target_date)
    return payload


def build_checkup(
    play_stats: Mapping,
    games_meta: Optional[Mapping] = None,
    classified: Optional[Mapping] = None,
    user_name: str = DEFAULT_USER_NAME,
    generated_at: Optional[str] = None,
) -> dict:
    """体检报告 payload（render/checkup.generate_checkup），比率均为 0~1。"""
    statuses = _statuses_map(play_stats, classified)
    stats = _stats_result(play_stats, games_meta, statuses)
    metas = meta_map(play_stats, games_meta)

    hours_map = genre_hours(play_stats, games_meta)
    top_genres = [
        {"name": name, "hours": hours}
        for name, hours in sorted(hours_map.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ]

    forgotten = []
    for entry in stats.get("most_forgotten") or []:
        appid = _to_int(entry.get("appid"))
        day = day_from_ts(entry.get("last_played"))
        forgotten.append(
            {
                "appid": appid,
                "name": game_name(appid, metas.get(appid) or {"name": entry.get("name")}),
                "last_played": day,
            }
        )
    forgotten = [row for row in forgotten if row["last_played"]][:5]

    ranked = sorted(
        library_appids(play_stats),
        key=lambda appid: -_to_int((play_stats.get(appid) or {}).get("playtime", 0)),
    )
    top_appid = ranked[0] if ranked else None

    # stats.most_forgotten 按 last_played 升序，第一条即「最久未玩」
    longest_days = 0
    if forgotten:
        try:
            longest_days = max(0, (date.today() - date.fromisoformat(forgotten[0]["last_played"])).days)
        except ValueError:
            longest_days = 0

    return {
        "user_name": user_name,
        "stats": {
            "total_games": _to_int(stats.get("total_games")),
            "total_playtime_hours": _to_float(stats.get("total_playtime_hours")),
            "avg_playtime_hours": _to_float(stats.get("avg_playtime_hours")),
            "dust_rate": round(_to_float(stats.get("dust_rate")) / 100.0, 4),
            "played_ratio": round(_to_float(stats.get("played_ratio")) / 100.0, 4),
            "unplayed_count": _to_int(stats.get("unplayed_count")),
            "shelved_count": _to_int((stats.get("status_counts") or {}).get(STATUS_SHELVED, 0)),
            "top_genres": top_genres,
            "most_forgotten": forgotten,
        },
        "top_game": {
            "appid": top_appid,
            "name": game_name(top_appid, metas.get(top_appid) or {}) if top_appid is not None else "-",
            "hours": minutes_to_hours((play_stats.get(top_appid) or {}).get("playtime", 0)) if top_appid is not None else 0.0,
        },
        "longest_unplayed": {
            "appid": forgotten[0]["appid"] if forgotten else None,
            "name": forgotten[0]["name"] if forgotten else "-",
            "days": longest_days,
        },
        "generated_at": generated_at or date.today().isoformat(),
    }


_BUILDERS = {
    "report": build_report,
    "graveyard": build_library,
    "starmap": build_library,
    "timeline": build_timeline,
    "today": build_today,
    "checkup": build_checkup,
}


def build_payload(
    kind: str,
    play_stats: Mapping,
    games_meta: Optional[Mapping] = None,
    classified: Optional[Mapping] = None,
    snapshots: Optional[list] = None,
    user_name: str = DEFAULT_USER_NAME,
    generated_at: Optional[str] = None,
) -> dict:
    """按渲染命令名装配 payload；未知 kind 抛 ValueError（调用方需自查）。

    六页**唯一共同入口**：在这里统一剔除非游戏条目（口径见 :mod:`core.non_game`），
    六页一致。过滤后的 ``play_stats`` 会连带把库范围（``total_games`` / 星星数 /
    墓碑数 / 快照行 / 时间轴事件 / 类型时长）一起重算，数字自洽 —— 面板顶部五块大数字
    与「游戏明细」列表因此显示同一个数。
    """
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"未知的渲染页面类型：{kind}（可选：{'/'.join(RENDER_KINDS)}）")
    if kind in LIBRARY_DETAIL_KINDS:
        play_stats = non_game.exclude_non_games(play_stats)
    if kind in ("timeline", "today"):
        return builder(play_stats, games_meta, snapshots=snapshots, user_name=user_name, generated_at=generated_at)
    return builder(play_stats, games_meta, classified=classified, user_name=user_name, generated_at=generated_at)


def load_snapshots() -> list[dict]:
    """读取全部历史快照行（供 timeline / today 使用）；失败时返回空列表。"""
    try:
        from core import snapshot
    except ImportError as exc:
        logger.warning("快照模块不可用：%s", exc)
        return []
    try:
        rows: list[dict] = []
        for record_date in snapshot.list_dates():
            rows.extend(snapshot.load(record_date))
        return rows
    except Exception as exc:
        logger.warning("读取历史快照失败，已降级为空：%s", exc)
        return []
