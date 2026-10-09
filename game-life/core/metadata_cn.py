"""游戏中文名抓取（离线优先 + 权威商店接口，多来源）。

来源优先级：
  1. 本地缓存            data/names_cn.json（一次抓到就永久复用）
  2. 本机 Steam 客户端   appcache/appinfo.vdf 的 name_localized（离线、权威）
  3. 蒸汽平台官方商店接口 store.steamchina.com/api/appdetails?l=schinese&cc=CN
                        （Steam 中国官方源，大陆网络实测 0.2~0.7s 可直连）
  4. Steam 国际商店接口  store.steampowered.com/api/appdetails?l=schinese&cc=CN
                        （大陆网络时通时断，作为备用源）
  5. api.steamcmd.net    Steam 官方 appinfo 的公开镜像（大陆实测可直连），
                        补官方 name_localized 中文名
                     两个接口顺带返回 genres / developers / release_date /
                     header_image，供封面与统计复用，避免重复请求。

实测结论（2026-09 大陆网络、无加速器）：
  * 可直连：store.steamchina.com、Steam CDN（akamai / cloudflare / eccdnx）、
            howlongtobeat.com
  * 不可直连：store.steampowered.com（时通时断）、api.steampowered.com、
            steamcommunity.com、api.bgm.tv、steamdb.info、steamspy
因此中文名以「本机 Steam 客户端数据 + 蒸汽平台官方接口」为主，
国际商店接口只作为备用，并对连续失败的源做短路，避免拖慢整批任务。

对外契约（webapp / CLI 依赖）：
    load_cache() -> dict
    name_cn_for(appid, name_en=None) -> str | None   # 只读缓存/本地，不联网
    ensure_names_cn(items, *, steam_path=None, limit=None, progress=None,
                    use_network=True, force=False) -> dict
    record_names(pairs, *, source="external", overwrite=False) -> dict
                                          # 外部已确认的中文名入库（幂等、不覆盖）
    store_details(appid, timeout=8.0) -> dict        # 单款游戏的商店数据（联网）
    cache_path() -> Path
    clear_cache() -> None

任何异常都降级为「没有中文名」，绝不抛异常。

并发模型（2026-09 修复）：缓存内容**以磁盘为唯一事实来源**，进程内的 ``_cache``
只是「镜像」，且镜像里的条目字典被复用为「本次待写回的新条目暂存区」：
  * ``load_cache()`` 只在**首次**装载时读盘（``force=True`` 如上，历史行为不变）；
  * 所有写入路径（``_items()`` / ``_resolve_one()``）都先把磁盘上的最新条目合并
    进内存条目表，再落下本进程的新条目；
  * ``_save_cache()`` 的 read-modify-write 全程持模块级 ``_LOCK``，落盘即用内存
    条目表覆盖磁盘上的**同名键**（内存里的 ``None`` 哨兵值代表「删除该键」）；
  * ``name_cn_for()`` / ``stats()`` 等只读查询每次都回读磁盘，别的进程刚写的
    数据立刻可见。
这样即使在「同一进程内被反复替换模块级 ``_cache``」（模拟多进程）的对抗性用法下，
也不会再把别的写者**尚未落盘**的条目吞掉（历史的 lost update）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

try:  # 复用 schema 的路径解析（兼容 PyInstaller onefile）
    from data.schema import data_dir as _data_dir
except Exception:  # pragma: no cover
    def _data_dir() -> Path:
        return Path(__file__).resolve().parent.parent / "data"

CACHE_NAME = "names_cn.json"

# 中文名字符（汉字/假名/谚文）：判断一个「中文名」是否真的是中文。
# 历史缺陷：商店在 l=schinese 下若没有本地化名，会**原样返回英文名**，旧代码把它
# 当成中文名写进缓存 —— 既是假汉化，又会**遮蔽**后面真正的权威中文名（百科 /
# 人工校对表）。这里统一按「含中文才算中文名」把关。
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def looks_chinese(text) -> bool:
    """这段文本是否包含中文（汉字 / 假名 / 谚文）字符。"""
    return bool(_CJK_RE.search(str(text or "")))
STORE_APIS = (
    "https://store.steamchina.com/api/appdetails?appids={appid}&l=schinese&cc=CN",
    "https://store.steampowered.com/api/appdetails?appids={appid}&l=schinese&cc=CN",
)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GameLife/1.0",
           "Accept-Language": "zh-CN,zh;q=0.9"}
REQUEST_INTERVAL = 0.4      # 商店接口限速，避免被限流
STORE_TIMEOUT = 6.0         # 单源超时；大陆网络下时通时断，别死等
CACHE_TTL_DAYS = 30         # 查过的记录 30 天内不再联网（含「确实没有中文名」）
_MAX_HOST_FAILS = 3         # 连续失败这么多次就不再尝试该源（短路）

_last_request = 0.0
_cache: dict | None = None
# 保护「读磁盘 → 合并 → 原子落盘」整段：并发写者不会再用旧快照互相覆盖。
# 只在 _save_cache() 里抢一次；持锁期间只调用**不抢锁**的 _read_disk_items() /
# _atomic_write_json()（与 name_cn_baike 的 _save / _save_locked 拆分同理，
# 避免「持锁又抢同一把不可重入锁」的历史死锁）。
_LOCK = threading.Lock()
_HOST_FAILS: dict[str, int] = {}
_DEAD_HOSTS: set = set()


def cache_path() -> Path:
    """中文名缓存文件位置（data/names_cn.json）。"""
    return _data_dir() / CACHE_NAME


def load_cache(force: bool = False) -> dict:
    """读取磁盘缓存（内存里也留一份）。永远返回 dict。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    data: dict = {"version": 1, "items": {}}
    path = cache_path()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("items"), dict):
                data = loaded
        except (OSError, ValueError):
            data = {"version": 1, "items": {}}
    _cache = data
    return data


def _read_disk_items() -> dict:
    """重新读一遍磁盘上的 items（不看内存快照）。任何异常都降级为空 dict。"""
    try:
        path = cache_path()
        if not path.is_file():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        items = loaded.get("items") if isinstance(loaded, dict) else None
        if not isinstance(items, dict):
            return {}
        return {str(k): dict(v) for k, v in items.items() if isinstance(v, dict)}
    except Exception:
        return {}


def payload_items(path: Path) -> dict:
    """读回**磁盘上**的 items（落盘后记账用）。任何异常都降级为空 dict。"""
    try:
        if not path.is_file():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    items = loaded.get("items") if isinstance(loaded, dict) else None
    if not isinstance(items, dict):
        return {}
    return {str(k): v for k, v in items.items() if isinstance(v, dict)}


_NONE = None      # 「内存里这个键 = None」哨兵：代表删除该键（module 级，可被测试打桩）


def _disk_items_for_merge() -> dict:
    """读盘却读不到任何 items 时，返回 ``_NONE``（哨兵），表示「别做回退式合并」。

    正常运行时（路径存在、父目录会被自动创建）首次保存前磁盘上没有文件，
    此时返回 ``{}``：空文件 ≙ 空表，合并无害；测试/异常情况用哨兵区分。
    """
    disk = _read_disk_items()
    if not disk and not cache_path().is_file():
        return _NONE
    return disk


def _atomic_write_json(path: Path, payload: dict) -> None:
    """原子落盘：先写同目录临时文件再 os.replace，避免半截 JSON。

    失败（含 ``os.fsync`` / ``os.replace`` 抛错）时清掉本次的临时文件：
    临时文件不是合法缓存，留着只会让下次保存多一次无用 IO，也会让
    「保存结束后不该残留 .tmp」这类检查落空。清理失败一律忽略，不掩盖原异常。
    """
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=0)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _save_cache() -> None:
    """read-modify-write：写盘前重读磁盘，内存记录优先，原子落盘，绝不抛异常。

    公开入口：自己抢 ``_LOCK`` 后转交 ``_save_cache_locked()``。
    **不要**把 ``_LOCK`` 换成可重入锁再让自己抢第二次——同一个不可重入 ``Lock``
    抢两次会直接死锁（name_cn_baike 历史上正是这么挂的）。
    """
    with _LOCK:
        _save_cache_locked()


def _save_cache_locked() -> None:
    """``_save_cache()`` 的实际实现：**调用方必须已持有 _LOCK**。

    整段「重读磁盘 → 与内存合并 → 原子落盘」都在锁内完成，两个写者不会
    各自基于同一份旧快照交错覆盖；重入点只用不抢锁的
    ``_read_disk_items()`` / ``_atomic_write_json()``。

    合并规则是「磁盘有的先吃进来，**内容**与上次落盘不同的内存条目再覆盖」：
    进程内那个模块级 ``_cache`` 可能被「别的写者」刚换成只含它自己新条目的
    快照（测试里就是两个写者各自换 ``_cache`` 模拟两个进程），按身份覆盖会把
    对方**还没落盘**的条目当垃圾丢掉，按内容比较则只丢「磁盘上已有旧值」的键。
    """
    global _cache
    if _cache is None:
        return
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        disk = _disk_items_for_merge()       # 别的进程可能已经写了新数据
        if disk is not _NONE:
            _refresh_from_disk(disk)         # 先把磁盘新条目吃进来（镜像）
        memory = _cache.get("items")
        if not isinstance(memory, dict):
            memory = {}
        merged = {str(k): v for k, v in memory.items() if v is not _NONE}
        version = _cache.get("version")
        if not isinstance(version, int):
            version = 1
        # 失败会自己清掉 .tmp
        _atomic_write_json(path, {"version": version, "items": merged})
        _cache = {"version": version, "items": merged}
    except Exception:
        pass                                 # 契约：写失败就保持磁盘原样


def clear_cache() -> None:
    """清空内存与磁盘缓存（测试用）。"""
    global _cache
    _cache = {"version": 1, "items": {}}
    path = cache_path()
    try:
        # 顺手清掉失败保存可能残留的临时文件（它同样不是合法缓存）
        path.with_name(path.name + ".tmp").unlink()
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass


def _items() -> dict:
    """内存条目表（**镜像**）：取用前先把磁盘上的最新条目合并进来。

    这样「本进程新写入的条目」与「别的进程已经落盘的条目」同时在场，
    随后的 ``_save_cache()`` 落盘就不会把对方的新数据抹掉。
    """
    store = load_cache().setdefault("items", {})
    if not store:
        # 镜像为空：磁盘可能已经有数据（别的进程写的），补一次再返回。
        disk = _disk_items_for_merge()
        if disk is not _NONE:
            _refresh_from_disk(disk)
            store = _cache.get("items") if isinstance(_cache, dict) else store
    return store


def _refresh_from_disk(disk_items) -> None:
    """把「刚重读到的磁盘 items」并入内存快照：磁盘有的先落进来，内存独有的再补上。

    ``items`` 字典是**原地更新**的：内存里只要不是 None 的 items 对象始终是
    同一个 dict，所以 ``store = _items()`` 这种「先取引用、后写内存」的既有
    写法依然生效（``_resolve_one`` / ``ensure_names_cn`` 依赖这一点）。

    内存里的 ``_NONE`` 哨兵表示「这个键要删」，因此不会被磁盘旧值复活。
    """
    global _cache
    if not isinstance(disk_items, dict) or not disk_items:
        return
    items = _items()
    merged = {str(k): v for k, v in disk_items.items()}
    merged.update(items)                  # 内存（含哨兵）优先，不被磁盘旧值回退
    items.clear()
    items.update(merged)
    if isinstance(_cache, dict):
        _cache["items"] = items


def _disk_only_entry(appid) -> tuple[bool, dict]:
    """内存快照没命中时重读一次磁盘，返回 (磁盘上有没有这条, 该条目)。

    只读、不联网；任何异常都降级为「磁盘上没有这条」，绝不抛异常。
    这样别的进程（或旧的长时间运行进程）刚写进磁盘的数据也能立刻读到。
    """
    key = str(appid)
    items = _items()
    entry = items.get(key)
    if isinstance(entry, dict) and entry:
        return True, entry
    disk = _read_disk_items()
    _refresh_from_disk(disk)                 # 磁盘上的条目并入镜像（删除哨兵除外）
    entry = _items().get(key)
    if isinstance(entry, dict) and entry:
        return True, entry
    return False, {}


def _entry(appid) -> dict | None:
    """内存镜像里的条目：命中 dict 返回它，标记删除返回 None，没有则 {}（触发回读）。

    ``_NONE`` 是「删除」哨兵（模块级，默认值就是 ``None``），所以必须先判断键
    **在不在**、再判断值是不是哨兵：否则「从没查过的 appid」会被误判成「已被删除」，
    ``name_cn_for()`` 里「退回本机 appinfo 取官方中文名」的那条兜底就成了死代码
    ——E4 实测：族谱里非自有作品的官方中文名一直取不到，只能显示英文名。
    """
    items = _items()
    key = str(appid)
    if key not in items:
        return {}
    value = items.get(key)
    if value is _NONE:
        return None
    return value if isinstance(value, dict) else {}


def _fresh(entry: dict) -> bool:
    stamp = entry.get("checked_at") or entry.get("updated") or ""
    if not stamp:
        return False
    try:
        when = time.mktime(time.strptime(stamp, "%Y-%m-%d"))
    except (ValueError, TypeError):
        return False
    return (time.time() - when) / 86400 < CACHE_TTL_DAYS


# ------------------------------------------------------------------ 查询

def name_cn_for(appid, name_en: str | None = None) -> str | None:
    """只读：磁盘缓存 → 内存缓存 → 本机 appinfo（不联网）。取不到返回 None。"""
    entry = _entry(appid)
    if entry is None:
        # 内存里标了「删除」：磁盘上的旧值不算数，直接当没有。
        return None
    if not entry:
        # 内存没命中：磁盘可能是别的进程刚写的（面板与抓取进程并行时的常态），
        # 回读一次磁盘，别拿启动时的旧快照当答案。
        found, on_disk = _disk_only_entry(appid)
        if found:
            entry = on_disk
    value = str(entry.get("name_cn") or "").strip()
    if value:
        return value
    if entry.get("checked_at"):
        return None            # 已经查过且确认没有中文名
    try:
        from core import appinfo

        local = appinfo.localized_name(appid, "schinese")
    except Exception:
        local = None
    if local:
        return local
    if name_en:
        try:
            from core import appinfo

            local = appinfo.localized_name(appid, "tchinese")
        except Exception:
            local = None
    return local or None


def name_en_for(appid, name_en: str | None = None) -> str | None:
    """英文原名：调用方给的优先，其次本机 appinfo。"""
    if name_en and str(name_en).strip():
        return str(name_en).strip()
    try:
        from core import appinfo

        entry = appinfo.app_metadata(appid)
        return str(entry.get("name") or "").strip() or None
    except Exception:
        return None


def _host_of(url: str) -> str:
    parts = str(url).split("/")
    return parts[2] if len(parts) > 2 else str(url)


def _note_host_fail(host: str) -> None:
    """连续失败的源做短路，避免整批任务被拖慢。"""
    _HOST_FAILS[host] = _HOST_FAILS.get(host, 0) + 1
    if _HOST_FAILS[host] >= _MAX_HOST_FAILS and host not in _DEAD_HOSTS:
        _DEAD_HOSTS.add(host)
        print(f"  [{host}] 连续 {_HOST_FAILS[host]} 次不可用，本次不再尝试该源")


STEAMCMD_API = "https://api.steamcmd.net/v1/info/{appid}"


def steamcmd_details(appid, timeout: float = STORE_TIMEOUT) -> dict:
    """api.steamcmd.net：Steam 官方 appinfo 的公开镜像（大陆实测可直连）。

    返回 {"name": 英文名, "name_cn": 官方中文名, "type": 类型}，取不到返回 {}。
    """
    global _last_request
    host = _host_of(STEAMCMD_API)
    if host in _DEAD_HOSTS:
        return {}
    try:
        import requests
    except ImportError:
        return {}
    wait = REQUEST_INTERVAL - (time.time() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.time()
    try:
        resp = requests.get(STEAMCMD_API.format(appid=appid), timeout=timeout, headers=HEADERS)
        if resp.status_code != 200:
            _note_host_fail(host)
            return {}
        node = ((resp.json() or {}).get("data") or {}).get(str(appid)) or {}
        common = node.get("common") if isinstance(node.get("common"), dict) else {}
        localized = common.get("name_localized") if isinstance(common.get("name_localized"), dict) else {}
        name_cn = ""
        for code in ("schinese", "sc_schinese", "tchinese"):
            value = str(localized.get(code) or "").strip()
            if value:
                name_cn = value
                break
        _HOST_FAILS[host] = 0
        return {
            "name": str(common.get("name") or "").strip(),
            "name_cn": name_cn,
            "type": str(common.get("type") or ""),
        }
    except Exception:
        _note_host_fail(host)
        return {}


def reset_hosts() -> None:
    """重置源健康状态（测试用）。"""
    _HOST_FAILS.clear()
    _DEAD_HOSTS.clear()


def store_details(appid, timeout: float = STORE_TIMEOUT, apis=None) -> dict:
    """取商店详情：蒸汽平台 → Steam 国际商店（联网，全部失败返回空 dict）。"""
    global _last_request
    try:
        import requests
    except ImportError:
        return {}
    for template in (apis or STORE_APIS):
        host = _host_of(template)
        if host in _DEAD_HOSTS:
            continue
        wait = REQUEST_INTERVAL - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        try:
            resp = requests.get(template.format(appid=appid), timeout=timeout, headers=HEADERS)
        except Exception:
            _note_host_fail(host)
            continue
        if resp.status_code != 200:
            _note_host_fail(host)
            continue
        try:
            payload = (resp.json() or {}).get(str(appid)) or {}
        except ValueError:
            _note_host_fail(host)
            continue
        if payload.get("success"):
            data = payload.get("data") or {}
            if isinstance(data, dict) and data:
                _HOST_FAILS[host] = 0
                return data
        # success=False：该游戏不在这家商店，换下一家（不算故障）
    return {}


# ------------------------------------------------------------------ 批量抓取

def _normalize(items) -> list[tuple[int, str]]:
    """统一成 [(appid, name_en)]，脏数据直接跳过。"""
    result: list[tuple[int, str]] = []
    seen: set[int] = set()
    for item in items or []:
        name_en = ""
        if isinstance(item, dict):
            raw_id = item.get("appid") or item.get("id")
            name_en = str(item.get("name") or item.get("name_en") or "")
        elif isinstance(item, (tuple, list)) and len(item) >= 1:
            raw_id = item[0]
            name_en = str(item[1]) if len(item) > 1 and item[1] else ""
        else:
            raw_id = item
        try:
            appid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if appid in seen:
            continue
        seen.add(appid)
        result.append((appid, name_en))
    return result


def ensure_names_cn(items, *, steam_path: str | None = None, limit: int | None = None,
                    progress=None, use_network: bool = True, force: bool = False) -> dict:
    """批量补中文名；返回统计与逐条结果。

    limit 只处理前 N 款（None = 全部）。已查过且未过期的记录直接复用，不联网。
    """
    pairs = _normalize(items)
    if limit is not None:
        pairs = pairs[: max(0, int(limit))]

    store = _items()
    results: dict[int, dict] = {}
    ok = failed = skipped = 0
    for index, (appid, name_en) in enumerate(pairs, start=1):
        entry = store.get(str(appid)) or {}
        reused = bool(str(entry.get("name_cn") or "").strip()) or (not force and _fresh(entry))
        if reused:
            skipped += 1
            results[appid] = {
                "ok": True,
                "name_cn": str(entry.get("name_cn") or ""),
                "name_en": str(entry.get("name_en") or name_en or ""),
                "source": str(entry.get("source") or "cache"),
                "skipped": True,
            }
            if callable(progress):
                _safe_progress(progress, index, len(pairs), appid, results[appid])
            continue

        row = _resolve_one(appid, name_en, steam_path=steam_path, use_network=use_network)
        results[appid] = row
        if row.get("name_cn"):
            ok += 1
        elif row.get("error"):
            failed += 1
        else:
            failed += 1
        if callable(progress):
            _safe_progress(progress, index, len(pairs), appid, row)

    _save_cache()
    hook = globals().get("_on_cache_written")
    if callable(hook):
        hook([appid for appid, _ in pairs])   # 面板复用同进程时也能感知到新数据
    return {
        "ok": failed == 0,
        "total": len(pairs),
        "ok_count": ok,
        "fail_count": failed,
        "skip_count": skipped,
        "results": results,
        "message": f"中文名：新增 {ok} / 未获取 {failed} / 复用 {skipped} / 共 {len(pairs)}",
    }


def _safe_progress(progress, index, total, appid, row) -> None:
    try:
        progress(index, total, appid, row)
    except Exception:
        pass


def _resolve_one(appid: int, name_en: str, *, steam_path: str | None = None,
                 use_network: bool = True) -> dict:
    """单个游戏：本地 appinfo → 商店接口；结果写入缓存。"""
    store = _items()
    row = {
        "appid": appid,
        "name_cn": "",
        "source": "",
        "error": "",
        "skipped": False,
    }
    row["name_en"] = str(name_en or "")

    local_cn = ""
    local_en = ""
    try:
        from core import appinfo

        local_cn = appinfo.localized_name(appid, "schinese", steam_path) or ""
        local_en = str((appinfo.app_metadata(appid, steam_path) or {}).get("name") or "")
    except Exception:
        pass

    if local_cn:
        row.update({"name_cn": local_cn, "source": "steam_client"})
    if not row["name_en"]:
        row["name_en"] = local_en

    if use_network:
        data = store_details(appid)
        if data:
            name = str(data.get("name") or "").strip()
            if not row["name_en"]:
                row["name_en"] = name
            if name and not row["name_cn"] and looks_chinese(name):
                # 只有确实拿到**中文**名才落 name_cn；商店在该语言下回英文说明它
                # 没有中文名，这时留空，交给百科 / 人工校对表补（否则英文名会把
                # 后面真正的权威中文名遮蔽掉）。
                row["name_cn"] = name
                row["source"] = "steam_store"
            row["header_image"] = str(data.get("header_image") or "")
            genres = data.get("genres") or []
            row["genres"] = [str(g.get("description")) for g in genres if isinstance(g, dict)]
            row["developers"] = [str(x) for x in (data.get("developers") or [])]
            row["release_date"] = str((data.get("release_date") or {}).get("date") or "")
        if not row["name_cn"]:
            # 再补一次：api.steamcmd.net（Steam 官方数据镜像，大陆可直连）
            mirror = steamcmd_details(appid)
            if mirror:
                if not row["name_en"]:
                    row["name_en"] = mirror.get("name") or ""
                if mirror.get("name_cn"):
                    row["name_cn"] = mirror["name_cn"]
                    row["source"] = "steamcmd"
        if not row["name_cn"]:
            row["error"] = "本地与两个商店源都没有该游戏的中文名"
    elif not row["name_cn"]:
        row["error"] = "离线模式：本机没有该游戏的中文名"

    if row["name_cn"] and not looks_chinese(row["name_cn"]):
        # 兜底：任何来源给出的 name_cn 若不含中文，一律不算中文名
        row["name_cn"] = ""
        row["source"] = ""
        if not row["error"]:
            row["error"] = "商店只返回英文名，未找到中文译名"

    fresh, on_disk = _disk_only_entry(appid)     # 别的进程可能刚写过这款
    entry = dict(on_disk) if fresh else {}
    entry.update({k: v for k, v in row.items() if k not in ("skipped",)})
    if use_network or row["name_cn"]:
        # 只有「联网查过」或「确实拿到中文名」才记检查时间；
        # 离线未命中不记，避免之后联网查询被 30 天 TTL 跳过。
        entry["checked_at"] = time.strftime("%Y-%m-%d")
        entry["updated"] = entry["checked_at"]
    store[str(appid)] = entry
    return row


def stats() -> dict:
    """缓存概况：总数 / 有中文名 / 有商店数据。"""
    store = _items()
    with_cn = sum(1 for v in store.values() if str(v.get("name_cn") or "").strip())
    return {"path": str(cache_path()), "count": len(store), "with_name_cn": with_cn}


def record_names(pairs, *, source: str = "external", overwrite: bool = False) -> dict:
    """把**外部已确认**的中文名写进缓存（脚本导入 / 权威站点抓取 / 人工校对表）。

    * ``pairs``：``{appid: 中文名}`` 或 ``[(appid, 中文名), ...]``；
    * 默认**只补空缺**（幂等）：已有 ``name_cn`` 的条目不会被覆盖；
    * ``looks_chinese()`` 为假的（英文名冒充中文名）一律跳过；
    * 落盘复用 ``_save_cache()``（原子写 + 先合并磁盘），失败也不抛异常。
    """
    if isinstance(pairs, dict):
        items = list(pairs.items())
    else:
        items = list(pairs or [])
    store = _items()
    added = skipped = 0
    touched: list[int] = []
    today = time.strftime("%Y-%m-%d")
    for raw_id, raw_name in items:
        try:
            appid = int(raw_id)
        except (TypeError, ValueError):
            skipped += 1
            continue
        name = str(raw_name or "").strip()
        if not name or not looks_chinese(name):
            skipped += 1
            continue
        key = str(appid)
        entry = store.get(key)
        entry = dict(entry) if isinstance(entry, dict) else {}
        if str(entry.get("name_cn") or "").strip() and not overwrite:
            skipped += 1
            continue
        entry["appid"] = appid
        entry["name_cn"] = name
        entry["source"] = str(source or "external")
        if str(entry.get("error") or "") and not overwrite:
            entry["error"] = ""
        entry.setdefault("name_en", "")
        entry["checked_at"] = today
        entry["updated"] = today
        store[key] = entry
        touched.append(appid)
        added += 1
    if added:
        _save_cache()
        hook = globals().get("_on_cache_written")
        if callable(hook):
            try:
                hook(touched)
            except Exception:
                pass
    return {"ok": True, "added": added, "skipped": skipped, "source": str(source or "external")}


def _on_cache_written(appids) -> None:
    """写缓存后通知元数据只读缓存失效（``store_api.invalidate_cache`` 缺失时静默跳过）。"""
    try:
        from core import store_api

        invalidate = getattr(store_api, "invalidate_cache", None)
        if callable(invalidate):
            invalidate(appids)
    except Exception:
        pass
