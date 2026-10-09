"""本机成就数据（离线，无需联网 / 无需加速器）。

数据来源：Steam 客户端自带缓存 <Steam>/appcache/stats/
  * UserGameStats_<账号id>_<appid>.bin —— 已解锁成就（含解锁时间）
  * UserGameStatsSchema_<appid>.bin    —— 该游戏成就定义（总数分母）

背景：Steam 官方接口 api.steampowered.com 与 steamcommunity.com 在大陆网络
无法直连，导致「成就冲刺雷达」拿不到任何数据。本模块改为读本机缓存，
不开加速器也能算完成度。

对外契约（webapp / CLI 依赖）：
    local_achievement(appid, account_id=None, steam_path=None) -> dict | None
        {"appid","achieved_count","total_count","completion_rate","source":"steam_client"}
    local_achievements(appids, account_id=None, steam_path=None) -> dict[int, dict]
    has_local_data(appid, steam_path=None) -> bool
    stats(account_id=None, steam_path=None) -> dict

解析失败一律返回 None / 空 dict，绝不抛异常。
"""
from __future__ import annotations

import struct
from pathlib import Path

_SCHEMA_CACHE: dict[tuple, dict] = {}


# ------------------------------------------------------------------ 基础工具

def _steam_root(steam_path: str | None = None) -> Path | None:
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


def _stats_dir(steam_path: str | None = None) -> Path | None:
    root = _steam_root(steam_path)
    if not root:
        return None
    folder = root / "appcache" / "stats"
    return folder if folder.is_dir() else None


def _kv_parse(buf: bytes, pos: int, end: int, depth: int = 0) -> tuple[dict, int]:
    """Steam 二进制 KeyValues（类型字节 + \\0 结尾键名 + 值）。"""
    if depth > 16:
        raise ValueError("嵌套过深")
    out: dict = {}
    while True:
        if pos >= end:
            return out, pos      # 文件末尾（真实缓存常以 0x08 收尾，这里两种都接受）
        vtype = buf[pos]
        pos += 1
        if vtype in (0x08, 0x0B):
            return out, pos
        stop = buf.find(b"\x00", pos)
        if stop < 0:
            raise ValueError("键名未闭合")
        key = buf[pos:stop].decode("utf-8", "replace")
        pos = stop + 1
        if vtype == 0x00:
            value, pos = _kv_parse(buf, pos, end, depth + 1)
        elif vtype == 0x01:
            stop = buf.find(b"\x00", pos)
            if stop < 0:
                raise ValueError("字符串未闭合")
            value = buf[pos:stop].decode("utf-8", "replace")
            pos = stop + 1
        elif vtype == 0x02:
            value = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        elif vtype == 0x07:
            value = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        else:
            raise ValueError(f"未知类型 {vtype:#x}")
        out[key] = value


def _load_kv(path: Path) -> dict:
    data = path.read_bytes()
    kv, _ = _kv_parse(data, 0, len(data))
    return kv


def _signature(path: Path) -> tuple:
    st = path.stat()
    return (str(path), int(st.st_mtime), st.st_size)


# ------------------------------------------------------------------ 成就总数

def _total_achievements(appid, steam_path: str | None = None) -> int | None:
    """从 Schema 文件数出该游戏成就总数（= bits 定义条数）。"""
    folder = _stats_dir(steam_path)
    if not folder:
        return None
    path = folder / f"UserGameStatsSchema_{appid}.bin"
    if not path.is_file():
        return None
    key = _signature(path)
    cached = _SCHEMA_CACHE.get(key)
    if cached is not None:
        return cached.get("total")
    total = 0
    names: list[str] = []
    try:
        kv = _load_kv(path)
        node = kv.get(str(appid)) if isinstance(kv.get(str(appid)), dict) else {}
        stats = node.get("stats") if isinstance(node.get("stats"), dict) else {}
        for stat in stats.values():
            bits = stat.get("bits") if isinstance(stat, dict) else None
            if not isinstance(bits, dict):
                continue
            for definition in bits.values():
                if not isinstance(definition, dict):
                    continue
                total += 1
                names.append(str(definition.get("name") or ""))
    except Exception:
        return None
    _SCHEMA_CACHE[key] = {"total": total or None, "names": names}
    return total or None


def _achievement_names(appid, steam_path: str | None = None) -> list[str]:
    folder = _stats_dir(steam_path)
    if not folder:
        return []
    path = folder / f"UserGameStatsSchema_{appid}.bin"
    if not path.is_file():
        return []
    _total_achievements(appid, steam_path)
    cached = _SCHEMA_CACHE.get(_signature(path)) or {}
    return list(cached.get("names") or [])


# ------------------------------------------------------------------ 已解锁数

def _stats_file(appid, account_id: str | None = None, steam_path: str | None = None) -> Path | None:
    folder = _stats_dir(steam_path)
    if not folder:
        return None
    if account_id:
        direct = folder / f"UserGameStats_{account_id}_{appid}.bin"
        return direct if direct.is_file() else None
    # 没给账号：取本机任意账号里最新的那份（同机多账号时以最近写入为准）
    candidates = [p for p in folder.glob(f"UserGameStats_*_{appid}.bin") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _unlocked_count(appid, account_id: str | None = None, steam_path: str | None = None) -> int:
    """统计 AchievementTimes 里的解锁条数（多个分组取并集）。"""
    path = _stats_file(appid, account_id, steam_path)
    if not path:
        return 0
    try:
        kv = _load_kv(path)
    except Exception:
        return 0
    unlocked: set[int] = set()

    def walk(node: dict) -> None:
        for key, value in node.items():
            if not isinstance(value, dict):
                continue
            if key == "AchievementTimes":
                for index in value:
                    try:
                        unlocked.add(int(index))
                    except (TypeError, ValueError):
                        continue
            else:
                walk(value)

    walk(kv)
    return len(unlocked)


# ------------------------------------------------------------------ 对外接口

def local_achievement(appid, account_id: str | None = None, steam_path: str | None = None) -> dict | None:
    """本机成就完成度；没有该游戏的成就定义时返回 None。"""
    try:
        appid_int = int(appid)
    except (TypeError, ValueError):
        return None
    try:
        total = _total_achievements(appid_int, steam_path)
    except Exception:
        return None
    if not total:
        return None
    achieved = _unlocked_count(appid_int, account_id, steam_path)
    achieved = max(0, min(achieved, total))
    return {
        "appid": appid_int,
        "achieved_count": achieved,
        "total_count": total,
        "completion_rate": round(achieved / total * 100, 2),
        "source": "steam_client",
    }


def local_achievements(appids, account_id: str | None = None,
                       steam_path: str | None = None) -> dict[int, dict]:
    """批量版本；没有成就数据的游戏不出现在结果里。"""
    result: dict[int, dict] = {}
    for appid in appids or []:
        row = local_achievement(appid, account_id=account_id, steam_path=steam_path)
        if row:
            result[int(row["appid"])] = row
    return result


def has_local_data(appid, steam_path: str | None = None) -> bool:
    """本机是否有该游戏的成就定义（不统计解锁数，很快）。"""
    try:
        return bool(_total_achievements(int(appid), steam_path))
    except (TypeError, ValueError):
        return False


def stats(account_id: str | None = None, steam_path: str | None = None) -> dict:
    """本机成就数据概况（面板/CLI 提示用）。"""
    folder = _stats_dir(steam_path)
    if not folder:
        return {"ok": False, "dir": "", "schema_files": 0, "player_files": 0,
                "message": "未找到 Steam 成就缓存目录"}
    schema_files = len(list(folder.glob("UserGameStatsSchema_*.bin")))
    pattern = f"UserGameStats_{account_id}_*.bin" if account_id else "UserGameStats_*_*.bin"
    player_files = len(list(folder.glob(pattern)))
    return {"ok": True, "dir": str(folder), "schema_files": schema_files,
            "player_files": player_files,
            "message": f"本机成就缓存：定义 {schema_files} 款 / 记录 {player_files} 份"}


def clear_cache() -> None:
    """清空内存缓存（测试用）。"""
    _SCHEMA_CACHE.clear()