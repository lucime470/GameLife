"""游戏「授权归属」判定（**全产品唯一口径**）。

背景
----
Steam 家庭库（Family Sharing / Steam Families）会把同一个家庭组里**别人拥有**的游戏
也写进本账号的 ``localconfig.vdf``，于是同一个游戏在多个账号下都出现：
用户原话是「有些游戏这个账号会显示，另一个账号也会显示，但是实际上这个游戏只属于
某一个账号」。具体表现：

  * 主账号 ``10000001`` 的库里有 42 款其实是家庭共享来的（``374320 DARK SOULS III`` /
    ``1245620 ELDEN RING`` / ``1623730 Palworld`` / ``294100 RimWorld`` …），
    一共虚增 **102.8h** 时长；
  * 六张可视化页面 / 族谱 / 性价比榜 / 推荐 / 反向愿望单都跟着虚高。

用户后续补充的规则（A4）：**共享归共享，只要这个账号真的玩过，就要算进这个账号的库。**
于是「家庭共享」里再按**本项目对「已玩」的既有定义**（``playtime > 0``，见
``engine/classifier.py`` 与 ``engine/stats.py``）分两类：

  * ``PLAYED_SHARED``：共享 **且** 玩过（``playtime > 0``）→ **保留**，与自有游戏同权
    参与所有统计；
  * ``SHARED``：共享 **且** 从未玩过（``playtime == 0``）→ **剔除**。

⚠️ 判定「玩过」只看 ``playtime``，**不看** ``last_played``：后者 > 0 只说明启动过、
没累计时长，与项目其它地方（已玩占比 / 吃灰率）的口径不一致，故不采用。

本模块把判定**收敛成一处**，装载入口（``main._load_library`` / ``core.library.load_library``）
调用一次，全产品自动同口径。

权威信号：``apptickets`` 里偏移 28 的 32 位整数
--------------------------------------------
``localconfig.vdf`` → ``UserLocalConfigStore`` → ``apptickets`` → ``{"<appid>": "<hex>"}``。
把 hex 解回字节后，**偏移 28 起的 4 字节小端无符号整数**是这份授权票的归属标记：

  * ``0`` = 本账号自己拥有
  * ``2`` = 家庭共享来的（授权属于家庭组里别人）

本机全部账号 632 条实测只有 ``{0: 369, 2: 263}`` 两种取值，没有第三个值。

独立交叉验证（``steamapps/appmanifest_*.acf`` 的 ``LastOwner``）
-------------------------------------------------------------
``LastOwner`` 是 Steam 自己记的「这份安装属于谁的授权」，与 apptickets 是**两个独立
数据源**。把 SteamID64 换算回 account_id（``steamid64 - 76561197960265728``）后对照：
25 款已安装游戏里 24 款一致（``flag == 0`` ⟺ ``LastOwner == 当前账号``），
唯一不一致的是 ``228980 Steamworks Common Redistributables``（每个账号都有的组件，
不是游戏）。``tests/test_ownership.py`` 里有复跑用的辅助函数。

判定规则（**宁放过不误杀**）
---------------------------
1. ``flag == 0`` → ``owned``（保留）
2. ``flag == 2``（家庭共享）：``playtime > 0`` → ``played_shared``（**保留**）；
   否则 → ``shared``（剔除）
3. 该账号**没有这张票**：本机任何其他账号对它是 ``flag == 0``（也是家庭共享）：
   ``playtime > 0`` → ``played_shared``（**保留**）；否则 → ``shared``（剔除）
4. 该账号没有这张票、本机也**没有任何账号拥有它** —— 这一类历史上被**无条件**当成
   免费保留，于是混进了 Steam 的**残留痕迹**：
   ``playtime > 0`` → ``free``（**保留**，确实玩过）；
   没玩过且是**免费游戏** → ``free``（**保留**）；
   没玩过、**有明确售价**、且 ``Apps`` 段里**只有痕迹（有条目但没有 ``Playtime``）**
   → ``trace``（**剔除**）
5. 票存在但解析失败（hex 坏了 / 长度不足）→ ``free``（**保留**，不猜）

「保留」= ``owned`` + ``played_shared`` + ``free``；``shared`` 与 ``trace`` 会被剔除。

残留痕迹（``trace``）是什么
---------------------------
用户原话：「为什么会有赛博朋克，这个游戏我既没有玩过，我的家庭库里好像也没有」。
实测 ``1091500 Cyberpunk 2077`` / ``1448440 Wo Long`` 这类条目：既没有授权票、本机
任何账号都没有它的 ``flag == 0``、也从未玩过；它们只是 ``localconfig.vdf`` 的 ``Apps``
段里一条「启动过 / 有 EULA / 有云存档」的记录（``1091500`` 只有 EULA + 云存档，
``1448440`` 连 ``Playtime`` 都没有，只有 ``autocloud``）。

不能简单「无票就剔除」：**免费游戏本来就没有授权票**（``230410 Warframe`` /
``438100 VRChat`` / ``1830840 The Planet Crafter: Prologue`` 都是 ``is_free``），
剔除它们会误伤真实库。所以判据必须是**组合条件**（T4 起两组都要成立）：
**没票 + 没人拥有 + 没玩过 + 有明确售价 + ``Apps`` 段有条目但没有 ``Playtime``** 才剔除。

「有明确售价」取 ``data/cache.db`` 的 ``price > 0`` 且 ``is_free`` 不为真、genres 里也没有
``Free to Play`` / ``免费开玩``；「只是一个痕迹」取 ``localconfig.vdf`` 的
``Software.Valve.Steam.Apps`` 段里**有条目但没有 ``Playtime``**。**两处任一查不到、
或任一不成立，一律保留** —— 绝不在信息不足时剔除。

为什么还要加第二条判据（**为未来加固**，当前数据上结果完全一样）
--------------------------------------------------------------
实测（本机 11 个账号全量）：只走规则 4 的条目共 25 条，「有明确售价」判出 4 条痕迹，
再加上第二条后**仍然恰好是那 4 条** —— 两条判据在当前数据上零分歧，所以这是纯加固，
不改变任何现有判定。**意义全在未来**：价格表是**外部抓来的**，本来就有对有错
（实测 ``644560 Mirror`` / ``1293830 Forza Horizon 4`` / ``6550 DMC3`` / ``246620
Plague Inc`` 记 ``price=0`` 却是付费；反向的 ``1515950`` 记 ``price=29800`` 却标着
「免费开玩」；本机还有 17 个 ``is_free`` 的免费游戏其实持有授权票）。要求「有明确售价」
之外**还必须看到 Apps 段里只有痕迹**，等于把「可能错的外部数据」升级成「必须有本机
正面证据」，将来价格表更乱也不会静默删掉用户的游戏 —— 与模块「宁放过不误杀」一致。

⚠️ 第二条**不能单独用**：本机实测单用它判 25 条，多出的 21 条里大量是 ``760 Steam`` /
``241100`` 这种「Apps 段只有一条 cloud 记录」的免费 / 非游戏条目。**两条都要**才是三个
候选判据里最保守的 —— 它是「纯价格」与「纯 Apps 痕迹」两个集合的**交集**，删除范围
严格不大于任一单独判据。详见 :func:`untimed_appids`。

为什么放在 core 层
------------------
与 :mod:`core.non_game` 同理：``core`` 是最底层，``engine`` 与 ``webapp`` 都依赖它；
唯一口径必须落在 core，否则 core 依赖上层或 engine/webapp 各留一份实现。

**只读**：本模块只读 ``localconfig.vdf`` 与 ``appmanifest_*.acf``，绝不写任何 Steam 文件。
任何异常都降级为「保留全部」，绝不把用户的库清空。

为什么还需要 Steam 客户端的 ``AccountOwnedApps`` 缓存（T12 线）
--------------------------------------------------------------
``apptickets`` 并不包含**全部**自有游戏：本机实测 ``10000002`` 的票面只有 **104 条**
（``flag=0`` 67 条），而 Steam 客户端自己的「该账号拥有」清单有 **139 条** ——
多出来的 79 条（``555950``/``567640`` 弹丸论破、``812140``/``582160``/``2208920``/``911400``
刺客信条、``1174180`` RDR2、``678960`` CODE VEIN …）既在客户端库里、又被 Steam 认作自有，
却**不在票面里**，于是被本模块误判成「无票」。

权威来源是客户端自己的 ``AccountOwnedApps``（``store.steampowered.com/dynamicstore/userdata``
的返回，Steam 把它缓存在 ``%LOCALAPPDATA%\\Steam\\htmlcache`` 的 CEF cache 里）。
它的语义经本机实测校准为**「本账号自有 + 免费开玩」**：

  * 与 ``flag == 0`` 高度一致（``10000002``：139 条里 60 条是 ``flag=0``）；
  * **不含**任何 ``flag == 2``（家庭共享）条目 —— ``1245620`` 艾尔登法环 / ``1086940``
    博德之门3 / ``553850`` 绝地潜兵2 都不在里面，正是「判定共享」所需的负面证据；
  * **不含**残留痕迹（``1091500`` / ``282070`` / ``397540`` / ``1448440`` 都不在）。

因此它既能补上「真拥有但票面没写」的漏网之鱼，又不会把家庭库 / 残留痕迹错认成自有，
是这三类判定里**唯一同时满足两个方向的**本机证据。读不到（客户端没跑过 / 缓存被清）
时**照旧只用票面**，绝不因为读不到就改判。

数据到底怎么读（T12 首版在这里踩过坑，记下来免得再犯）
------------------------------------------------------
缓存里存的是**被反复 JSON 转义过的文本**，把反斜杠全去掉之后长这样::

    "queryKey":["AccountOwnedApps",10000002] ...
    "setOwnedApps":{"_t":9,"v":[95400,222940,...,567640]}

两个关键事实（都是实测确认，不是推测）：

  * 账号 id 在 ``queryKey`` 里**不带引号**。首版按 ``AccountOwnedApps","<id>"`` 去匹配
    （那是 API 的写法，不是缓存里的写法），于是**恒不命中**、整个功能静默失效；
  * 清单本体挂在 ``setOwnedApps`` 字段下。同一份 blob 里还有一个体积几乎一样的
    ``setFreeApps`` —— 所以**必须用字段名锚定**，不能退化成「找最近的
    ``_t":9`` 大数组」（本机实测 ``setFreeApps`` 有 139 条，一旦拿错就是灾难）。

一份 cache 只属于一个账号：按位置把 ``AccountOwnedApps,<id>`` 标记与清单关联（取清单
之前**最近**的那个标记）。**找不到标记就丢弃**，绝不猜成某个账号的自有 —— 宁可漏补
「真拥有」，也不能把家庭共享错认成自有。
"""

from __future__ import annotations

import logging
import os
import re
import struct
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

logger = logging.getLogger(__name__)

# account_id（Steam32）与 SteamID64 之间的固定偏移；appmanifest 的 LastOwner 是 SteamID64
STEAM64_BASE = 76561197960265728

# apptickets 值（hex 串）里授权标记的字节偏移：偏移 28 起的 4 字节小端无符号整数
LICENSE_FLAG_OFFSET = 28

# 授权标记取值
LICENSE_SELF = 0      # 本账号自己拥有
LICENSE_SHARED = 2    # 家庭共享（授权属于家庭组里别人）

# classify() 的五类结果
OWNED = "owned"                  # 自己拥有 -> 保留
PLAYED_SHARED = "played_shared"  # 家庭共享、但本账号玩过（playtime > 0）-> 保留
SHARED = "shared"                # 家庭共享、且从未玩过 -> 剔除
FREE = "free"                    # 免费 / 试玩 / 未知 / 票解不出 -> 保留
TRACE = "trace"                  # 无票、无人拥有、没玩过、有明确售价、Apps 段只有痕迹 -> 剔除

# 会被剔除的类别：「共享且未玩过」与「Steam 残留痕迹」
# 「保留」= OWNED + PLAYED_SHARED + FREE
EXCLUDED_KINDS = frozenset({SHARED, TRACE})

# 「已玩」的唯一口径：与 engine/classifier.py:160、engine/stats.py:254 完全一致
PLAYED_THRESHOLD = 0


def is_played(playtime: Any) -> bool:
    """本项目的「已玩」定义：``playtime > 0``（分钟）。

    与 :mod:`engine.classifier`（``play_time <= 0 -> 未玩``）和 :mod:`engine.stats`
    （``played_count = sum(1 for minutes in ... if minutes > 0)``）**同一口径**，
    绝不另立标准（``last_played`` 不参与判定）。
    """
    try:
        return int(playtime) > PLAYED_THRESHOLD
    except (TypeError, ValueError):
        return False

_LOCK = threading.Lock()
# (localconfig.vdf 路径, (mtime_ns, size)) -> {appid: flag|None}
_TICKET_CACHE: dict[tuple, dict[int, int | None]] = {}
# (userdata 路径, 该目录下所有 vdf 的指纹) -> 本机「有人自己拥有」的 appid 集合
_OWNER_CACHE: dict[tuple, frozenset[int]] = {}
# (localconfig.vdf 路径, (mtime_ns, size)) -> {appid: Apps 段里有没有 Playtime 字段}
_APP_PLAYTIME_CACHE: dict[tuple, dict[int, bool]] = {}
# (CEF cache 根, account_id, 目录指纹) -> Steam 客户端缓存的自有 appid 集合（None = 读不到）
_CLIENT_OWNED_CACHE: dict[tuple, "frozenset[int] | None"] = {}
# (CEF cache 根, 目录指纹) -> {账号 id: 自有 appid 集合}（**一次扫盘**的结果，T12 修正）
_CLIENT_SCAN_CACHE: dict[tuple, dict[str, frozenset[int]]] = {}
# (librarycache 目录, 指纹) -> 该账号**库索引**里的 appid 集合（T13 线）
_LIBRARY_INDEX_CACHE: dict[tuple, frozenset[int]] = {}
# (userdata 目录, 全部 vdf 指纹) -> 本机所有账号 apptickets 的 appid 并集（T13 线）
_TICKET_KEYS_CACHE: dict[tuple, frozenset[int]] = {}
# (librarysharing 日志路径, 指纹) -> 「本机从家庭组**远端成员**借来的」appid 集合（T14 线）
_BORROWED_CACHE: dict[tuple, frozenset[int]] = {}
# (steam 路径, 本账号, 其余账号的库指纹) -> 本机**别的**账号「库里可见」的 appid 并集（T18 线）
_PEER_LIBRARY_CACHE: dict[tuple, frozenset[int]] = {}
# (steam 路径, 本账号, 其余账号的票面指纹) -> 本机**别的**账号「确实拥有」的 appid 并集（T18 线）
_PEER_OWNED_CACHE: dict[tuple, frozenset[int]] = {}
# (steam 路径, 本账号) -> Steam 客户端**实时归属**快照（T22 线）；None = 信息不足
_LIVE_CACHE: dict[tuple, "dict | None"] = {}


# ------------------------------------------------------------------ 票解析

def license_flag(hex_str: Any) -> int | None:
    """从 ``apptickets`` 的 hex 串里解出授权标记；解不出返回 ``None``。

    ``0`` = 本账号自己拥有，``2`` = 家庭共享来的。长度不足 32 字节 / hex 非法 / 非字符串
    一律返回 ``None``（调用方按规则 4 保留，绝不当成「家庭共享」误杀）。
    """
    try:
        raw = bytes.fromhex(str(hex_str))
    except (TypeError, ValueError):
        return None
    if len(raw) < LICENSE_FLAG_OFFSET + 4:
        return None
    return struct.unpack_from("<I", raw, LICENSE_FLAG_OFFSET)[0]


def account_id_from_steam64(steam_id64: Any) -> int | None:
    """SteamID64 → account_id（Steam32）。非法输入返回 ``None``。"""
    try:
        value = int(steam_id64)
    except (TypeError, ValueError):
        return None
    if value <= STEAM64_BASE:
        return None
    return value - STEAM64_BASE


def _localconfig_path(steam_path: Any, account_id: Any) -> Path:
    return Path(str(steam_path)) / "userdata" / str(account_id) / "config" / "localconfig.vdf"


def _file_fingerprint(path: Path) -> tuple[int, int] | None:
    """文件的 (mtime_ns, size)；读不到返回 None。"""
    try:
        st = path.stat()
    except OSError:
        return None
    return (int(st.st_mtime_ns), int(st.st_size))


def ticket_flags(steam_path: Any, account_id: Any) -> dict[int, int | None] | None:
    """该账号 ``apptickets`` 的 ``appid -> 授权标记``；读不到 vdf 返回 ``None``（降级）。

    标记为 ``None`` 表示「票在、但解不出」——上层按规则 4 保留，**不会**退化成
    「无票」再去套规则 3。
    """
    if not str(steam_path or "").strip() or not str(account_id or "").strip():
        return None
    path = _localconfig_path(steam_path, account_id)
    fingerprint = _file_fingerprint(path)
    if fingerprint is None:
        return None
    key = (str(path), fingerprint)
    with _LOCK:
        hit = _TICKET_CACHE.get(key)
    if hit is not None:
        return hit

    try:
        import vdf

        with open(path, "r", encoding="utf-8-sig") as handle:
            data = vdf.load(handle)
        store = data.get("UserLocalConfigStore") or {}
        # vdf.load 对**损坏**的文件未必报错（可能解析成残缺 dict）。节点直接缺失说明这份
        # vdf 没读全，此时按「读不到」降级 —— 否则规则 3 会在一份残文件上误杀。节点存在
        # 但为空（新账号）是合法的，照常走规则 3。
        tickets = store.get("apptickets")
        if not isinstance(tickets, Mapping):
            logger.warning("localconfig.vdf 里没有 apptickets 节点（%s），按降级处理", path)
            return None
    except Exception as exc:                      # 缺依赖 / 文件被写坏 / 权限：一律降级
        logger.warning("读取 apptickets 失败（%s）：%s", path, exc)
        return None

    flags: dict[int, int | None] = {}
    if isinstance(tickets, Mapping):
        for appid_key, ticket_hex in tickets.items():
            try:
                appid = int(appid_key)
            except (TypeError, ValueError):
                continue
            flags[appid] = license_flag(ticket_hex)
    with _LOCK:
        _TICKET_CACHE[key] = flags
    return flags


def read_ownership(steam_path: Any, account_id: Any) -> dict:
    """解析该账号的 apptickets。

    返回 ``{"owned": set[int], "shared": set[int], "available": bool}``：
    ``shared`` 只含「本账号标为 2」的（规则 2）；``available=False`` 表示读不到 vdf，
    调用方必须**保留全部**。
    """
    flags = ticket_flags(steam_path, account_id)
    if flags is None:
        return {"owned": set(), "shared": set(), "available": False}
    owned = {appid for appid, flag in flags.items() if flag == LICENSE_SELF}
    shared = {appid for appid, flag in flags.items() if flag == LICENSE_SHARED}
    return {"owned": owned, "shared": shared, "available": True}


# ------------------------------------------------------------------ Apps 段 Playtime 查询
#
# 这是残留痕迹判据（规则 4）的**第二条依据**：`localconfig.vdf` → `Software.Valve.Steam.Apps`
# 里**有条目但没有 `Playtime` 字段**，说明 Steam 只留下过启动 / EULA / 云存档这类痕迹，
# 从没给这个 appid 记过累计时长。
#
# 为什么在「有明确售价」之外还要加这一条
# --------------------------------------
# 本机 11 个账号实测：只走规则 4 的条目共 25 条，「纯价格」判 4 条、再加上本条仍恰好 4 条
# —— **零分歧**，所以对当前行为没有任何改变。意义全在未来：价格是**外部抓来的数据**，
# 已知有对有错（`644560` / `1293830` / `6550` / `246620` 记 0 却实为付费；反向的
# `1515950` 记 29800 却标着「免费开玩」；本机还有 17 个 `is_free` 的免费游戏持有授权票），
# 而「Apps 段里只有启动 / EULA / 云存档痕迹」是**本机自己的正面证据**。
# 要求两条同时成立，等于给外部数据加了一道本机证据的闸门；任何一侧读不到就一律保留，
# 与模块「宁放过不误杀」的既有哲学一致。
#
# ⚠️ 本条**不能单独使用**：单用它在本机判 25 条，多出的 21 条里大量是 `760 Steam` /
# `241100` 这种「Apps 段只有一条 cloud 记录」的免费 / 非游戏条目。两条都要 = 两个集合
# 的交集，是本模块三个候选判据里删除范围最小、最保守的那个。

_APP_PLAYTIME_NODES = ("Software", "Valve", "Steam", "Apps")


def app_playtime_flags(steam_path: Any, account_id: Any) -> dict[int, bool] | None:
    """该账号 ``Software.Valve.Steam.Apps`` 的 ``appid -> 有没有 Playtime 字段``。

    ``True``  = Steam 给这个 appid 记过累计时长（真拥有 / 真玩过）；
    ``False`` = 这个 appid 在 ``Apps`` 段里**只有痕迹**（``cloud`` / ``autocloud`` /
                ``eula`` / ``LastPlayed`` / ``BadgeData`` …，唯独没有 ``Playtime``）；
    字典里**没有**这个键 = ``Apps`` 段根本没有这个条目（信息不足，调用方按保留处理）。
    读不到 vdf / 节点缺失 / 解析失败一律返回 ``None``（降级 = 信息不足）。
    """
    if not str(steam_path or "").strip() or not str(account_id or "").strip():
        return None
    path = _localconfig_path(steam_path, account_id)
    fingerprint = _file_fingerprint(path)
    if fingerprint is None:
        return None
    key = (str(path), fingerprint)
    with _LOCK:
        hit = _APP_PLAYTIME_CACHE.get(key)
    if hit is not None:
        return hit

    try:
        import vdf

        with open(path, "r", encoding="utf-8-sig") as handle:
            data = vdf.load(handle)
        node: Any = data.get("UserLocalConfigStore") or {}
        for name in _APP_PLAYTIME_NODES:
            if not isinstance(node, Mapping):
                node = None
                break
            node = node.get(name)
        # 节点缺失说明这份 vdf 没读全（与 ticket_flags 同一理由）：按「读不到」降级。
        # 否则会在一份残文件上把「读不到 Apps」误当成「Apps 里没条目 / 没有 Playtime」，
        # 从而把真买的游戏判成痕迹。
        if not isinstance(node, Mapping):
            logger.warning("localconfig.vdf 里没有 Software.Valve.Steam.Apps 节点（%s），按降级处理", path)
            return None
        apps = node
    except Exception as exc:                      # 缺依赖 / 文件被写坏 / 权限：一律降级
        logger.warning("读取 Apps 段的 Playtime 失败（%s）：%s", path, exc)
        return None

    flags: dict[int, bool] = {}
    for appid_key, entry in apps.items():
        try:
            appid = int(appid_key)
        except (TypeError, ValueError):
            continue
        flags[appid] = isinstance(entry, Mapping) and "Playtime" in entry
    with _LOCK:
        _APP_PLAYTIME_CACHE[key] = flags
    return flags


def untimed_appids(steam_path: Any, account_id: Any) -> "frozenset[int] | None":
    """**有 ``Apps`` 条目但没有 ``Playtime``** 的 appid 集合（= 痕迹候选）；读不到返回 ``None``。

    只作为规则 4 的**第二条依据**，必须与 :func:`paid_appids` 同时成立才判 ``trace``
    （交集 = 本模块最保守的判据）。本机 11 账号实测它与纯价格判据**零分歧**，
    对当前行为没有改变，价值在「给外部价格数据加一道本机证据的闸门」。

    ``None`` 表示**信息不足**，调用方必须按「全部保留」处理
    （与 :func:`paid_appids` 同一约定）。自己用 :func:`app_playtime_flags` 的缓存，
    所以对同一账号的重复调用不会重读 vdf。
    """
    flags = app_playtime_flags(steam_path, account_id)
    if flags is None:
        return None
    return frozenset(appid for appid, has_playtime in flags.items() if not has_playtime)


# ------------------------------------------------------------------ 本机「有人自己拥有」的集合

def _userdata_dir(steam_path: Any) -> Path | None:
    text = str(steam_path or "").strip()
    if not text:
        return None
    return Path(text) / "userdata"


def scan_local_owners(steam_path: Any) -> frozenset[int]:
    """``<Steam 安装目录>\\userdata\\*`` 里**任何一个账号** ``flag == 0`` 的 appid 并集（规则 3 用）。

    **不限家庭组**：本机实测 ``FamilyGroup`` 里出现过互相矛盾的过期数据
    （``1731285328`` 与 ``10000002`` 对同一款游戏的说法不一致），
    所以只信 apptickets 票面，不信 FamilyGroup 名单。
    目录不存在 / 全部读不到 → 空集（等于「谁都没有」，规则 3 不剔除任何东西）。

    T12 线起并入**所有**本机账号 :func:`client_owned_appids` 的并集：票面漏掉的「真拥有」
    条目若被算作「谁都没有」，规则 3 会把**别人真拥有的游戏**判成 ``SHARED`` 而误剔。
    客户端清单读不到（``None``）时只贡献票面，绝不因此少收集。

    ⚠️ 客户端清单**只扫盘一次**（:func:`_local_client_owned`）：首版按账号逐个调用
    :func:`client_owned_appids`，每次都要把 5300+ 个候选文件读一遍（441 MB），本机实测
    11 个账号共 **107 秒**；改成单次扫盘后约 **3 秒**。结果与逐账号取并集完全一致。
    """
    base = _userdata_dir(steam_path)
    if base is None:
        return frozenset()
    try:
        accounts = sorted(p.name for p in base.iterdir() if p.is_dir())
    except OSError as exc:
        logger.warning("扫描 userdata 失败（%s）：%s", base, exc)
        return frozenset()

    stamp: list[tuple[str, tuple[int, int] | None]] = [
        (acc, _file_fingerprint(_localconfig_path(steam_path, acc))) for acc in accounts
    ]
    key = (str(base), tuple(stamp))
    with _LOCK:
        hit = _OWNER_CACHE.get(key)
    if hit is not None:
        return hit

    # 客户端自有清单：**一次**扫盘拿到「账号 -> 自有集合」的表，再逐账号并入
    client_by_account = _local_client_owned(_client_cache_root())
    owners: set[int] = set()
    for account in accounts:
        flags = ticket_flags(steam_path, account)
        if flags:
            owners.update(appid for appid, flag in flags.items() if flag == LICENSE_SELF)
        # 票面读不到也要并一遍客户端清单：票可能只是这份 vdf 没写全
        client = client_by_account.get(account)
        if client:
            owners.update(client)
    result = frozenset(owners)
    with _LOCK:
        _OWNER_CACHE[key] = result
    return result


# ------------------------------------------------------------------ 库索引（librarycache）
#
# 为什么还需要它
# --------------
# ``apptickets`` 只覆盖 Steam **给这份安装写过授权票**的游戏：买过但从没装过 /
# 从没启动过的作品根本不会出现在票面里。用户点名的那一类正是如此 ——
# 「明明是拥有的却显示未拥有，比如战地系列」。
#
# 本机实测（``10000001``）：库里 467 条，``apptickets`` 只有 348 条。
# ``1238840 战地1`` / ``24960 战地：叛逆连队2`` 这类「买过、有成就 / 商店缓存
# / 库索引，却没有票」的条目一共 333 条，其中 330 条还因为「没票 + 本机没人拥有」
# 被旧口径当成免费条目保留，于是族谱只能靠 ``playtime > 0`` 判「玩过」。
#
# 数据在哪
# --------
# ``<steam>\\userdata\\<account_id>\\config\\librarycache\\<appid>.json`` —— Steam
# 客户端**库页面**的每游戏缓存。本机实测它与客户端权威自有清单
# （:func:`client_owned_appids` 的 ``AccountOwnedApps``）方向一致但**都不是全集**：
# 未安装的自有游戏只有 ``librarycache`` 有，装过还没下载完的只有 ``AccountOwnedApps``
# 有。两个来源**取并集**才最接近「真实拥有」。
#
# 判定口径（**宁放过不误杀**）
# ----------------------------
# 单独一个来源都会有假阳性，所以库索引在 :func:`classify` 里被排到**规则 1/2/3/5
# 全部之后**，只捡票面口径一条都没认领的条目，并且还要满足两道闸门：
#   * 本账号 ``apptickets`` 里**没有**这张票（有票就在前面分流了，绝不覆盖）；
#   * 本机**任何**账号都没有这张票（任何账号见过 -> 交给票面口径；本机实测这一条
#     挡掉 79 条）。
# 再加上「规则 3（本机有人拥有）优先于库索引」的排序，本机实测本账号的 352 条库索引
# 候选里 20 条命中本账号客户端清单、32 条命中他人客户端清单，全部被前面的规则拦下，
# 只有 332 条真走库索引分支 —— 与 ``AccountOwnedApps`` 的并集就是规则 1 的补充来源。
#
# 本模块只做**只读、纯本地**的目录列举：不联网、不写任何文件、绝不抛异常。
# 读不到就返回 ``None``（降级），上层照旧走原来的票面判定 —— 绝不因为读不到而改判。

def _library_cache_dir(steam_path: Any, account_id: Any) -> Path | None:
    """该账号 ``librarycache`` 目录；账号非法 / 目录不存在返回 ``None``。"""
    account = _clean_account_id(account_id)
    text = str(steam_path or "").strip()
    if account is None or not text:
        return None
    directory = Path(text) / "userdata" / account / "config" / "librarycache"
    return directory if directory.is_dir() else None


def _library_cache_file_appids(directory: Path) -> frozenset[int]:
    """目录里 ``<appid>.json`` 的 appid 集合（**不读文件内容**，只列举目录项）。"""
    found: set[int] = set()
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                name = entry.name
                if not name.endswith(".json"):
                    continue
                stem = name[:-5]
                if stem.isdigit():
                    found.add(int(stem))
    except OSError as exc:
        logger.debug("扫描 librarycache 失败（%s）：%s", directory, exc)
    return frozenset(found)


def library_index_appids(steam_path: Any, account_id: Any) -> "frozenset[int] | None":
    """该账号 Steam 客户端**库索引**（``librarycache``）里的 appid 集合；读不到返回 ``None``。

    ``None`` = 信息不足（目录不存在 / 账号是合流库 ``all`` / 读盘失败），调用方必须
    保持原有判定不变。结果按 (目录, 指纹) 缓存 —— 目录项变化（装新游戏 / 清缓存）会自动失效。
    """
    directory = _library_cache_dir(steam_path, account_id)
    if directory is None:
        return None
    fingerprint = _file_fingerprint(directory)
    key = (str(directory), fingerprint)
    with _LOCK:
        hit = _LIBRARY_INDEX_CACHE.get(key)
    if hit is not None:
        return hit
    result = _library_cache_file_appids(directory)
    with _LOCK:
        _LIBRARY_INDEX_CACHE[key] = result
    return result


_SHARING_LINE_RE = re.compile(
    r"(?:Shared lock for app|Launching shared app)\s+(\d+)\s*[:\-]?\s*(.*)$"
)
_SHARING_OWNER_RE = re.compile(r"owner\s+(\d+)")


def _local_account_ids(steam_path: Any) -> frozenset[str]:
    """``userdata`` 下的账号目录名集合；读不到返回空集。"""
    base = _userdata_dir(steam_path)
    if base is None:
        return frozenset()
    try:
        return frozenset(p.name for p in base.iterdir() if p.is_dir())
    except OSError as exc:
        logger.debug("扫描 userdata 失败（%s）：%s", base, exc)
        return frozenset()


def local_ticket_keys(steam_path: Any) -> frozenset[int]:
    """本机**所有**账号 ``apptickets`` 的 appid 并集（不看 flag）；读不到返回空集。

    与 :func:`scan_local_owners` 的区别：这里只要「这张票存在」，用来判断
    「某个 appid 是不是本机任何账号都从没见过」。空集 = 没有可用信息，
    调用方必须按保守方向处理（不据此改动任何判定）。
    """
    base = _userdata_dir(steam_path)
    if base is None:
        return frozenset()
    try:
        accounts = sorted(p.name for p in base.iterdir() if p.is_dir())
    except OSError as exc:
        logger.debug("扫描 userdata 失败（%s）：%s", base, exc)
        return frozenset()
    stamp = tuple(
        (account, _file_fingerprint(_localconfig_path(steam_path, account)))
        for account in accounts
    )
    key = (str(base), stamp)
    with _LOCK:
        hit = _TICKET_KEYS_CACHE.get(key)
    if hit is not None:
        return hit
    keys: set[int] = set()
    for account in accounts:
        flags = ticket_flags(steam_path, account)
        if flags:
            keys.update(flags)
    result = frozenset(keys)
    with _LOCK:
        _TICKET_KEYS_CACHE[key] = result
    return result


def _peer_accounts(steam_path: Any, account_id: Any) -> list[str]:
    """本机 ``userdata`` 下**除本账号之外**的账号 id（排序；账号非法返回空）。"""
    mine = _clean_account_id(account_id)
    if mine is None:
        return []
    return sorted(a for a in _local_account_ids(steam_path) if a != mine)


def peer_library_appids(steam_path: Any, account_id: Any) -> frozenset[int]:
    """本机**别的**账号「库里可见」的 appid 并集（库索引 ∪ 客户端自有清单）。

    为什么需要它（T18 线，用户第八次反馈）
    ------------------------------------
    ``librarycache`` 是 Steam 客户端**库页面**的视图，而库页面把**家庭库里别人拥有
    的游戏**和你自己的游戏混在一起显示 —— 它**不等于自有清单**。上一轮的
    :func:`library_owned_appids` 只减了「本机所有账号的票面」与「远端借用日志」，
    漏了这道：实测主账号 ``10000001`` 的库索引 467 条里有 105 条同时出现在**本机
    别的账号**的库索引里；其中 12 条本机任何账号都没有它的授权票，于是被当成
    「买过、只是没写票」的自有游戏 —— 用户点名的 ``412020 地铁：离乡`` /
    ``379720 DOOM`` / ``1057090 奥日与萤火意志`` 正是这一类。

    判据是直白的：**真属于自己的游戏不会出现在别人的库里**。同一个 appid 出现在
    本机另一个账号的库视图中，就是「它属于别人」的直接证据。

    ⚠️ 只有当本账号**没有** Steam 客户端权威自有清单
    （:func:`client_owned_appids`）时才使用它 —— 有了账号级的权威清单就直接取交集，
    不必（也不该）拿别人的库来减。本机实测只有 ``10000002`` 留下了权威清单。

    ``account_id`` 非法 / 本机只有这一个账号 -> 空集（= 没有否决信息，绝不改判）。
    结果按 (本账号, 其余账号的库指纹) 缓存；目录项变化会自动失效。
    """
    peers = _peer_accounts(steam_path, account_id)
    if not peers:
        return frozenset()
    stamp = tuple(
        (account, str(directory), _file_fingerprint(directory) if directory else None)
        for account in peers
        for directory in (_library_cache_dir(steam_path, account),)
    )
    key = (str(steam_path), str(_clean_account_id(account_id) or ""), stamp)
    with _LOCK:
        hit = _PEER_LIBRARY_CACHE.get(key)
    if hit is not None:
        return hit
    found: set[int] = set()
    for account in peers:
        index = library_index_appids(steam_path, account)
        if index:
            found.update(index)
        client = client_owned_appids(steam_path, account)
        if client:
            found.update(client)
    result = frozenset(found)
    with _LOCK:
        _PEER_LIBRARY_CACHE[key] = result
    return result


def free_appids(db_path: Any = None) -> "frozenset[int] | None":
    """``data/cache.db`` 里**明确是免费游戏**的 appid 集合；读不到返回 ``None``。

    「免费」= ``is_free`` 为真，**或** genres 里有免费开玩标记（``免费开玩`` /
    ``free to play``）。这与 :func:`paid_appids` 严格互补，两份数据来自同一次库读取。

    用途（T15 线）：**免费游戏**（CS2 / Apex / 彩虹六号 / 命运2 / NGU 放置 …）没有属于自己的
    授权票 —— Steam 会把它们在任何账号下都写成 ``flag == 2``（家庭共享）+ ``playtime == 0``。
    只看票面就会把它们当「别人借给你的、你还没玩过」而**剔除**，于是免费游戏整批从游戏明细
    里消失。有这张权威表，:func:`classify` 才能把「真共享的付费游戏」和「自己的免费游戏」分开。

    ``None`` 表示**读不到**，调用方必须保持原有判定不变，绝不据此剔除任何条目。
    """
    path = Path(str(db_path)) if db_path else _cache_db_path()
    if path is None:
        return None
    try:
        stat = path.stat()
        key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError as exc:
        logger.debug("cache.db 不可用（按信息不足处理）：%s：%s", path, exc)
        return None
    with _LOCK:
        if key in _FREE_CACHE:
            return _FREE_CACHE[key]

    result: "frozenset[int] | None" = None
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT appid, price, is_free, genres, genres_cn FROM games"
            ).fetchall()
        finally:
            conn.close()
        free: set[int] = set()
        for row in rows:
            appid = _as_int(row[0])
            if appid is None:
                continue
            price, is_free = row[1], row[2]
            genres = f"{row[3] or ''} {row[4] or ''}".lower()
            if is_free in (True, 1, "1", "true", "True"):
                free.add(appid)                     # 商店明确标免费
                continue
            # 「免费开玩」标记 **且** 没有明确售价才算 —— 本机已知反例 `1515950 卡普空街机合集`
            # （库里有 `29800` 的售价却被标了「免费开玩」，见变更记录第六节的错值勘误）。
            # 不加这道价格闸门，一款付费游戏会被当免费放回明细，属于「多留」而非「误杀」，
            # 但仍然违背「宁可少留也不失真」的取舍，所以一并挡掉。
            if (_as_int(price) or 0) <= 0 and any(
                marker in genres for marker in _FREE_GENRE_MARKERS
            ):
                free.add(appid)                     # 免费开玩
        result = frozenset(free)
    except Exception as exc:                        # 表缺失 / 库损坏 / 权限：降级
        logger.warning("读取 cache.db 免费标记失败（按信息不足处理）：%s：%s", path, exc)
        result = None

    with _LOCK:
        _FREE_CACHE[key] = result
    return result


def borrowed_appids(steam_path: Any) -> frozenset[int]:
    """本机**从家庭组远端成员借来**的 appid（依据 ``logs/librarysharing_log.txt``）。

    读不到日志 / 日志里没有相关记录 -> 空集（= 没有否决信息，绝不改判任何条目）。
    """
    text = str(steam_path or "").strip()
    if not text:
        return frozenset()
    root = Path(text)
    path = root / "logs" / "librarysharing_log.txt"
    fingerprint = _file_fingerprint(path)
    if fingerprint is None:
        return frozenset()
    key = (str(path), fingerprint)
    with _LOCK:
        hit = _BORROWED_CACHE.get(key)
    if hit is not None:
        return hit

    local = _local_account_ids(root)
    owners: dict[int, set[str]] = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _SHARING_LINE_RE.search(line)
                if not match:
                    continue
                found = [o for o in _SHARING_OWNER_RE.findall(match.group(2)) if o != "0"]
                if found:
                    owners.setdefault(int(match.group(1)), set()).update(found)
    except OSError as exc:                      # 读不到 = 没有否决信息
        logger.debug("读取家庭共享日志失败（%s）：%s", path, exc)
        owners = {}

    borrowed = frozenset(
        appid for appid, who in owners.items() if who and not (who & local)
    )
    with _LOCK:
        _BORROWED_CACHE[key] = borrowed
    return borrowed


def library_owned_appids(steam_path: Any, account_id: Any) -> "frozenset[int] | None":
    """规则 1 的**第二个补充来源**：库索引里**确实还属于本账号**的自有条目。

    ``None`` = 信息不足（库索引读不到），调用方必须**保持原有判定不变**。

    闸门缺一不可：

    1. 减去「本机所有账号的票面并集」（T13）—— 有票的条目早在规则 1/2/3 分流了；
    2. 减去 :func:`borrowed_appids`（T14）—— 库索引里**混着家庭共享来的游戏**，
       而共享方可能是家庭组里的**远端成员**（本机没有它的 ``userdata``），
       「本机无票」根本挡不住。用户点名的 ``24960 战地：叛逆连队2`` 正是这一类；
    3. 减去 :func:`peer_library_appids`（T18）—— 库索引是**库页面视图**，
       本机**别的账号**能看见的家庭库游戏也在里面。用户第八次反馈的
       「还是把家庭库里的游戏算作当前账号的游戏」就是缺了这道闸门：
       实测主账号 467 条库索引里有 105 条同时出现在别的账号的库里，其中 12 条
       本机无票 —— 于是 ``412020 地铁：离乡`` / ``379720 DOOM`` 被当成自有。

    Steam 客户端留下**权威自有清单**（:func:`client_owned_appids`）时口径最强：
    它是该账号自有的**全集**，直接与之**取交集**即可 —— 既能补全「买过没写票」的
    自有游戏，又能剔掉**已退款**的残留（用户点名的 ``413410 弹丸论破 1``），
    还天然排除家庭库（清单不含任何 ``flag == 2``）。此时**不再**套用第 3 道闸门：
    账号级权威证据优先于拿别的账号做间接推断。

    只有拿不到权威清单时，才退回「三道减法」，并且第 3 道只减
    **本机别的账号的库视图**（不含本账号自己的）。任一来源读不到都只是降级，
    绝不因为信息不足剔条目。
    """
    index = library_index_appids(steam_path, account_id)
    if index is None:
        return None
    candidates = index - local_ticket_keys(steam_path) - borrowed_appids(steam_path)
    client = client_owned_appids(steam_path, account_id)
    if client is not None:
        return frozenset(candidates & client)
    return frozenset(candidates - peer_library_appids(steam_path, account_id))


# ------------------------------------------------------------------ Steam 客户端的「自有」缓存（T12 线）
#
# 为什么不能只用 apptickets
# -------------------------
# 本机实测：``10000002`` 的 ``apptickets`` 只有 104 条（``flag=0`` 67 条），
# 而 Steam 客户端自己的自有清单有 139 条 —— 相差的 79 条里全是**真正买过、
# 却因为没装过 / 没启动过而没写进票面**的游戏（弹丸论破 V3 / 绝对绝望少女、
# 刺客信条 起源 / 奥德赛 / 英灵殿 / 3 重制版、RDR2、CODE VEIN …）。
# 用户的反馈正是这一条：「我拥有的但是没玩过的还是不显示拥有」。
#
# 数据在哪
# --------
# Steam 客户端把 ``store.steampowered.com/dynamicstore/userdata`` 的返回缓存在
# ``%LOCALAPPDATA%\\Steam\\htmlcache`` 的 CEF cache 里，形如::
#
#     {"queryKey":["AccountOwnedApps","10000002"],
#      "state":{"data":{"_t":9,"v":[95400,222940,...]}}}
#
# 本模块只做**只读、纯本地**的解析：不联网、不写任何文件、绝不抛异常。
# 缓存不一定存在（客户端没跑过 / 被清理 / 换机器），读不到就返回 ``None``（降级），
# 上层照旧走原来的票面判定 —— 绝不因为读不到而改判。

# CEF cache 相对 ``%LOCALAPPDATA%\\Steam`` 的位置（按可靠性排序，先看最可能命中的）
_CLIENT_CACHE_DIRS = (
    ("htmlcache", "Default", "Cache", "Cache_Data"),
    ("htmlcache", "Default", "Code Cache", "js"),
)
# 只读这两个目录里**尺寸合理**的条目：真正带清单的那份约 200 KB；
# 过小的不可能是它，过大的（视频 / 字体缓存）读了纯浪费时间。
_CLIENT_CACHE_MIN_BYTES = 200
_CLIENT_CACHE_MAX_BYTES = 8 * 1024 * 1024
# ``{"_t":9,"v":[...]}``：Steam 内部对「整数数组」的序列化形式（``\_t`` 是类型标记）
# 清单本体：``"setOwnedApps":{"_t":9,"v":[<几十个 appid>]}``。
# **必须锚定字段名** —— 同一份 blob 里 ``setFreeApps`` 也是 ``_t":9`` 大数组，
# 只按「最近的整数数组」取会拿错（详见模块文档末节）。
_CLIENT_LIST_RE = re.compile(r'"setOwnedApps":\{"_t":9,"v":\[([0-9][0-9,\s]*)\]')
# ``["AccountOwnedApps",10000002]``（去转义后）里的账号 id：**不带引号**，且
# 引号 / 逗号 / 冒号可能已被去掉，所以只要求后面紧跟少量分隔符。
_CLIENT_ACCOUNT_RE = re.compile(r'AccountOwnedApps["\s,:\]]{0,8}?([0-9]{5,})')
# 清单长度的下限（个 appid）：太短的只是碰巧长得像，不是这份清单
_CLIENT_LIST_MIN_ITEMS = 24

# ``%LOCALAPPDATA%`` 每进程只探一次（探不到也不反复试）
_LOCALAPPDATA: str | None = os.environ.get("LOCALAPPDATA") or ""


def _client_cache_root() -> Path | None:
    """``%LOCALAPPDATA%\\Steam``；环境变量缺失 / 目录不存在返回 ``None``。"""
    if not _LOCALAPPDATA:
        return None
    root = Path(_LOCALAPPDATA) / "Steam"
    return root if root.is_dir() else None


def _client_cache_files(root: Path) -> list[Path]:
    """两个 CEF cache 目录里的候选文件，**按修改时间倒序**（最新的最可能有清单）。"""
    entries: list[tuple[float, Path]] = []
    for parts in _CLIENT_CACHE_DIRS:
        directory = root.joinpath(*parts)
        try:
            with os.scandir(directory) as scan:
                for entry in scan:
                    try:
                        if not entry.is_file():
                            continue
                        info = entry.stat()
                    except OSError:
                        continue
                    if not (_CLIENT_CACHE_MIN_BYTES <= info.st_size <= _CLIENT_CACHE_MAX_BYTES):
                        continue
                    entries.append((info.st_mtime, Path(entry.path)))
        except OSError:
            continue
    entries.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in entries]


def _client_list_for_account(text: str, account_id: str) -> set[int] | None:
    """从**去转义后**的 cache 文本里取属于 ``account_id`` 的自有清单；没有返回 ``None``。

    ``queryKey`` 里的账号 id **不带引号**（``["AccountOwnedApps",10000002]``），
    所以按位置关联：取清单之前**最近**的那个账号标记。标记缺失 / 最近的标记是别人的
    账号 -> ``None``（宁可漏补，也绝不把别人的自有算成你的）。
    """
    marks = [(m.start(), m.group(1)) for m in _CLIENT_ACCOUNT_RE.finditer(text)]
    for match in _CLIENT_LIST_RE.finditer(text):
        owner = None
        for start, value in marks:
            if start < match.start():
                owner = value
            else:
                break
        if owner != account_id:
            continue
        appids = {
            int(token)
            for token in match.group(1).replace(",", " ").split()
            if token.isdigit()
        }
        if len(appids) >= _CLIENT_LIST_MIN_ITEMS:
            return appids
    return None


def _clean_account_id(account_id: Any) -> str | None:
    """规范化账号 id：非空且全数字才认（合流库 ``all`` / ``None`` -> ``None``）。"""
    account = str(account_id or "").strip()
    return account if account.isdigit() else None


def _client_cache_stamp(root: Path) -> tuple:
    """两个 CEF cache 目录的指纹（缓存 key 用，判断要不要重新扫）。"""
    stamp: list[tuple[str, tuple[int, int] | None]] = []
    for parts in _CLIENT_CACHE_DIRS:
        directory = root.joinpath(*parts)
        try:
            fingerprint = _file_fingerprint(directory)
        except Exception:
            fingerprint = None
        stamp.append((str(directory), fingerprint))
    return tuple(stamp)


def _scan_client_cache(root: Path) -> dict[str, frozenset[int]]:
    """**真正扫盘**：返回 ``{账号 id: 该账号自有 appid 集合}``（不缓存、不抛异常）。"""
    found: dict[str, frozenset[int]] = {}
    for path in _client_cache_files(root):
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if b"setOwnedApps" not in blob:
            continue
        try:
            # latin-1 = 无损字节映射：这里只做子串 / 正则，不做文本解码
            text = blob.decode("latin-1").replace("\\", "")
        except Exception:                       # 理论上不会，兜底
            continue
        # 同一份文本里可能有多个账号的清单：每个都收，谁先读到算谁的（不覆盖已有）
        for account in dict.fromkeys(m.group(1) for m in _CLIENT_ACCOUNT_RE.finditer(text)):
            if account in found:
                continue
            appids = _client_list_for_account(text, account)
            if appids is not None:
                found[account] = frozenset(appids)
    return found


def _local_client_owned(root: Path | None) -> dict[str, frozenset[int]]:
    """``{账号 id: 该账号自有 appid 集合}``；整个进程按 cache 指纹只扫**一次**。

    为什么不是「按账号查」：本机 5300+ 个候选文件、合计 441 MB，**完整读一遍约 3 秒**
    （命中即停的最坏情况约 0.4s）。逐账号调用会把这套 I/O 重复 N 遍（本机 11 个账号
    实测 107 秒），而一份 cache 里本来就可能同时留下多个账号的 ``queryKey``。
    """
    if root is None:
        return {}
    key = (str(root), _client_cache_stamp(root))
    with _LOCK:
        hit = _CLIENT_SCAN_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        found = _scan_client_cache(root)
    except Exception as exc:                    # 绝不因为读缓存失败影响主流程
        logger.warning("扫描 Steam 客户端自有清单失败（降级为只用票面）：%s", exc)
        found = {}
    with _LOCK:
        _CLIENT_SCAN_CACHE[key] = found
    return found


def client_owned_appids(steam_path: Any, account_id: Any) -> "frozenset[int] | None":
    """Steam 客户端缓存的「本账号自有 appid」集合；读不到返回 ``None``（降级）。

    语义（本机实测校准，见模块文档「为什么还需要 …」一节）：**本账号自有 + 免费开玩**，
    **不含家庭共享**（无任何 ``flag == 2``）、**不含残留痕迹**。

    ``None`` = 信息不足（客户端没跑过 / 缓存被清理 / 换机器 / 读盘失败），
    调用方必须**保持原有判定不变**，绝不退化成「无票」或「谁都没有」。

    ``account_id`` 为合流库 id（``all``）或空时返回 ``None``：合流库不是真实账号，
    由调用方逐成员取并集（与 :func:`scan_local_owners` 同一约定）。
    结果按 (缓存目录指纹, account_id) 缓存；另外这里的「逐账号」语义会被
    :func:`_local_client_owned` 的单次扫盘缓存兜住，扫盘不会随账号数翻倍。
    """
    account = _clean_account_id(account_id)
    if account is None:
        return None
    root = _client_cache_root()
    if root is None:
        return None
    # 「账号不在缓存里」也要缓存成 None，否则每个查不到的账号都会重扫一遍全盘
    key: tuple = ("client", str(root), account, _client_cache_stamp(root))
    with _LOCK:
        if key in _CLIENT_OWNED_CACHE:
            return _CLIENT_OWNED_CACHE[key]
    try:
        # 走 `_local_client_owned` 的单次扫盘缓存：多账号时只 I/O 一遍
        result = _local_client_owned(root).get(account)
    except Exception as exc:                    # 绝不因为读缓存失败影响主流程
        logger.warning("读取 Steam 客户端自有清单失败（降级为只用票面）：%s", exc)
        result = None
    with _LOCK:
        _CLIENT_OWNED_CACHE[key] = result
    return result


# ---------------------------------------------------------------- Steam 客户端的「实时归属」（T22 线）
#
# 为什么还要这一路
# ----------------
# 静态证据（票面 ``flag == 0`` / CEF 里的 ``AccountOwnedApps`` / 库索引）**都答不了**
# 同一个问题：「``librarycache`` 里的这一条，是本账号买过但没装过的，还是家庭组成员
# 共享给我的？」—— 两者在本机留下的东西一模一样（就一行库索引）。
#
# 本机实测 ``10000001``：467 条库索引里 **207 条其实归家庭组别人**
# （``17480 命令与征服：红色警戒 3`` / ``6850 Hitman 2: Silent Assassin`` …），
# 被 :func:`library_owned_appids` 当成了自有；反过来 ``1238840 战地1`` /
# ``386360 神之浩劫``（真拥有、从没玩过）会被任何「本机痕迹闸门」误杀。
#
# Steam 客户端自己知道答案
# ------------------------
# 客户端（``steamwebhelper.exe``）**默认**带 ``--remote-debugging-port=8080``，
# :mod:`core.steam_live` 通过它调用客户端自己的 ``CFamilyGroups_GetSharedLibraryApps``，
# 直接拿到**每个 appid 的 owner**：权威数据，不是推断，也不含任何硬编码 appid。
#
# 读不到（Steam 没开 / 没登录 / 不在家庭组）一律返回 ``None``，上层**保持原判定**。


def live_ownership(steam_path: Any, account_id: Any) -> "dict | None":
    """本账号的**权威归属快照**（T22 线）；``None`` = 信息不足（降级）。

    返回 ``{"owned": frozenset[int], "others": frozenset[int], "at": int,
    "source": "live"|"cache", "group": str}``：

    * ``owned``  —— Steam 说「这些归本账号」（含从未安装的购买与免费游戏）；
    * ``others`` —— Steam 说「这些归家庭组**别人**」/「不属于任何人」—— 本账号绝不拥有。

    结果按 (steam 路径, 账号) 只问一次；读不到也把 ``None`` 记住，绝不反复连端口。
    ``steam_path`` 不参与查询，只用于缓存分键，可传 ``None``。
    """
    account = _clean_account_id(account_id)
    if account is None:
        return None
    key = (str(steam_path or ""), account)
    with _LOCK:
        if key in _LIVE_CACHE:
            return _LIVE_CACHE[key]
    try:
        from core import steam_live

        result = steam_live.ownership_for(account)
    except Exception as exc:                    # 缺模块 / 端口异常：降级，绝不改判
        logger.debug("读取 Steam 实时归属失败（降级）：%s", exc)
        result = None
    with _LOCK:
        _LIVE_CACHE[key] = result
    return result


def live_owned_appids(steam_path: Any, account_id: Any) -> "frozenset[int] | None":
    """Steam 客户端说「归本账号」的 appid 集合；读不到返回 ``None``（降级）。"""
    snapshot = live_ownership(steam_path, account_id)
    return None if snapshot is None else snapshot["owned"]


def live_other_owned_appids(steam_path: Any, account_id: Any) -> "frozenset[int] | None":
    """Steam 客户端说「归家庭组**别人**」的 appid 集合；读不到返回 ``None``（降级）。

    这是**负面证据**：只有权威清单在手上时，才敢断言「这条不是你的」。
    """
    snapshot = live_ownership(steam_path, account_id)
    return None if snapshot is None else snapshot["others"]


def authoritative_owned_appids(steam_path: Any, account_id: Any) -> "frozenset[int] | None":
    """**权威归属**：本账号确实拥有的 appid（票面 ``flag == 0`` ∪ 客户端清单 ∪ 实时清单）。

    ``None`` = 读不到票面（信息不足）-> 调用方保持原判定、绝不改判。

    这是 ``OWNED`` 的**唯一汇总口径**：:func:`classify`、族谱三态、自动体检都复用它。
    没有实时清单（Steam 没开 / 没登录 / 不在家庭组）时退回旧的
    :func:`library_owned_appids`，行为与 T22 之前**完全一致**。
    """
    try:
        flags = ticket_flags(steam_path, account_id)
    except Exception as exc:                    # 绝不因为读票失败影响主流程
        logger.debug("读取票面失败（权威归属按无票处理）：%s", exc)
        flags = None
    if flags is None:
        return None
    owned = {appid for appid, flag in flags.items() if flag == LICENSE_SELF}
    try:
        client = client_owned_appids(steam_path, account_id)
        if client:
            owned.update(client)
        live = live_owned_appids(steam_path, account_id)
        if live is not None:
            owned.update(live)                  # T22：有权威清单就不再靠库索引推断
        else:
            fallback = library_owned_appids(steam_path, account_id)
            if fallback:
                owned.update(fallback)
    except Exception as exc:
        logger.debug("汇总自有清单失败（已忽略）：%s", exc)
    return frozenset(owned)


# ------------------------------------------------------------------ 「库里应有」补明细（M2f / T16）
#
# 为什么需要它（用户第六次反馈的后续，原话「部分免费游戏都不在游戏明细中」）
# -------------------------------------------------------------------
# 游戏明细是从 ``localconfig.vdf`` 的 ``Software.Valve.Steam.Apps`` 段建的，而那个段
# **只记录 Steam 写过游玩记录的条目**。免费游戏没有属于自己的授权票，Steam 也只在
# **真正启动过**之后才给它写 ``Apps`` 条目 —— 于是「加进库、还没启动」的免费游戏在
# 那份 vdf 里**一条记录都没有**，明细里自然整条不见；可 Steam 客户端的库里能看见它
# （``librarycache`` / ``AccountOwnedApps`` 都有）。
#
# 本机实测缺口（修复前）：``10000002`` 缺 ``544810 KARDS`` / ``1085660 命运2`` /
# ``1399720 反物质维度``；``10000004`` 缺 ``1399720 反物质维度``。
#
# 判据（只用**库页自己的证据**，不猜、不联网、只读）
#   * 「在库里」= 本账号库索引 :func:`library_index_appids` ∪ 客户端自有清单
#     :func:`client_owned_appids`；
#   * 只补**免费游戏**（:func:`free_appids`）—— 免费游戏「没有票」是正常的，
#     这条推理是闭合的；付费游戏「没票 + 没 Apps 条目」则可能是远端共享，证据不足，
#     一律不补（宁缺勿滥）。
#   * 信息不足（读不到库 / 读不到 cache.db）→ 空集，**绝不凭空造条目**。


def _is_non_game(appid: Any) -> bool:
    """是否非游戏条目（薄转发 :mod:`core.non_game`；读不到时按「是游戏」放行）。"""
    try:
        from core import non_game

        return bool(non_game.is_non_game(appid))
    except Exception:
        return False


def library_appids(steam_path: Any, account_id: Any) -> "frozenset[int] | None":
    """本账号「库里有什么」的并集：库索引 ∪ 客户端自有清单；两者都读不到返回 ``None``。

    这两个来源都是 Steam 客户端**库页面**自己的数据，比 ``localconfig.vdf`` 的
    ``Apps`` 段更接近「这个账号的库里有哪些游戏」。
    """
    index = library_index_appids(steam_path, account_id)
    client = client_owned_appids(steam_path, account_id)
    if index is None and client is None:
        return None
    return frozenset((index or frozenset()) | (client or frozenset()))


def owned_library_appids(steam_path: Any, account_id: Any) -> frozenset[int]:
    """本账号**确实拥有**的 appid（票面 ``flag == 0`` ∪ 权威自有清单 ∪ 库索引补全）。

    这是「归属」的**唯一汇总口径**：:func:`classify` 里 ``OWNED`` 的全部来源都在这里，
    免费游戏补充（:func:`missing_free_library_games`）、自动体检（:func:`audit_ownership`）
    与族谱渲染都复用它，避免各处各写一份、again 把「库里可见」误当「自己拥有」。

    ⚠️ 与 :func:`library_appids` 的区别是根本性的：后者只是「库里有什么」
    （含家庭库视图，用于**显示**），这里回答的是「**属于谁**」（用于**归属判定**）。
    用户第八次反馈的两个问题正是把这两者混用导致的。

    信息不足时不猜：票面读不到（``None``）就当作没有这一路来源；其余来源各自尽力。
    绝不抛异常。
    """
    owned: set[int] = set()
    try:
        flags = ticket_flags(steam_path, account_id)
    except Exception as exc:                      # 绝不因为读票失败影响主流程
        logger.debug("读取票面失败（归属汇总按无票处理）：%s", exc)
        flags = None
    if flags:
        owned.update(appid for appid, flag in flags.items() if flag == LICENSE_SELF)
    try:
        client = client_owned_appids(steam_path, account_id)
        if client:
            owned.update(client)
        live = live_owned_appids(steam_path, account_id)
        if live is not None:
            owned.update(live)                  # T22：权威清单优先，库索引推断整条停用
        else:
            library_owned = library_owned_appids(steam_path, account_id)
            if library_owned:
                owned.update(library_owned)
    except Exception as exc:
        logger.debug("汇总自有清单失败（已忽略）：%s", exc)
    return frozenset(owned)


def peer_owned_appids(steam_path: Any, account_id: Any) -> frozenset[int]:
    """本机**别的**账号「确实拥有」的 appid 并集（票面 ``flag == 0`` ∪ 客户端自有清单）。

    免费游戏归属判定的**负面证据**（T18 线）：免费游戏没有属于自己的授权票是常态，
    「在库里」因此不能当拥有的依据；但**本机别的账号持有它的 ``flag == 0``**却是
    「它属于别人」的直接证据。用户第八次反馈的「把别的账号的免费游戏同时算到别的账号中」
    正是缺了这条：实测 ``544810 KARDS`` / ``1085660 命运2`` / ``1399720 反物质维度``
    的 ``flag == 0`` 只落在主账号 ``10000001`` 名下，其余账号本该判「不是自己的」。

    与 :func:`peer_library_appids` 的分工：那个是「别人的库里可见」（含没票的），
    这个只认「别人的票面 / 权威清单」—— 更硬的证据，用于把免费游戏从「我的」里摘出去。

    本机只有这一个账号 / 读不到任何对端证据 -> 空集（= 无从判断，保持原判定，绝不改判）。
    """
    peers = _peer_accounts(steam_path, account_id)
    if not peers:
        return frozenset()
    stamp = tuple(
        (account,
         _file_fingerprint(_localconfig_path(steam_path, account)),
         _client_cache_stamp(_client_cache_root()) if _client_cache_root() else None)
        for account in peers
    )
    key = (str(steam_path), str(_clean_account_id(account_id) or ""), stamp)
    with _LOCK:
        hit = _PEER_OWNED_CACHE.get(key)
    if hit is not None:
        return hit
    found: set[int] = set()
    for account in peers:
        flags = ticket_flags(steam_path, account)
        if flags:
            found.update(a for a, flag in flags.items() if flag == LICENSE_SELF)
        client = client_owned_appids(steam_path, account)
        if client:
            found.update(client)
    result = frozenset(found)
    with _LOCK:
        _PEER_OWNED_CACHE[key] = result
    return result


def missing_free_library_games(
    steam_path: Any, account_id: Any, listed: Iterable[Any] | None = None
) -> frozenset[int]:
    """**确实属于本账号、是免费游戏、却没出现在明细里**的 appid（自动识别，不用人点名）。

    判据 = ``owned_library_appids ∩ 免费游戏`` − ``listed``。

    ⚠️ 为什么不用「库索引 ∪ 客户端清单」（T18 线修正，用户第八次反馈）
    ---------------------------------------------------------------
    上一版判据是 ``(库索引 ∪ 客户端清单) ∩ 免费游戏``，而库索引是**库页面视图**：
    家庭库里别人名下的免费游戏在本机每个账号的库页面上都看得见。实测
    ``10000002`` 因此被补进 11 条、``10000004`` 被补进 7 条**主账号的**免费游戏
    （``544810 KARDS`` / ``1085660 命运2`` / ``1399720 反物质维度`` …），用户原话是
    「你还把别的账号的免费游戏同时算到别的账号中」。改成只在**自己确实拥有**
    的集合里挑免费游戏，这个问题从判据层面消失。

    读不到自有证据 / 读不到 cache.db 时返回**空集** —— 绝不凭空补条目。
    """
    owned = owned_library_appids(steam_path, account_id)
    free = free_appids()
    if not owned or not free:
        return frozenset()
    shown: set[int] = set()
    for item in listed or ():
        value = _as_int(item)
        if value is not None:
            shown.add(value)
    return frozenset(a for a in (owned & free) - shown if a > 0)


def augment_library(play_stats: Any, steam_path: Any, account_id: Any) -> dict:
    """把「在库里的免费游戏」补进 ``play_stats``（时长 0）；**不改入参、不删任何条目**。

    补进来的条目 ``playtime == 0`` / ``last_played == 0``，因此状态判定为「未玩」，
    与 Steam 客户端里看到的一致。非游戏条目一律不补；读不到库 / 读不到 cache.db 时
    原样返回（降级：宁可少补，绝不凭空造条目）。
    """
    current = dict(play_stats or {})
    try:
        extra = missing_free_library_games(steam_path, account_id, listed=current)
    except Exception as exc:                      # 补库失败绝不影响主流程
        logger.warning("补充「在库免费游戏」失败（已忽略）：%s", exc)
        return current
    if not extra:
        return current
    added: list[int] = []
    for appid in sorted(extra):
        if _is_non_game(appid):
            continue
        current[appid] = {"playtime": 0, "last_played": 0}
        added.append(appid)
    if added:
        logger.info(
            "已补入 %d 款「在库但从未启动」的免费游戏：%s",
            len(added), ", ".join(str(a) for a in added[:12]),
        )
    return current


# ------------------------------------------------------------------ 「明确售价」查询（残留痕迹判据）
#
# 为什么要读 cache.db
# -------------------
# 规则 4（无票 + 本机无人拥有）要区分「免费游戏」（保留）与「Steam 残留痕迹」（剔除），
# 唯一可靠的客观依据是**有没有明确售价**：免费游戏没有授权票是正常的（``is_free``），
# 而 ``1091500 Cyberpunk 2077``（¥298）／``1448440 Wo Long``（¥199）这种没有票、
# 没人拥有、又从未玩过的付费条目只会是 vdf 里的启动痕迹。
#
# 这一层只做**查表**，不做网络请求；任何异常都返回 ``None``（= 信息不足 -> 全部保留）。
# 放在这里而不是散进 :func:`classify` 的各分支，是为了让「读一次库、建一张表」这件事
# 有唯一实现，也让 :func:`classify` 的调用方可以显式注入（测试用）而不必碰真实磁盘。

_PAID_CACHE: dict[tuple, frozenset[int] | None] = {}
# (库路径, mtime, size) -> 「明确免费」的 appid 集合（T15 线）
_FREE_CACHE: dict[tuple, "frozenset[int] | None"] = {}

# genres / genres_cn 里出现这些字样 => 免费（Steam 的免费开玩标记）
_FREE_GENRE_MARKERS = ("free to play", "免费开玩")


def _cache_db_path() -> "Path | None":
    """``data/cache.db`` 的真实路径；不可用返回 ``None``（懒加载，绝不 import 失败即崩）。"""
    try:
        from data import schema

        return Path(schema.DB_PATH)
    except Exception as exc:                       # 打包缺依赖 / 迁移中：降级
        logger.debug("取 cache.db 路径失败（按信息不足处理）：%s", exc)
        return None


def _as_int(value: Any) -> "int | None":
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def paid_appids(db_path: Any = None) -> "frozenset[int] | None":
    """``data/cache.db`` 里**有明确售价**（付费）的 appid 集合；读不到返回 ``None``。

    「付费」= ``price`` 是 > 0 的整数，且 ``is_free`` 不为真、genres 里也没有
    免费开玩标记。``price`` 为 ``0`` / ``NULL`` / 查不到该 appid 一律**不进集合**
    （= 当成免费或未知，保留）——这与模块「宁放过不误杀」的降级哲学一致。

    ``None`` 表示**整个库都读不到**，调用方必须按「全部保留」处理，绝不据此剔除任何条目。
    """
    path = Path(str(db_path)) if db_path else _cache_db_path()
    if path is None:
        return None
    try:
        stat = path.stat()
        key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError as exc:
        logger.debug("cache.db 不可用（按信息不足处理）：%s：%s", path, exc)
        return None
    with _LOCK:
        if key in _PAID_CACHE:
            return _PAID_CACHE[key]

    result: "frozenset[int] | None" = None
    try:
        import sqlite3

        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT appid, price, is_free, genres, genres_cn FROM games"
            ).fetchall()
        finally:
            conn.close()
        paid: set[int] = set()
        for row in rows:
            appid = _as_int(row[0])
            if appid is None:
                continue
            price, is_free = row[1], row[2]
            genres = f"{row[3] or ''} {row[4] or ''}".lower()
            if is_free in (True, 1, "1", "true", "True"):
                continue                            # 明确免费 -> 保留
            if any(marker in genres for marker in _FREE_GENRE_MARKERS):
                continue                            # 免费开玩 -> 保留
            if (_as_int(price) or 0) > 0:
                paid.add(appid)                     # 有明确售价 -> 唯一的剔除依据
        result = frozenset(paid)
    except Exception as exc:                        # 表缺失 / 库损坏 / 权限：降级为「读不到」
        logger.warning("读取 cache.db 售价失败（按信息不足处理，全部保留）：%s：%s", path, exc)
        result = None

    with _LOCK:
        _PAID_CACHE[key] = result
    return result


# ------------------------------------------------------------------ 判定主入口

def _normalize(appids: Iterable[Any] | Mapping[Any, Any] | None) -> list[int]:
    """把 appid 集合（dict / list / set，元素可以是 str 或 int）规整成去重后的 int 列表。"""
    if appids is None:
        return []
    if isinstance(appids, Mapping):
        appids = list(appids.keys())
    out: list[int] = []
    seen: set[int] = set()
    for item in appids:
        try:
            appid = int(item)
        except (TypeError, ValueError):
            continue
        if appid not in seen:
            seen.add(appid)
            out.append(appid)
    return out


def _playtime_lookup(
    appids: Any, playtimes: Mapping[Any, Any] | None = None
) -> "Callable[[int], bool]":
    """返回 ``appid -> 是否玩过`` 的查询函数（供 classify 判定 PLAYED_SHARED / SHARED）。

    优先级：显式 ``playtimes`` > ``appids`` 里带的 ``playtime``。两者都没有时一律
    视为「没玩过」——**只影响家庭共享条目**（自有 / 免费条目的类别与时长无关），
    所以缺时长信息不会误杀自有游戏，只是共享条目会保守地按「未玩过」处理。
    """
    table: dict[int, Any] = {}
    if isinstance(appids, Mapping):
        for key, value in appids.items():
            try:
                appid = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, Mapping):
                table[appid] = value.get("playtime", 0)
    if isinstance(playtimes, Mapping):
        for key, value in playtimes.items():
            try:
                table[int(key)] = value
            except (TypeError, ValueError):
                continue

    def played(appid: int) -> bool:
        return is_played(table.get(appid, 0))

    return played


def classify(
    steam_path: Any,
    account_id: Any,
    appids: Any,
    playtimes: Mapping[Any, Any] | None = None,
    paid: "frozenset[int] | None" = None,
    untimed: "frozenset[int] | None" = None,
) -> dict[int, str]:
    """对给定 appid 集合返回 ``appid -> 五类之一``（严格按上面 5 条规则）。

    五类：``OWNED`` / ``PLAYED_SHARED`` / ``SHARED`` / ``FREE`` / ``TRACE``；
    ``SHARED``（共享且未玩过）与 ``TRACE``（残留痕迹）会被剔除。

    ⚠️ **免费游戏归属的边界（T18 线，用户第八次反馈）**

    免费游戏没有属于自己的授权票是**常态**，所以「它在库里」不能证明「它是你的」——
    库页面（``librarycache``）展示的是**家庭库视图**，别人名下的免费游戏在本机每个
    账号里都看得见。因此带 ``flag == 2``（或本账号没票）的免费游戏只有**本机没有别的
    账号持有它的 ``flag == 0``**（:func:`peer_owned_appids`）时才按 ``FREE`` 保留；
    票明确落在别人名下的一律按共享归类。这样「A 账号的免费游戏被算进 B 账号」从判据
    上不可能再发生，而**自己持票**的免费游戏（规则 1）完全不受影响。

    ``playtimes`` 决定「家庭共享」是 ``PLAYED_SHARED`` 还是 ``SHARED``，两种传法都支持：

    * **不传**：从 ``appids`` 自己读 —— 当它是 ``{appid: {...}}`` 映射（如 ``play_stats``）
      时取其中的 ``playtime``；
    * **显式传** ``playtimes={appid: minutes}``：只给 appid 列表 + 单独时长表的场景。

    规则 1 的「自有」由**两个本机来源取并集**（T12 线起）：

    * ``apptickets`` 的 ``flag == 0``（唯一口径，任何机器上都有）；
    * Steam 客户端缓存的 :func:`client_owned_appids`（``AccountOwnedApps``）——
      补上「真买过但票面没写」的游戏（本机 ``10000002`` 实测多出 79 条）。

    后者读不到（``None``）时**只用票面**，绝不改判。

    规则 1 还有**第三个来源**（T22 线）：Steam 客户端的实时归属
    :func:`live_owned_appids` —— 现代客户端默认开着 CEF 调试端口，
    :mod:`core.steam_live` 通过它调 ``CFamilyGroups_GetSharedLibraryApps``，
    直接拿到每个 appid 的 owner。拿到权威清单后 :func:`library_owned_appids`
    （库索引推断）**整条停用**：既能补全「买过没装过」的自有（战地1 / 神之浩劫），
    又能剔掉家庭库共享（命令与征服 / 杀手）。读不到时行为与 T22 之前完全一致。

    规则 4 区分「免费游戏」与「残留痕迹」需要**两组依据同时成立**（T4 起）：

    * ``paid`` = 「有明确售价的 appid 集合」，默认懒加载 :func:`paid_appids`（只读 cache.db）；
    * ``untimed`` = 「``Apps`` 段有条目但没有 ``Playtime`` 的 appid 集合」，
      默认懒加载 :func:`untimed_appids`（只读同一份 localconfig.vdf）。

    两者都可显式传入（测试用，完全离线）：传 ``frozenset()`` 即「一条都不算」。
    **任一侧是 ``None``（读不到）就一律保留** —— 绝不因为信息不足剔除任何条目。

    读不到 vdf / 没这个账号 → 全部 ``free``（**保留全部**，降级不清空用户库）。
    绝不抛异常。
    """
    ids = _normalize(appids)
    played = _playtime_lookup(appids, playtimes)
    # T15：免费游戏判据（见 free_appids 的说明）。免费游戏没有属于自己的授权票，
    # 在别人账号里会以「家庭共享 + 没玩过」的样子出现而被剔除 —— 这正是
    # 「部分免费游戏不在游戏明细中」的根因。
    try:
        flags = ticket_flags(steam_path, account_id)
        if flags is None:                       # 降级：没有票面信息，一律保留
            return {appid: FREE for appid in ids}
        local_owners = scan_local_owners(steam_path)
        resolved_free = free_appids()
        # 客户端清单是**本账号自己**的（不含家庭共享），必须按账号单独取。
        client_owned = client_owned_appids(steam_path, account_id)
        # 家庭组**远端成员**借来的（T14 线）：本机没有 owner 的 userdata，票面也未必写，
        # 只有 `logs/librarysharing_log.txt` 记得下来。
        borrowed = borrowed_appids(steam_path)
        # T22：Steam 客户端**实时归属**（每条到底归谁）。读不到只是 ``None``（降级）。
        live = live_ownership(steam_path, account_id)
        live_owned = None if live is None else live["owned"]
        live_others = None if live is None else live["others"]
        # 库索引补充来源（T13 线）：补上「买过但从没装过 / 没启动过、票面没写」的自有游戏。
        # **有权威清单时整条停用** —— 它分不清「买过没装过」与「家庭库共享」，
        # 正是用户反馈「命令与征服 / 杀手系列都不对」的根因。读不到只是 ``None``。
        library_owned = (None if live_owned is not None
                         else library_owned_appids(steam_path, account_id))
        # 「本机别的账号确实拥有」的并集 —— 免费游戏的**负面证据**（详见 peer_owned_appids）。
        peer_owned = peer_owned_appids(steam_path, account_id)
    except Exception as exc:                    # 兜底：任何意外都不剔除任何条目
        logger.warning("授权归属判定失败，已降级为全部保留：%s", exc)
        return {appid: FREE for appid in ids}

    # 规则 4 的两组依据：None = 读不到（整批按免费/未知保留）；各自懒加载一次，
    # 绝不逐条查库 / 逐条重读 vdf。
    paid_lookup_done = False
    if paid is not None:
        paid_lookup_done = True
    resolved_paid: frozenset[int] | None = paid
    untimed_lookup_done = False
    if untimed is not None:
        untimed_lookup_done = True
    resolved_untimed: frozenset[int] | None = untimed

    def _is_trace(appid: int) -> bool:
        """是否为 Steam 残留痕迹（规则 4 的「两组依据同时成立」判据）。

        **两侧任一读不到（``None``）都返回 ``False``** —— 信息不足时一律保留，
        绝不在信息不足时剔除任何条目。库索引补充来源（T13）也要过这道闸门，
        否则「有售价、Apps 段却没有 Playtime」的痕迹条目会被库索引误升级成自有。
        """
        nonlocal resolved_paid, resolved_untimed, paid_lookup_done, untimed_lookup_done
        if not paid_lookup_done:
            resolved_paid = paid_appids()
            paid_lookup_done = True
        if not untimed_lookup_done:
            resolved_untimed = untimed_appids(steam_path, account_id)
            untimed_lookup_done = True
        return bool(resolved_paid) and appid in resolved_paid and \
            bool(resolved_untimed) and appid in resolved_untimed

    result: dict[int, str] = {}
    for appid in ids:
        if live_owned is not None and appid in live_owned:
            # T22 规则 1：Steam 客户端权威清单说这一条归本账号
            # （含「买过没装过」与免费游戏 —— 与玩没玩过无关）。
            result[appid] = OWNED
        elif appid in flags:
            flag = flags[appid]
            if flag == LICENSE_SELF:
                result[appid] = OWNED           # 规则 1
            elif flag == LICENSE_SHARED:
                # 规则 2：家庭共享。再分一次（T15 线）——
                # **免费游戏**（CS2 / Apex / 彩虹六号 / 命运2 / NGU 放置 …）没有属于自己的
                # 授权票，Steam 会把它们在任何账号下都写成 ``flag == 2`` + ``playtime == 0``。
                # 只看票面就会把它们当「别人借你的、你还没玩过」而**剔除**，
                # 于是免费游戏从游戏明细里整批消失（用户反馈的就是这个）。
                # 有 cache.db 的权威免费标记、且**本机没有别的账号持有它**时按 FREE 保留；
                # 若票面明确落在别的账号名下（``peer_owned``），那便是**别人的**免费游戏，
                # 绝不能算进本账号（用户第八次反馈：「把别的账号的免费游戏算到别的账号中」）。
                if (resolved_free and appid in resolved_free
                        and appid not in peer_owned and live_owned is None):
                    result[appid] = FREE
                else:
                    result[appid] = PLAYED_SHARED if played(appid) else SHARED
            else:
                result[appid] = FREE            # 规则 5：解不出 -> 保留
        elif client_owned and appid in client_owned:
            # 票面没这一条（Steam 没给没装过的游戏写票），但客户端认它属于本账号 ->
            # 规则 1 的补充来源：真拥有，与玩没玩过无关。
            result[appid] = OWNED
        elif appid in local_owners:
            # 规则 3：本账号没票，但**本机有人拥有** -> 别人借给你的。
            # T15 线先把免费游戏摘出来，T18 线再收紧：免费游戏的授权票只会落在**某一个**
            # 账号名下（本机实测是主账号），而对**其余账号**而言，那恰恰是「它属于别人」的
            # 直接证据 —— 必须落到「共享」，不能因为它在库里就算成自己的（用户第八次反馈）。
            if (resolved_free and appid in resolved_free
                    and appid not in peer_owned and live_owned is None):
                result[appid] = FREE
            else:
                result[appid] = PLAYED_SHARED if played(appid) else SHARED
        elif appid in borrowed:
            # 规则 3 的**远端版本**：本机没人有票，但家庭组里的远端成员借给了我们。
            # 借来的终究不是自己的 —— 按共享归类（本账号玩过 -> 保留；没玩过 -> 剔除），
            # **绝不能**落到下面 FREE / 库索引分支被当成「已拥有」。
            # 排在 client_owned 之后：客户端权威自有清单是账号本人的，压过过期的借用日志。
            result[appid] = PLAYED_SHARED if played(appid) else SHARED
        elif library_owned and appid in library_owned:
            # 规则 1 的第二个补充来源：库索引里有、且本机任何账号都没票 -> 真拥有。
            # **必须排在规则 3 之后**：库索引里也含本机**别人**拥有的游戏，先让规则 3
            # 把「本机有人拥有」全部拦下，这里剩下的才是「本机谁都查不到出处」的条目。
            # 本机实测：本账号库索引候选中有 20 条命中本账号客户端清单、32 条命中他人
            # 客户端清单，全部靠这条优先级挡在规则 3，只有 332 条真正走这里。
            # **残留痕迹优先级更高**：有明确售价、Apps 段却没有 Playtime 的条目
            # （赛博朋克 / 卧龙）只是启动痕迹，绝不能被库索引升级成「已拥有」。
            result[appid] = TRACE if _is_trace(appid) else OWNED
        elif played(appid):
            # 规则 4：确实玩过 -> 保留。Steam 权威清单明说这条归别人（家庭库）时
            # 走「玩过（家庭库）」，否则维持原来的「免费 / 未知」保留（T22 线）。
            result[appid] = (PLAYED_SHARED
                             if (live_others is not None and appid in live_others)
                             else FREE)
        else:
            # 规则 4：没票、本机没人拥有、也没玩过 —— 免费游戏保留；**两组依据同时成立**
            # （有明确售价 **且** Apps 段只有痕迹、没有 Playtime）才判残留痕迹。
            # 任一侧读不到（None）一律保留，绝不误杀。
            if _is_trace(appid):
                result[appid] = TRACE           # 残留痕迹 -> 剔除
            elif live_others is not None and appid in live_others:
                # T22：Steam 权威清单说这条归家庭组别人（或不属于任何人）-> 剔除
                result[appid] = SHARED
            else:
                # 免费 / 未知 / 信息不足 / 有价格但 Apps 里记着 Playtime（真买过）-> 保留
                result[appid] = FREE
    return result

def filter_owned(
    steam_path: Any,
    account_id: Any,
    appids: Any,
    playtimes: Mapping[Any, Any] | None = None,
    paid: "frozenset[int] | None" = None,
    untimed: "frozenset[int] | None" = None,
) -> set[int]:
    """返回应**保留**的 appid（``OWNED`` + ``PLAYED_SHARED`` + ``FREE``）。

    函数名沿用历史的 ``filter_owned``（上层调用点不动），但语义已按 A4 + T1 收窄为
    「自己拥有 + 共享但本账号玩过 + 免费 / 未知」——**``SHARED``（共享且从未玩过）
    与 ``TRACE``（Steam 残留痕迹）会被剔除**。
    ``playtimes`` / ``paid`` / ``untimed`` 见 :func:`classify`。
    """
    kinds = classify(steam_path, account_id, appids, playtimes, paid=paid, untimed=untimed)
    return {appid for appid, kind in kinds.items() if kind not in EXCLUDED_KINDS}


def summarize(
    steam_path: Any,
    account_id: Any,
    appids: Any,
    playtimes: Mapping[Any, Any] | None = None,
    paid: "frozenset[int] | None" = None,
    untimed: "frozenset[int] | None" = None,
) -> dict:
    """过滤口径的自述信息（CLI 提示行与面板 ``ownership`` 字段共用的唯一实现）。

    返回::

        {"owned": N, "played_shared": P, "shared": M, "free": K, "trace": T,
         "total": N+P+K, "played_shared_appids": [...], "shared_appids": [...],
         "trace_appids": [...], "degraded": bool}

    语义（A4 + T1 起）：

    * ``owned``            自己拥有；
    * ``played_shared``    家庭共享**但本账号玩过**（``playtime > 0``）→ **保留**；
    * ``shared``           家庭共享**且从未玩过** → **剔除**（``shared`` / ``shared_appids``
      收窄为「被剔除的那部分」，界面据此显示「已剔除 M 款」）；
    * ``trace``            没票、本机没人拥有、没玩过、有明确售价、且 ``Apps`` 段只有痕迹
      （有条目但没有 ``Playtime``）→ **剔除**（T4 起两组依据都要成立）；
    * ``free``             免费 / 试玩 / 未知 / 票解不出 → 保留；
    * ``total``            **保留后的款数** = ``owned + played_shared + free``
      （= 全体减去被剔除的 ``shared`` 与 ``trace``）。

    ``degraded``：读不到授权票 -> 本次没有剔除任何东西；界面据此不显示剔除提示。
    """
    kinds = classify(steam_path, account_id, appids, playtimes, paid=paid, untimed=untimed)
    owned = sum(1 for kind in kinds.values() if kind == OWNED)
    played_shared = sum(1 for kind in kinds.values() if kind == PLAYED_SHARED)
    shared = sum(1 for kind in kinds.values() if kind == SHARED)
    free = sum(1 for kind in kinds.values() if kind == FREE)
    trace = sum(1 for kind in kinds.values() if kind == TRACE)
    played_shared_appids = sorted(appid for appid, kind in kinds.items() if kind == PLAYED_SHARED)
    shared_appids = sorted(appid for appid, kind in kinds.items() if kind == SHARED)
    trace_appids = sorted(appid for appid, kind in kinds.items() if kind == TRACE)
    return {
        "owned": owned,
        "played_shared": played_shared,
        "shared": shared,
        "free": free,
        "trace": trace,
        "total": owned + played_shared + free,
        "played_shared_appids": played_shared_appids,
        "shared_appids": shared_appids,
        "trace_appids": trace_appids,
        # 读不到授权票 -> 本次没有剔除任何东西；界面据此不显示「已剔除 M 款」的假提示
        "degraded": ticket_flags(steam_path, account_id) is None,
    }


def excluded_appids(
    steam_path: Any,
    account_id: Any,
    appids: Any,
    playtimes: Mapping[Any, Any] | None = None,
    paid: "frozenset[int] | None" = None,
    untimed: "frozenset[int] | None" = None,
) -> set[int]:
    """应**剔除**的 appid 集合 = ``SHARED``（共享且从未玩过）+ ``TRACE``（残留痕迹）。

    面板「显示家庭共享」勾选框用它列灰显行 —— A4 起只列没玩过的共享条目；
    T1 起再并上 Steam 残留痕迹（T4 起要「有明确售价」**且**「``Apps`` 段只有痕迹」
    两个依据同时成立才判）。
    """
    kinds = classify(steam_path, account_id, appids, playtimes, paid=paid, untimed=untimed)
    return {appid for appid, kind in kinds.items() if kind in EXCLUDED_KINDS}


# ------------------------------------------------------------------ 归属自动体检（T15）

# 体检结论的严重度（面板/CLI 据此排序与配色）
AUDIT_OK = "ok"              # 证据自洽，无需人工介入
AUDIT_WARN = "warn"          # 有可疑条目，但无法确证（多为数据缺失）
AUDIT_ERROR = "error"        # 确证的矛盾，一定是判定 bug

# 单条结论的字段：level / appid / name / reason / evidence
def audit_ownership(
    steam_path: Any,
    account_id: Any,
    appids: Any = None,
    playtimes: Mapping[Any, Any] | None = None,
    limit: int = 40,
) -> dict:
    """**自动体检**：把归属判定与依据的各路证据逐条对账，自己报出可疑条目。

    为什么要有它（用户反馈原话）：「不要我说什么就改什么，要是自动化的识别」——
    判定逻辑本身是通用的（**没有任何硬编码 appid**），但此前**没有一处会自己发现问题**：
    每次都靠用户点名「某某游戏显示错了」。本函数把「人肉核对」变成工具自己的对账。

    体检项（每条都用**独立证据交叉验证**，不做启发式猜测）：

    | 检查 | 级别 | 触发条件 | 为什么算问题 |
    | --- | --- | --- | --- |
    | `shared-free-game` | error | 判为 ``SHARED``（共享且未玩过 → 剔除）、cache.db 标它**免费**，且**本机没有别的账号持有它的授权** | 免费游戏没有自己的授权票，走共享分支就会被**整批剔除** —— 用户反馈的「免费游戏不在明细里」。反之，票明确落在**别的账号**名下时（``peer_owned``）那是正常家庭共享，不报 |
    | `trace-but-free` | error | 判为 ``TRACE``（残留痕迹 → 剔除）但 cache.db 标它**免费** | 免费游戏不该被当痕迹剔除 |
    | `dropped-despite-playtime` | error | 被剔除（``SHARED`` / ``TRACE``）但本账号 ``playtime > 0`` | 玩过的游戏不该被剔除 |
    | `owned-but-refunded` | warn | 判为 ``OWNED``，客户端自有清单**存在**且不含它，且不在票面 | 疑似已退款 / 退库残留 |
    | `owned-without-evidence` | warn | 判为 ``OWNED`` 但既无票、也不在客户端清单、也不在库索引 | 自有判定缺出处 |
    | `index-entry-not-classified` | warn | 在库索引里却既没被保留、也没被剔除 | 判定漏项 |
    | `free-in-library-not-listed` | error | 确实拥有、是免费游戏，却**不在传给体检的明细**里 | 免费游戏没有授权票，Steam 也只在启动过之后才写 Apps 条目 —— 「加进库、还没启动」的免费游戏会整条不在明细里（用户反馈「部分免费游戏都不在游戏明细中」） |
    | `owned-but-peer-evidence` | warn | 判为 ``OWNED``，但该 appid 只出现在**本机别的账号**的票面 / 权威清单里 | 「家庭库游戏被算成本账号拥有」的直接证据（用户第八次反馈的 ``412020 地铁：离乡`` / ``379720 DOOM`` 一类）；也可能只是本机数据缺失，故为存疑 |

    ⚠️ **「本机其它账号拥有、本账号没票」是正常的家庭共享**（Steam 家庭库的固有行为），
    **不算问题**，因此不列入体检项 —— 只有「免费游戏 / 玩过却被剔除」这类
    **逻辑上说不通**的组合才报错。

    传入 ``appids`` 时它同时扮演两个角色：①体检范围；②**用户当前看到的明细**——
    工具据此对账出「在库里却没显示」的免费游戏（``free-in-library-not-listed``），
    这正是「自动化识别」的关键：**不用用户逐个点名**。
    省略 ``appids`` 时用本账号**库索引 ∪ 游玩记录**，此时**不做**漏项体检（我们不知道
    用户看到的是什么，绝不凭空断言）。``limit`` 限制**每类**返回条数。
    读不到票面 / 库索引时返回 ``degraded=True`` —— **信息不足时绝不谎报「一切正常」**。
    绝不抛异常、绝不写盘。
    """
    def _issue(level, appid, reason, evidence):
        return {"level": level, "appid": int(appid), "reason": reason,
                "evidence": str(evidence)}

    empty = {"ok": False, "degraded": True, "issues": [], "counts": {},
             "checked": 0, "errors": 0, "warnings": 0}
    account = _clean_account_id(account_id)
    if account is None:
        return empty
    try:
        flags = ticket_flags(steam_path, account)
        if flags is None:
            return empty
        index = library_index_appids(steam_path, account)
        if index is None:
            return empty
        # ``listed`` = 调用方交上来的「当前明细里到底有哪些」。
        # 只有调用方**明确给了**（``appids is not None``）才做「明细漏项」体检 —— 否则
        # 我们不知道用户实际看到的是什么，宁可不报，也绝不凭空断言「漏了」。
        listed: "set[int] | None" = None
        if appids is not None:
            listed = {a for a in _normalize(appids) if a > 0}
        ids = sorted(set(index) | set(_normalize(appids if appids is not None else playtimes or {})))
        ids = [a for a in ids if a > 0]          # appid 0 不是游戏
        if not ids:
            return {"ok": True, "degraded": False, "issues": [], "counts": {},
                    "checked": 0, "errors": 0, "warnings": 0}

        kinds = classify(steam_path, account, ids, playtimes)
        played = _playtime_lookup({}, playtimes)
        client = client_owned_appids(steam_path, account)
        free = free_appids()
        # 「明细漏项」的自动判据：**在库里 + 是免费游戏 + 明细里却没有**。
        # 免费游戏没有属于自己的授权票，Steam 也只在真正启动过之后才写 Apps 条目 ——
        # 所以「加进库、还没点过开始」的免费游戏会整条不在明细里。这条规则让工具
        # **自己发现**它，不用用户逐个点名（M2f / T16）。
        # 「确实拥有」的汇总（T18）：体检的漏项判据也必须用**归属**口径，不能用
        # 「在库里」（含家庭库视图）—— 否则会把别人名下的免费游戏误报成本账号漏项。
        owned_set = owned_library_appids(steam_path, account)
        peer_owned = peer_owned_appids(steam_path, account)
        unlisted_free = frozenset(
            a for a in ((owned_set & free) - (listed or set()))
            if a > 0 and not _is_non_game(a)
        ) if (free and listed is not None) else frozenset()
    except Exception as exc:                       # 体检失败绝不抛给调用方
        logger.warning("归属体检失败：%s", exc)
        return empty

    issues: list[dict] = []
    for appid in ids:
        if _is_non_game(appid):
            continue          # 非游戏条目（Steam 客户端本体 / 工具）不参与归属体检
        kind = kinds.get(appid)
        is_free = bool(free) and appid in free
        if kind == SHARED:
            # T18：免费游戏被判共享时，只有「本机没有别的账号持有它」才算错标 ——
            # 若票明确落在别的账号名下，那正是「它属于别人」，走共享分支完全正确。
            if is_free and appid not in peer_owned:
                issues.append(_issue(
                    AUDIT_ERROR, appid, "shared-free-game",
                    "被判为「家庭共享且未玩过」而剔除，但它是免费游戏 —— "
                    "免费游戏没有属于自己的授权票，不该走共享分支",
                ))
            elif played(appid):
                issues.append(_issue(
                    AUDIT_ERROR, appid, "dropped-despite-playtime",
                    "被判为家庭共享而剔除，但本账号 playtime > 0（玩过的游戏不该被剔除）",
                ))
        elif kind == TRACE:
            if is_free and appid not in peer_owned:
                issues.append(_issue(
                    AUDIT_ERROR, appid, "trace-but-free",
                    "被判为 Steam 残留痕迹而剔除，但它是免费游戏",
                ))
            elif played(appid):
                issues.append(_issue(
                    AUDIT_ERROR, appid, "dropped-despite-playtime",
                    "被判为残留痕迹而剔除，但本账号 playtime > 0",
                ))
        elif kind == OWNED:
            in_ticket = appid in flags
            if client is not None and not in_ticket and appid not in client:
                issues.append(_issue(
                    AUDIT_WARN, appid, "owned-but-refunded",
                    "被判为已拥有，但既不在票面、也不在客户端自有清单（疑似已退款 / 退库残留）",
                ))
            elif not in_ticket and appid not in index:
                issues.append(_issue(
                    AUDIT_WARN, appid, "owned-without-evidence",
                    "被判为已拥有，但票面 / 客户端清单 / 库索引里都没有出处",
                ))
            elif (not in_ticket and appid in peer_owned
                    and appid not in (client or frozenset())
                    and not _is_non_game(appid)):
                # T18 自动识别：本账号**自己没有票**，却因为「库里可见」被判成「拥有」，
                # 而本机**别的账号**持有它的授权 —— 这正是「家庭库的游戏被算进当前账号」
                # 的直接证据。用户第八次反馈的 ``412020 地铁：离乡`` / ``379720 DOOM``
                # 就是这个形态。
                # ⚠️ 必须有 ``not in_ticket``：本账号**自己也持票**（``flag == 0``）时，
                # 与别的账号并存是合法的（同一款游戏可被多人拥有，本机实测 ``238960``
                # 在 ``10000003`` 与 ``10000002`` 下都是 ``flag == 0``），绝不误报。
                peers = sorted(
                    b for b in _local_account_ids(steam_path)
                    if b != account
                    and (ticket_flags(steam_path, b) or {}).get(appid) == LICENSE_SELF
                )
                issues.append(_issue(
                    AUDIT_WARN, appid, "owned-but-peer-evidence",
                    "被判为已拥有，但本账号没有它的授权票，且它出现在本机其它账号的票面里"
                    "（%s）—— 疑似家庭库条目被算成本账号拥有" % ("、".join(peers) or "对端"),
                ))
        elif kind is None:
            issues.append(_issue(
                AUDIT_WARN, appid, "index-entry-not-classified",
                "在库索引里，但没有拿到任何归属类别（判定漏项）",
            ))

    # 自动报出「明细漏项」：在库里、是免费游戏、却整条没出现在游戏明细里（M2f / T16）。
    # 这条专门替代「用户逐个点名某个免费游戏没显示」——
    # 工具自己拿「库索引 ∪ 客户端清单」与明细对账，缺什么自己说。
    for appid in sorted(unlisted_free):
        issues.append(_issue(
            AUDIT_ERROR, appid, "free-in-library-not-listed",
            "在库里、是免费游戏，却没有出现在游戏明细里 —— 免费游戏没有属于自己的授权票，"
            "Steam 也只在**真正启动过**之后才写 Apps 条目，所以「加进库、还没点过开始」的"
            "免费游戏会整条不在明细里",
        ))

    counts: dict[str, int] = {}
    for item in issues:
        counts[item["reason"]] = counts.get(item["reason"], 0) + 1
    # 截断是**每个原因**各留 limit 条，保证每一类都能看到样例
    per_reason: dict[str, int] = {}
    trimmed: list[dict] = []
    for item in issues:
        reason = item["reason"]
        seen = per_reason.get(reason, 0)
        if seen >= max(1, int(limit)):
            continue
        per_reason[reason] = seen + 1
        trimmed.append(item)

    hard = sum(1 for item in issues if item["level"] == AUDIT_ERROR)
    return {
        "ok": hard == 0,
        "degraded": False,
        "issues": trimmed,
        "counts": counts,
        "checked": len(ids),
        "errors": hard,
        "warnings": len(issues) - hard,
    }


def reset_cache() -> None:
    """清空缓存（测试用；下一次调用重新读 vdf 与 cache.db）。"""
    with _LOCK:
        _TICKET_CACHE.clear()
        _OWNER_CACHE.clear()
        _APP_PLAYTIME_CACHE.clear()
        _PAID_CACHE.clear()
        _FREE_CACHE.clear()
        _CLIENT_OWNED_CACHE.clear()
        _CLIENT_SCAN_CACHE.clear()
        _LIBRARY_INDEX_CACHE.clear()
        _TICKET_KEYS_CACHE.clear()
        _BORROWED_CACHE.clear()
        _PEER_LIBRARY_CACHE.clear()
        _PEER_OWNED_CACHE.clear()
        _LIVE_CACHE.clear()
    try:                                        # 实时源的进程内记忆同样要清
        from core import steam_live

        steam_live.reset_cache()
    except Exception:                           # 缺模块 / 任何异常都不影响清缓存
        pass


# ------------------------------------------------------------------ LastOwner 交叉验证（只读）

def read_last_owners(steam_path: Any) -> dict[int, int]:
    """``steamapps/appmanifest_*.acf`` 的 ``appid -> LastOwner(account_id)``。

    独立于 apptickets 的第二数据源，**只用于交叉验证与排查**（不参与判定）。
    读不到 / 解析失败的行直接跳过；绝不抛异常、绝不写文件。
    """
    base = Path(str(steam_path or "")) / "steamapps"
    result: dict[int, int] = {}
    try:
        manifests = sorted(base.glob("appmanifest_*.acf"))
    except OSError as exc:
        logger.warning("扫描 appmanifest 失败（%s）：%s", base, exc)
        return result
    import re

    for manifest in manifests:
        try:
            text = manifest.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            logger.debug("读取 %s 失败：%s", manifest, exc)
            continue
        match = re.search(r'"LastOwner"\s+"(\d+)"', text)
        if not match:
            continue
        appid_match = re.search(r'"appid"\s+"(\d+)"', text)
        if not appid_match:
            continue
        account = account_id_from_steam64(match.group(1))
        if account is not None:
            result[int(appid_match.group(1))] = account
    return result
