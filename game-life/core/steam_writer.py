"""分类写回 sharedconfig.vdf（状态引擎层的写回模块）。

职责：
    1. 写前用 psutil 检测 Steam 进程，运行中则中止
    2. 合并现有分类（只追加、不覆盖，可重复运行）
    3. 备份原文件为 .bak，临时文件校验后原子替换（os.replace）

复用 core/vdf_io 的 read_existing_tags / build_sharedconfig 完成
读取与序列化，本模块只负责流程控制与安全写回。
"""

import os
import shutil
from pathlib import Path

import vdf

from core import vdf_io

# 状态分类名（内部键 + 展示名），remove_categories 仅删这些
STATUS_NAMES = {"未玩", "在玩", "搁置", "沉迷", "通关", "已通关"}

STEAM_PROCESS_NAMES = ["steam.exe"]


class SteamRunningError(RuntimeError):
    """Steam 正在运行时中止写回。"""


def is_steam_running(process_names: list = None) -> bool:
    """检测 Steam 客户端是否在运行（匹配进程名，不区分大小写）。"""
    try:
        import psutil
    except ImportError as e:
        raise RuntimeError(
            "未安装 psutil，无法检测 Steam 进程，已中止写回。"
            "请先执行: pip install psutil"
        ) from e

    names = {n.strip().lower() for n in (process_names or STEAM_PROCESS_NAMES)}
    for proc in psutil.process_iter(["name"]):
        try:
            if (proc.info.get("name") or "").strip().lower() in names:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def merge_tags(existing: set, new: list) -> list:
    """合并分类：保留现有分类，追加新分类，去重且保持顺序（纯函数）。"""
    merged = []
    seen = set()
    for tag in existing:
        if tag not in seen:
            seen.add(tag)
            merged.append(tag)
    for tag in new:
        if tag not in seen:
            seen.add(tag)
            merged.append(tag)
    return merged


def status_to_categories(classify_results: dict) -> dict:
    """把 classifier.classify_games 的输出转成 {appid: [状态分类]}。

    写回 Steam 的是展示名（rules.json 的 categories.status_names，
    如 通关 -> 已通关），与设计文档 3.5「仅状态」模式一致。
    旧数据缺少 status_name 时回退到内部状态键，保持容错；
    两者都在 STATUS_NAMES 中，remove_categories 均可识别删除。
    """
    return {
        appid: [info.get("status_name") or info["status"]]
        for appid, info in classify_results.items()
    }


def _sharedconfig_path(steam_path: str, account_id: str) -> Path:
    return Path(steam_path) / "userdata" / account_id / "7" / "remote" / "sharedconfig.vdf"


def write_categories(steam_path: str, account_id: str, categories: dict) -> str:
    """把状态分类写回 sharedconfig.vdf。

    steam_path : Steam 安装目录
    account_id : Steam 账号 id（userdata 下的目录名）
    categories : {appid: [分类名, ...]}，如 {730: ["在玩"]}

    流程：检测进程 -> 读取现有分类 -> 合并 -> 序列化 -> 备份 ->
          写临时文件 -> 校验 -> 原子替换。
    返回备份文件路径。
    """
    if is_steam_running():
        raise SteamRunningError(
            "检测到 Steam 正在运行。请先完全退出 Steam 再执行分类写回，"
            "否则 Steam 退出时会覆盖本地配置。"
        )

    shared_path = _sharedconfig_path(steam_path, account_id)
    if not shared_path.exists():
        raise FileNotFoundError(f"未找到 sharedconfig.vdf: {shared_path}")

    # 读取原文件结构，保留其他字段与未涉及的 appid
    try:
        with open(shared_path, "r", encoding="utf-8-sig") as f:
            data = vdf.load(f)
    except Exception as e:
        raise RuntimeError(f"解析 sharedconfig.vdf 失败: {e}") from e

    # 合并：只追加不覆盖，保证幂等
    existing = vdf_io.read_existing_tags(steam_path, account_id)
    games_tags = {}
    for appid, tags in categories.items():
        # 排序保证多次运行输出字节一致（现有分类来自 set，顺序不定）
        games_tags[appid] = sorted(merge_tags(existing.get(appid, set()), tags))

    content = vdf_io.build_sharedconfig(data, games_tags)

    # 备份
    backup_path = shared_path.with_suffix(".bak")
    shutil.copy2(shared_path, backup_path)

    # 原子写入：临时文件 -> 校验 -> 替换
    tmp_path = shared_path.with_suffix(".vdf.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8-sig")
        with open(tmp_path, "r", encoding="utf-8-sig") as f:
            check = vdf.load(f)
        if not _verify_tags(check, games_tags):
            raise RuntimeError("写入校验失败：临时文件分类与预期不一致")
        os.replace(tmp_path, shared_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    return str(backup_path)


def remove_categories(steam_path: str, account_id: str, appids: list) -> str:
    """删除指定 appid 的状态分类（仅删状态名，保留用户手动分类）。

    appids: 要清除状态分类的 appid 列表
    返回备份文件路径。
    """
    if is_steam_running():
        raise SteamRunningError(
            "检测到 Steam 正在运行。请先完全退出 Steam 再执行取消分类。"
        )

    shared_path = _sharedconfig_path(steam_path, account_id)
    if not shared_path.exists():
        raise FileNotFoundError(f"未找到 sharedconfig.vdf: {shared_path}")

    try:
        with open(shared_path, "r", encoding="utf-8-sig") as f:
            data = vdf.load(f)
    except Exception as e:
        raise RuntimeError(f"解析 sharedconfig.vdf 失败: {e}") from e

    existing = vdf_io.read_existing_tags(steam_path, account_id)
    games_tags = {}
    for appid in appids:
        tags = existing.get(appid, set())
        filtered = {t for t in tags if t not in STATUS_NAMES}
        games_tags[appid] = sorted(filtered)

    content = vdf_io.build_sharedconfig(data, games_tags)

    backup_path = shared_path.with_suffix(".bak")
    shutil.copy2(shared_path, backup_path)

    tmp_path = shared_path.with_suffix(".vdf.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8-sig")
        with open(tmp_path, "r", encoding="utf-8-sig") as f:
            check = vdf.load(f)
        if not _verify_tags(check, games_tags):
            raise RuntimeError("写入校验失败：临时文件分类与预期不一致")
        os.replace(tmp_path, shared_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    return str(backup_path)


def classify_and_write(steam_path: str, account_id: str,
                       play_stats: dict, games: dict,
                       mode: str = "status", hltb_data: dict = None) -> dict:
    """一键分类并写回 sharedconfig.vdf。

    状态判定只基于 play_stats（时长/最近游玩），games 仅作可选元数据（名称来源）；
    跑 classifier.classify_games → 合并写回，返回 {written, appids, backup}。
    """
    if mode != "status":
        raise ValueError(f"不支持的 mode: {mode}")
    from engine import classifier as _c

    classified = _c.classify_games(play_stats, hltb_data=hltb_data)
    categories = status_to_categories(classified)
    backup = write_categories(steam_path, account_id, categories)
    return {"written": len(categories), "appids": list(categories.keys()),
            "backup": backup}


def classify_and_remove(steam_path: str, account_id: str,
                        appids: list) -> str:
    """一键取消分类：删除指定 appid 的状态分类。

    返回备份文件路径。
    """
    return remove_categories(steam_path, account_id, appids)


def _verify_tags(data, games_tags: dict) -> bool:
    """校验临时文件解析结果中的 tags 是否与预期一致。"""
    if not data:
        return False
    steam = (data.get("UserRoamingConfigStore", {})
             .get("Software", {})
             .get("Valve", {})
             .get("Steam", {}))
    apps = steam.get("Apps", {})
    for appid, expected in games_tags.items():
        tags = apps.get(str(appid), {}).get("tags", {})
        actual = sorted(tags.values())
        if actual != sorted(expected):
            return False
    return True
