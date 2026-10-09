"""封面（标题图 / 竖版大图）本地缓存与多源抓取。

用户诉求：**不开加速器、在大陆网络下也能用**，所以封面必须做到「离线也能显示」。

三级取图顺序（离线优先）：
    1. 本工具缓存        data/covers/<appid>.jpg
    2. 本机 Steam 现成图  <Steam>/appcache/librarycache/<appid>/
       顶层：header.jpg / library_600x900.jpg / 40 位哈希命名的文件
       子目录：<appid>/<40位哈希>/ 里的 library_header.jpg、library_capsule.jpg 等
       注意：文件名可能带 _schinese 等本地化后缀，图片也可能**没有扩展名**，
             因此一律按魔数（magic bytes）识别，不看后缀。
    3. Steam 官方 CDN 镜像（国内实测速度排序，eccdnx 最快）

硬性约束：
    * 任何异常都降级为 ok=False，**绝不抛异常、绝不 hang**
    * 单个源超时 <= timeout（默认 8s），单款游戏总预算 <= 20s
    * 只接受「像图」的响应：HTTP 200 + Content-Type 含 image + 体积 > 1000 字节
    * 落盘原子：先写 .tmp 再 os.replace
    * 只监听/访问公开 CDN，不需要账号，不需要加速器

本模块只依赖标准库 + requests。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

try:  # requests 缺失时模块仍可导入（降级为「只有本地图」）
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore[assignment]

__all__ = [
    "covers_dir",
    "find_librarycache_dir",
    "local_cover",
    "local_cover_ex",
    "cover_url_candidates",
    "ensure_cover",
    "ensure_covers",
    "cover_for_panel",
    "cover_kind",
    "mime_for_cover",
    "cover_index",
    "no_art_path",
    "no_art_appids",
    "mark_no_art",
]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

_GAME_LIFE_ROOT = Path(__file__).resolve().parent.parent


def _stable_data_dir() -> Path:
    """数据目录：源码运行用 game-life/data；打包运行用 exe 附近的 data。

    onefile 下 ``__file__`` 指向临时解包目录（退出即删），封面下载落在那
    里会「看不见也留不住」，因此打包时改用 exe 旁的稳定目录。
    """
    import sys as _sys

    if getattr(_sys, "frozen", False):
        try:
            from data import schema

            return Path(schema.DB_PATH).resolve().parent
        except Exception:
            return Path(_sys.executable).resolve().parent / "data"
    return _GAME_LIFE_ROOT / "data"


_COVERS_DIR = _stable_data_dir() / "covers"
_INDEX_NAME = "index.json"

# 实测（本机、无加速器、2026-09）平均耗时升序。
# 全部是 Steam 官方 CDN 域名，大陆直连可达；末尾的注释是实测平均耗时（秒）。
_CDN_HOSTS: list[tuple[str, str, float]] = [
    ("media.st.dl.eccdnx.com", "/steam/apps/{appid}/{file}", 0.14),
    ("shared.cdn.steamchina.eccdnx.com", "/store_item_assets/steam/apps/{appid}/{file}", 0.09),
    ("steamcdn-a.akamaihd.net", "/steam/apps/{appid}/{file}", 0.39),
    ("shared.akamai.steamstatic.com", "/store_item_assets/steam/apps/{appid}/{file}", 0.50),
    ("cdn.cloudflare.steamstatic.com", "/steam/apps/{appid}/{file}", 0.53),
    # 末位兜底（Fastly 系，走美国节点）。本机复测**时通时断**：同样的 URL 30 秒内连打 6 次
    # 100% 被 TCP 重置，而上面 5 个国内 CDN 域名同期 100% 200 —— 与 store.steampowered.com
    # 同一类「非国内节点被重置」现象。放末尾：失败只用 ~0.15s 就换下一个，不影响主流路径。
    ("cdn.fastly.steamstatic.com", "/steam/apps/{appid}/{file}", 0.50),
    ("shared.steamstatic.com", "/store_item_assets/steam/apps/{appid}/{file}", 0.60),
]

# size 参数 -> CDN 上的文件名
_SIZE_FILES: dict[str, str] = {
    "header": "header.jpg",
    "capsule": "capsule_231x87.jpg",
    "small_capsule": "capsule_231x87.jpg",
    "portrait": "library_600x900.jpg",
    "600x900": "library_600x900.jpg",
    "library_600x900": "library_600x900.jpg",
    "hero": "library_hero.jpg",
    "library_hero": "library_hero.jpg",
}

# 本机 librarycache 里用具名文件优先，其次才是哈希文件。
# 横版（header 一族）优先给面板当封面；竖版（capsule / 600x900）在 size=portrait 时才优先。
# 严格按「原有条目在前、新条目在后」排列：任何以前能命中的文件，现在依然优先命中。
_LIBRARY_HEADER_NAMES = ("header.jpg", "library_600x900.jpg",
                         "library_header.jpg", "library_capsule.jpg")
_LIBRARY_PORTRAIT_NAMES = ("library_600x900.jpg", "header.jpg",
                           "library_capsule.jpg", "library_header.jpg")
_LIBRARY_EXTRA_NAMES = ("library_hero.jpg", "capsule_231x87.jpg")

# 40 位十六进制命名的文件：**可能带扩展名，也可能一个都没有**（Steam 用内容哈希命名）。
_HASH_NAME_RE = re.compile(r"^[0-9a-f]{40}(?:\.[A-Za-z0-9]{1,8})?$", re.IGNORECASE)
# 40 位十六进制命名的目录（新版 Steam 把素材放在 <appid>/<hash>/ 里）。
_HASH_DIR_EXACT_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_HASH_DIR_RE = re.compile(r"\b([0-9a-f]{40})\b", re.IGNORECASE)

# Steam 会给部分素材准备本地化版本（library_header_schinese.jpg 等），按顺序作为变体候选。
_LOCALIZED_SUFFIXES = ("_schinese", "_tchinese", "_english", "_japanese", "_koreana", "_russian")

_MIN_BYTES = 1000
_TOTAL_BUDGET_SECONDS = 20.0
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GameLife/1.0 (+local)"
_HEAD_READ = 64 * 1024


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _norm_appid(appid: Any) -> int | None:
    """把 appid 归一化成正整数；非法输入返回 None（不抛异常）。"""
    try:
        if isinstance(appid, bool):
            return None
        if isinstance(appid, int):
            return appid if appid > 0 else None
        text = str(appid).strip()
        # 必须是最朴素的 ASCII 数字：str.isdigit() 会放过全角 '１２３'、上标等
        if not text or not text.isascii() or not text.isdigit():
            return None
        value = int(text)
        return value if value > 0 else None
    except Exception:
        return None


def _is_file(path: Path | None) -> bool:
    try:
        return path is not None and path.is_file() and path.stat().st_size > 0
    except Exception:
        return False


def _is_dir(path: Path | None) -> bool:
    try:
        return path is not None and path.is_dir()
    except Exception:
        return False


# Steam 客户端有时只写了 32x32 的占位图（真图还没下载），面板不该拿它当封面。
# 规则：能解析出尺寸时，最长边必须 >= 该阈值；解析不出时只按字节数兜底。
_PLACEHOLDER_MAX = 96


def _is_real_cover(path: Path | None) -> bool:
    """判定是否为「可用封面」：排除 Steam 的 32x32 占位图。"""
    if not _is_file(path):
        return False
    try:
        dims = image_size(path)
    except Exception:
        dims = None
    if dims is None:
        try:
            return path.stat().st_size >= _MIN_BYTES
        except Exception:
            return False
    return max(dims[0], dims[1]) >= _PLACEHOLDER_MAX


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(256 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_head(path: Path, limit: int = _HEAD_READ) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except Exception:
        return b""


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """从 JPEG 头部解析 (宽, 高)；解析不了返回 None。纯标准库，不依赖 PIL。"""
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:
            return None
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if seg_len < 2:
            return None
        if marker in sof_markers:
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            if width and height:
                return (width, height)
            return None
        i += 2 + seg_len
    return None


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[0:8] != b"\x89PNG\r\n\x1a\n":
        return None
    try:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
    except Exception:
        return None
    if width and height:
        return (width, height)
    return None


def webp_size(data: bytes) -> tuple[int, int] | None:
    """从 WebP 头部解析 (宽, 高)；支持 VP8 / VP8L / VP8X 三种容器。"""
    if len(data) < 30 or data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    try:
        if chunk == b"VP8 ":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
        elif chunk == b"VP8L":
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        elif chunk == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
        else:
            return None
    except Exception:
        return None
    return (width, height) if width and height else None


def gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[0:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return (width, height) if width and height else None


def bmp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 26 or data[0:2] != b"BM":
        return None
    width = int.from_bytes(data[18:22], "little")
    height = abs(int.from_bytes(data[22:26], "little", signed=True))
    return (width, height) if width and height else None


def image_kind_bytes(data: bytes) -> str | None:
    """按魔数判断图片格式：jpeg / png / webp / gif / bmp / None。

    **不看扩展名** —— Steam 缓存里很多图没有后缀（40 位哈希命名），只能靠魔数认。
    """
    if len(data) < 4:
        return None
    if data[0:2] == b"\xff\xd8":
        return "jpeg"
    if data[0:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[0:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[0:2] == b"BM":
        return "bmp"
    return None


def image_size_bytes(data: bytes) -> tuple[int, int] | None:
    """按魔数分派解析尺寸（不依赖文件扩展名）。"""
    if not data:
        return None
    return (
        jpeg_size(data)
        or png_size(data)
        or webp_size(data)
        or gif_size(data)
        or bmp_size(data)
    )


def image_size(path: Path) -> tuple[int, int] | None:
    """探测本地图片尺寸（只读文件头，按魔数分派；无扩展名也能认）。"""
    head = _read_head(path)
    if not head:
        return None
    return image_size_bytes(head)


def looks_like_image_bytes(data: bytes) -> bool:
    """按魔数判断像不像图片（用于 Content-Type 缺失/为 octet-stream 时的兜底）。"""
    return image_kind_bytes(data) is not None


def looks_like_image_file(path: Path | None) -> bool:
    """按魔数判断某个本地文件是不是图片（不看扩展名）。"""
    if not _is_file(path):
        return False
    return looks_like_image_bytes(_read_head(path, 64))


_MIME_BY_KIND = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
}


def mime_for_cover(path: Path | str | None) -> str:
    """给面板用：先按扩展名猜 MIME，扩展名缺失/未知时按魔数嗅探。

    Steam 缓存里存在**没有扩展名**的封面文件，只看后缀会退化成
    application/octet-stream，浏览器就不显示了。
    """
    if not path:
        return "application/octet-stream"
    suffix = Path(str(path)).suffix.lower()
    known = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }
    if suffix in known:
        return known[suffix]
    try:
        kind = image_kind_bytes(_read_head(Path(str(path)), 64))
    except Exception:
        kind = None
    return _MIME_BY_KIND.get(kind or "", "application/octet-stream")


def cover_kind(path: Path | str | None) -> str:
    """给面板用：这张图是横版还是竖版。

    返回 "landscape" | "portrait" | "square" | "unknown"。
    `library_600x900.jpg` 这类竖版图会被识别为 portrait，面板可换用不同的显示尺寸。
    """
    if not path:
        return "unknown"
    p = Path(str(path))
    name = p.name.lower()
    if name == "library_600x900.jpg":
        return "portrait"
    size = image_size(p)
    if not size:
        return "unknown"
    width, height = size
    if width > height * 1.05:
        return "landscape"
    if height > width * 1.05:
        return "portrait"
    return "square"


# --------------------------------------------------------------------------
# 目录与索引
# --------------------------------------------------------------------------

def covers_dir() -> Path:
    """本工具封面缓存目录（不存在时自动创建）。"""
    try:
        _COVERS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return _COVERS_DIR


def _index_path() -> Path:
    return _COVERS_DIR / _INDEX_NAME


def _cache_candidates(appid: int) -> list[Path]:
    root = _COVERS_DIR
    return [root / f"{appid}.jpg", root / f"{appid}.jpeg", root / f"{appid}.png"]


def _rebuild_index() -> dict[str, dict]:
    """索引损坏/缺失时，按磁盘上的图片重建索引（best effort）。"""
    index: dict[str, dict] = {}
    root = _COVERS_DIR
    try:
        entries = list(root.iterdir())
    except Exception:
        return index
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                continue
            if not entry.stem.isdigit():
                continue
            stat = entry.stat()
            index[str(int(entry.stem))] = {
                "source": "cache",
                "bytes": stat.st_size,
                "sha256": _sha256_file(entry),
                "updated_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(
                    timespec="seconds"
                ),
                "file": entry.name,
            }
        except Exception:
            continue
    return index


def _load_index() -> dict[str, dict]:
    """读取 data/covers/index.json；损坏时自动重建，永不抛异常。"""
    path = _index_path()
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("index 不是对象")
        clean: dict[str, dict] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                clean[str(key)] = value
        return clean
    except Exception:
        rebuilt = _rebuild_index()
        try:
            _save_index(rebuilt)
        except Exception:
            pass
        return rebuilt


def _save_index(index: Mapping[str, dict]) -> None:
    path = _index_path()
    payload = json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _record_index(appid: int, source: str, path: Path) -> None:
    """把一条封面记录写进索引（失败就算了，不影响主流程）。"""
    try:
        index = _load_index()
        index[str(appid)] = {
            "source": source,
            "bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
            "updated_at": _now_iso(),
            "file": path.name,
        }
        _save_index(index)
    except Exception:
        pass


def cover_index() -> dict[str, dict]:
    """公开接口：当前封面索引（只读快照）。"""
    return _load_index()


# --------------------------------------------------------------------------
# 本机 Steam 现成图
# --------------------------------------------------------------------------

def find_librarycache_dir(steam_path: str | None = None) -> Path | None:
    """定位 <Steam>/appcache/librarycache（Steam 客户端自己缓存的封面图）。"""
    root = steam_path
    if not root:
        try:
            from core import locator  # 延迟导入，避免循环依赖
            root = locator.find_steam_path()
        except Exception:
            return None
    if not root:
        return None
    try:
        candidate = Path(root) / "appcache" / "librarycache"
    except Exception:
        return None
    try:
        return candidate if candidate.is_dir() else None
    except Exception:
        return None


def _appid_image_dir(appid: int, steam_path: str | None = None) -> Path | None:
    """<librarycache>/<appid>/（不存在返回 None）。"""
    lc = find_librarycache_dir(steam_path)
    if lc is None:
        return None
    candidate = lc / str(appid)
    return candidate if _is_dir(candidate) else None


def _appid_hash_dirs(folder: Path | None) -> list[Path]:
    """<appid>/ 下 40 位十六进制命名的子目录（新版 Steam 把素材放在这里）。

    子目录数量有上限（防止异常目录结构拖慢），顺序稳定便于测试复现。
    """
    if not _is_dir(folder):
        return []
    found: list[Path] = []
    try:
        entries = sorted(folder.iterdir(), key=lambda e: e.name.lower())
    except Exception:
        return []
    for entry in entries:
        try:
            if _HASH_DIR_EXACT_RE.match(entry.name) and _is_dir(entry):
                found.append(entry)
        except Exception:
            continue
    return found


def _has_image_entries(folder: Path | None) -> bool:
    """目录里是否有任何可用的图片文件（按魔数判断，不看后缀）。"""
    if not _is_dir(folder):
        return False
    try:
        entries = list(folder.iterdir())
    except Exception:
        return False
    for entry in entries:
        try:
            if entry.is_file() and looks_like_image_file(entry):
                return True
        except Exception:
            continue
    return False


def _named_candidates(folder: Path, name: str) -> list[Path]:
    """一个基名 + 本地化变体，例如 header.jpg -> header_schinese.jpg。"""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, "jpg"
    out = [folder / name]
    for suffix in _LOCALIZED_SUFFIXES:
        out.append(folder / f"{stem}{suffix}.{ext}")
    return out


def _pick_named(dirs: Sequence[Path], names: Sequence[str]) -> Path | None:
    """按名字优先级在多个目录里找第一张真封面（先给名字，再给其本地化变体）。

    `dirs` 的顺序即目录优先级：本 appid 目录在前，哈希子目录在后。
    """
    for name in names:
        for folder in dirs:
            for candidate in _named_candidates(folder, name):
                if _is_real_cover(candidate):
                    return candidate
    return None


def _pick_hash_image(dirs: Sequence[Path]) -> Path | None:
    """挑 40 位十六进制哈希命名的图片：优先横版，其次面积/体积最大。

    兼容两种形态：``<40位哈希>.jpg`` 和**完全没有扩展名**的 ``<40位哈希>``
    —— Steam 用内容哈希命名素材，很多文件没有后缀，只能靠魔数识别。
    """
    picks: list[tuple[int, int, int, Path]] = []
    for folder in dirs:
        try:
            entries = list(folder.iterdir())
        except Exception:
            continue
        for entry in entries:
            try:
                if not entry.is_file() or not _HASH_NAME_RE.match(entry.name):
                    continue
                size = entry.stat().st_size
                if size <= 0:
                    continue
                if not looks_like_image_bytes(_read_head(entry, 64)):
                    continue          # 名字像哈希但内容不是图，别当封面
                dims = image_size(entry)
                if dims:
                    width, height = dims
                    if max(width, height) < _PLACEHOLDER_MAX:
                        continue      # 32x32 占位图，跳过
                    landscape = 1 if width >= height else 0
                    area = width * height
                elif size < _MIN_BYTES:
                    continue
                else:
                    landscape = 0
                    area = size
                picks.append((landscape, area, size, entry))
            except Exception:
                continue
    if not picks:
        return None
    picks.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    return picks[0][3]


# 旧名字保留（内部历史调用/外部可能引用），语义等价
_pick_hash_jpg = _pick_hash_image


def _pick_any_image(dirs: Sequence[Path]) -> Path | None:
    """最后的兜底：这些目录里任意一张图片（排除 logo/图标）。

    只看魔数、不看扩展名 —— Steam 缓存里存在没有后缀的封面文件，
    旧版本的「扩展名白名单」会把它们全部漏掉。
    """
    best: tuple[int, Path] | None = None
    for folder in dirs:
        try:
            entries = list(folder.iterdir())
        except Exception:
            continue
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                if entry.name.lower().startswith("logo"):
                    continue
                size = entry.stat().st_size
                if size <= 0:
                    continue
                if not looks_like_image_file(entry):
                    continue
                if not _is_real_cover(entry):
                    continue
                if best is None or size > best[0]:
                    best = (size, entry)
            except Exception:
                continue
    return best[1] if best else None


def local_cover_ex(
    appid: Any,
    steam_path: str | None = None,
    size: str = "header",
) -> tuple[Path | None, str]:
    """本地上找封面，返回 (路径, 来源标记)。

    来源标记：cache / header / library_600x900 / hash / library_hero / capsule / other / none
    """
    aid = _norm_appid(appid)
    if aid is None:
        return (None, "none")

    # 1) 本工具缓存
    for candidate in _cache_candidates(aid):
        if _is_file(candidate):
            return (candidate, "cache")

    # 2) 本机 Steam librarycache
    #    搜索顺序（保持历史优先级，只在末尾追加新的兜底）：
    #      a. 顶层具名横版/竖版图（含本地化变体）
    #      b. 哈希子目录 <appid>/<40位哈希>/ 里的同名图
    #      c. 40 位哈希命名的图片（含无扩展名的）
    #      d. 顶层其它已具名图（library_hero 等）
    #      e. 任意图片兜底
    folder = _appid_image_dir(aid, steam_path)
    if folder is not None:
        hash_dirs = _appid_hash_dirs(folder)
        dirs = [folder] + hash_dirs
        wanted = str(size or "header").strip().lower()
        if wanted in ("portrait", "600x900", "library_600x900"):
            named = list(_LIBRARY_PORTRAIT_NAMES)
        else:
            named = list(_LIBRARY_HEADER_NAMES)
        picked = _pick_named(dirs, named)
        if picked is not None:
            return (picked, picked.stem.lower())
        hashed = _pick_hash_image(dirs)
        if hashed is not None:
            return (hashed, "hash")
        extra = _pick_named(dirs, _LIBRARY_EXTRA_NAMES)
        if extra is not None:
            return (extra, extra.stem.lower())
        other = _pick_any_image(dirs)
        if other is not None:
            return (other, "other")

    return (None, "none")


def local_cover(appid: Any, steam_path: str | None = None, size: str = "header") -> Path | None:
    """纯本地取图（不联网）。没有就返回 None。"""
    path, _kind = local_cover_ex(appid, steam_path, size)
    return path


# --------------------------------------------------------------------------
# CDN
# --------------------------------------------------------------------------

def _clean_hash_hint(hint: Any) -> str | None:
    """从 upstream 的 header_image 字段里提取 40 位哈希目录名。

    兼容三种形态：
        "header.jpg"                          -> None
        "e9047d8ec47ae3d94bb8b464fb0fc9e9972b4ac7/header.jpg" -> "e904..."
        {"english": "e904.../header.jpg"}     -> "e904..."
    """
    if hint is None:
        return None
    if isinstance(hint, Mapping):
        for value in hint.values():
            found = _clean_hash_hint(value)
            if found:
                return found
        return None
    try:
        text = str(hint).strip()
    except Exception:
        return None
    if not text:
        return None
    match = _HASH_DIR_RE.search(text)
    if not match:
        return None
    return match.group(1).lower()


def cover_url_candidates(
    appid: Any,
    size: str = "header",
    header_image_hint: Any = None,
) -> list[str]:
    """按实测速度排序的 CDN 候选地址（国内可达，不需要加速器）。

    顺序：eccdnx(0.14s) -> akamaihd(0.39s) -> akamai(0.50s) -> cloudflare(0.53s)。
    给了 `header_image_hint`（形如 ``<40位哈希>/header.jpg``）时，会把带哈希的
    官方路径插到最前面——那才是 Steam 客户端实际用的地址。
    """
    aid = _norm_appid(appid)
    if aid is None:
        return []

    key = str(size or "header").strip().lower()
    primary = _SIZE_FILES.get(key, "header.jpg")
    files: list[str] = [primary]
    if primary != "header.jpg":
        files.append("header.jpg")

    urls: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        if url not in seen:
            seen.add(url)
            urls.append(url)

    hint_text = "" if isinstance(header_image_hint, Mapping) else str(header_image_hint or "").strip()
    # 上游给的是**完整图片直链**（小黑盒 T9 / 官方 appdetails 都会给）时，优先试它：
    # 官方 CDN 的固定文件名模式对未发售/下架作品经常 404，直链才是真正可用的那张。
    direct_url = hint_text if hint_text.startswith(("http://", "https://")) else ""
    if direct_url:
        add(direct_url)

    hashed_dir = None if direct_url else _clean_hash_hint(header_image_hint)
    if hashed_dir:
        for host, template, _cost in _CDN_HOSTS:
            add(f"https://{host}" + template.format(appid=aid, file=f"{hashed_dir}/{primary}"))

    for name in files:
        for host, template, _cost in _CDN_HOSTS:
            add(f"https://{host}" + template.format(appid=aid, file=name))

    return urls


def _http_get(url: str, timeout: float):
    """单独抽出来，方便测试打桩（tests 里会替换掉它）。"""
    if requests is None:  # pragma: no cover
        raise RuntimeError("requests 不可用")
    return requests.get(url, timeout=timeout, headers={"User-Agent": _UA})


def _fetch_image(url: str, timeout: float) -> tuple[bytes | None, str]:
    """抓一张图。返回 (bytes|None, error)。

    只接受：HTTP 200 + Content-Type 含 image + 体积 > 1000 字节。
    Content-Type 缺失/为 octet-stream 时，用魔数兜底（仍要求体积达标）。
    """
    try:
        response = _http_get(url, timeout)
    except Exception as exc:
        return (None, f"{type(exc).__name__}: {str(exc)[:80]}")
    try:
        status = getattr(response, "status_code", None)
        if status != 200:
            return (None, f"HTTP {status}")
        content_type = str(getattr(response, "headers", {}) or {}.get("Content-Type", "") or "").lower()
        try:
            data = response.content
        except Exception as exc:
            return (None, f"读取响应失败: {type(exc).__name__}")
        if not isinstance(data, (bytes, bytearray)):
            return (None, "响应不是二进制内容")
        data = bytes(data)
        if len(data) <= _MIN_BYTES:
            return (None, f"体积过小({len(data)}B)")
        if "image" in content_type:
            return (data, "")
        if not content_type or "octet-stream" in content_type or "binary" in content_type:
            if looks_like_image_bytes(data):
                return (data, "")
        return (None, f"Content-Type 不是图片({content_type[:40] or '缺失'})")
    except Exception as exc:  # pragma: no cover - 防御
        return (None, f"{type(exc).__name__}: {str(exc)[:80]}")
    finally:
        try:
            response.close()
        except Exception:
            pass


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                os.remove(tmp)
        except Exception:
            pass


# --------------------------------------------------------------------------
# 「官方确认无图（404）」名单
# --------------------------------------------------------------------------
#
# 有的 appid 官方 CDN **根本没有图**（未发售的新作 / 已下架 / 只有一篇纯色占位图），
# 抓多少次都是 404。面板的族谱提示必须区分「可以点按钮补齐」与「官方没有图」，
# 否则就是在骗用户；这里把「明确 404」的 appid 落盘记住，供面板只读查询。
#
# 只记 404；超时 / 网络错误**不记**（下次还要再试）。
# 路径走 covers_dir()，所以打包态天然落在 exe 旁的 data/covers（绝不硬编码路径）。

_NO_ART_NAME = "no_art.json"


def no_art_path() -> Path:
    """官方无图名单的落盘位置（与封面缓存同目录）。"""
    return covers_dir() / _NO_ART_NAME


def _load_no_art() -> dict[str, str]:
    """读官方无图名单 -> ``{appid: 记录时间}``；缺失 / 损坏 / 形态不对一律当空表。

    兼容两种形态：``{"appids": {appid: ts}}``（本模块写的）与 ``{appid: ts}`` / ``[appid, ...]``。
    """
    try:
        with open(no_art_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    raw = data.get("appids", data)
    if isinstance(raw, (list, tuple, set)):
        raw = {item: "" for item in raw}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        appid = _norm_appid(key)
        if appid is not None:
            out[str(appid)] = str(value or "")
    return out


def no_art_appids() -> set[int]:
    """官方确认无图（404）的 appid 集合：只读本地小 JSON，**绝不联网**、绝不抛。"""
    return {int(key) for key in _load_no_art()}


def mark_no_art(appids: Iterable) -> set[int]:
    """把「本次抓取中官方明确 404」的 appid **增量**并进名单并原子落盘。

    只该传 404 的 appid（超时 / 网络错误不算，下次还要再试）。返回合并后的完整名单；
    任何一步失败都只降级（返回已读到的部分），绝不抛异常、绝不联网。
    """
    merged = _load_no_art()
    stamp = _now_iso()
    changed = False
    try:
        candidates = list(appids)
    except Exception:
        candidates = []
    for raw in candidates:
        appid = _norm_appid(raw)
        if appid is None:
            continue
        key = str(appid)
        if key not in merged:
            changed = True
        merged[key] = stamp
    if changed:
        payload = {
            "updated_at": stamp,
            "appids": {key: merged[key] for key in sorted(merged, key=int)},
        }
        try:
            _atomic_write(
                no_art_path(),
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
            )
        except Exception:
            pass
    return {int(key) for key in merged}


# --------------------------------------------------------------------------
# 对外主接口
# --------------------------------------------------------------------------

def ensure_cover(
    appid: Any,
    steam_path: str | None = None,
    timeout: float = 8.0,
    header_image_hint: Any = None,
) -> dict:
    """确保某一款游戏的封面在本地可用。

    返回：
        {
          "appid": int,
          "ok": bool,
          "path": str | None,
          "source": "cache" | "librarycache" | "cdn:<host>" | "none",
          "bytes": int,
          "error": str | None,
          "kind": "landscape" | "portrait" | ...,   # 附加：面板可据此选显示尺寸
        }

    永不抛异常；任何失败都体现在 ok/error 上。
    """
    result: dict[str, Any] = {
        "appid": appid,
        "ok": False,
        "path": None,
        "source": "none",
        "bytes": 0,
        "error": None,
        "kind": "unknown",
    }

    aid = _norm_appid(appid)
    if aid is None:
        result["error"] = f"非法 appid: {appid!r}"
        return result
    result["appid"] = aid

    # 1) 离线优先：本工具缓存 -> 本机 Steam librarycache
    local, kind = local_cover_ex(aid, steam_path, "header")
    if local is not None:
        try:
            size = local.stat().st_size
        except Exception:
            size = 0
        source = "cache" if kind == "cache" else "librarycache"
        result.update(ok=True, path=str(local), source=source, bytes=size,
                      kind=cover_kind(local))
        if kind != "cache":
            _record_index(aid, source, local)
        return result

    # 2) 联网补图
    if requests is None:
        result["error"] = "缺少 requests 库，无法联网补齐封面"
        return result

    try:
        timeout = float(timeout)
    except Exception:
        timeout = 8.0
    if timeout <= 0:
        timeout = 8.0

    deadline = time.time() + _TOTAL_BUDGET_SECONDS
    errors: list[str] = []
    target = covers_dir() / f"{aid}.jpg"

    for url in cover_url_candidates(aid, "header", header_image_hint):
        budget = deadline - time.time()
        if budget <= 0.5:
            errors.append("超过单款总耗时预算，停止尝试")
            break
        data, error = _fetch_image(url, min(timeout, budget))
        if data is None:
            errors.append(f"{url.split('/')[2]}: {error}")
            continue
        try:
            _atomic_write(target, data)
        except Exception as exc:
            errors.append(f"落盘失败: {type(exc).__name__}: {str(exc)[:60]}")
            continue
        host = url.split("/")[2]
        result.update(
            ok=True,
            path=str(target),
            source=f"cdn:{host}",
            bytes=len(data),
            kind=cover_kind(target),
        )
        _record_index(aid, result["source"], target)
        return result

    result["error"] = "; ".join(errors[-3:]) if errors else "所有封面源均失败"
    return result


def ensure_covers(
    appids: Iterable,
    steam_path: str | None = None,
    limit: int | None = None,
    progress: Callable[[int, int, dict], None] | None = None,
    timeout: float = 8.0,
    header_image_hint: Any = None,
) -> dict:
    """批量确保封面可用。

    appids：appid 序列（或 dict，取 keys）
    limit：最多真正尝试多少款（超出的计入 skipped）
    progress：可选回调 progress(done, total, result)
    header_image_hint：可选，单个 hint 或 {appid: hint} 映射

    返回：
        {"total", "ok", "failed", "skipped", "unsupported", "by_source", "results"}
    """
    try:
        items = list(appids.keys()) if isinstance(appids, Mapping) else list(appids)
    except Exception as exc:
        return {
            "total": 0, "ok": 0, "failed": 0, "skipped": 0, "unsupported": 0,
            "by_source": {}, "results": [], "error": f"appids 无法遍历: {exc}",
        }

    total = len(items)
    try:
        limit_value = int(limit) if limit is not None else None
    except Exception:
        limit_value = None

    hints: Mapping | None = header_image_hint if isinstance(header_image_hint, Mapping) else None
    results: list[dict] = []
    ok = failed = skipped = unsupported = 0
    by_source: dict[str, int] = {}
    attempted = 0
    done = 0

    for raw in items:
        done += 1
        aid = _norm_appid(raw)
        if aid is None:
            unsupported += 1
            entry = {
                "appid": raw, "ok": False, "path": None, "source": "none",
                "bytes": 0, "error": f"非法 appid: {raw!r}", "kind": "unknown",
            }
        elif limit_value is not None and attempted >= limit_value:
            skipped += 1
            entry = {
                "appid": aid, "ok": False, "path": None, "source": "none",
                "bytes": 0, "error": "超过 limit，未尝试", "kind": "unknown",
            }
        else:
            attempted += 1
            hint = None
            if hints is not None:
                hint = hints.get(aid, hints.get(str(aid)))
            entry = ensure_cover(aid, steam_path=steam_path, timeout=timeout,
                                 header_image_hint=hint)
            if entry.get("ok"):
                ok += 1
                by_source[entry.get("source", "unknown")] = by_source.get(entry.get("source", "unknown"), 0) + 1
            else:
                failed += 1

        results.append(entry)
        if progress is not None:
            try:
                progress(done, total, entry)
            except Exception:
                pass

    return {
        "total": total,
        "ok": ok,
        "failed": failed,
        "skipped": skipped,
        "unsupported": unsupported,
        "by_source": by_source,
        "results": results,
    }


def cover_for_panel(appid: Any, steam_path: str | None = None) -> Path | None:
    """面板路由专用：**只做本地查找，绝不联网**。找不到返回 None。

    保持向后兼容（仍返回横版优先的路径）。要竖版优先请用 :func:`panel_cover`。
    """
    return local_cover(appid, steam_path, "header")


# 面板统一规格：优先竖版 library_600x900，无竖版再退横版。
PANEL_COVER_KINDS: tuple[str, ...] = ("portrait", "landscape", "square", "unknown")


def panel_cover(
    appid: Any,
    steam_path: str | None = None,
    *,
    prefer: str = "portrait",
) -> dict:
    """面板统一封面规格（**纯本地，不联网**）：优先竖版，返回是否竖版标记。

    ``prefer="portrait"``（默认）：先找 ``library_600x900.jpg``（竖版），找不到再退横版 ``header.jpg``；
    ``prefer="header"``：保持历史行为（横版优先）。

    返回（字段稳定，供展示层 / render 层使用）::

        {
          "appid": int,
          "ok": bool,
          "path": str | None,
          "source": "cache" | "library_600x900" | "header" | "hash" | ... | "none",
          "kind": "portrait" | "landscape" | "square" | "unknown",
          "is_portrait": bool,     # kind == "portrait"
          "preferred": "portrait" | "header",
        }

    任何异常都降级为 ``ok=False``，绝不抛。
    """
    wanted = "header" if str(prefer or "").strip().lower() in ("header", "landscape", "wide") else "portrait"
    result: dict[str, Any] = {
        "appid": appid,
        "ok": False,
        "path": None,
        "source": "none",
        "kind": "unknown",
        "is_portrait": False,
        "preferred": wanted,
    }
    aid = _norm_appid(appid)
    if aid is None:
        return result
    result["appid"] = aid
    try:
        path, source = _pick_panel_cover(aid, steam_path, wanted)
        if path is None:
            return result
        kind = cover_kind(path)
        result.update(ok=True, path=str(path), source=source, kind=kind,
                      is_portrait=(kind == "portrait"))
    except Exception:
        return result
    return result


def _steam_named(appid: int, names: Sequence[str], steam_path: str | None):
    """在**本机 Steam librarycache**里按具名文件顺序找（绕过本工具缓存）。"""
    try:
        folder = _appid_image_dir(appid, steam_path)
        if folder is None:
            return None, ""
        dirs = [folder] + _appid_hash_dirs(folder)
        picked = _pick_named(dirs, list(names))
        if picked is None:
            return None, ""
        return picked, picked.stem.lower()
    except Exception:
        return None, ""


def _cached_cover(appid: int):
    """本工具自己下载的封面缓存（appid.jpg 等）。"""
    try:
        for candidate in _cache_candidates(appid):
            if _is_file(candidate):
                return candidate, "cache"
    except Exception:
        pass
    return None, ""


def _pick_panel_cover(appid: int, steam_path: str | None, wanted: str):
    """按规格挑封面：``wanted="portrait"`` 时优先真竖版，避免被缓存的横版挡住。

    顺序（portrait）：Steam 竖版 -> 工具缓存 -> Steam 横版。
    顺序（header  ）：工具缓存 -> Steam 横版 -> Steam 竖版（与历史行为一致）。
    """
    steam_portrait = _steam_named(appid, _LIBRARY_PORTRAIT_NAMES, steam_path)
    steam_header = _steam_named(appid, _LIBRARY_HEADER_NAMES, steam_path)
    cached = _cached_cover(appid)
    order = (
        (steam_portrait, cached, steam_header)
        if wanted == "portrait"
        else (cached, steam_header, steam_portrait)
    )
    for path, source in order:
        if path is not None:
            return path, source
    return None, ""


def panel_cover_kind(appid: Any, steam_path: str | None = None) -> str:
    """只问「这张封面是不是竖版」：返回 ``portrait`` / ``landscape`` / ... / ``unknown``。"""
    return str(panel_cover(appid, steam_path).get("kind") or "unknown")
