"""游戏人生 GameLife —— 应用入口（CLI）

argparse 子命令路由。顶层只依赖标准库；core/engine/render 各层模块一律在
命令处理函数内延迟 import，保证缺依赖或模块未完成时 CLI 仍可用、不中断。

接入约定（供各层 Agent 对接）：
    占位命令对应的目标模块完成后，只需在模块内提供 ``run(args) -> int``
    入口，main.py 会自动调用并透传退出码，无需改动本文件。

命令与目标模块对应关系：
    setup      core.locator + core.vdf_io      （已对接，首期打通）
    run        已对接：engine.classifier + core.snapshot（存快照 + 本地导出分类，不写回 Steam）
    stats      已对接：engine.classifier + engine.stats（可选 core.store_api）
    recommend  已对接：engine.recommender（注入 play_stats/games/statuses）
    persona    已对接：engine.persona（注入 play_stats/games/statuses）
    report     已对接：render.annual_report（--demo/--data/--output；--account-id 读真实库）
    graveyard  已对接：render.graveyard（--demo/--data/--output；--account-id 读真实库）
    price      engine.value_ranking           （分析引擎）
    family     engine.series_tree             （分析引擎）
    radar      engine.achievement_radar       （分析引擎）
    wishlist   engine.wishlist_guard          （分析引擎）
    challenge  engine.challenge               （分析引擎）
    today      render.timemachine             （可视化层）
    report     render.annual_report           （可视化层）
    starmap    render.starmap                 （可视化层）
    graveyard  render.graveyard               （可视化层）
    timeline   render.archaeologist           （可视化层）
    checkup    render.checkup                 （可视化层）
    persona    engine.persona                 （分析引擎）
    config     已对接：main.py 内读写 rules.json（骨架）
"""

import argparse
import importlib
import io
import json
import logging
import os
import shutil
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Callable

APP_ROOT = Path(__file__).resolve().parent


def _stable_app_dir() -> Path:
    """运行期读写所在的 game-life 根目录（打包态 = exe 附近的真实目录）。

    源码运行返回 ``game-life/``（行为与以往完全一致）；onefile 打包后
    ``__file__`` 指向进程退出即删的 ``sys._MEIPASS``，若不改写，logs/、
    写回状态、导出产物等运行期读写都会落进临时目录并随进程消失。
    """
    if not getattr(sys, "frozen", False):
        return APP_ROOT
    exe_dir = Path(sys.executable).resolve().parent
    for candidate in (exe_dir / "game-life", exe_dir.parent / "game-life", exe_dir):
        if candidate.is_dir():
            return candidate
    return exe_dir


def _stable_data_dir() -> Path:
    """运行期读写数据的真实 data 目录（打包态 = exe 附近的 game-life/data）。

    统一走 :func:`data.schema.data_dir`（已按 frozen 解析到 exe 附近的真实
    data 目录），与 core/engine 各模块保持单一口径。
    """
    try:
        from data import schema
        return Path(schema.data_dir())
    except Exception:  # 兜底：退回源码布局，不制造 import 期副作用
        return _stable_app_dir() / "data"


LOGS_DIR = _stable_app_dir() / "logs"
VERSION = "1.0.0"

# 无论从哪个目录启动，都保证能 import 到 game-life 下的包
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

logger = logging.getLogger("game-life")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2


_CONSOLE_ENCODING_GUARDED = False


def _rewrap_with_replace(stream):
    """给「没有 reconfigure 的文本流」重包一层：沿用原编码，编不出的字符替换掉。

    仅在流既没有 ``reconfigure``、又有底层 ``buffer`` 时使用（CPython 常规的
    stdout/stderr 都走 ``reconfigure``，这里只兜住被包装过的少数情形）。
    任何失败都返回 None，由调用方保持原样，绝不因此抛异常。
    """
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        return None
    try:
        return io.TextIOWrapper(
            buffer,
            encoding=getattr(stream, "encoding", None) or "utf-8",
            errors="replace",
            newline=None,
            line_buffering=bool(getattr(stream, "line_buffering", False)),
            write_through=True,
        )
    except Exception:
        return None


def _guard_stream(stream):
    """让单个标准流「编不出的字符降级为占位符」，返回应替换到 sys 上的流。

    原则（任务书 F6）：**保持控制台原有编码**，只把 errors 从 strict 降级为
    replace。绝不改成 ``encoding="utf-8"``——那会把中文 Windows 控制台变乱码。
    流没有 ``reconfigure``（被重定向 / 被替换成 StringIO / 老环境）时退回包一层；
    再不行就原样返回，绝不抛异常。
    """
    if stream is None:  # windowed 解释器可能根本没有标准流
        return stream
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(errors="replace")
            return stream
        except Exception:
            pass
    return _rewrap_with_replace(stream) or stream


def _make_console_encoding_safe() -> None:
    """进程级兜底：把 sys.stdout / sys.stderr 配成「编不出就替换」。

    必须在**任何输出之前**调用（``main()`` 第一行），并覆盖
    ``if __name__ == "__main__"`` 这条路径。幂等，可重复调用。

    目的：游戏名这类**外部数据**里随时会出现 GBK 编不出的字符（® / ™ / emoji /
    生僻字），让命令带着 ``UnicodeEncodeError`` 崩溃、输出截断。这里统一改成
    「替换后继续打印」，命令永不因字符编不出而失败。
    """
    global _CONSOLE_ENCODING_GUARDED
    if _CONSOLE_ENCODING_GUARDED:
        return
    _CONSOLE_ENCODING_GUARDED = True
    for name in ("stdout", "stderr"):
        try:
            setattr(sys, name, _guard_stream(getattr(sys, name, None)))
        except Exception:  # 兜底本身失败也绝不中断
            pass


def setup_logging() -> None:
    """日志规范：INFO 写入 logs/game-life.log，控制台仅显示 WARNING 以上。"""
    LOGS_DIR.mkdir(exist_ok=True)
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.FileHandler(LOGS_DIR / "game-life.log", encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)
    # 走已被 _make_console_encoding_safe() 保护过的 sys.stderr（编不出就替换），
    # 避免日志里的游戏名/异常文本在 GBK 控制台上再次触发 UnicodeEncodeError。
    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(console_handler)


def cmd_setup(args: argparse.Namespace) -> int:
    """首次配置：定位 Steam → 选择账号 → 打印游戏与时长（已对接 core）。"""
    print("== 首次配置：定位 Steam ==")
    try:
        from core import locator
    except ImportError as exc:
        print(f"[setup] 无法加载 core.locator：{exc}")
        return EXIT_ERROR

    steam_path = args.steam_path
    if not steam_path:
        try:
            steam_path = locator.find_steam_path()
        except FileNotFoundError as exc:
            print(f"[setup] 错误：{exc}")
            print('提示：可通过 --steam-path "D:\\Steam" 手动指定安装目录')
            return EXIT_ERROR
        print(f"已定位 Steam：{steam_path}")
    else:
        steam_path = str(Path(steam_path))

    try:
        accounts = locator.list_user_accounts(steam_path)
        account = locator.select_account(accounts)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[setup] 错误：{exc}")
        logger.warning("定位账号失败：%s", exc)
        return EXIT_ERROR
    except EOFError:
        print("[setup] 当前不在交互终端，无法交互选账号。")
        print("提示：请改用  main.py stats --account-id <ID>")
        logger.warning("账号选择需要交互输入，当前 stdin 不可用")
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\n[setup] 已取消")
        return 130

    print(f"已选择账号：{account['name']} ({account['id']})")

    try:
        from core import vdf_io
    except ImportError as exc:
        print(f"[setup] 读取游玩数据需要 vdf 库，当前不可用：{exc}")
        print("提示：请先执行  pip install -r requirements.txt")
        return EXIT_ERROR

    try:
        playtime_data = vdf_io.read_playtime_data(steam_path, account["id"])
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[setup] 读取游玩数据失败：{exc}")
        logger.exception("读取 localconfig.vdf 失败")
        return EXIT_ERROR

    games = sorted(playtime_data.items(), key=lambda kv: kv[1]["playtime"], reverse=True)
    if not games:
        print("未在该账号的 localconfig.vdf 中读到游戏记录。")
        return EXIT_OK

    print(f"\n共读取 {len(games)} 款游戏：")
    print(f"{'APPID':<10} {'时长':>9} {'最后游玩':>12}")
    total_minutes = 0
    for appid, info in games:
        minutes = info["playtime"]
        total_minutes += minutes
        hours = minutes / 60.0
        last_text = "从未"
        if info["last_played"]:
            last_text = datetime.fromtimestamp(info["last_played"]).strftime("%Y-%m-%d")
        print(f"{appid:<10} {hours:>7.1f}h {last_text:>12}")
    print(f"总计：{len(games)} 款游戏，{total_minutes / 60.0:.1f} 小时")
    return EXIT_OK


def _resolve_steam_account(args: argparse.Namespace):
    """解析 Steam 目录与账号 id；多账号时提示用 --account-id 指定。

    返回 (steam_path, account_id)，失败时打印友好提示并返回 None。
    """
    try:
        from core import locator
    except ImportError as exc:
        print(f"无法加载 core.locator：{exc}")
        return None

    steam_path = getattr(args, "steam_path", None)
    if not steam_path:
        try:
            steam_path = locator.find_steam_path()
        except FileNotFoundError as exc:
            print(f"错误：{exc}")
            print('提示：可通过 --steam-path "D:\\Steam" 手动指定安装目录')
            return None
    else:
        steam_path = str(Path(steam_path))

    account_id = getattr(args, "account_id", None)
    if account_id:
        text = str(account_id)
        # 合流库（--account-id all / merged / 合流）：把「已添加」的账号合并成一个账号统计
        if _is_merge_account(text):
            try:
                text = _merge_module().MERGE_ID
            except Exception as exc:  # 模块不可用就按原样当账号处理，别拦住命令
                logger.debug("合流 id 归一化失败（按原样处理）：%s", exc)
        return steam_path, text

    try:
        accounts = locator.list_user_accounts(steam_path)
    except FileNotFoundError as exc:
        print(f"错误：{exc}")
        return None
    if len(accounts) == 1:
        return steam_path, accounts[0]["id"]
    if not accounts:
        print("错误：未找到任何 Steam 账号")
        return None
    print("检测到多个 Steam 账号，请用 --account-id 指定其一：")
    for acc in accounts[:8]:
        print(f"  {acc['id']}  当前显示名：{acc['name']}")
    if len(accounts) > 8:
        print(f"  ... 其余 {len(accounts) - 8} 个")
    print("  合流库         也可用 --account-id all：把已添加的账号合并统计")
    return None


def _read_playtime(steam_path: str, account_id: str):
    """读取账号游玩数据（缺 vdf 依赖时友好提示）。"""
    try:
        from core import vdf_io
    except ImportError as exc:
        print(f"读取游玩数据需要 vdf 库，当前不可用：{exc}")
        print("提示：请先执行  pip install -r requirements.txt")
        return None
    try:
        return vdf_io.read_playtime_data(steam_path, account_id)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"读取游玩数据失败：{exc}")
        return None


def _fetch_metadata_optional(appids: list) -> dict:
    """可选抓取 Store 元数据；缺依赖/网络失败时降级为空 dict。"""
    try:
        from core import store_api
    except ImportError as exc:
        print(f"跳过元数据抓取（缺少 requests 依赖或 store_api 不可用）：{exc}")
        return {}
    try:
        return store_api.fetch_metadata(appids)
    except Exception as exc:
        print(f"元数据抓取失败，将只显示 appid：{exc}")
        return {}


def _fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "从未"


def _appid_in(appid_set: set, key) -> bool:
    """键（int / str / 其他）是否命中 appid 集合；非数字键一律不命中。"""
    try:
        return int(key) in appid_set
    except (TypeError, ValueError):
        return False


def _filter_by_ownership(steam_path: str, account_id: str, play_stats: dict, games_meta: dict):
    """按「授权归属」剔掉**家庭共享且本账号从未玩过**的游戏（**全产品唯一口径**，见 core.ownership）。

    家庭库会把别人拥有的游戏也写进本账号的 localconfig.vdf，导致同一个游戏在多个账号
    下都出现、时长各算一遍。这里在装载入口过滤一次，play_stats 与 games_meta 一起收缩
    （不留孤儿 meta），下游分类/统计/渲染/推荐自动同口径。

    A4 起「共享但本账号玩过」（``playtime > 0``，与 engine 的「已玩」同口径）**保留**，
    与自有游戏同权参与所有统计；只有「共享且从未玩过」才剔除。

    T1 起再并上「Steam 残留痕迹」：既没有授权票、本机也没人拥有、又从未玩过，却**有明确
    售价**（实测 ``1091500 Cyberpunk 2077`` / ``1448440 Wo Long``）——它们只是 vdf 的
    ``Apps`` 段里一条启动记录，用户原话是「我既没有玩过，我的家庭库里好像也没有」。
    免费游戏（无票属正常）与**查不到价格**的条目一律保留，绝不误杀。

    返回 (filtered_play_stats, filtered_games_meta, ownership)。降级时原样返回全部
    （读不到 vdf 绝不把用户的库清空）。过滤**只影响展示与统计**，不影响写回范围
    （写回本来就只针对播放过的条目）。
    """
    empty = {"owned": 0, "played_shared": 0, "shared": 0, "free": 0, "trace": 0,
             "total": len(play_stats or {}),
             "played_shared_appids": [], "shared_appids": [], "trace_appids": [],
             "degraded": True}
    try:
        from core import ownership
    except ImportError as exc:                   # 缺模块时降级为不过滤
        print(f"授权归属模块不可用，本次不做家庭共享过滤：{exc}")
        return play_stats, games_meta, empty

    try:
        summary = ownership.summarize(steam_path, account_id, play_stats or {})
        # 「共享且未玩过」与「Steam 残留痕迹」都要剔除 —— 两者都不属于这个账号的库。
        drop = set(summary.get("shared_appids") or []) | set(summary.get("trace_appids") or [])
    except Exception as exc:                     # 绝不因过滤失败而中断主流程
        logger.warning("授权归属过滤失败，已降级为全部保留：%s", exc)
        return play_stats, games_meta, empty

    if not drop:
        return play_stats, games_meta, summary

    # 被剔除条目（= 共享且从未玩过 + Steam 残留痕迹）的原始记录也带上：面板「显示家庭共享」
    # 勾选框要灰显它们，但**绝不参与任何统计**（下游只用它渲染，不进 play_stats / games_meta）。
    # 「共享但玩过」的那些不在这里 —— 它们留在 play_stats 里正常参与统计。
    summary["shared_stats"] = {
        appid: dict(play_stats[appid])
        for appid in drop
        if isinstance(play_stats.get(appid), dict)
    }

    filtered_stats = {
        key: value for key, value in (play_stats or {}).items()
        if not _appid_in(drop, key)
    }
    # games_meta 跟着收缩，别留孤儿条目（键可能是 int 也可能是 str）
    filtered_meta = {
        key: value for key, value in (games_meta or {}).items()
        if not _appid_in(drop, key)
    }
    return filtered_stats, filtered_meta, summary


def _augment_library(play_stats: dict, steam_path: str, account_id: str) -> dict:
    """把「在库里、是免费游戏、却因为从未启动而没有 vdf 条目」的补进库（M2f / T16）。

    游戏明细建在 ``localconfig.vdf`` 的 ``Apps`` 段上，而那段**只记录 Steam 写过游玩
    记录的条目** —— 免费游戏没有授权票，Steam 也只在**真正启动过**之后才写 Apps 条目，
    于是「加进库、还没点过开始」的免费游戏整条不在明细里（用户反馈「部分免费游戏都不
    在游戏明细中」）。判据与实现在 :func:`core.ownership.augment_library`，**只补免费
    游戏**、只读本地、读不到就原样返回（降级：宁可少补，绝不凭空造条目）。
    """
    try:
        from core import ownership
    except ImportError as exc:                   # 缺模块：不补，绝不因此中断
        logger.debug("授权归属模块不可用，跳过「在库免费游戏」补充：%s", exc)
        return play_stats
    try:
        return ownership.augment_library(play_stats, steam_path, account_id)
    except Exception as exc:                     # 补充失败绝不影响主流程
        logger.warning("补充「在库免费游戏」失败（已忽略）：%s", exc)
        return play_stats


def _describe_ownership(ownership: dict) -> str:
    """CLI 提示行：让用户看得见剔了什么（**只做说明**，不改任何统计数字）。

    合流库（``ownership["merged"]`` 为真）返回空串：它的说明由
    :func:`_load_merged_library` 在装载时**已经打印过一次**（那样每个命令都能看到），
    这里再打印就重复了，而且「本账号自己拥有 N 款」对合流库本来是错的说法。
    """
    if not ownership:
        return ""
    if ownership.get("merged"):
        return ""
    if ownership.get("degraded"):
        return "授权归属：未能读取授权票，本次未剔除任何游戏（全部按自己拥有计入）。"
    text = f"已按授权归属过滤：本账号自己拥有 {ownership.get('owned', 0)} 款"
    played_shared = ownership.get("played_shared", 0) or 0
    if played_shared:
        text += f"；另有 {played_shared} 款来自家庭共享但本账号玩过，已保留"
    shared = ownership.get("shared", 0) or 0
    if shared:
        # 共享且从未玩过 = 0 时**不打印**「还有 0 款」，避免无意义的半句
        text += f"；还有 {shared} 款家庭共享且未玩过，未计入本次统计"
    trace = ownership.get("trace", 0) or 0
    if trace:
        # Steam 残留痕迹（没票 / 没人拥有 / 没玩过 / 却有明确售价）也剔除了，同样要看得见
        text += f"；另有 {trace} 款是 Steam 残留痕迹，未计入"
    return text + "。"


def _merge_module():
    """延迟 import core.merge（main.py 顶层只依赖标准库，见模块 docstring）。"""
    from core import merge

    return merge


def _is_merge_account(account_id) -> bool:
    """该 account_id 是否表示「合流库」；合流模块不可用时按普通账号处理。"""
    try:
        return _merge_module().is_merge_id(account_id)
    except Exception as exc:  # 缺模块/坏环境都不该让 CLI 崩
        logger.debug("合流模块不可用，按普通账号处理：%s", exc)
        return False


def _finish_library(play_stats, games_meta, steam_path, account_id, ownership):
    """装载收尾（单账号与合流库共用）：本地元数据兜底 → 状态判定。

    返回 (play_stats, games_meta, statuses, classified, steam_path, account_id, ownership)。
    """
    # 本地元数据兜底（离线、只读、不联网、不写库）：把 data/cache.db 里缓存的
    # 游戏名 / 类型补进 games_meta。历史上只有渲染路径与 value_ranking 做了这一步，
    # 于是其他 CLI 命令（recommend / persona / stats / family / wishlist / radar /
    # backfill）在 with_metadata=False 时拿着空 games_meta 一路走到底，把游戏名
    # 退化成 "appid N"、类型退化成「各类类型」。--with-metadata 时网络抓取的字段
    # 优先，缓存只补缺失字段（fill_metadata 的既定语义），返回值严格限定在
    # play_stats 范围内（绝不把缓存里多出的 appid 并进库）。
    try:
        from core import render_bridge

        games_meta = render_bridge.fill_metadata(games_meta, play_stats)
    except Exception as exc:
        logger.debug("本地元数据兜底不可用（忽略，继续用原 games_meta）：%s", exc)

    classified = None
    statuses = {}
    try:
        from engine import classifier
        classified = classifier.classify_games(play_stats)
        to_statuses = getattr(classifier, "to_statuses", None)
        if callable(to_statuses):
            statuses = to_statuses(classified)
        else:  # 兼容旧版 classifier：退化为手工提取状态键
            statuses = {appid: info["status"] for appid, info in classified.items()}
    except ImportError as exc:
        print(f"状态引擎不可用，将使用简化兜底判定：{exc}")
    return play_stats, games_meta, statuses, classified, steam_path, account_id, ownership


def _load_single_library(steam_path, account_id, with_metadata):
    """只装一个账号的库：游玩数据 → 授权归属过滤 →（可选元数据）→ 状态判定。"""
    play_stats = _read_playtime(steam_path, account_id)
    if play_stats is None:
        return None

    # M2f：先补上「在库但从未启动」的免费游戏（它们没有票、Apps 段也没有条目，
    # 不补就整条不在明细里），再做归属过滤 —— 这样归属计数把它们的 free 一起算进去。
    play_stats = _augment_library(play_stats, steam_path, account_id)

    # 授权归属过滤必须在元数据抓取**之前**：家庭共享来的游戏连元数据都不必再抓。
    # ownership 只认这一次的结果（在**原始**库上算的），后面那次是为了收缩 games_meta。
    play_stats, _meta_before, ownership = _filter_by_ownership(steam_path, account_id, play_stats, {})

    games_meta = _fetch_metadata_optional(list(play_stats.keys())) if with_metadata else {}
    # games_meta 跟着收缩（键可能是 int 也可能是 str），绝不留孤儿 meta
    _stats_after, games_meta, _ = _filter_by_ownership(steam_path, account_id, play_stats, games_meta)
    # 不静默：过滤说明进日志（排查用）；面向用户的提示行由各命令自己打印
    logger.info("[%s/%s] %s", steam_path, account_id, _describe_ownership(ownership))
    return _finish_library(play_stats, games_meta, steam_path, account_id, ownership)


def _load_merged_library(steam_path, with_metadata):
    """合流库：把「已添加」的账号各装一遍（各自过滤授权归属），再合并成一份库。

    合并规则见 ``core/merge.py``：时长**相加**、last_played 取最大、归属**先各自
    过滤再合并**。单个成员读不到就跳过并提示，绝不因为一个账号坏掉整个合流库。
    """
    merge = _merge_module()
    members = merge.resolve_members(steam_path)
    if not members:
        print("错误：合流库至少要有一个账号（先在面板添加，或改用 --account-id <ID>）")
        return None

    parts: list[dict] = []
    skipped: list[str] = []
    for member in members:
        single = _load_single_library(steam_path, member["id"], with_metadata)
        if single is None:
            skipped.append(str(member["id"]))
            continue
        parts.append({
            "id": member["id"],
            "name": member["name"],
            "play_stats": single[0],
            "games_meta": single[1],
            "ownership": single[6],
        })
    if not parts:
        print("错误：合流库的成员账号都没有读到游玩数据")
        return None
    if skipped:
        print(f"提示：账号 {'、'.join(skipped)} 读取失败，已跳过（仍按其余 {len(parts)} 个合流）")

    merged = merge.merge_library_parts(parts)
    line = merge.summary_line(merged["ownership"])
    print(line)
    logger.info("[%s/%s] %s", steam_path, merge.MERGE_ID, line)
    return _finish_library(
        merged["play_stats"], merged["games_meta"], steam_path, merge.MERGE_ID, merged["ownership"]
    )


def _load_library(args: argparse.Namespace, with_metadata: bool):
    """统一数据加载：游玩数据 → 授权归属过滤 →（可选元数据）→ 状态判定。

    返回 (play_stats, games_meta, statuses, classified, steam_path, account_id, ownership)，
    失败时返回 None（已打印提示）。
    classified 为 classifier.classify_games 的原始输出；classifier 不可用时为 None。

    ``--account-id all``（别名 ``merged`` / ``合流``）走**合流库**：把面板里已添加的
    账号当作同一个账号统一统计（实现在 ``core/merge.py``）。
    """
    resolved = _resolve_steam_account(args)
    if not resolved:
        return None
    steam_path, account_id = resolved
    if _is_merge_account(account_id):
        return _load_merged_library(steam_path, with_metadata)
    return _load_single_library(steam_path, account_id, with_metadata)


def _print_stats_table(s: dict, show_meta: bool = False) -> None:
    print("\n== 统计看板 ==")
    print(
        f"总游戏数 {s['total_games']}    总时长 {s['total_playtime_hours']}h    "
        f"平均每款 {s['avg_playtime_hours']}h"
    )
    print(
        f"吃灰率 {s['dust_rate']}%    已玩占比 {s['played_ratio']}%    "
        f"已玩 {s.get('played_count', 0)} / 未玩 {s.get('unplayed_count', 0)} 款"
    )

    print("\n状态分布：")
    for status, count in s["status_counts"].items():
        print(f"  {status:<4} {count:>5}  ({s['status_percent'].get(status, 0):.1f}%)")

    print("\n时长分布：")
    for row in s["playtime_distribution"]:
        print(f"  {row['label']:<12} {row['count']:>5}  ({row['percent']:.1f}%)")

    if show_meta and s.get("top_genres"):
        total = s["total_games"] or 1
        print("\n类型分布 TOP：")
        for row in s["top_genres"][:5]:
            print(f"  {row['name']:<12} {row['count']:>5}  ({row['count'] / total * 100:.1f}%)")

    print("\n最久未玩 TOP：")
    for g in s.get("most_forgotten", [])[:5]:
        print(f"  {g['name']:<32} {g['playtime_hours']:>7}h  {_fmt_ts(g['last_played'])}")

    print("\n最近游玩 TOP：")
    for g in s.get("recent_played", [])[:5]:
        print(f"  {g['name']:<32} {g['playtime_hours']:>7}h  {_fmt_ts(g['last_played'])}")


def cmd_stats(args: argparse.Namespace) -> int:
    """统计看板：游玩数据 →（可选元数据）→ 状态判定 → engine.stats 计算 → 表格输出。"""
    loaded = _load_library(args, with_metadata=args.with_metadata)
    if loaded is None:
        return EXIT_ERROR
    play_stats, games_meta, statuses, _, _, _, _ownership = loaded

    try:
        from engine.stats import compute_stats
    except ImportError as exc:
        print(f"统计模块不可用：{exc}")
        return EXIT_ERROR

    _print_stats_table(compute_stats(play_stats, games_meta, statuses=statuses), show_meta=bool(games_meta))
    # 让用户看得见剔了什么（只做说明，不改上面任何统计数字）
    ownership_line = _describe_ownership(_ownership)
    if ownership_line:
        print("\n" + ownership_line)
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    """扫描 + 状态判定 +（按钮开启时）写回 Steam + 存快照 + 本地导出分类。

    --dry-run：仅预览状态分类，不存快照、不导出文件。
    写回由「按钮」控制：python game-life/main.py writeback
    """
    loaded = _load_library(args, with_metadata=args.with_metadata)
    if loaded is None:
        return EXIT_ERROR
    play_stats, games_meta, _, classified, steam_path, account_id, _ownership = loaded

    if classified is None:
        print("状态引擎不可用，无法完成分类，已中止")
        return EXIT_ERROR

    counter = Counter(info["status"] for info in classified.values())
    total_minutes = sum(int((play_stats.get(appid) or {}).get("playtime", 0)) for appid in classified)
    print("\n== 状态判定结果 ==")
    print("  " + "  ".join(f"{status}={n}" for status, n in counter.most_common()))
    print(f"  总时长：{total_minutes / 60.0:.1f} 小时（{len(classified)} 款游戏）")

    if args.dry_run:
        print("\n(dry-run) 仅预览，不存快照、不导出文件：")
        for appid, info in sorted(classified.items())[:10]:
            print(f"  {appid:<10} {info['status_name']}")
        if len(classified) > 10:
            print(f"  ... 共 {len(classified)} 款游戏")
        return EXIT_OK

    if _is_merge_account(account_id):
        # 合流库不是账号：不写回 Steam 分类、不写历史快照（两者都是按账号记录的），
        # 只做本地导出，绝不替用户猜「代表哪个账号」。
        print("\n(合流库) 跳过写回与快照，只导出本地分类文件。")
        export_path = _export_classification(
            classified, play_stats, games_meta, filename="classification_merged.json"
        )
        if export_path is None:
            return EXIT_ERROR
        print(f"分类已本地导出：{export_path}")
        return EXIT_OK

    # 0. 写回 Steam（按钮开启时）
    writeback_on = _load_writeback_state()
    if writeback_on and classified:
        _steam_writeback(steam_path, account_id, classified)
    else:
        print("\n(未开启写回) 跳过 Steam 写回；开启：main.py writeback")

    # 1. 保存当日快照（供时光机/考古/挑战使用）
    snapshot_games = {
        appid: {
            "playtime": int((play_stats.get(appid) or {}).get("playtime", 0)),
            "last_played": int((play_stats.get(appid) or {}).get("last_played", 0)),
            "status": info["status"],
        }
        for appid, info in classified.items()
    }
    try:
        from core import snapshot
        snapshot.save(snapshot_games)
    except Exception as exc:
        print(f"保存快照失败：{exc}")
        return EXIT_ERROR
    print(f"\n快照已保存：{len(snapshot_games)} 条（{date.today().isoformat()}）")

    # 2. 本地导出分类（game-life/output/，绝不触碰 Steam 目录）
    export_path = _export_classification(classified, play_stats, games_meta)
    if export_path is None:
        return EXIT_ERROR
    print(f"分类已本地导出：{export_path}")
    return EXIT_OK


def _export_classification(classified: dict, play_stats: dict, games_meta: dict,
                           filename: str = "classification.json"):
    """把状态分类导出为 game-life/output/<filename>（含 appid/name/status 等）。"""
    output_dir = _stable_output_dir() or (Path(__file__).resolve().parent / "output")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for appid in sorted(classified):
            info = classified[appid]
            raw = play_stats.get(appid) or {}
            meta = games_meta.get(appid) or {}
            entries.append(
                {
                    "appid": appid,
                    "name": meta.get("name") or f"appid {appid}",
                    "status": info["status"],
                    "status_name": info["status_name"],
                    "playtime": int(raw.get("playtime", 0)),
                    "last_played": int(raw.get("last_played", 0)),
                }
            )
        counter = Counter(entry["status"] for entry in entries)
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total_games": len(entries),
            "total_playtime_hours": round(sum(entry["playtime"] for entry in entries) / 60.0, 2),
            "status_counts": dict(counter),
            "games": entries,
        }
        export_path = output_dir / filename
        export_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return export_path
    except OSError as exc:
        print(f"导出分类失败：{exc}")
        return None

RULES_PATH = Path(__file__).resolve().parent / "rules.json"


def _config_show(path: Path) -> int:
    if not path.exists():
        print(f"rules.json 不存在：{path}")
        print("当前使用内置默认规则；可用如下命令创建：")
        print("  python game-life/main.py config --set playtime.recent_days 30")
        return EXIT_OK
    try:
        print(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"读取 rules.json 失败：{exc}")
        return EXIT_ERROR
    return EXIT_OK


def _config_get(path: Path, dotted_key: str) -> int:
    try:
        from engine import classifier
        rules = classifier.load_rules()  # 与默认规则合并后的完整规则
    except ImportError as exc:
        print(f"无法加载规则引擎：{exc}")
        return EXIT_ERROR
    node = rules
    for part in dotted_key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            print(f"未找到规则键：{dotted_key}")
            return EXIT_ERROR
    source = "rules.json" if path.exists() else "默认规则（rules.json 不存在）"
    print(f"{dotted_key} = {json.dumps(node, ensure_ascii=False)}  [{source}]")
    return EXIT_OK


def _config_set(path: Path, dotted_key: str, raw_value: str) -> int:
    try:
        value = json.loads(raw_value)  # 先校验 VALUE 必须是合法 JSON
    except json.JSONDecodeError as exc:
        print(f"--set 的值不是合法 JSON：{exc}")
        print("示例：--set playtime.recent_days 30    --set status_names.通关 \"已通关\"")
        return EXIT_ERROR

    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"rules.json 解析失败，未修改：{exc}")
            return EXIT_ERROR
        if not isinstance(data, dict):
            print("rules.json 顶层不是 JSON 对象，未修改")
            return EXIT_ERROR
    else:
        data = {}

    node = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value

    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        print(f"写入 rules.json 失败：{exc}")
        return EXIT_ERROR
    print(f"已更新 {dotted_key} = {json.dumps(value, ensure_ascii=False)}")
    print(f"rules.json：{path}")
    return EXIT_OK


def cmd_config(args: argparse.Namespace) -> int:
    """查看/修改规则配置（rules.json）。"""
    if args.get_path and args.set_path:
        print("错误：--get 与 --set 不能同时使用")
        return EXIT_ERROR
    if args.set_path:
        return _config_set(RULES_PATH, args.set_path[0], args.set_path[1])
    if args.get_path:
        return _config_get(RULES_PATH, args.get_path)
    return _config_show(RULES_PATH)


def cmd_recommend(args: argparse.Namespace) -> int:
    """智能推荐：加载库数据并注入 play_stats/games/statuses 后调用 engine.recommender。"""
    loaded = _load_library(args, with_metadata=args.with_metadata)
    if loaded is None:
        return EXIT_ERROR
    play_stats, games_meta, statuses, _, _, _, _ownership = loaded
    try:
        from engine.recommender import run as recommender_run
    except ImportError as exc:
        print(f"推荐引擎不可用：{exc}")
        return EXIT_ERROR
    args.play_stats = play_stats
    args.games = games_meta
    args.statuses = statuses
    return recommender_run(args)


def cmd_persona(args: argparse.Namespace) -> int:
    """游戏人格测试：加载库数据并注入 play_stats/games/statuses 后调用 engine.persona。"""
    loaded = _load_library(args, with_metadata=args.with_metadata)
    if loaded is None:
        return EXIT_ERROR
    play_stats, games_meta, statuses, _, _, _, _ownership = loaded
    try:
        from engine.persona import run as persona_run
    except ImportError as exc:
        print(f"人格测试模块不可用：{exc}")
        return EXIT_ERROR
    args.play_stats = play_stats
    args.games = games_meta
    args.statuses = statuses
    return persona_run(args)

_RENDER_MODULES = {
    "report": "render.annual_report",
    "graveyard": "render.graveyard",
    "starmap": "render.starmap",
    "timeline": "render.archaeologist",
    "today": "render.timemachine",
    "checkup": "render.checkup",
}

# 渲染命令 -> 模块内「payload -> HTML 路径」生成函数名（_live_render 用）
_RENDER_GENERATORS = {
    "report": "generate_report",
    "graveyard": "generate_graveyard",
    "starmap": "generate_starmap",
    "timeline": "generate_timeline",
    "today": "generate_timemachine",
    "checkup": "generate_checkup",
}


def _stable_output_dir() -> "Path | None":
    """打包（onefile）运行时，渲染产物默认写到 exe 附近的稳定目录。

    源码运行时返回 None，各渲染模块沿用原有 ``game-life/output/`` 默认行为；
    打包后 ``__file__`` 指向临时解包目录（进程退出即删除），若不改写会把页面
    写进 ``_MEIxxxx/output/`` 并随进程消失——这里改写到仓库 ``game-life/output``。
    """
    if not getattr(sys, "frozen", False):
        return None
    exe_dir = Path(sys.executable).resolve().parent
    for candidate in (exe_dir / "game-life", exe_dir.parent / "game-life", exe_dir):
        if candidate.is_dir():
            return candidate / "output"
    return exe_dir / "output"


def _render_live_mode(args) -> bool:
    """是否走真实库数据：显式 --live，或提供了 --account-id / --steam-path。"""
    return bool(
        getattr(args, "live", False)
        or getattr(args, "account_id", None)
        or getattr(args, "steam_path", None)
    )


def _account_persona_name(steam_path, account_id) -> str:
    """尽力取账号显示名；失败时退回账号 id（仅用于页面标题）。"""
    try:
        from core import locator

        for account in locator.list_user_accounts(steam_path):
            if str(account.get("id")) == str(account_id):
                return str(account.get("name") or account_id)
    except Exception as exc:
        logger.debug("获取账号显示名失败：%s", exc)
    return str(account_id)


def _live_render(kind: str, args) -> int:
    """用真实 Steam 库生成可视化页面（--demo/--data 路径保持原样不变）。"""
    loaded = _load_library(args, with_metadata=bool(getattr(args, "with_metadata", False)))
    if loaded is None:
        return EXIT_ERROR
    play_stats, games_meta, _, classified, steam_path, account_id, _ownership = loaded

    try:
        from core import render_bridge
    except ImportError as exc:
        print(f"[{args.command}] 真实数据桥接不可用：{exc}")
        return EXIT_ERROR

    if not play_stats:
        print("提示：该账号没有读到任何游戏记录，将生成空态页面。")

    games_meta = render_bridge.fill_metadata(games_meta, play_stats)
    named = sum(1 for meta in games_meta.values() if str(meta.get("name") or "").strip())
    if named == 0:
        print("提示：未抓取到元数据，游戏名/类型可能缺失，可加 --with-metadata（需联网）")
    elif named < len(play_stats):
        print(f"提示：{named}/{len(play_stats)} 款有元数据，其余显示为 appid；可加 --with-metadata 补全（需联网）")

    snapshots = None
    if kind in ("timeline", "today"):
        snapshots = render_bridge.load_snapshots()
        if not snapshots:
            print("提示：暂无历史快照，页面将只使用当前库数据；可先执行 backfill 回填历史。")

    payload = render_bridge.build_payload(
        kind,
        play_stats,
        games_meta,
        classified=classified,
        snapshots=snapshots,
        user_name=_account_persona_name(steam_path, account_id),
    )

    module = _RENDER_MODULES[kind]
    generator_name = _RENDER_GENERATORS[kind]
    try:
        rendered = importlib.import_module(module)
    except ImportError as exc:
        print(f"[{args.command}] 模块 {module} 不可用：{exc}")
        return EXIT_ERROR
    generator = getattr(rendered, generator_name, None)
    if not callable(generator):
        print(f"[{args.command}] 模块 {module} 未提供 {generator_name}()")
        return EXIT_NOT_IMPLEMENTED

    output = getattr(args, "output", None)
    stable_dir = _stable_output_dir()
    if stable_dir is not None and not output:
        rendered.DEFAULT_OUTPUT_DIR = stable_dir
    try:
        out = generator(payload, output_path=output, open_browser=not getattr(args, "no_open", False))
    except Exception as exc:
        logger.exception("[%s] 生成失败", args.command)
        print(f"[{args.command}] 生成失败：{exc}")
        return EXIT_ERROR
    print(f"[{args.command}] 已生成（真实库数据）：{out}")
    return EXIT_OK


def _run_render_command(module: str, args: argparse.Namespace) -> int:
    """调用 render 层生成的模块（annual_report / graveyard 的 main(argv) 风格）。"""
    try:
        rendered = importlib.import_module(module)
    except ImportError as exc:
        print(f"[{args.command}] 模块 {module} 不可用：{exc}")
        return EXIT_ERROR
    main_fn = getattr(rendered, "main", None)
    if not callable(main_fn):
        print(f"[{args.command}] 模块 {module} 未提供 main(argv) 入口")
        return EXIT_NOT_IMPLEMENTED
    argv = []
    if getattr(args, "demo", False):
        argv.append("--demo")
    if getattr(args, "data", None):
        argv += ["--data", str(args.data)]
    if getattr(args, "output", None):
        argv += ["--output", str(args.output)]
    if getattr(args, "no_open", False):
        argv.append("--no-open")
    try:
        return main_fn(argv) or EXIT_OK
    except Exception as exc:
        logger.exception("[%s] 生成失败", args.command)
        print(f"[{args.command}] 生成失败：{exc}")
        return EXIT_ERROR


def cmd_report(args: argparse.Namespace) -> int:
    """生成年度报告 HTML（支持 --demo/--data/--output；加 --account-id/--live 读真实库）。"""
    if _render_live_mode(args):
        return _live_render("report", args)
    return _run_render_command("render.annual_report", args)


def cmd_graveyard(args: argparse.Namespace) -> int:
    """生成游戏墓园 HTML（支持 --demo/--data/--output；加 --account-id/--live 读真实库）。"""
    if _render_live_mode(args):
        return _live_render("graveyard", args)
    return _run_render_command("render.graveyard", args)


def cmd_price(args: argparse.Namespace) -> int:
    """每小时单价排行榜：加载库数据并注入 play_stats/games/statuses 后调用 engine.value_ranking。"""
    loaded = _load_library(args, with_metadata=args.with_metadata)
    if loaded is None:
        return EXIT_ERROR
    play_stats, games_meta, statuses, _, _, _, _ownership = loaded
    try:
        from engine.value_ranking import run as value_run
    except ImportError as exc:
        print(f"性价比模块不可用：{exc}")
        return EXIT_ERROR
    args.play_stats = play_stats
    args.games = games_meta
    args.statuses = statuses
    return value_run(args)


def cmd_family(args: argparse.Namespace) -> int:
    """游戏族谱：加载库数据并注入 play_stats/games/statuses 后调用 engine.series_tree。"""
    loaded = _load_library(args, with_metadata=args.with_metadata)
    if loaded is None:
        return EXIT_ERROR
    play_stats, games_meta, statuses, _, steam_path, account_id, _ownership = loaded
    try:
        from engine.series_tree import run as family_run
    except ImportError as exc:
        print(f"族谱模块不可用：{exc}")
        return EXIT_ERROR
    args.play_stats = play_stats
    args.games = games_meta
    args.statuses = statuses
    # 族谱要按「授权归属」区分「确实拥有 / 家庭库玩过 / 未拥有」，必须把**解析后**的
    # steam_path 与 account_id 传下去：args 里的 --steam-path 可能是空的（自动探测），
    # 只传 args 会让族谱拿不到授权票、退化成「在库里即拥有」。
    args.steam_path = steam_path
    args.account_id = account_id
    return family_run(args)


def cmd_wishlist(args: argparse.Namespace) -> int:
    """反向愿望单：加载库数据并注入 play_stats/games/statuses 后调用 engine.wishlist_guard。"""
    loaded = _load_library(args, with_metadata=args.with_metadata)
    if loaded is None:
        return EXIT_ERROR
    play_stats, games_meta, statuses, _, _, _, _ownership = loaded
    try:
        from engine.wishlist_guard import run as wish_run
    except ImportError as exc:
        print(f"愿望单模块不可用：{exc}")
        return EXIT_ERROR
    args.play_stats = play_stats
    args.games = games_meta
    args.statuses = statuses
    return wish_run(args)


def cmd_starmap(args: argparse.Namespace) -> int:
    """生成星图 HTML（支持 --demo/--data/--output；加 --account-id/--live 读真实库）。"""
    if _render_live_mode(args):
        return _live_render("starmap", args)
    return _run_render_command("render.starmap", args)


def cmd_timeline(args: argparse.Namespace) -> int:
    """生成游戏考古时间轴 HTML（支持 --demo/--data/--output；加 --account-id/--live 读真实库）。"""
    if _render_live_mode(args):
        return _live_render("timeline", args)
    return _run_render_command("render.archaeologist", args)


def cmd_today(args: argparse.Namespace) -> int:
    """生成时光机 · 去年今日 HTML（支持 --demo/--data/--output；加 --account-id/--live 读真实库）。"""
    if _render_live_mode(args):
        return _live_render("today", args)
    return _run_render_command("render.timemachine", args)


def cmd_checkup(args: argparse.Namespace) -> int:
    """生成游戏体检报告 HTML（支持 --demo/--data/--output；加 --account-id/--live 读真实库）。"""
    if _render_live_mode(args):
        return _live_render("checkup", args)
    return _run_render_command("render.checkup", args)


def cmd_audit(args: argparse.Namespace) -> int:
    """归属自动体检：把判定结果与票面 / 客户端清单 / 库索引 / 共享日志逐条对账。

    用户反馈「不要我说什么就改什么，要是自动化的识别」——判定逻辑本身没有硬编码，
    但此前不会自己发现问题。本命令让工具自己**报出**显示错标的条目（按严重度分级），
    用的是 :func:`core.ownership.audit_ownership` 的独立交叉验证，不做启发式猜测。

    退出码：有 error 级问题返回 ``EXIT_ERROR``（便于脚本 / CI 拦），否则 ``EXIT_OK``。
    """
    resolved = _resolve_steam_account(args)
    if not resolved:
        return EXIT_ERROR
    steam_path, account_id = resolved
    try:
        from core import ownership
    except ImportError as exc:
        print(f"归属模块不可用：{exc}")
        return EXIT_ERROR

    def _detail_appids(member: str) -> "list[int] | None":
        """该账号**当前游戏明细里实际有哪些**（体检据此对账出「在库里却没显示」）。"""
        try:
            loaded = _load_single_library(steam_path, member, with_metadata=False)
        except Exception as exc:                 # 读不到就不做漏项体检（绝不凭空断言）
            logger.debug("体检取明细失败（%s），跳过漏项对账：%s", member, exc)
            return None
        if not loaded:
            return None
        return sorted(int(a) for a in (loaded[0] or {}) if str(a).lstrip("-").isdigit())

    if _is_merge_account(account_id):
        members = _merge_module().resolve_member_ids(steam_path)
        if not members:
            print("错误：合流库需要至少一个已添加账号")
            return EXIT_ERROR
        # 合流库的明细是各成员的**并集**：某个免费游戏只要在任一成员库里，就可能出现在
        # 合流明细里，所以对账要用并集（否则会把「别的成员已显示」误报成漏项）。
        union: set[int] = set()
        per_member: dict[str, "list[int] | None"] = {}
        for member in members:
            per_member[member] = _detail_appids(member)
            if per_member[member]:
                union.update(per_member[member])
        listed = sorted(union) if union else None
        reports = [
            (member, ownership.audit_ownership(steam_path, member, listed))
            for member in members
        ]
    else:
        reports = [
            (account_id, ownership.audit_ownership(
                steam_path, account_id, _detail_appids(account_id)
            ))
        ]

    total_errors = 0
    for name, report in reports:
        if report.get("degraded"):
            print(f"[{name}] 信息不足：读不到授权票 / 库索引，本次体检不作结论"
                  "（绝不谎报「一切正常」）")
            continue
        errors = int(report.get("errors") or 0)
        warnings = int(report.get("warnings") or 0)
        total_errors += errors
        head = "未发现显示错标" if errors == 0 else f"发现 {errors} 条确证错标"
        print(f"[{name}] 体检 {report.get('checked', 0)} 条 -> {head}"
              + (f"（另有 {warnings} 条存疑）" if warnings else ""))
        for item in report.get("issues") or []:
            level = "错误" if item["level"] == ownership.AUDIT_ERROR else "存疑"
            print(f"    [{level}] appid {item['appid']}：{item['reason']}")
            print(f"           {item['evidence']}")
        if report.get("counts"):
            summary = "  ".join(f"{k}={v}" for k, v in sorted(report["counts"].items()))
            print(f"    小结：{summary}")

    if total_errors:
        print(f"\n体检结论：有 {total_errors} 条确证错标 —— 请把上面的 appid 与 reason 反馈给开发者。")
        return EXIT_ERROR
    print("\n体检结论：归属判定与各数据源的证据自洽。")
    return EXIT_OK


def cmd_panel(args: argparse.Namespace) -> int:
    """启动本地网页面板：一个网页里用上全部功能，并自动打开浏览器。"""
    try:
        from webapp import server
    except ImportError as exc:
        print(f"[panel] 网页面板模块不可用：{exc}")
        return EXIT_ERROR
    return server.run(
        port=int(getattr(args, "port", 8765) or 8765),
        account_id=getattr(args, "account_id", None),
        steam_path=getattr(args, "steam_path", None),
        with_metadata=bool(getattr(args, "with_metadata", False)),
        open_browser=not bool(getattr(args, "no_open", False)),
    )


def cmd_radar(args: argparse.Namespace) -> int:
    """成就冲刺雷达：装载游戏库（可选成就）后调用 engine.achievement_radar。"""
    try:
        from core import library as core_library
    except ImportError as exc:
        print(f"数据底座不可用：{exc}")
        return EXIT_ERROR
    resolved = _resolve_steam_account(args)
    if not resolved:
        return EXIT_ERROR
    steam_path, account_id = resolved

    offline = bool(getattr(args, "offline", False))
    with_achievements = bool(getattr(args, "with_achievements", False))
    lib = core_library.load_library(
        steam_path,
        account_id,
        with_metadata=bool(getattr(args, "with_metadata", False)),
        with_achievements=with_achievements and not offline,
        steam_id=str(getattr(args, "steam_id", "") or ""),
        api_key=getattr(args, "api_key", None),
    )

    if with_achievements and offline:
        try:
            from core import achievements as achievements_mod
            get_cached = getattr(achievements_mod, "get_cached", None)
            if callable(get_cached):
                for appid in lib:
                    lib[appid]["achievements"] = get_cached(appid)
        except ImportError as exc:
            print(f"成就缓存不可用：{exc}")

    achievement_data: dict = {}
    games_meta: dict = {}
    playtime_minutes: dict = {}
    for appid, info in lib.items():
        playtime_minutes[appid] = int(info.get("playtime", 0))
        name = str(info.get("name") or "").strip()
        if name:
            games_meta[appid] = {"name": name}
        ach = info.get("achievements") or {}
        if not isinstance(ach, dict):
            continue
        achieved = ach.get("achieved_count", ach.get("achieved"))
        total = ach.get("total_count", ach.get("total"))
        if total:
            achievement_data[appid] = {
                "achieved": achieved,
                "total": total,
                "completion_rate": ach.get("completion_rate"),
            }

    try:
        from engine.achievement_radar import run as radar_run
    except ImportError as exc:
        print(f"成就雷达模块不可用：{exc}")
        return EXIT_ERROR

    args.achievement_data = achievement_data
    args.games = games_meta
    args.playtime_minutes = playtime_minutes
    return radar_run(args)


def cmd_challenge(args: argparse.Namespace) -> int:
    """清库挑战：加载库数据并注入 play_stats 后调用 engine.challenge。"""
    loaded = _load_library(args, with_metadata=bool(getattr(args, "with_metadata", False)))
    if loaded is None:
        return EXIT_ERROR
    play_stats, games_meta, statuses, _, _, _, _ownership = loaded
    try:
        from engine.challenge import run as challenge_run
    except ImportError as exc:
        print(f"清库挑战模块不可用：{exc}")
        return EXIT_ERROR
    args.play_stats = play_stats
    args.games = games_meta
    args.statuses = statuses
    return challenge_run(args)


def _library_appids(args):
    """给 artwork/localize 用：解析账号并读出库里的 appid 列表（支持合流库）。"""
    resolved = _resolve_steam_account(args)
    if not resolved:
        return None
    steam_path, account_id = resolved
    if _is_merge_account(account_id):
        loaded = _load_merged_library(steam_path, False)
        if loaded is None:
            return None
        play_stats = loaded[0]
    else:
        play_stats = _read_playtime(steam_path, account_id)
        if play_stats is None:
            return None
    return steam_path, account_id, sorted(int(appid) for appid in play_stats)


def _artwork_stats(artwork) -> dict:
    """封面缓存概况（兼容不同版本的 artwork 模块，取不到也不报错）。"""
    try:
        info = artwork.stats()
        if isinstance(info, dict):
            return {
                "dir": str(info.get("dir") or ""),
                "count": int(info.get("count") or 0),
            }
    except Exception:
        pass
    try:
        folder = artwork.covers_dir()
        exts = (".jpg", ".jpeg", ".png", ".webp")
        count = sum(1 for path in folder.glob("*") if path.suffix.lower() in exts)
        return {"dir": str(folder), "count": count}
    except Exception:
        return {"dir": "", "count": 0}


def cmd_artwork(args: argparse.Namespace) -> int:
    """抓取/补齐游戏封面（离线优先：本机 Steam 图 → 官方 CDN → 商店接口）。"""
    try:
        from core import artwork
    except ImportError as exc:
        print(f"封面模块不可用：{exc}")
        return EXIT_ERROR

    loaded = _library_appids(args)
    if not loaded:
        return EXIT_ERROR
    steam_path, account_id, appids = loaded
    limit = getattr(args, "limit", None)
    print(f"账号 {account_id}：库里 {len(appids)} 款游戏" + (f"，本次最多处理 {limit} 款" if limit else ""))

    def progress(done, total, entry):
        ok = bool(entry.get("ok"))
        appid = entry.get("appid")
        if done % 20 == 0 or done == total or not ok:
            flag = "✓" if ok else "✗"
            detail = entry.get("source") if ok else (entry.get("error") or "")
            print(f"  [{done}/{total}] {flag} {appid} {str(detail)[:60]}")

    stat = artwork.ensure_covers(appids, steam_path=steam_path, limit=limit, progress=progress)

    local = _artwork_stats(artwork)
    ok = stat.get("ok", stat.get("ok_count", 0))
    failed = stat.get("failed", stat.get("fail_count", 0))
    total = stat.get("total", 0)
    skipped = stat.get("skipped", stat.get("skip_count", 0))
    print(f"\n封面完成：成功 {ok} / 失败 {failed} / 跳过 {skipped} / 共 {total}")
    print(f"封面缓存目录：{local['dir']}（已有 {local['count']} 张）")
    return EXIT_OK


def cmd_localize(args: argparse.Namespace) -> int:
    """抓取游戏中文名（离线优先：本机 appinfo → Steam 官方商店中文名）。"""
    try:
        from core import metadata_cn
    except ImportError as exc:
        print(f"中文名模块不可用：{exc}")
        return EXIT_ERROR

    loaded = _library_appids(args)
    if not loaded:
        return EXIT_ERROR
    steam_path, account_id, appids = loaded
    limit = getattr(args, "limit", None)
    print(f"账号 {account_id}：库里 {len(appids)} 款游戏" + (f"，本次最多处理 {limit} 款" if limit else ""))

    def progress(index, total, appid, row):
        if index % 10 == 0 or index == total:
            name = row.get("name_cn") or "（暂无）"
            print(f"  [{index}/{total}] {appid} → {name} ({row.get('source') or row.get('error', '')})")

    stat = metadata_cn.ensure_names_cn(
        appids, steam_path=steam_path, limit=limit,
        use_network=not bool(getattr(args, "offline", False)), progress=progress,
    )

    print(f"\n{stat['message']}")
    print(f"中文名缓存：{metadata_cn.stats()['path']}")
    return EXIT_OK


def cmd_backfill(args: argparse.Namespace) -> int:
    """历史快照回填：按 last_played 的日期补写历史快照，解决时光机/考古冷启动。

    只补缺、默认不覆盖既有历史、绝不触碰今天；回填值是「以当前数据近似该日期」
    的**推断值**，非当时真实时长。
    """
    if _is_merge_account(getattr(args, "account_id", None)):
        print("合流库不做历史快照回填（快照按账号记录）：请对单个账号执行 backfill。")
        return EXIT_ERROR
    loaded = _load_library(args, with_metadata=bool(getattr(args, "with_metadata", False)))
    if loaded is None:
        return EXIT_ERROR
    play_stats, _games_meta, _statuses, classified, _steam_path, _account_id, _ownership = loaded
    if classified is None:
        print("状态引擎不可用，无法为回填快照附加状态，已中止")
        return EXIT_ERROR

    try:
        from core import snapshot
    except ImportError as exc:
        print(f"无法加载 core.snapshot：{exc}")
        return EXIT_ERROR

    min_days_ago = max(int(getattr(args, "min_days_ago", 1) or 0), 0)
    dry_run = bool(getattr(args, "dry_run", False))
    overwrite = bool(getattr(args, "overwrite", False))

    try:
        stats = snapshot.backfill_from_play_stats(
            play_stats,
            classified=classified,
            only_missing=not overwrite,
            min_days_ago=min_days_ago,
            dry_run=dry_run,
        )
    except Exception as exc:
        logger.exception("回填历史快照失败")
        print(f"回填历史快照失败：{exc}")
        return EXIT_ERROR

    mode = "覆盖既有日期" if overwrite else "只补缺（不覆盖既有）"
    print("\n== 历史快照回填 ==")
    print(f"  策略：{mode}；仅回填距今 ≥ {min_days_ago} 天的记录（今天永不写入）")
    if dry_run:
        print("  (dry-run) 仅预览，不写入任何快照行")
    print(f"  回填日期数：{stats['dates']}")
    print(f"  回填条数：{stats['rows']}")
    print(f"  跳过日期数：{stats['skipped']}"
          + (f"（其中距今天数不足被忽略 {stats['skipped_recent']} 条）"
             if stats.get("skipped_recent") else ""))
    if not dry_run:
        try:
            print(f"  当前快照日期数：{len(snapshot.list_dates())}")
        except Exception as exc:  # 仅统计展示，失败不阻断
            logger.warning("读取快照日期列表失败：%s", exc)
    print("  说明：历史 playtime 无法精确还原，回填值是以当前已知数据作为该日期的近似推断值。")
    return EXIT_OK


# 写回状态持久化：路径按**调用时**环境解析，打包态落 exe 附近的真实 data 目录。
# （模块级常量会在 import 期固化，frozen/源码切换或测试 monkeypatch 后不刷新，
#  故用函数 + 模块 __getattr__ 兼容旧名。）


def _writeback_state_path() -> Path:
    """写回状态文件位置（真实 data 目录，绝不是退出即删的 _MEIPASS）。"""
    return _stable_data_dir() / "writeback_state.json"


def __getattr__(name):
    # 兼容旧名：始终反映**当前**环境的解析结果（外部只读用）。
    if name == "_WRITEBACK_STATE_PATH":
        return _writeback_state_path()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _load_writeback_state() -> bool:
    try:
        data = json.loads(_writeback_state_path().read_text(encoding="utf-8"))
        return bool(data.get("writeback", False))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


def _save_writeback_state(on: bool) -> None:
    path = _writeback_state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"writeback": on}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"写回状态保存失败：{exc}")


def _steam_writeback(steam_path: str, account_id: str, classified: dict) -> bool:
    """把分类写回 Steam sharedconfig.vdf（共用逻辑，返回是否成功）。"""
    try:
        from core.steam_writer import write_categories, status_to_categories, SteamRunningError
    except ImportError as exc:
        print(f"[写回] steam_writer 不可用：{exc}")
        return False
    try:
        categories = status_to_categories(classified)
        backup = write_categories(steam_path, account_id, categories)
    except SteamRunningError as exc:
        print(f"[写回] 已中止：{exc}")
        return False
    except Exception as exc:
        logger.exception("[写回] 写回失败")
        print(f"[写回] 写回失败：{exc}")
        return False
    print(f"已写回 Steam {len(categories)} 款（备份:{backup}）")
    return True


def _steam_unwrite(steam_path: str, account_id: str, appids: list) -> bool:
    """从 Steam 移除状态分类（保留用户手动分类；返回是否成功）。"""
    try:
        from core.steam_writer import remove_categories, SteamRunningError
    except ImportError as exc:
        print(f"[取消分类] steam_writer 不可用：{exc}")
        return False
    try:
        backup = remove_categories(steam_path, account_id, appids)
    except SteamRunningError as exc:
        print(f"[取消分类] 已中止：{exc}")
        return False
    except Exception as exc:
        logger.exception("[取消分类] 失败")
        print(f"[取消分类] 失败：{exc}")
        return False
    print(f"已从 Steam 移除 {len(appids)} 款的状态分类（手动分类保留；备份:{backup}）")
    return True


def cmd_writeback(args: argparse.Namespace) -> int:
    """按钮：切换「分类写回 Steam」。

    关闭 -> 开启：按当前游玩数据分类并写回 Steam
    开启 -> 关闭：从 Steam 移除状态分类（用户手动分类保留）
    只有实际操作成功后才翻转按钮状态，保证按钮与实际一致。
    """
    if _is_merge_account(getattr(args, "account_id", None)):
        print("合流库不是账号，无法写回 Steam 分类：请对单个账号执行 writeback。")
        return EXIT_ERROR
    resolved = _resolve_steam_account(args)
    if not resolved:
        return EXIT_ERROR
    steam_path, account_id = resolved

    play_stats = _read_playtime(steam_path, account_id)
    if play_stats is None:
        return EXIT_ERROR

    if _load_writeback_state():
        if not _steam_unwrite(steam_path, account_id, list(play_stats.keys())):
            return EXIT_ERROR
        _save_writeback_state(False)
        print(f"写回按钮：关闭（状态文件：{_writeback_state_path()}）")
        return EXIT_OK

    try:
        from engine import classifier
    except ImportError as exc:
        print(f"状态引擎不可用，无法分类：{exc}")
        return EXIT_ERROR
    classified = classifier.classify_games(play_stats)
    if not _steam_writeback(steam_path, account_id, classified):
        return EXIT_ERROR
    _save_writeback_state(True)
    counter = Counter(info["status"] for info in classified.values())
    dist = "  ".join(f"{status}={n}" for status, n in counter.most_common())
    print(f"写回按钮：开启（{dist}）")
    print(f"状态文件：{_writeback_state_path()}")
    return EXIT_OK


def _make_placeholder(
    name: str, module: str, entry: str = "run"
) -> Callable[[argparse.Namespace], int]:
    """构造延迟接入的占位命令：模块提供 run(args) -> int 后自动生效。"""

    def handler(args: argparse.Namespace) -> int:
        try:
            mod = importlib.import_module(module)
        except ImportError as exc:
            missing = getattr(exc, "name", "") or ""
            if missing == module or missing.startswith(module + "."):
                print(f"[{name}] 尚未实现：模块 {module} 还未创建（由对应层实现）")
                return EXIT_NOT_IMPLEMENTED
            print(f"[{name}] 暂时不可用：加载 {module} 时缺少依赖 {missing}")
            print("提示：请先执行  pip install -r requirements.txt")
            return EXIT_ERROR

        fn = getattr(mod, entry, None)
        if not callable(fn):
            print(f"[{name}] 尚未实现：等待 {module}.{entry}() 完成（由对应层实现）")
            return EXIT_NOT_IMPLEMENTED

        try:
            result = fn(args)
        except Exception as exc:
            logger.exception("[%s] 执行异常", name)
            print(f"[{name}] 执行出错：{exc}")
            return EXIT_ERROR
        return result if isinstance(result, int) else EXIT_OK

    return handler


# 占位命令 → 目标模块（对应方案文档各层模块清单；已接线的命令见各自 handler）
_COMMAND_TARGETS = {
    # 说明：引擎模块提供 run(args) 入口后，按此格式登记即自动接入（当前均已接线）。
}

_COMMAND_DESCRIPTIONS = {
    "setup": "首次配置：定位 Steam、选账号、读游戏与时长",
    "run": "扫描、更新状态、存快照、导出分类（不写回 Steam）",
    "stats": "终端统计看板",
    "recommend": "按时间 / 心情 / 类型推荐下一款玩什么",
    "price": "每小时单价排行榜",
    "family": "游戏族谱",
    "radar": "成就冲刺雷达",
    "wishlist": "反向愿望单",
    "challenge": "本月挑战进度",
    "today": "时光机 · 去年今日",
    "report": "生成年度报告 HTML",
    "starmap": "生成星图 HTML",
    "graveyard": "生成墓园 HTML",
    "timeline": "生成考古时间轴 HTML",
    "checkup": "生成体检报告 HTML",
    "persona": "游戏人格测试",
    "config": "查看/修改规则配置",
    "writeback": "开启 / 关闭：把状态标签写回 Steam 分类",
    "backfill": "回填历史快照（按最后游玩时间近似推断）",
    "panel": "启动本地网页面板",
    "artwork": "补齐游戏封面（先本机 Steam，再官方 CDN）",
    "localize": "补齐游戏中文名（先本机 Steam，再官方商店）",
    "audit": "归属自动体检（自动报出显示错标）",
}


# --account-id 的统一说明（17 处子命令共用）：all = 合流库（把已添加账号当作同一个账号）
_ACCOUNT_ID_HELP = "账号 id（userdata 目录名）；all = 合流库（把已添加账号合并统计）"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="game-life",
        description="读取本机 Steam 库，生成统计看板与可视化报告。",
    )
    parser.add_argument("--version", action="version", version=f"game-life {VERSION}")
    subparsers = parser.add_subparsers(
        dest="command", metavar="<command>", required=True, title="可用命令"
    )

    p_setup = subparsers.add_parser("setup", help=_COMMAND_DESCRIPTIONS["setup"])
    p_setup.add_argument(
        "--steam-path", metavar="PATH", help="手动指定 Steam 安装目录，跳过注册表定位"
    )
    p_setup.set_defaults(handler=cmd_setup)

    p_recommend = subparsers.add_parser("recommend", help=_COMMAND_DESCRIPTIONS["recommend"])
    p_recommend.add_argument(
        "--time", choices=["30m", "1h", "2h", "不限"], default="不限", help="剩余时间（默认：不限）"
    )
    p_recommend.add_argument(
        "--mood", choices=["轻松", "挑战", "沉浸", "随便"], default="随便", help="心情（默认：随便）"
    )
    p_recommend.add_argument("--genre", default="不限", help="想玩的类型（默认：不限）")
    p_recommend.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_recommend.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_recommend.add_argument(
        "--with-metadata", action="store_true", help="从 Steam Store 抓取元数据（游戏名/类型/开发商），需要联网"
    )
    p_recommend.set_defaults(handler=cmd_recommend)

    p_stats = subparsers.add_parser("stats", help=_COMMAND_DESCRIPTIONS["stats"])
    p_stats.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_stats.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_stats.add_argument(
        "--with-metadata", action="store_true", help="从 Steam Store 抓取元数据（游戏名/类型/开发商），需要联网"
    )
    p_stats.set_defaults(handler=cmd_stats)

    p_run = subparsers.add_parser("run", help=_COMMAND_DESCRIPTIONS["run"])
    p_run.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_run.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_run.add_argument(
        "--dry-run", action="store_true", help="仅预览状态分类，不存快照、不导出文件"
    )
    p_run.add_argument(
        "--with-metadata", action="store_true",
        help="从 Steam Store 抓取元数据（用于导出游戏名），需要联网"
    )
    p_run.set_defaults(handler=cmd_run)

    p_persona = subparsers.add_parser("persona", help=_COMMAND_DESCRIPTIONS["persona"])
    p_persona.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_persona.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_persona.add_argument(
        "--with-metadata", action="store_true", help="从 Steam Store 抓取元数据（类型/开发商），需要联网"
    )
    p_persona.set_defaults(handler=cmd_persona)

    p_report = subparsers.add_parser("report", help=_COMMAND_DESCRIPTIONS["report"])
    p_report.add_argument("--data", metavar="JSON", help="数据 JSON 文件路径（缺省使用示例数据）")
    p_report.add_argument("--demo", action="store_true", help="使用内置示例数据")
    p_report.add_argument("--output", metavar="PATH", help="输出 HTML 路径（缺省 game-life/output/）")
    p_report.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    p_report.add_argument(
        "--live", action="store_true",
        help="使用真实 Steam 库生成（等价于指定 --account-id/--steam-path）"
    )
    p_report.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_report.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_report.add_argument(
        "--with-metadata", action="store_true",
        help="从 Steam Store 抓取元数据（游戏名/类型），需要联网"
    )
    p_report.set_defaults(handler=cmd_report)

    p_graveyard = subparsers.add_parser("graveyard", help=_COMMAND_DESCRIPTIONS["graveyard"])
    p_graveyard.add_argument("--data", metavar="JSON", help="数据 JSON 文件路径（缺省使用示例数据）")
    p_graveyard.add_argument("--demo", action="store_true", help="使用内置示例数据")
    p_graveyard.add_argument("--output", metavar="PATH", help="输出 HTML 路径（缺省 game-life/output/）")
    p_graveyard.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    p_graveyard.add_argument(
        "--live", action="store_true",
        help="使用真实 Steam 库生成（等价于指定 --account-id/--steam-path）"
    )
    p_graveyard.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_graveyard.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_graveyard.add_argument(
        "--with-metadata", action="store_true",
        help="从 Steam Store 抓取元数据（游戏名/类型），需要联网"
    )
    p_graveyard.set_defaults(handler=cmd_graveyard)

    p_config = subparsers.add_parser("config", help=_COMMAND_DESCRIPTIONS["config"])
    p_config.add_argument(
        "--get", dest="get_path", metavar="KEY", help="查看规则键（点分路径，如 playtime.recent_days）"
    )
    p_config.add_argument(
        "--set", dest="set_path", nargs=2, metavar=("KEY", "VALUE"),
        help="设置规则键值，VALUE 需为合法 JSON（如 --set playtime.recent_days 60）",
    )
    p_config.set_defaults(handler=cmd_config)

    p_writeback = subparsers.add_parser("writeback", help=_COMMAND_DESCRIPTIONS["writeback"])
    p_writeback.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_writeback.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_writeback.set_defaults(handler=cmd_writeback)


    p_price = subparsers.add_parser("price", help=_COMMAND_DESCRIPTIONS["price"])
    p_price.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_price.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_price.add_argument(
        "--with-metadata", action="store_true", help="从 Steam Store 抓取元数据，需要联网"
    )
    p_price.set_defaults(handler=cmd_price)

    p_family = subparsers.add_parser("family", help=_COMMAND_DESCRIPTIONS["family"])
    p_family.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_family.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_family.add_argument(
        "--with-metadata", action="store_true", help="从 Steam Store 抓取元数据，需要联网"
    )
    p_family.set_defaults(handler=cmd_family)

    p_wishlist = subparsers.add_parser("wishlist", help=_COMMAND_DESCRIPTIONS["wishlist"])
    p_wishlist.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_wishlist.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_wishlist.add_argument(
        "--with-metadata", action="store_true", help="从 Steam Store 抓取元数据，需要联网"
    )
    p_wishlist.set_defaults(handler=cmd_wishlist)

    p_starmap = subparsers.add_parser("starmap", help=_COMMAND_DESCRIPTIONS["starmap"])
    p_starmap.add_argument("--data", metavar="JSON", help="数据 JSON 文件路径（缺省使用示例数据）")
    p_starmap.add_argument("--demo", action="store_true", help="使用内置示例数据")
    p_starmap.add_argument("--output", metavar="PATH", help="输出 HTML 路径（缺省 game-life/output/）")
    p_starmap.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    p_starmap.add_argument(
        "--live", action="store_true",
        help="使用真实 Steam 库生成（等价于指定 --account-id/--steam-path）"
    )
    p_starmap.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_starmap.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_starmap.add_argument(
        "--with-metadata", action="store_true",
        help="从 Steam Store 抓取元数据（游戏名/类型），需要联网"
    )
    p_starmap.set_defaults(handler=cmd_starmap)

    p_timeline = subparsers.add_parser("timeline", help=_COMMAND_DESCRIPTIONS["timeline"])
    p_timeline.add_argument("--data", metavar="JSON", help="数据 JSON 文件路径（缺省使用示例数据）")
    p_timeline.add_argument("--demo", action="store_true", help="使用内置示例数据")
    p_timeline.add_argument("--output", metavar="PATH", help="输出 HTML 路径（缺省 game-life/output/）")
    p_timeline.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    p_timeline.add_argument(
        "--live", action="store_true",
        help="使用真实 Steam 库生成（等价于指定 --account-id/--steam-path）"
    )
    p_timeline.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_timeline.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_timeline.add_argument(
        "--with-metadata", action="store_true",
        help="从 Steam Store 抓取元数据（游戏名/类型），需要联网"
    )
    p_timeline.set_defaults(handler=cmd_timeline)

    p_today = subparsers.add_parser("today", help=_COMMAND_DESCRIPTIONS["today"])
    p_today.add_argument("--data", metavar="JSON", help="数据 JSON 文件路径（缺省使用示例数据）")
    p_today.add_argument("--demo", action="store_true", help="使用内置示例数据")
    p_today.add_argument("--output", metavar="PATH", help="输出 HTML 路径（缺省 game-life/output/）")
    p_today.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    p_today.add_argument(
        "--live", action="store_true",
        help="使用真实 Steam 库生成（等价于指定 --account-id/--steam-path）"
    )
    p_today.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_today.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_today.add_argument(
        "--with-metadata", action="store_true",
        help="从 Steam Store 抓取元数据（游戏名/类型），需要联网"
    )
    p_today.set_defaults(handler=cmd_today)

    p_checkup = subparsers.add_parser("checkup", help=_COMMAND_DESCRIPTIONS["checkup"])
    p_checkup.add_argument("--data", metavar="JSON", help="数据 JSON 文件路径（缺省使用示例数据）")
    p_checkup.add_argument("--demo", action="store_true", help="使用内置示例数据")
    p_checkup.add_argument("--output", metavar="PATH", help="输出 HTML 路径（缺省 game-life/output/）")
    p_checkup.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    p_checkup.add_argument(
        "--live", action="store_true",
        help="使用真实 Steam 库生成（等价于指定 --account-id/--steam-path）"
    )
    p_checkup.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_checkup.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_checkup.add_argument(
        "--with-metadata", action="store_true",
        help="从 Steam Store 抓取元数据（游戏名/类型），需要联网"
    )
    p_checkup.set_defaults(handler=cmd_checkup)

    p_radar = subparsers.add_parser("radar", help=_COMMAND_DESCRIPTIONS["radar"])
    p_radar.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_radar.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_radar.add_argument(
        "--with-achievements", action="store_true", help="抓取成就完成度（需要联网）"
    )
    p_radar.add_argument(
        "--offline", action="store_true", help="仅使用本地成就缓存，不发起网络请求"
    )
    p_radar.add_argument(
        "--with-metadata", action="store_true", help="从 Steam Store 抓取游戏名/类型，需要联网"
    )
    p_radar.add_argument("--steam-id", metavar="ID", help="SteamID64（配合 --api-key 走官方接口）")
    p_radar.add_argument("--api-key", metavar="KEY", help="Steam Web API Key（可选）")
    p_radar.set_defaults(handler=cmd_radar)

    p_challenge = subparsers.add_parser("challenge", help=_COMMAND_DESCRIPTIONS["challenge"])
    p_challenge.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_challenge.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_challenge.add_argument("--month", metavar="YYYY-MM", help="目标月份（缺省本月）")
    p_challenge.set_defaults(handler=cmd_challenge)

    p_backfill = subparsers.add_parser("backfill", help=_COMMAND_DESCRIPTIONS["backfill"])
    p_backfill.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_backfill.add_argument(
        "--account-id", metavar="ID", help=_ACCOUNT_ID_HELP
    )
    p_backfill.add_argument(
        "--with-metadata", action="store_true", help="从 Steam Store 抓取元数据，需要联网"
    )
    p_backfill.add_argument(
        "--dry-run", action="store_true", help="只预览不落库"
    )
    p_backfill.add_argument(
        "--overwrite", action="store_true",
        help="允许覆盖该日期既有快照，默认关闭（只补缺）",
    )
    p_backfill.add_argument(
        "--min-days-ago", type=int, default=1, metavar="N",
        help="只回填距今至少 N 天的记录（默认 1，避免覆盖今天）",
    )
    p_backfill.set_defaults(handler=cmd_backfill)


    p_audit = subparsers.add_parser("audit", help=_COMMAND_DESCRIPTIONS["audit"])
    p_audit.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_audit.add_argument("--account-id", metavar="ID", help=_ACCOUNT_ID_HELP)
    p_audit.set_defaults(handler=cmd_audit)

    p_panel = subparsers.add_parser("panel", help=_COMMAND_DESCRIPTIONS["panel"])
    p_panel.add_argument(
        "--port", type=int, default=8765, metavar="N", help="面板端口（默认 8765，被占用时自动顺延）"
    )
    p_panel.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_panel.add_argument(
        "--account-id", metavar="ID", help="默认账号 id（userdata 目录名），可在页面上切换"
    )
    p_panel.add_argument(
        "--with-metadata", action="store_true", help="默认在页面上抓取 Store 元数据（需联网，较慢）"
    )
    p_panel.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    p_panel.set_defaults(handler=cmd_panel)

    p_artwork = subparsers.add_parser("artwork", help=_COMMAND_DESCRIPTIONS["artwork"])
    p_artwork.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_artwork.add_argument(
        "--account-id", metavar="ID", help="指定账号 id（默认自动选主账号）"
    )
    p_artwork.add_argument("--limit", type=int, metavar="N", help="本次最多处理 N 款")
    p_artwork.add_argument(
        "--missing-only", action="store_true", default=True, help="只补缺的（默认开）"
    )
    p_artwork.add_argument(
        "--with-metadata", action="store_true", help="同时抓取 Store 元数据（较慢）"
    )
    p_artwork.set_defaults(handler=cmd_artwork)

    p_localize = subparsers.add_parser("localize", help=_COMMAND_DESCRIPTIONS["localize"])
    p_localize.add_argument("--steam-path", metavar="PATH", help="手动指定 Steam 安装目录")
    p_localize.add_argument(
        "--account-id", metavar="ID", help="指定账号 id（默认自动选主账号）"
    )
    p_localize.add_argument("--limit", type=int, metavar="N", help="本次最多处理 N 款")
    p_localize.add_argument(
        "--missing-only", action="store_true", default=True, help="只补缺的（默认开）"
    )
    p_localize.add_argument(
        "--offline", action="store_true", help="完全离线：只读本机 Steam 数据，不访问商店接口"
    )
    p_localize.add_argument(
        "--with-metadata", action="store_true", help="同时抓取 Store 元数据（较慢）"
    )
    p_localize.set_defaults(handler=cmd_localize)

    for name, module in _COMMAND_TARGETS.items():
        subparsers.add_parser(name, help=_COMMAND_DESCRIPTIONS[name]).set_defaults(
            handler=_make_placeholder(name, module)
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    # 最早时机：先让标准流「编不出就替换」，保证后面任何 print（含 argparse
    # 的 usage/报错、日志控制台 handler、except 兜底）都不因编码而崩溃。
    _make_console_encoding_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging()
    logger.info("命令: %s", args.command)
    try:
        return args.handler(args) or EXIT_OK
    except KeyboardInterrupt:
        print("\n已取消")
        return 130
    except Exception as exc:  # 兜底，避免裸 traceback 中断
        logger.exception("未捕获异常")
        print(f"错误：{exc}")
        return EXIT_ERROR


if __name__ == "__main__":
    # 直接运行本文件时不经过 main() 之前的任何代码，这里同样先保护一次（幂等）。
    _make_console_encoding_safe()
    sys.exit(main())
