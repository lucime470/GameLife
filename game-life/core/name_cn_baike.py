# -*- coding: utf-8 -*-
"""中文名补全（百度百科，大陆直连可达）。

用途：Steam 官方数据里**没有**简体中文名的游戏（appinfo.name_localized 缺失，
官方商店接口在大陆又被墙），用百度百科的词条标题补一个常用中文名。

判定规则（保守，宁可留空也不写错）：
    * 词条标题必须含中文字符；
    * 词条简介必须出现「游戏」（排除 "Celeste = 英语单词" 这类误命中）。

结果写 ``data/name_cn_baike.json``（30 天有效，含「确认没有」的负缓存）。

对外接口::

    lookup(appid, name_en) -> str | None        # 联网查一款
    ensure_names(items, ...) -> dict            # 批量补名（items 为 (appid, name_en) 或 dict）
    name_cn_for(appid) -> str | None            # 只读缓存，不联网
    stats() -> dict

绝不抛异常；单款超时 8s，连不上时自动短路，不拖慢整批任务。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import requests

logger = logging.getLogger("game-life.name_cn_baike")

APP_ROOT = Path(__file__).resolve().parent.parent
CACHE_NAME = "name_cn_baike.json"

API = ("https://baike.baidu.com/api/openapi/BaikeLemmaCardApi"
       "?scope=103&format=json&appid=379020&bk_key={key}&bk_length=200")

TIMEOUT = 8.0
REQUEST_INTERVAL = 0.35
CACHE_TTL_DAYS = 30
MAX_HOST_FAILS = 3

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GameLife/1.0 (+local)"

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_STRIP = re.compile(r"[™®©]")

_LOCK = threading.Lock()
_HOST_FAILS = 0
_HOST_DEAD = False
_MEMORY: dict[str, dict] = {}
_LOADED = False


def cache_path() -> Path:
    """缓存位置：与 cache.db 同目录（打包后是 exe 旁真实 data/，不是 _MEIPASS）。"""
    try:
        from data.schema import data_dir as _data_dir

        return Path(_data_dir()) / CACHE_NAME
    except Exception:                     # pragma: no cover - 极端降级
        return APP_ROOT / "data" / CACHE_NAME


def _load() -> dict:
    global _LOADED
    if _LOADED:
        return _MEMORY
    data: dict = {}
    path = cache_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw.get("items"), dict):
                data = raw["items"]
        except Exception as exc:
            logger.info("读取中文名补充缓存失败：%s", exc)
    _MEMORY.clear()
    _MEMORY.update(data)
    _LOADED = True
    return _MEMORY


def _read_disk() -> dict:
    """重新读一遍磁盘上的 items（不看内存快照）。任何异常都降级为空 dict。"""
    try:
        path = cache_path()
        if not path.is_file():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = raw.get("items") if isinstance(raw, Mapping) else None
        if not isinstance(items, Mapping):
            return {}
        return {str(k): dict(v) for k, v in items.items() if isinstance(v, Mapping)}
    except Exception as exc:
        logger.info("重新读取中文名补充缓存失败：%s", exc)
        return {}


def _atomic_write_json(path: Path, payload: Mapping) -> None:
    """原子落盘：先写同目录临时文件再 os.replace，避免半截 JSON。"""
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=0)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _save(disk_items: Mapping | None = None) -> None:
    """read-modify-write：写盘前重读磁盘，内存记录优先，原子落盘，绝不抛异常。

    ``disk_items`` 可用「写盘前刚读到的磁盘快照」覆盖本次重读（仅省一次 IO）；
    传 None 时现场重读，保证多进程并发写不会用旧快照覆盖别人的新数据。

    公开入口：自己抢锁后转交 ``_save_locked()``。**不要**把 ``_LOCK`` 改成不可重入的锁
    再让自己抢一次（历史上正是「持锁调用 _save，_save 又抢同一把 Lock」导致永久死锁）。
    """
    with _LOCK:
        _save_locked(disk_items)


def _save_locked(disk_items: Mapping | None = None) -> None:
    """``_save()`` 的实际实现：**调用方必须已持有 _LOCK**（供 lookup 等持锁路径复用）。

    重入点全部只用「不抢锁」的 ``_read_disk()`` / ``_atomic_write_json()``。
    """
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from_disk = dict(disk_items) if isinstance(disk_items, Mapping) else _read_disk()
        merged = {str(k): v for k, v in from_disk.items() if isinstance(v, Mapping)}
        merged.update({str(k): v for k, v in _MEMORY.items()})   # 内存优先（更新）
        _atomic_write_json(path, {"version": 1, "items": merged})
        if merged != _MEMORY:
            _MEMORY.clear()
            _MEMORY.update(merged)
    except Exception as exc:
        logger.info("写入中文名补充缓存失败：%s", exc)   # 契约：失败保持磁盘原样


def clear_cache() -> None:
    """测试用：清空缓存。"""
    global _LOADED, _HOST_FAILS, _HOST_DEAD
    _MEMORY.clear()
    _LOADED = True
    try:
        # 清掉上次保存可能残留的临时文件（临时文件不是合法缓存，别让 clear_cache 漏掉）
        cache_path().with_name(cache_path().name + ".tmp").unlink()
    except OSError:
        pass
    _HOST_FAILS = 0
    _HOST_DEAD = False
    try:
        cache_path().unlink()
    except OSError:
        pass


def reset_hosts() -> None:
    """测试用：重置「百科不可用」短路标记。"""
    global _HOST_FAILS, _HOST_DEAD
    _HOST_FAILS = 0
    _HOST_DEAD = False


# ------------------------------------------------------------------ 判定

def _entry(appid: Any) -> dict:
    return _load().get(str(appid)) or {}


def _fresh(entry: Mapping) -> bool:
    stamp = str(entry.get("checked_at") or "")
    try:
        when = datetime.strptime(stamp, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return (date.today() - when).days < CACHE_TTL_DAYS


def is_usable_title(title: str, desc: str) -> bool:
    """词条是否是一个「游戏」的中文名（保守判定）。"""
    title = str(title or "").strip()
    desc = str(desc or "").strip()
    if not title or not _CJK.search(title):
        return False
    lowered = desc.lower()
    return "游戏" in desc or "game" in lowered


def query_variants(name_en: str) -> list[str]:
    """英文名 -> 依次尝试的查询词（保序去重）。"""
    base = _STRIP.sub("", str(name_en or "")).strip()
    out: list[str] = []
    for candidate in (base, base.split(":")[0].strip(), base.split(" - ")[0].strip()):
        if candidate and candidate not in out and len(candidate) >= 3:
            out.append(candidate)
    return out


# ------------------------------------------------------------------ 联网

def _query(key: str) -> tuple[str, str]:
    """查一个词条，返回 (标题, 简介)；失败返回 ("", "")。"""
    global _HOST_FAILS, _HOST_DEAD
    if _HOST_DEAD:
        return "", ""
    url = API.format(key=urllib.parse.quote(key))
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": UA})
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        with _LOCK:
            _HOST_FAILS += 1
            if _HOST_FAILS >= MAX_HOST_FAILS and not _HOST_DEAD:
                _HOST_DEAD = True
                logger.info("百度百科连续不可用，本次跳过该来源：%s", exc)
        return "", ""
    with _LOCK:
        _HOST_FAILS = 0
    if not isinstance(payload, Mapping):
        return "", ""
    return str(payload.get("title") or ""), str(payload.get("desc") or "")


def lookup(appid: Any, name_en: str) -> str | None:
    """联网查一款游戏的中文名；查不到返回 None（并记负缓存）。"""
    key = str(appid)
    entry = _entry(appid)
    if _fresh(entry):
        return str(entry.get("name_cn") or "").strip() or None
    if _HOST_DEAD:
        return None

    found = ""
    for variant in query_variants(name_en):
        title, desc = _query(variant)
        time.sleep(REQUEST_INTERVAL)
        if is_usable_title(title, desc):
            found = title.strip()
            break

    entry = {
        "name_cn": found,
        "name_en": str(name_en or ""),
        "source": "baike" if found else "",
        "checked_at": date.today().isoformat(),
    }
    try:
        fresh_disk = _read_disk()      # 先取磁盘快照，再合并内存，避免覆盖别的进程的新数据
    except Exception:
        fresh_disk = {}
    with _LOCK:
        _MEMORY[key] = entry
        _save_locked(fresh_disk)      # 已持锁，不能再走会抢锁的 _save()（不可重入）
    return found or None


def name_cn_for(appid: Any) -> str | None:
    """只读缓存（不联网）；没有记录或记录为空时返回 None。"""
    entry = _entry(appid)
    if not _fresh(entry):
        return None
    return str(entry.get("name_cn") or "").strip() or None


def ensure_names(
    items: Iterable,
    limit: int | None = None,
    workers: int = 4,
    progress: Callable[[int, int, Any], None] | None = None,
) -> dict:
    """批量补中文名。

    items：``(appid, name_en)`` 二元组序列，或 ``{appid: name_en}`` 映射。
    返回 ``{"total", "ok", "miss", "skipped", "results": {appid: name_cn}, "names": {...}}``。
    """
    pairs: list[tuple[int, str]] = []
    try:
        if isinstance(items, Mapping):
            iterator = items.items()
        else:
            iterator = items
        for item in iterator:
            try:
                appid, name_en = item
                pairs.append((int(appid), str(name_en or "")))
            except (TypeError, ValueError):
                continue
    except Exception as exc:
        return {"total": 0, "ok": 0, "miss": 0, "skipped": 0, "results": {}, "error": str(exc)}

    todo: list[tuple[int, str]] = []
    skipped = 0
    for appid, name_en in pairs:
        entry = _entry(appid)
        if _fresh(entry):
            skipped += 1
            continue
        if not name_en.strip():
            skipped += 1
            continue
        todo.append((appid, name_en))
    if limit is not None:
        todo = todo[: max(0, int(limit))]

    results: dict[int, str] = {}
    total = len(todo)
    done = 0

    def _work(appid: int, name_en: str):
        return appid, lookup(appid, name_en)

    if total:
        pool_size = max(1, min(int(workers) or 4, total))
        if pool_size == 1:
            for appid, name_en in todo:
                _, value = _work(appid, name_en)
                if value:
                    results[appid] = value
                done += 1
                _progress(progress, done, total, appid, value)
        else:
            with ThreadPoolExecutor(max_workers=pool_size) as pool:
                futures = [pool.submit(_work, appid, name_en) for appid, name_en in todo]
                for future in as_completed(futures):
                    try:
                        appid, value = future.result()
                    except Exception as exc:
                        logger.info("补名失败：%s", exc)
                        continue
                    if value:
                        results[appid] = value
                    done += 1
                    _progress(progress, done, total, appid, value)

    return {
        "total": total, "ok": len(results), "miss": total - len(results),
        "skipped": skipped, "results": results, "names": results,
    }


def _progress(progress, done: int, total: int, appid: int, value) -> None:
    if progress is None:
        return
    try:
        progress(done, total, {"appid": appid, "name_cn": value or ""})
    except Exception:
        pass


def stats() -> dict:
    items = _load()
    named = sum(1 for v in items.values() if str(v.get("name_cn") or "").strip())
    return {"count": len(items), "named": named, "cache": str(cache_path()), "dead": _HOST_DEAD}