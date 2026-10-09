"""数据底座统一装载入口（核心模块）。

字段契约（供入口 Agent 与渲染层对接）：
    返回 dict[appid, {
        name: str,
        playtime: int,           # 分钟（localconfig.vdf）
        last_played: int,        # unix 时间戳
        genres: list[str],       # Store 原始类型名
        categories: list[str],   # Store 标签
        developers: list[str],
        price: int,              # 原价（分），0 = 免费
        release_date: str,
        hltb: {main:int|None, extra:int|None, completionist:int|None}  # 仅 with_hltb=True
        achievements: {...}|None  # 仅 with_achievements=True
    }]

hltb 单位：分钟，取区间下限（与 classifier 的 hltb_data={"main": 分钟} 约定一致）。
任何步骤失败均降级为缺失字段，不中断主流程。
增量缓存由 store_api / hltb / achievements 各自管理；本入口不做重复请求。
限速：下游自行限速（hltb 1s/次，achievements 300ms/次）。
"""
from typing import Any

from core import vdf_io, store_api, hltb, achievements

_EMPTY_META = {"name": "", "genres": [], "categories": [], "developers": [], "price": 0, "release_date": ""}
_EMPTY_HLTB = {"main": None, "extra": None, "completionist": None}


def _as_appid(value):
    """把 appid 键规整成 int；非数字键原样返回（让它自然不命中集合）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _merge_meta(target: dict, src: dict) -> None:
    for k in ("name", "genres", "categories", "developers", "price", "release_date"):
        if k in src:
            target[k] = src[k]


def load_library(steam_path: str, account_id: str,
                 with_metadata: bool = False,
                 with_hltb: bool = False,
                 with_achievements: bool = False,
                 steam_id: str = "",
                 api_key: str | None = None) -> dict:
    """串联数据底座各模块，返回统一游戏库 dict。

    失败降级：单步异常会打印错误并继续，缺失字段以空值/None 填充。
    """
    # 1. 游玩数据（VDF，本地，秒级）
    try:
        play = vdf_io.read_playtime_data(steam_path, account_id)
    except Exception as exc:
        print(f"library: 读取游玩数据失败，已降级: {exc}")
        play = {}

    # 1a. 补「在库但从未启动」的免费游戏（M2f / T16，与 main._load_single_library 同一口径）。
    #     游戏明细建在 vdf 的 Apps 段上，而那段只记录 Steam 写过游玩记录的条目；免费游戏
    #     没有授权票、Steam 也只在真正启动过之后才写 Apps 条目 —— 不补就整条不在明细里。
    #     读不到库 / 读不到 cache.db 一律原样返回（降级：宁可少补，绝不凭空造条目）。
    try:
        from core import ownership as _ownership_mod

        play = _ownership_mod.augment_library(play, steam_path, account_id)
    except Exception as exc:                      # 补充失败绝不影响主流程
        print(f"library: 补充「在库免费游戏」失败，已忽略: {exc}")

    # 1b. 授权归属过滤（**全产品唯一口径** core.ownership）：家庭库会把别人拥有的游戏
    #     也写进本账号的 vdf，这里剔掉，radar 等走本入口的命令与 CLI/面板同口径。
    #     A4 起「共享但本账号玩过」（playtime > 0）**保留**，只有「共享且从未玩过」被剔除。
    #     降级（读不到 vdf）时原样保留，绝不清空用户库。
    try:
        from core import ownership as ownership_mod

        kept = ownership_mod.filter_owned(steam_path, account_id, play)
        # 非数字键本就进不了库范围，原样保留（绝不顺手删数据）
        filtered = {
            appid: info
            for appid, info in play.items()
            if not isinstance(_as_appid(appid), int) or _as_appid(appid) in kept
        }
        dropped = len(play) - len(filtered)
        if dropped > 0:
            # 在**过滤前**的原始库上自述（五类计数都全，口径与 main._describe_ownership 一致）
            summary = ownership_mod.summarize(steam_path, account_id, play)
            play = filtered
            note = f"library: 已按授权归属过滤：本账号自己拥有 {summary['owned']} 款"
            played_shared = summary.get("played_shared", 0) or 0
            if played_shared:
                note += f"；另有 {played_shared} 款来自家庭共享但本账号玩过，已保留"
            # 被剔除的是两类，必须分开报：`dropped` 是「两者之和」，
            # 直接拿它当「家庭共享且未玩过」会把 Steam 残留痕迹算错（实测 33 = 31 + 2）。
            shared = summary.get("shared", 0) or 0
            if shared:
                note += f"；还有 {shared} 款家庭共享且未玩过，未计入"
            trace = summary.get("trace", 0) or 0
            if trace:
                note += f"；另有 {trace} 款是 Steam 残留痕迹，未计入"
            print(note + "。")
    except Exception as exc:                      # 过滤失败绝不中断主流程
        print(f"library: 授权归属过滤失败，已降级为全部保留: {exc}")

    lib: dict[int, dict[str, Any]] = {}
    for appid, info in play.items():
        lib[appid] = {
            "name": "",
            "playtime": int(info.get("playtime", 0)),
            "last_played": int(info.get("last_played", 0)),
            "genres": [], "categories": [], "developers": [],
            "price": 0, "release_date": "",
        }

    # 2. Store 元数据（增量缓存）
    if with_metadata and lib:
        try:
            meta = store_api.fetch_metadata(list(lib.keys()))
            for appid, m in meta.items():
                if appid in lib:
                    _merge_meta(lib[appid], m)
        except Exception as exc:
            print(f"library: 元数据抓取失败，已降级: {exc}")

    # 3. HLTB 时长（增量缓存 + 负缓存）
    if with_hltb and lib:
        try:
            name_map = {a: (lib[a].get("name") or "") for a in lib}
            h = hltb.fill_hltb(name_map)
            for appid, t in h.items():
                lib[appid]["hltb"] = t if isinstance(t, dict) else dict(_EMPTY_HLTB)
            for appid in lib:
                lib[appid].setdefault("hltb", dict(_EMPTY_HLTB))
        except Exception as exc:
            print(f"library: HLTB 获取失败，已降级: {exc}")
            for appid in lib:
                lib[appid]["hltb"] = dict(_EMPTY_HLTB)

    # 4. 成就（可选，三级降级）
    if with_achievements and lib:
        try:
            ach = achievements.fetch_achievements(
                list(lib.keys()), steam_id, api_key,
                account_id=str(account_id or ""), steam_path=steam_path,
            )
            for appid in lib:
                lib[appid]["achievements"] = ach.get(appid)
        except Exception as exc:
            print(f"library: 成就获取失败，已降级: {exc}")
            for appid in lib:
                lib[appid]["achievements"] = None

    return lib

