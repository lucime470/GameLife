"""本地 Steam 元数据（离线，不联网）。

数据来源（权威且本机自带）：
  1. <Steam>/appcache/appinfo.vdf —— 客户端应用信息库，含多语言名称
     （schinese / tchinese / sc_schinese）、header_image 文件名与类型；
  2. <Steam>/appcache/librarycache/<appid>/ —— 客户端已缓存的封面文件；
  3. <Steam>/steamapps/appmanifest_*.acf —— 已安装游戏名称（兜底）。

对外契约（webapp / CLI / 其它 core 模块依赖，请勿随意改签名）：
    load_index(steam_path=None, force=False) -> dict[str, dict]
    app_metadata(appid, steam_path=None) -> dict
    localized_name(appid, lang="schinese", steam_path=None) -> str | None
    localized_names(appids, lang="schinese", steam_path=None) -> dict[int, str]
    local_cover_files(appid, steam_path=None) -> list[Path]
    header_image_name(appid, lang="schinese", steam_path=None) -> str | None
    franchises(appid, steam_path=None) -> list[str]
    franchise_index(steam_path=None) -> dict[str, list[int]]
    app_names(appids, steam_path=None) -> dict[int, str]
    clear_cache() -> None

设计约束：任何一步失败都降级为空结果并打印一行提示，**绝不抛异常**
（面板依赖本模块，不能让接口 500）。
"""
from __future__ import annotations

import html
import json
import re
import struct
import time
from pathlib import Path

try:  # 复用 schema 的路径解析（兼容 PyInstaller onefile）
    from data.schema import data_dir as _data_dir
except Exception:  # pragma: no cover - schema 不可用时退化为包内路径
    def _data_dir() -> Path:
        return Path(__file__).resolve().parent.parent / "data"

APP_ROOT = Path(__file__).resolve().parent.parent
CACHE_NAME = "appinfo_index.json"
CACHE_VERSION = 4            # 缓存载荷结构版本；不匹配时自动重建（4：修正 _parse_entry 候选错选）

# 语言取值优先级（同名语言的 Steam 变体互为主备）
_LANG_ORDER = {
    "schinese": ("schinese", "sc_schinese", "tchinese", "tc_tchinese"),
    "sc_schinese": ("sc_schinese", "schinese", "tchinese", "tc_tchinese"),
    "tchinese": ("tchinese", "tc_tchinese", "schinese", "sc_schinese"),
    "tc_tchinese": ("tc_tchinese", "tchinese", "schinese", "sc_schinese"),
    "english": ("english",),
}

# 封面文件优先级：优先横版（面板缩略图为横版），其次方图/竖版
_COVER_PRIORITY = (
    "header",
    "library_header",
    "capsule_231x87",
    "library_capsule",
    "library_600x900",
    "library_hero",
    "logo",
)

_INDEX: dict = {"key": None, "apps": {}, "source": "", "raw_count": 0}


# ------------------------------------------------------------------ 路径解析

def cache_path() -> Path:
    """本地索引缓存文件位置（data/appinfo_index.json）。"""
    return _data_dir() / CACHE_NAME


def _steam_root(steam_path: str | None = None) -> Path | None:
    """确定 Steam 安装目录：显式 > 注册表；都不行返回 None。"""
    if steam_path:
        path = Path(str(steam_path))
        if path.exists():
            return path
    try:
        from core.locator import find_steam_path

        found = find_steam_path()
        if found:
            return Path(found)
    except Exception:
        pass
    return None


def _appinfo_file(root: Path | None) -> Path | None:
    if not root:
        return None
    for rel in ("appcache/appinfo.vdf", "appcache/appinfo.vdf.bak"):
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


def _signature(path: Path | None) -> str:
    if not path or not path.is_file():
        return ""
    try:
        st = path.stat()
    except OSError:
        return ""
    return f"{path}|{int(st.st_mtime)}|{st.st_size}"


# ------------------------------------------- 官方系列（franchise）提取

# common.associations 里 type=developer / publisher 的项
_DEV_PUB_TYPES = ("developer", "publisher")

# 归一化时剥掉的公司后缀（仅用于「与开发商/发行商同名」的判定，不改动展示名）
_CORP_SUFFIX = frozenset({
    "co", "ltd", "llc", "inc", "gmbh", "corp", "corporation", "limited", "company",
    "studios", "studio", "games", "game", "official", "interactive", "entertainment",
    "publishing", "publisher", "group", "works", "collective", "team", "digital",
    "software", "media", "labs", "production", "productions", "holding", "holdings",
    "sa", "sro", "pte", "pty", "kk",
})

# 明显不是系列名的占位值
_NOISE_FRANCHISES = frozenset({"franchise", "series", "steam", "valve"})


def _compact(name: object) -> str:
    """去掉一切非字母数字/汉字后的紧凑形式（用于同名判定）。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(name or "").lower())


def _norm_name(name: object) -> str:
    """归一化公司/系列名：小写、& -> and、去标点、剥掉尾部公司后缀。"""
    text = str(name or "").lower().replace("&", " and ")
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)
    tokens = [token for token in text.split() if token]
    while tokens and tokens[-1] in _CORP_SUFFIX:
        tokens.pop()
    return " ".join(tokens)


def _assoc_pairs(common: dict) -> list[tuple[str, str]]:
    """把 common.associations（dict of {type,name}）拍平成 [(type, name)]。"""
    raw = common.get("associations") if isinstance(common, dict) else None
    pairs: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        for value in raw.values():
            if not isinstance(value, dict):
                continue
            kind = str(value.get("type") or "").strip().lower()
            name = str(value.get("name") or "").strip()
            if kind and name:
                pairs.append((kind, name))
    return pairs


def _is_noise_franchise(name: str, same_app_devpub: set[str], global_devpub: set[str]) -> bool:
    """判断某个 franchise 值其实是不是「公司名」而非真系列。"""
    compact = _compact(name)
    if not compact or compact in _NOISE_FRANCHISES:
        return True
    # 与本条记录的开发商/发行商同名 -> 公司名
    if compact in same_app_devpub:
        return True
    normalized = _norm_name(name)
    if not normalized:
        return True
    # 与全局任一开发商/发行商同名 -> 公司名
    if normalized in global_devpub:
        return True
    # 归一化后互为子串（如 "Ndemic Inc." vs "Ndemic Creations"）
    if len(normalized) >= 6:
        for other in global_devpub:
            if len(other) >= 6 and (normalized in other or other in normalized):
                return True
    return False


def _franchise_names(pairs: list[tuple[str, str]], global_devpub: set[str]) -> list[str]:
    """从 associations 里挑出真正的官方系列名（去噪、去重、保持原始大小写）。"""
    same_app = {_compact(name) for kind, name in pairs if kind in _DEV_PUB_TYPES}
    result: list[str] = []
    for kind, name in pairs:
        if kind != "franchise":
            continue
        clean = html.unescape(str(name or "")).strip()
        if not clean or clean in result:
            continue
        if _is_noise_franchise(clean, same_app, global_devpub):
            continue
        result.append(clean)
    return result


# ------------------------------------------------------ appinfo.vdf 二进制解析

def _read_string_table(data: bytes, offset: int) -> list[str]:
    """v29 字符串索引表：uint32 条数 + N 个 \\0 结尾字符串。"""
    if offset <= 0 or offset + 4 > len(data):
        return []
    count = struct.unpack_from("<I", data, offset)[0]
    pos = offset + 4
    strings: list[str] = []
    for _ in range(count):
        end = data.find(b"\x00", pos)
        if end < 0:
            break
        strings.append(data[pos:end].decode("utf-8", "replace"))
        pos = end + 1
    return strings


def _kv_parse(buf: bytes, pos: int, end: int, strings: list[str], depth: int = 0):
    """解析二进制 KeyValues（类型字节 + u32 键索引 + 值）。"""
    if depth > 24:
        raise ValueError("嵌套过深")
    out: dict = {}
    while True:
        if pos >= end:
            raise ValueError("数据提前结束")
        vtype = buf[pos]
        pos += 1
        if vtype in (0x08, 0x0B):  # 字典结束
            return out, pos
        key_index = struct.unpack_from("<I", buf, pos)[0]
        pos += 4
        key = strings[key_index] if key_index < len(strings) else f"#{key_index}"
        if vtype == 0x00:
            value, pos = _kv_parse(buf, pos, end, strings, depth + 1)
        elif vtype == 0x01:
            stop = buf.find(b"\x00", pos)
            if stop < 0:
                raise ValueError("字符串未闭合")
            value = buf[pos:stop].decode("utf-8", "replace")
            pos = stop + 1
        elif vtype == 0x02:
            value = struct.unpack_from("<i", buf, pos)[0]
            pos += 4
        elif vtype == 0x03:
            value = struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        elif vtype in (0x04, 0x06):
            value = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        elif vtype == 0x05:
            stop = buf.find(b"\x00\x00", pos)
            if stop < 0:
                raise ValueError("宽字符串未闭合")
            value = buf[pos:stop].decode("utf-16-le", "replace")
            pos = stop + 2
        elif vtype == 0x07:
            value = struct.unpack_from("<q", buf, pos)[0]
            pos += 8
        else:
            raise ValueError(f"未知类型 {vtype:#x}")
        out[key] = value


# 候选质量分级：数值越大越像「真 appinfo」。
#   3 = 规范根：顶层只有 ``appinfo`` 一个键，且 ``appinfo.common`` 是 dict
#   2 = 有 ``appinfo.common``（缺少「顶层只有 appinfo」这一特征）
#   1 = 有 ``appinfo``（dict）但没 ``common``（如 ``{appid, public_only}`` 占位）
#   0 = 根本没有 ``appinfo``（几乎必然是错位解析出的垃圾键，如 ``#3768123989``）
_SCORE_NO_APPINFO = 0
_SCORE_APPINFO = 1
_SCORE_APPINFO_COMMON = 2
_SCORE_CANONICAL = 3

# 头部长度（字节）：u32 版本 + u32 时间戳 + 8 字节 0 + 40 字节哈希 + u32 末尾 0 = 60。
# 绝大多数记录的真 appinfo 恰好从 offset 60 开始，已知布局时直接先试它。
_LIKELY_START = 60


def _candidate_score(kv: object) -> int:
    """给一个解析候选打分（见上方分级说明）。任何非 dict 都算 0 分。"""
    if not isinstance(kv, dict) or not kv:
        return _SCORE_NO_APPINFO
    info = kv.get("appinfo")
    if not isinstance(info, dict):
        return _SCORE_NO_APPINFO
    score = _SCORE_APPINFO
    if isinstance(info.get("common"), dict):
        score = _SCORE_APPINFO_COMMON
        if len(kv) == 1:
            score = _SCORE_CANONICAL
    return score


def _parse_entry(blob: bytes, strings: list[str]) -> dict | None:
    """单条记录：头部长度在各版本间不一致，用「完整消费 + 候选打分」定位真正的 appinfo。

    旧实现取**第一个**能完整消费 blob 的候选，实测 1801 条里有 497 条首个候选是
    错位解析出的垃圾（顶层键形如 ``#3768123989``），其中 478 条明明存在正确的
    ``{"appinfo": {...}}`` 候选却被丢弃 —— 于是 292030（巫师3）等大批游戏在索引里
    变成空记录，中文名/封面/系列全部落空。

    现在改为：扫描候选并**按质量取最优**（优先 ``appinfo.common`` 规范根），
    只有在完全没有「带 appinfo」的候选时才退回旧行为（取第一个完整候选），
    保证向后兼容且绝不抛异常。命中规范根时提前退出，耗时与旧实现相当。
    """
    limit = min(len(blob), 128)
    first_kv: dict | None = None
    best_kv: dict | None = None
    best_score = -1

    # 已知布局：绝大多数记录的真 appinfo 恰好从 offset 60 开始，先试它以便提前退出。
    starts = range(0, limit)
    if limit > _LIKELY_START:
        starts = [_LIKELY_START] + [s for s in range(0, limit) if s != _LIKELY_START]

    for start in starts:
        try:
            kv, stop = _kv_parse(blob, start, len(blob), strings)
        except Exception:
            continue
        if stop != len(blob) or not kv:
            continue
        if first_kv is None:
            first_kv = kv
        score = _candidate_score(kv)
        if score > best_score:
            best_kv = kv
            best_score = score
            if score >= _SCORE_CANONICAL:
                break                      # 已是最优（规范根），无需继续扫描
    if best_score >= _SCORE_APPINFO:
        return best_kv
    return first_kv                        # 兜底：保持旧行为（第一个完整候选）


def _parse_appinfo_file(path: Path) -> dict[str, dict]:
    """解析整个 appinfo.vdf，返回 {appid: {...}}。"""
    raw = path.read_bytes()
    if len(raw) < 16:
        return {}
    magic = struct.unpack_from("<I", raw, 0)[0]
    if magic not in (0x07564427, 0x07564428, 0x07564429):
        raise ValueError(f"appinfo.vdf 魔数不识别：{magic:#x}")
    offset = 8
    strings: list[str] = []
    if magic >= 0x07564428:
        table_offset = struct.unpack_from("<q", raw, offset)[0]
        offset += 8
        strings = _read_string_table(raw, table_offset)

    raw_apps: dict[str, dict] = {}
    while offset + 8 <= len(raw):
        appid, size = struct.unpack_from("<II", raw, offset)
        if appid == 0:
            break
        blob = raw[offset + 8:offset + 8 + size]
        if len(blob) < size:
            break
        kv = _parse_entry(blob, strings)
        if kv:
            info = kv.get("appinfo")
            if not isinstance(info, dict):
                info = {}
            common = info.get("common") if isinstance(info.get("common"), dict) else {}
            localized = common.get("name_localized") if isinstance(common.get("name_localized"), dict) else {}
            header = common.get("header_image") if isinstance(common.get("header_image"), dict) else {}
            raw_apps[str(appid)] = {
                "appid": int(appid),
                "name": str(common.get("name") or ""),
                "name_localized": {str(k): str(v) for k, v in localized.items()},
                "header_image": {str(k): str(v) for k, v in header.items()},
                "type": str(common.get("type") or ""),
                "_assoc": _assoc_pairs(common),
            }
        offset += 8 + size

    # 全局开发商/发行商名集合：用于剔除「其实是公司名」的伪系列
    global_devpub: set[str] = set()
    for entry in raw_apps.values():
        for kind, name in entry.get("_assoc") or ():
            if kind in _DEV_PUB_TYPES:
                normalized = _norm_name(name)
                if normalized:
                    global_devpub.add(normalized)

    apps: dict[str, dict] = {}
    for key, entry in raw_apps.items():
        item = {k: v for k, v in entry.items() if k != "_assoc"}
        item["franchises"] = _franchise_names(entry.get("_assoc") or (), global_devpub)
        apps[key] = item
    return apps


def _read_appmanifests(root: Path | None) -> dict[str, dict]:
    """已安装游戏的名称兜底（appmanifest_*.acf，只有英文名）。"""
    result: dict[str, dict] = {}
    if not root:
        return result
    for folder in ("steamapps", "steamapps/common"):
        base = root / folder
        if not base.is_dir():
            continue
        for acf in base.glob("appmanifest_*.acf"):
            try:
                import vdf

                data = vdf.loads(acf.read_text(encoding="utf-8", errors="replace"))
                state = data.get("AppState") or {}
                appid = str(state.get("appid") or acf.stem.split("_")[-1])
                name = str(state.get("name") or "")
            except Exception:
                continue
            if appid and name:
                result.setdefault(appid, {"appid": int(appid), "name": name,
                                          "name_localized": {}, "header_image": {},
                                          "type": "Game"})
        break
    return result


# ------------------------------------------------------------------ 索引装载

def load_index(steam_path: str | None = None, force: bool = False) -> dict[str, dict]:
    """装载本地索引 {appid_str: {...}}；带磁盘缓存，源文件变了自动重建。"""
    root = _steam_root(steam_path)
    source = _appinfo_file(root)
    key = _signature(source)
    if not force and _INDEX["key"] == key and _INDEX["apps"]:
        return _INDEX["apps"]

    path = cache_path()
    apps: dict[str, dict] = {}
    if not force and source and path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (cached.get("signature") == key
                    and cached.get("version") == CACHE_VERSION
                    and isinstance(cached.get("apps"), dict)):
                apps = cached["apps"]
        except Exception:
            apps = {}

    if not apps:
        if source:
            try:
                apps = _parse_appinfo_file(source)
            except Exception as exc:
                print(f"appinfo: 解析 {source.name} 失败，改用已安装清单：{exc}")
                apps = {}
        if not apps:
            apps = _read_appmanifests(root)
        if apps:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"signature": key, "version": CACHE_VERSION,
                                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "apps": apps}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                pass

    _INDEX.update({"key": key, "apps": apps, "source": str(source or ""), "raw_count": len(apps)})
    return apps


def clear_cache() -> None:
    """清掉内存索引与磁盘缓存（测试用）。"""
    _INDEX.update({"key": None, "apps": {}, "source": "", "raw_count": 0})
    try:
        cache_path().unlink()
    except OSError:
        pass


def _entry(appid, steam_path: str | None = None) -> dict:
    index = load_index(steam_path)
    return index.get(str(appid)) or {}


# ------------------------------------------------------------------ 对外查询

def app_metadata(appid, steam_path: str | None = None) -> dict:
    """返回该 appid 的本地元数据（取不到返回空 dict，不抛异常）。"""
    try:
        return dict(_entry(appid, steam_path))
    except Exception:
        return {}


def localized_name(appid, lang: str = "schinese", steam_path: str | None = None) -> str | None:
    """本地多语言名称；没有该语言时返回 None（不回退英文，交由上层决定）。"""
    entry = _entry(appid, steam_path)
    return _localized_pick(entry.get("name_localized") or {}, _LANG_ORDER.get(lang, (lang,)))


def _localized_pick(localized: dict, codes) -> str | None:
    """按语言优先级从 name_localized 里取第一个非空值。"""
    for code in codes:
        value = str(localized.get(code) or "").strip()
        if value:
            return value
    return None


def franchises(appid, steam_path: str | None = None) -> list[str]:
    """该 appid 的**官方系列名**（Valve 在 appinfo 的 franchise 标注，已去噪）。

    取不到时返回 []（绝不抛异常）。条目本身不带 type 过滤，供「我拥有什么系列」使用。
    """
    try:
        entry = _entry(appid, steam_path)
    except Exception:
        return []
    names = entry.get("franchises") or []
    return [str(name) for name in names if str(name).strip()]


def franchise_index(steam_path: str | None = None, *, games_only: bool = True) -> dict[str, list[int]]:
    """官方系列 -> 该系列下的 appid 列表（全量目录，非仅本机库）。

    games_only=True 时只统计 type == "Game" 的条目（排除 DLC / 工具 / 配置）。
    任何异常都降级为 {}。
    """
    result: dict[str, list[int]] = {}
    try:
        index = load_index(steam_path)
    except Exception:
        return result
    for key, entry in index.items():
        if games_only and str(entry.get("type") or "").strip().lower() != "game":
            continue
        try:
            appid = int(key)
        except (TypeError, ValueError):
            continue
        for name in entry.get("franchises") or ():
            text_name = str(name).strip()
            if text_name:
                result.setdefault(text_name, []).append(appid)
    return result


def app_names(appids, steam_path: str | None = None) -> dict[int, str]:
    """批量取英文名：{int(appid): name}；未命中/脏输入自动跳过，绝不抛异常。"""
    result: dict[int, str] = {}
    try:
        index = load_index(steam_path)
    except Exception:
        return result
    for raw in appids or ():
        try:
            appid = int(raw)
        except (TypeError, ValueError):
            continue
        entry = index.get(str(appid)) or {}
        name = str(entry.get("name") or "").strip()
        if name:
            result[appid] = name
    return result


def localized_names(appids, lang: str = "schinese",
                    steam_path: str | None = None) -> dict[int, str]:
    """批量取本地化名称：{int(appid): 名称}，未命中的 appid 不出现在结果里。

    只装载一次索引（load_index 自带内存 + 磁盘缓存），不会逐个 appid 重读文件。
    任何异常都降级成部分结果/空 dict，绝不抛异常。
    """
    codes = _LANG_ORDER.get(lang, (lang,))
    result: dict[int, str] = {}
    try:
        index = load_index(steam_path)
    except Exception:
        return result
    for raw in appids or ():
        try:
            appid = int(raw)
        except (TypeError, ValueError):
            continue
        entry = index.get(str(appid)) or {}
        value = _localized_pick(entry.get("name_localized") or {}, codes)
        if value:
            result[appid] = value
    return result


def header_image_name(appid, lang: str = "schinese", steam_path: str | None = None) -> str | None:
    """appinfo 里记录的封面文件名（相对 librarycache/<appid>/）。"""
    entry = _entry(appid, steam_path)
    header = entry.get("header_image") or {}
    for code in _LANG_ORDER.get(lang, (lang,)) + ("english",):
        value = str(header.get(code) or "").strip()
        if value:
            return value
    return None


def local_cover_files(appid, steam_path: str | None = None) -> list[Path]:
    """客户端已缓存的封面候选（按横版优先排序，只返回真实存在的文件）。"""
    root = _steam_root(steam_path)
    if not root:
        return []
    folder = root / "appcache" / "librarycache" / str(appid)
    if not folder.is_dir():
        return []

    names: list[str] = []
    pinned = header_image_name(appid, steam_path)
    if pinned:
        names.append(pinned)
    for stem in _COVER_PRIORITY:
        for lang in ("schinese", "sc_schinese", "tchinese", ""):
            suffix = f"_{lang}" if lang else ""
            for ext in (".jpg", ".png"):
                names.append(f"{stem}{suffix}{ext}")

    ordered: list[Path] = []
    seen: set[str] = set()
    for name in names:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        candidate = folder / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            ordered.append(candidate)
    if ordered:
        return ordered

    # 兜底：目录里任意图片，仍按横版优先
    extras = sorted(
        (p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png") and p.stat().st_size > 0),
        key=lambda p: (0 if "header" in p.name or "capsule" in p.name else 1, p.name),
    )
    return extras


def stats(steam_path: str | None = None) -> dict:
    """索引概况（面板/CLI 用）：条数、有中文名、有封面文件名。"""
    apps = load_index(steam_path)
    with_cn = sum(1 for v in apps.values() if any(str(x).strip() for x in (v.get("name_localized") or {}).values()))
    with_header = sum(1 for v in apps.values() if v.get("header_image"))
    return {
        "source": _INDEX.get("source") or "",
        "count": len(apps),
        "with_name_cn": with_cn,
        "with_header_image": with_header,
    }