"""store_api 价格路径自测：三态语义 + 独立价格回填（**不联网**）。

运行方式（game-life 目录或仓库根目录均可）：
    ..\\.venv\\Scripts\\python.exe tests\\test_store_price.py

覆盖（全部打桩，不发真实请求、不写真实库）：
    1. ``price_state`` 三态判定：有价 / 免费 / 未知；
    2. ``_from_store_price`` 解析：price_overview / is_free / success=false；
    3. ``price_for`` 与 ``backfill_prices`` 的独立路径行为（与「缺名字才取」解耦）；
    4. ``fetch_one`` 在「已有名字」时**依然取价**（历史回归：曾永不取价）；
    5. 官方商店价格源连续失败后的熔断；
    6. ``games.is_free`` 列幂等迁移与三态持久化（临时库，不碰真实缓存）。
"""

import json
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import store_api as s  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """带 .get(url) 的打桩会话：按 appid 命中打桩响应；值为 None 表示不可达。"""

    def __init__(self, mapping):
        self._mapping = mapping

    def get(self, url, timeout=None, headers=None):
        for appid, payload in self._mapping.items():
            if str(appid) in url:
                if payload is None:
                    raise RuntimeError("unreachable")
                return _Resp(payload)
        raise RuntimeError("unknown url")


class _JsonResp:
    """带 status_code 的打桩响应（DoH / 小黑盒用）。"""

    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
        return None

    def json(self):
        return self._payload


class _FixedSession:
    """任何 URL 都返回同一份 payload 的打桩会话；payload 可给 callable 按 URL 分流。

    返回 ``None`` 表示「连不上」（抛异常），``_JsonResp`` 可带非 200 状态码。
    """

    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status
        self.calls: list[str] = []

    def get(self, url, timeout=None, headers=None):
        self.calls.append(str(url))
        payload = self._payload(url) if callable(self._payload) else self._payload
        if payload is None:
            raise RuntimeError("unreachable")
        if isinstance(payload, _JsonResp):
            return payload
        return _JsonResp(payload, self._status)


def test_price_state():
    assert s.price_state({"price": 4200}) == "paid"
    assert s.price_state({"price": 0, "is_free": True}) == "free"
    assert s.price_state({"is_free": True}) == "free"
    assert s.price_state({"price": 0}) == "unknown"
    assert s.price_state({}) == "unknown"
    assert s.price_state(None) == "unknown"
    assert s._has_price({"is_free": True}) is True
    assert s._has_price({"price": 100}) is True
    assert s._has_price({"price": 0}) is False
    print("PASS price_state 三态判定")


def test_from_store_price_parsing():
    paid = {100: {"100": {"success": True, "data": {"price_overview": {"initial": 29800}}}}}
    got, reachable = s._from_store_price(100, "https://x/100", 1.0,
                                         session=_FakeSession(paid), report=True)
    assert got == {"price": 29800} and reachable is True

    free = {101: {"101": {"success": True, "data": {"is_free": True}}}}
    got, _ = s._from_store_price(101, "https://x/101", 1.0,
                                 session=_FakeSession(free), report=True)
    assert got == {"price": 0, "is_free": True}

    # success=True 但无价格（区域不可售）：返回 {}，**不能**伪造成免费
    empty = {102: {"102": {"success": True, "data": {"name": "X"}}}}
    got, reachable = s._from_store_price(102, "https://x/102", 1.0,
                                         session=_FakeSession(empty), report=True)
    assert got == {} and reachable is True

    # success=False：不在这家商店，不算故障
    absent = {103: {"103": {"success": False}}}
    got, reachable = s._from_store_price(103, "https://x/103", 1.0,
                                         session=_FakeSession(absent), report=True)
    assert got == {} and reachable is True

    # 网络异常：reachable=False（供熔断判断）
    got, reachable = s._from_store_price(104, "https://x/104", 1.0,
                                         session=_FakeSession({104: None}), report=True)
    assert got == {} and reachable is False
    print("PASS _from_store_price 有价/免费/无价/未上架/不可达")


def test_price_for_and_backfill():
    calls = []

    def _fake_price(appid, session=None):
        calls.append(appid)
        return {1: {"price": 4200}, 2: {"is_free": True}, 3: {}}.get(appid, {})

    originals = (s._price_from_official, s._save_price, s._cached_rows)
    saved = {}
    s._price_from_official = _fake_price
    s._save_price = lambda aid, data: saved.setdefault(aid, dict(data))
    s._cached_rows = lambda ids: {1: {"price": 4200}}
    try:
        assert s.price_for(1) == {"price": 4200}
        assert s.price_for(2) == {"is_free": True}
        assert s.price_for(3) == {}
        assert s.price_for("abc") == {}

        out = s.backfill_prices([1, 2, 3], quiet=True)
        assert 1 not in out, "已知价格的行应被跳过"
        assert out.get(2) == {"is_free": True} and 3 not in out
        assert calls.count(1) == 1 and 2 in calls and 3 in calls
        assert saved.get(2) == {"is_free": True} and 3 not in saved
    finally:
        s._price_from_official, s._save_price, s._cached_rows = originals
    print("PASS price_for / backfill_prices 独立路径与跳过逻辑")


def test_fetch_one_takes_price_even_with_name():
    """历史回归：已有名字的游戏也必须取价（旧实现被『缺名字才取』把门）。"""
    calls = []

    def _fake_price(appid, session=None):
        calls.append(appid)
        return {"price": 29800}

    stubs = (s._price_from_official, s._from_appinfo, s._from_steamcmd,
             s._from_steamchina, s._from_official)
    s._price_from_official = _fake_price
    s._from_appinfo = lambda aid, steam_path=None: {"name": "ELDEN RING", "name_cn": "艾尔登法环"}
    s._from_steamcmd = lambda aid, session=None: {}
    s._from_steamchina = lambda aid, session=None: {}
    s._from_official = lambda aid, session=None: {}
    try:
        record = s.fetch_one(1245620, with_mirror=False)
    finally:
        (s._price_from_official, s._from_appinfo, s._from_steamcmd,
         s._from_steamchina, s._from_official) = stubs
    assert 1245620 in calls, "即使已有名字也必须走价格路径"
    assert record["price"] == 29800
    assert "price" in str(record.get("source") or "")
    print("PASS fetch_one 已有名字仍然取价（回归）")


def test_price_source_circuit_breaker():
    """国际站价格源连续失败后熔断，本进程不再为价格访问它。"""
    original = (s._OFFICIAL_PRICE_BLOCKED, s._OFFICIAL_PRICE_FAILS, s._from_store_price,
                s._xiaoheihe_one)
    try:
        s._OFFICIAL_PRICE_BLOCKED = False
        s._OFFICIAL_PRICE_FAILS = 0

        def _dead(appid, url, timeout, session=None, report=False):
            return ({}, False) if report else {}

        s._xiaoheihe_one = lambda appid, session=None: {}   # 不打真实小黑盒
        s._from_store_price = _dead
        for _ in range(s.MAX_PRICE_SOURCE_FAILS + 1):
            if s._OFFICIAL_PRICE_BLOCKED:
                break
            s._price_from_official(999)
        assert s._OFFICIAL_PRICE_BLOCKED is True, "连续失败后应熔断"
    finally:
        (s._OFFICIAL_PRICE_BLOCKED, s._OFFICIAL_PRICE_FAILS, s._from_store_price,
         s._xiaoheihe_one) = original
    print("PASS 价格源熔断（连续失败即停用）")


def test_schema_migration_and_tristate_persistence():
    """临时库上验证：is_free 列幂等迁移 + 三态读写 + 不回退已知价格。"""
    import shutil
    from data import schema

    with tempfile.TemporaryDirectory(prefix="gl_price_") as tmp:
        original = (schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY)
        schema.DB_PATH = Path(tmp) / "cache.db"
        s.init_db = schema.init_db
        s.get_connection = schema.get_connection
        s._PRICE_COLUMN_READY = False
        try:
            schema.init_db()
            db = schema.get_connection()
            try:
                assert "is_free" not in {r[1] for r in db.execute("PRAGMA table_info(games)")}
            finally:
                db.close()

            s._ensure_price_column()
            db = schema.get_connection()
            try:
                assert "is_free" in {r[1] for r in db.execute("PRAGMA table_info(games)")}
            finally:
                db.close()

            s._save({"appid": 1, "name": "Paid", "price": 4200, "source": "t"})
            s._save({"appid": 2, "name": "Free", "price": 0, "is_free": True, "source": "t"})
            s._save({"appid": 3, "name": "Unknown", "price": 0, "source": "t"})
            rows = s._cached_rows([1, 2, 3])
            assert s.price_state(rows[1]) == "paid" and rows[1]["price"] == 4200
            assert s.price_state(rows[2]) == "free" and rows[2]["is_free"] is True
            assert s.price_state(rows[3]) == "unknown"

            # 后续「未知」写入不得把已知价格/免费标记清掉
            s._save({"appid": 1, "name": "Paid", "price": 0, "source": "t2"})
            s._save({"appid": 2, "name": "Free", "price": 0, "source": "t2"})
            rows = s._cached_rows([1, 2])
            assert rows[1]["price"] == 4200 and s.price_state(rows[1]) == "paid"
            assert rows[2]["is_free"] is True and s.price_state(rows[2]) == "free"

            # _save_price 只更新价格两列
            s._save_price(3, {"price": 9900})
            rows = s._cached_rows([3])
            assert rows[3]["price"] == 9900 and s.price_state(rows[3]) == "paid"

            # T9：价格来源如实落库（新列 price_source / miss_at 幂等迁移）
            s._save_price(3, {"price": 9900, "price_source": "xiaoheihe"})
            assert s._cached_rows([3])[3]["price_source"] == "xiaoheihe"
            s._save_price(3, {"price": 9900})
            assert s._cached_rows([3])[3]["price_source"] == "xiaoheihe", "没带来源时不能清空"
            db = s.get_connection()
            try:
                cols = {r[1] for r in db.execute("PRAGMA table_info(games)")}
            finally:
                db.close()
            assert {"miss_at", "price_source"} <= cols, cols
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY = original
    print("PASS 幂等迁移 / 三态持久化 / 不回退已知价格")


def test_fetch_metadata_runs_price_pass():
    """fetch_metadata 命中缓存时也必须补价（否则已有名字的游戏永远无价）。"""
    originals = (s._row_usable, s._cached_rows, s.backfill_prices, s.xiaoheihe_batch)
    called = {}

    def _fake_backfill(appids, **kwargs):
        called["appids"] = list(appids)
        return {11: {"price": 5000}}

    try:
        s._row_usable = lambda row: True
        s._cached_rows = lambda ids: {11: {"appid": 11, "name": "Cached", "price": 0, "source": "appinfo"}}
        s.backfill_prices = _fake_backfill
        s.xiaoheihe_batch = lambda appids, session=None, sleep_between=True: {}
        out = s.fetch_metadata([11], quiet=True)
    finally:
        s._row_usable, s._cached_rows, s.backfill_prices, s.xiaoheihe_batch = originals

    assert 11 in called.get("appids", []), "命中缓存时也应跑价格回填"
    assert out[11]["price"] == 5000
    assert "price" in str(out[11].get("source") or "")
    print("PASS fetch_metadata 命中缓存仍补价")


def test_appdetails_node_trusts_inner_appid():
    """核心回归：CDN 返回的外层 key 是错的（实测 5/5），必须认内层 data.steam_appid。"""
    scrambled = {"1390430": {"success": True, "data": {"steam_appid": 582010, "name": "MHW"}}}
    node = s._appdetails_node(scrambled, 582010)
    assert node.get("success") is True and node["data"]["steam_appid"] == 582010

    # 外层 key 正确的正常响应：行为不变（回归）
    normal = {"582010": {"success": True, "data": {"steam_appid": 582010}}}
    assert s._appdetails_node(normal, 582010) == normal["582010"]

    # 内层 appid 与请求**不一致**且只有一条 -> 必须拒绝（绝不能写错价）
    wrong = {"999": {"success": True, "data": {"steam_appid": 1,
                                               "price_overview": {"initial": 100}}}}
    assert s._appdetails_node(wrong, 582010) == {}

    # 内层 appid 缺失 + 只有一条记录 -> 接受（外层 key 不对，记 warning）
    no_inner = {"999": {"success": True, "data": {"name": "X"}}}
    assert s._appdetails_node(no_inner, 582010).get("success") is True

    # 多条记录、没有一条自述匹配 -> {}
    multi = {"1": {"data": {"steam_appid": 2}}, "3": {"data": {"steam_appid": 4}}}
    assert s._appdetails_node(multi, 582010) == {}
    # 多条记录里有匹配的 -> 取那条
    multi2 = {"1": {"data": {"steam_appid": 2}}, "3": {"data": {"steam_appid": 582010}}}
    assert s._appdetails_node(multi2, 582010)["data"]["steam_appid"] == 582010
    print("PASS _appdetails_node：信内层 appid / 拒绝不一致 / 正常响应不变")


def test_store_price_with_scrambled_outer_key():
    """端到端（取价路径）：外层 key 错、内层正确时照样取到价与免费标记。"""
    paid = {"1390430": {"success": True, "data": {
        "steam_appid": 582010, "price_overview": {"initial": 14800, "currency": "CNY"}}}}
    got, reachable = s._from_store_price(582010, "https://x/582010", 1.0,
                                         session=_FakeSession({582010: paid}), report=True)
    assert got == {"price": 14800} and reachable is True, got

    free = {"5290420": {"success": True, "data": {"steam_appid": 359550, "is_free": True}}}
    got, _ = s._from_store_price(359550, "https://x/359550", 1.0,
                                 session=_FakeSession({359550: free}), report=True)
    assert got == {"price": 0, "is_free": True}, got

    # 只有一条记录、内层 appid 却对不上 -> 不取价（宁可未知，也不写错）
    alien = {"999": {"success": True, "data": {"steam_appid": 12345,
                                              "price_overview": {"initial": 9900}}}}
    got, reachable = s._from_store_price(582010, "https://x/582010", 1.0,
                                         session=_FakeSession({582010: alien}), report=True)
    assert got == {} and reachable is True, got
    print("PASS 外层 key 错也能取价；内层不一致坚决不取；is_free 才算免费")


def test_steamcmd_node_validates_inner_appid():
    """steamcmd 镜像同样不轻信外层 key（第 557 行的加固）。"""
    good = {"status": "success", "data": {"999999": {
        "appid": 582010, "common": {"name": "Monster Hunter: World",
                                    "header_image": {"english": "hash/header.jpg"}}}}}
    got = s._from_steamcmd(582010, session=_FixedSession(good))
    assert got.get("name") == "Monster Hunter: World", got

    bad = {"status": "success", "data": {"999999": {
        "appid": 1, "common": {"name": "别的游戏"}}}}
    assert s._from_steamcmd(582010, session=_FixedSession(bad)) == {}
    print("PASS steamcmd 镜像：认记录自述的 appid，不一致则拒绝")


def test_miss_negative_cache_is_short_and_self_healing():
    """负缓存只做**短 TTL**；T7 之前写下的老行（没有 miss_at）必须立即失效重试。"""
    assert s._within_hours("", 6) is False, "没有时间戳的老负缓存行 -> 立即重试"
    assert s._within_hours(datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), 6) is True
    old = (datetime.now() - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%S")
    assert s._within_hours(old, 6) is False
    assert s.MISS_TTL_HOURS <= 6, "负缓存必须 <= 6 小时"

    import shutil
    from data import schema

    with tempfile.TemporaryDirectory(prefix="gl_miss_") as tmp:
        original = (schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY)
        schema.DB_PATH = Path(tmp) / "cache.db"
        s.init_db = schema.init_db
        s.get_connection = schema.get_connection
        s._PRICE_COLUMN_READY = False
        try:
            # 什么都没抓到 -> miss 行 + miss_at（短 TTL 内不重试）
            s._save({"appid": 5, "source": ""})
            row = s._cached_rows([5])[5]
            assert row["source"] == "miss" and row["miss_at"], row
            assert s._row_usable(row) is True

            # 手工模拟「T7 之前写下的老负缓存行」：把时间戳清掉
            db = s.get_connection()
            try:
                db.execute("UPDATE games SET miss_at=NULL WHERE appid=5")
                db.commit()
            finally:
                db.close()
            assert s._row_usable(s._cached_rows([5])[5]) is False, "老负缓存行必须立即失效"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY = original
    print("PASS 负缓存 6 小时短 TTL + 老行自动失效（不必等 3 天）")


def test_backfill_prices_multi_round_budget_and_resume():
    """温柔多轮：单轮预算到点停止、拿到即落库、下一轮只补没拿到的。"""
    originals = (s._price_from_official, s._save_price, s._cached_rows, s.PRICE_INTERVAL_RANGE)
    saved: dict = {}
    tries: dict = {}
    rounds_seen: list = []
    try:
        s.PRICE_INTERVAL_RANGE = (0.001, 0.002)      # 测试里不必真等 1.5 秒
        s._cached_rows = lambda ids: {}

        def _fake(aid, session=None):
            tries[aid] = tries.get(aid, 0) + 1
            time.sleep(0.05)
            return {"price": 100 * aid, "price_source": "xiaoheihe"} if aid % 2 == 0 else {}

        s._price_from_official = _fake
        s._save_price = lambda aid, data: saved.__setitem__(aid, dict(data))
        out = s.backfill_prices(list(range(1, 31)), quiet=True, rounds=3, round_budget=1.0,
                                on_round=lambda i, n, stats: rounds_seen.append(dict(stats)))
    finally:
        (s._price_from_official, s._save_price, s._cached_rows, s.PRICE_INTERVAL_RANGE) = originals

    assert rounds_seen, "必须回调每轮统计"
    first = rounds_seen[0]
    assert first["deferred"] > 0, f"预算到点应留下没跑完的：{first}"
    assert first["attempts"] + first["deferred"] == 30, first
    assert len(out) == 15 and all(v["price"] > 0 for v in out.values())
    assert sorted(saved) == sorted(out), "拿到必须**立即**落库"
    assert max(tries.values()) >= 2, "没取到的下一轮必须再试"
    print("PASS 价格多轮 + 单轮预算 + 拿到即落库 + 断点续做")


def test_doh_ips_union_and_priority():
    """DoH 必须**取并集**（不能第一个非空就返回）；腾讯优先、阿里最后。"""
    calls: list[str] = []
    table = {"doh.pub": "1.1.1.1", "1.12.12.12": "2.2.2.2", "doh.360.cn": "3.3.3.3",
             "dns.alidns.com": "4.4.4.4", "223.5.5.5": "5.5.5.5", "223.6.6.6": "6.6.6.6"}

    def _fake_get(url, timeout=None, headers=None):
        calls.append(str(url))
        for key, ip in table.items():
            if key in url:
                return _JsonResp({"Answer": [{"type": 1, "data": ip}]})
        return _JsonResp({"Answer": []})

    original = s.requests
    s.requests = types.SimpleNamespace(get=_fake_get)
    try:
        ips = s._doh_ips("store.steampowered.com")
    finally:
        s.requests = original
    assert ips == ["1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4", "5.5.5.5", "6.6.6.6"], ips
    assert len(calls) == 6, "必须问完所有 DoH 再取并集（历史 bug：阿里污染 IP 第一个就 return）"
    assert s.DOH_ENDPOINTS[0].startswith("https://doh.pub"), "腾讯必须排第一"
    assert s.DOH_ENDPOINTS[-1].startswith(("https://223.5.5.5", "https://223.6.6.6")), "阿里垫底"
    print("PASS DoH 取并集 + 优先级（腾讯 -> 360 -> 阿里）")


def test_pick_pinned_ip_validates_and_cools_down():
    """候选 IP 必须逐个真实校验；通过才钉住，全不通则主机进入分钟级冷却。"""
    import shutil
    tmp = tempfile.mkdtemp(prefix="gl_pin_")
    originals = (s._candidate_ips, s._validate_ip, s.ip_history_path)
    try:
        # 历史 IP 要落盘：因此这里必须把落盘路径指到临时目录，别污染真实 data/
        s.ip_history_path = lambda: Path(tmp) / "pinned_ips.json"
        s.reset_routes()
        s._candidate_ips = lambda host, use_cache=True: ["10.0.0.1", "10.0.0.2"]
        tried: list[str] = []

        def _validate(host, ip, timeout):
            tried.append(ip)
            return ip == "10.0.0.2"

        s._validate_ip = _validate
        assert s._pick_pinned_ip("h.example", 1.0) == "10.0.0.2"
        assert tried == ["10.0.0.1", "10.0.0.2"], "必须逐个校验，不能想当然"
        assert s.pinned_ip_status()["h.example"] == "10.0.0.2"

        s._PINNED_IPS.clear()
        s._validate_ip = lambda host, ip, timeout: False
        assert s._pick_pinned_ip("h2.example", 1.0) is None
        assert s._host_cooling("h2.example") is True, "全候选不通 -> 冷却，别每次都白试"
    finally:
        s._candidate_ips, s._validate_ip, s.ip_history_path = originals
        s.reset_routes()
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS 直连 IP 逐个校验 + 全不通冷却")


def test_host_cooldown_fails_fast_without_waiting():
    """处于阻断窗口（主机冷却）时，_store_json 必须快速返回，绝不白等超时。"""
    s.reset_routes()
    try:
        s._HOST_DOWN["blocked.example"] = time.time() + 60
        started = time.time()
        payload, reachable = s._store_json("https://blocked.example/api/appdetails", 5.0)
        spent = time.time() - started
    finally:
        s.reset_routes()
    assert payload is None and reachable is False
    assert spent < 0.5, f"冷却期内必须立刻返回（实测 {spent:.2f}s）"
    print("PASS 阻断窗口内快速失败（不耗超时预算）")


def test_fen_from_yuan_uses_decimal():
    """元 -> 分必须用 Decimal：11.6 * 100 用浮点会得到 1159.99…。"""
    assert s._fen_from_yuan("11.6") == 1160
    assert s._fen_from_yuan(11.6) == 1160
    assert s._fen_from_yuan("42") == 4200
    assert s._fen_from_yuan("¥ 21.00") == 2100
    assert s._fen_from_yuan("1,299.90") == 129990
    assert s._fen_from_yuan("0") == 0 and s._fen_from_yuan("") == 0
    assert s._fen_from_yuan(None) == 0 and s._fen_from_yuan("免费") == 0
    assert s._fen_from_yuan("-5") == 0
    print("PASS 元->分：Decimal 精确换算（含 11.6 浮点坑）")


def test_xhh_record_semantics_unknown_not_free():
    """免费只认 is_free：价格 0 且 is_free 为假 -> 什么都不要（= 未知）。"""
    assert s._xhh_record({"steam_appid": 1, "price": {"current": "0", "initial": "0"},
                          "is_free": False}) == {}
    free = s._xhh_record({"steam_appid": 1, "is_free": True, "price": {"initial": "0"}})
    assert free == {"price": 0, "is_free": True, "price_source": "xiaoheihe"}, free
    paid = s._xhh_record({"steam_appid": 582010, "name": "怪物猎人：世界",
                          "image": "https://img/1.jpg",
                          "price": {"initial": "148", "current": "148"}, "is_free": False})
    assert paid["price"] == 14800 and paid["name_cn"] == "怪物猎人：世界"
    assert paid["header_image"] == "https://img/1.jpg" and paid["price_source"] == "xiaoheihe"
    print("PASS 小黑盒记录语义：免费只认 is_free / 未知绝不当免费")


def test_xiaoheihe_batch_chunks_and_validates():
    """40 款/批 + 内层 steam_appid 校验（不在本批请求里的记录一律丢弃）。"""
    def _payload(url):
        chunk = [int(x) for x in str(url).split("appids=")[1].split(",")]
        infos = [{"steam_appid": aid, "name": f"中文{aid}",
                  "image": "https://img.example/x.jpg",
                  "price": {"initial": "11.6", "current": "11.6"}, "is_free": False}
                 for aid in chunk]
        infos.append({"steam_appid": 424242, "name": "不该出现",
                      "price": {"initial": "1"}})               # 不属于本批 -> 丢弃
        return {"result": {"base_infos": infos}}

    session = _FixedSession(_payload)
    got = s.xiaoheihe_batch(list(range(1, 46)), session=session, sleep_between=False)
    assert len(session.calls) == 2, session.calls
    assert "appids=" + ",".join(str(i) for i in range(1, 41)) in session.calls[0]
    assert session.calls[1].endswith("appids=41,42,43,44,45")
    assert len(got) == 45 and 424242 not in got
    assert got[1]["price"] == 1160 and got[1]["price_source"] == "xiaoheihe"
    assert got[1]["name_cn"] == "中文1" and got[1]["header_image"].startswith("https://")
    print("PASS 小黑盒分批（40/次）+ 内层 appid 校验")


def test_xiaoheihe_batch_failures_are_silent():
    """任何失败都静默降级（绝不抛异常、绝不拖垮补齐）。"""
    assert s.xiaoheihe_batch([], session=_FixedSession({})) == {}
    assert s.xiaoheihe_batch([1], session=_FixedSession(None)) == {}          # 连不上
    assert s.xiaoheihe_batch([1], session=_FixedSession({"x": 1}, status=500)) == {}
    assert s.xiaoheihe_batch([1], session=_FixedSession({"result": {}})) == {}  # 结构不认识
    assert s.xiaoheihe_batch([1], session=_FixedSession({"result": {"base_infos": [
        {"steam_appid": "abc", "price": {"initial": "5"}}]}})) == {}
    original = s.requests
    s.requests = None
    try:
        assert s.xiaoheihe_batch([1]) == {}
    finally:
        s.requests = original
    print("PASS 小黑盒失败静默降级（断网/500/结构异常/无 requests）")


def test_price_priority_xiaoheihe_then_official():
    """价格优先级：小黑盒命中就不问官方；小黑盒没有才回落官方，且绝不写 0 元/免费。"""
    originals = (s._xiaoheihe_one, s._from_store_price,
                 s._OFFICIAL_PRICE_BLOCKED, s._OFFICIAL_PRICE_FAILS)
    calls = {"official": 0}
    try:
        s._OFFICIAL_PRICE_BLOCKED = False
        s._OFFICIAL_PRICE_FAILS = 0
        s._xiaoheihe_one = lambda appid, session=None: {"price": 14800,
                                                       "price_source": "xiaoheihe"}

        def _official(appid, url, timeout, session=None, report=False):
            calls["official"] += 1
            return ({"price": 29800}, True) if report else {"price": 29800}

        s._from_store_price = _official
        assert s._price_from_official(582010) == {"price": 14800, "price_source": "xiaoheihe"}
        assert calls["official"] == 0, "小黑盒命中时不该再问官方商店"

        s._xiaoheihe_one = lambda appid, session=None: {}       # 未命中的那 13 款
        got = s._price_from_official(7)
        assert calls["official"] > 0, "小黑盒没有的必须回落官方商店"
        assert got.get("price", 0) > 0 and got.get("is_free") is not True
        assert got.get("price_source") == "steampowered"
    finally:
        (s._xiaoheihe_one, s._from_store_price,
         s._OFFICIAL_PRICE_BLOCKED, s._OFFICIAL_PRICE_FAILS) = originals
    print("PASS 价格优先级：小黑盒 -> 官方商店（缺的必须回落，不许当 0 元/免费）")


def test_save_upsert_merges_text_columns_without_erasing():
    """真库副本上验证 UPSERT 叠加：官方只带英文名/类型、小黑盒只带中文名/封面，
    两边必须先写后写都**不互相擦除**（T9 关键风险点，用临时库真写 SQLite）。"""
    import shutil
    from data import schema

    with tempfile.TemporaryDirectory(prefix="gl_upsert_") as tmp:
        original = (schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY)
        schema.DB_PATH = Path(tmp) / "cache.db"
        s.init_db = schema.init_db
        s.get_connection = schema.get_connection
        s._PRICE_COLUMN_READY = False
        try:
            # 1) 先落官方元数据：只有英文名 + 类型 + 开发商，没有中文名/封面/价格
            s._save({"appid": 582010, "name": "Monster Hunter: World",
                     "genres": ["Action", "RPG"], "developers": ["CAPCOM Co., Ltd."],
                     "price": 0, "source": "appinfo+steamcmd"})
            row = s._cached_rows([582010])[582010]
            assert row["name"] == "Monster Hunter: World" and row["genres"] == ["Action", "RPG"]
            assert row["name_cn"] == "" and row["header_image"] == ""
            assert s.price_state(row) == "unknown"
            before_updated = row["last_updated"]

            # 2) 再落小黑盒记录：只带中文名 / 封面 / 价格，**没有**英文名与类型
            s._save({"appid": 582010, "name_cn": "怪物猎人：世界",
                     "header_image": "https://img/x.jpg", "price": 14800,
                     "price_source": "xiaoheihe", "source": "appinfo+steamcmd+xiaoheihe"})
            row = s._cached_rows([582010])[582010]
            assert row["name"] == "Monster Hunter: World", "英文名被小黑盒覆盖擦掉了！"
            assert row["genres"] == ["Action", "RPG"], "类型被小黑盒覆盖擦掉了！"
            assert row["developers"] == ["CAPCOM Co., Ltd."], "开发商被擦掉了！"
            assert row["name_cn"] == "怪物猎人：世界" and row["header_image"] == "https://img/x.jpg"
            assert row["price"] == 14800 and row["price_source"] == "xiaoheihe"
            assert s.price_state(row) == "paid"
            assert before_updated <= row["last_updated"]

            # 3) 反向再来一次：先有小黑盒（中文名/封面/价），官方后写只带英文名/类型
            s._save({"appid": 1245620, "name_cn": "艾尔登法环",
                     "header_image": "https://img/y.jpg", "price": 29800,
                     "price_source": "xiaoheihe", "source": "xiaoheihe"})
            s._save({"appid": 1245620, "name": "ELDEN RING", "genres": ["Action"],
                     "price": 0, "source": "xiaoheihe+steamcmd"})
            row = s._cached_rows([1245620])[1245620]
            assert row["name_cn"] == "艾尔登法环", "中文名被官方记录擦掉了！"
            assert row["header_image"] == "https://img/y.jpg", "封面被擦掉了！"
            assert row["price"] == 29800 and row["price_source"] == "xiaoheihe", "价格被改回未知了！"
            assert row["name"] == "ELDEN RING"

            # 4) 免费标记是「一次性不可回退」的三态
            s._save({"appid": 359550, "name": "Rainbow Six Siege X", "source": "steamcmd"})
            s._save({"appid": 359550, "price": 0, "is_free": True,
                     "price_source": "xiaoheihe", "source": "steamcmd+xiaoheihe"})
            row = s._cached_rows([359550])[359550]
            assert row["is_free"] is True and s.price_state(row) == "free"
            s._save({"appid": 359550, "price": 0, "source": "steamcmd"})
            row = s._cached_rows([359550])[359550]
            assert row["is_free"] is True, "免费标记被无信息写入清掉了"
            print("PASS UPSERT 叠加：官方(英文名/类型) 与 小黑盒(中文名/封面/价) 互不擦除")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY = original


def test_xiaoheihe_pass_fills_gaps_only():
    """小黑盒补数只填空缺：已有的英文名/价格绝不被覆盖，来源如实叠加。"""
    result = {
        582010: {"appid": 582010, "name": "Monster Hunter: World", "name_cn": "",
                 "header_image": "", "price": 0, "is_free": None,
                 "source": "appinfo+steamcmd"},
        100: {"appid": 100, "name": "Counter-Strike", "name_cn": "反恐精英",
              "price": 29800, "is_free": None, "source": "appinfo+price"},
    }
    found = {582010: {"name_cn": "怪物猎人：世界", "header_image": "https://img/1.jpg",
                      "price": 14800, "price_source": "xiaoheihe"},
             100: {"price": 100, "price_source": "xiaoheihe"},
             999001: {"name_cn": "范围外", "price": 100, "price_source": "xiaoheihe"}}
    originals = (s.xiaoheihe_batch, s._save)
    saved: list = []
    try:
        s.xiaoheihe_batch = lambda appids, session=None, sleep_between=True: dict(found)
        s._save = lambda record: saved.append(dict(record))
        touched = s._xiaoheihe_pass(result, [582010, 100], quiet=True)
    finally:
        s.xiaoheihe_batch, s._save = originals

    assert touched == 1, "只有真的补到新信息的行才算被改"
    rec = result[582010]
    assert rec["name_cn"] == "怪物猎人：世界" and rec["price"] == 14800
    assert rec["name"] == "Monster Hunter: World", "已有的英文名绝不能被覆盖"
    assert "xiaoheihe" in rec["source"].split("+")
    assert result[100]["price"] == 29800, "已有价格绝不能被覆盖"
    assert 999001 not in result, "只处理本次请求范围内的 appid"
    print("PASS 小黑盒补数：只填空缺 + 来源叠加 + 不越界")


def test_price_source_survives_merge_and_lands_in_db():
    """回归：价格走完 ``_merge`` 之后 ``price_source`` 必须还在，并能随 ``_save`` 落库。

    历史 bug：``_FIELDS`` 白名单漏了 ``price_source``，``_merge`` 静默丢弃它
    → 真实补齐流程写出的行 ``source`` 含 ``+price``、``price_source`` 却是空串。
    """
    import shutil
    from data import schema

    stubs = (s._price_from_official, s._from_appinfo, s._from_steamcmd,
             s._from_steamchina, s._from_official)
    s._price_from_official = lambda appid, session=None: {"price": 14800,
                                                          "price_source": "xiaoheihe"}
    s._from_appinfo = lambda aid, steam_path=None: {"name": "MONSTER HUNTER: WORLD",
                                                    "name_cn": "怪物猎人：世界"}
    s._from_steamcmd = lambda aid, session=None: {}
    s._from_steamchina = lambda aid, session=None: {}
    s._from_official = lambda aid, session=None: {}
    try:
        record = s.fetch_one(582010, with_mirror=False)
    finally:
        (s._price_from_official, s._from_appinfo, s._from_steamcmd,
         s._from_steamchina, s._from_official) = stubs
    assert record["price"] == 14800
    assert record["price_source"] == "xiaoheihe", "price_source 被 _merge 丢掉了！"
    assert "price" in str(record["source"] or "").split("+")

    # 再走一遍真实 ``_save``（临时库，不碰真实缓存）确认它真的落库
    with tempfile.TemporaryDirectory(prefix="gl_psrc_") as tmp:
        original = (schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY)
        schema.DB_PATH = Path(tmp) / "cache.db"
        s.init_db = schema.init_db
        s.get_connection = schema.get_connection
        s._PRICE_COLUMN_READY = False
        try:
            s._save(record)
            row = s._cached_rows([582010])[582010]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY = original
    assert row["price"] == 14800 and row["price_source"] == "xiaoheihe", row
    assert "price" in str(row["source"] or "").split("+")
    print("PASS price_source 经 _merge 保留并落库（回归）")


def test_fetch_metadata_with_price_false_passes_with_price_down():
    """``with_price=False`` 必须**透传**到 ``fetch_one``：元数据阶段不许顺带取价。

    否则每个 appid 都会在元数据阶段偷偷取一次价（自带小黑盒单款请求），
    让独立的价格阶段与多轮兜底形同虚设。
    """
    originals = (s.fetch_one, s._cached_rows, s.backfill_prices, s.xiaoheihe_batch, s._save)
    seen: dict = {}

    def _fake_fetch_one(appid, steam_path=None, session=None,
                        with_mirror=True, with_price=True):
        seen["with_price"] = with_price
        return {"appid": appid, "name": "X", "source": "appinfo"}

    try:
        s._cached_rows = lambda ids: {}
        s.fetch_one = _fake_fetch_one
        s.backfill_prices = lambda appids, **kw: {}
        s.xiaoheihe_batch = lambda appids, session=None, sleep_between=True: {}
        s._save = lambda record: None          # 绝不写真实缓存库
        s.fetch_metadata([1], quiet=True, with_price=False)
    finally:
        (s.fetch_one, s._cached_rows, s.backfill_prices, s.xiaoheihe_batch,
         s._save) = originals
    assert seen.get("with_price") is False, seen
    print("PASS with_price=False 透传到 fetch_one（元数据阶段不偷偷取价）")


def test_metadata_store_path_tags_price_source():
    """元数据路径（蒸汽平台 / 官方商店）带回价格或免费时，也必须写明来源。

    历史缺口：``_from_official_store`` 会带出 ``price`` / ``is_free``（如 steamchina），
    却没标 ``price_source`` → 这几行会出现「有价无来源」（真库 5 行）。
    没有价格 / 免费信息的行必须**不写**，保持「取不到 = 未知」语义。
    """
    originals = (s._store_json, s._OFFICIAL_BLOCKED, s._OFFICIAL_BLOCKED_AT)

    def _payload(payload):
        return lambda url, timeout, session=None, attempts=1: (payload, True)

    paid = {"999": {"success": True, "data": {
        "steam_appid": 431960, "name": "Wallpaper Engine",
        "price_overview": {"initial": 2290, "currency": "CNY"}}}}
    free = {"999": {"success": True, "data": {"steam_appid": 359550, "is_free": True}}}
    plain = {"999": {"success": True, "data": {"steam_appid": 3590, "name": "PvZ"}}}
    try:
        s._OFFICIAL_BLOCKED = False
        s._store_json = _payload(paid)
        got_paid = s._from_steamchina(431960)
        s._store_json = _payload(free)
        got_free = s._from_official(359550)
        s._store_json = _payload(plain)
        got_plain = s._from_steamchina(3590)
    finally:
        s._store_json, s._OFFICIAL_BLOCKED, s._OFFICIAL_BLOCKED_AT = originals

    assert got_paid.get("price") == 2290 and got_paid.get("price_source") == "steamchina", got_paid
    assert got_free.get("is_free") is True and got_free.get("price_source") == "steampowered", got_free
    assert "price_source" not in got_plain, got_plain
    print("PASS 元数据商店路径也标明 price_source（仅有价/免费时）")


def test_cached_data_survives_total_source_outage():
    """**缓存永不回退**（T11）：所有源都不可达时跑一轮完整补齐，旧数据必须原封不动。

    用户点名的硬要求（「缓存是肯定要的」）。做法：往临时库种一行完整数据，
    把所有数据源打桩成「什么都拿不到」（不碰任何系统设置），再跑
    ``fetch_metadata(force=True)``（含小黑盒 + 独立价格阶段），逐字段比对。
    """
    import shutil
    from data import schema

    with tempfile.TemporaryDirectory(prefix="gl_outage_") as tmp:
        original = (schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY,
                    s._from_appinfo, s._from_steamcmd, s._from_steamchina, s._from_official,
                    s._from_extra, s._from_baike, s._price_from_official, s.xiaoheihe_batch)
        schema.DB_PATH = Path(tmp) / "cache.db"
        s.init_db = schema.init_db
        s.get_connection = schema.get_connection
        s._PRICE_COLUMN_READY = False
        try:
            s._save({"appid": 1245620, "name": "ELDEN RING", "name_cn": "艾尔登法环",
                     "genres": ["Action", "RPG"], "genres_cn": ["动作", "角色扮演"],
                     "categories": ["单人"], "developers": ["FromSoftware"],
                     "publisher": "万代南梦宫", "price": 29800, "is_free": False,
                     "release_date": "2022-02-25", "header_image": "https://img/x.jpg",
                     "source": "appinfo+extra+xiaoheihe", "price_source": "xiaoheihe"})
            before = dict(s._cached_rows([1245620])[1245620])
            assert before["price"] == 29800 and before["price_source"] == "xiaoheihe"

            # ---- 断网模拟：所有源都「什么都拿不到」 ----
            s._from_appinfo = lambda aid, steam_path=None: {}
            s._from_steamcmd = lambda aid, session=None: {}
            s._from_steamchina = lambda aid, session=None: {}
            s._from_official = lambda aid, session=None: {}
            s._from_extra = lambda aid: {}
            s._from_baike = lambda aid, name_en: {}
            s._price_from_official = lambda aid, session=None: {}
            s.xiaoheihe_batch = lambda appids, session=None, sleep_between=True: {}

            s.fetch_metadata([1245620], quiet=True, force=True)

            after = dict(s._cached_rows([1245620])[1245620])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            (schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY,
             s._from_appinfo, s._from_steamcmd, s._from_steamchina, s._from_official,
             s._from_extra, s._from_baike, s._price_from_official, s.xiaoheihe_batch) = original

    for key in ("name", "name_cn", "genres", "genres_cn", "price", "is_free",
                "price_source", "header_image", "release_date", "publisher", "developers"):
        assert after[key] == before[key], f"{key} 被清掉了：{before[key]!r} -> {after[key]!r}"
    print("PASS 全部源不可达时缓存一字未动（价格/中文名/封面/来源都在）")


def test_cached_gaps_fill_incrementally_without_refetching():
    """**跨次累积**（T11）：已有的不再问，缺的（价格）下次继续补，绝不重复抓。

    临时库种两行：A 齐全、B 只有英文名。跑一次正常（非 force）补齐，断言
    「一个 ``fetch_one`` 都不发」「只有 B 被问价」。
    """
    import shutil
    from data import schema

    with tempfile.TemporaryDirectory(prefix="gl_gap_") as tmp:
        original = (schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY,
                    s.fetch_one, s.xiaoheihe_batch, s._price_from_official, s._save_price,
                    s.PRICE_INTERVAL_RANGE)
        schema.DB_PATH = Path(tmp) / "cache.db"
        s.init_db = schema.init_db
        s.get_connection = schema.get_connection
        s._PRICE_COLUMN_READY = False
        fetch_calls: list = []
        price_calls: list = []
        xhh_calls: list = []
        try:
            s._save({"appid": 111, "name": "A", "name_cn": "甲", "price": 14800,
                     "price_source": "xiaoheihe", "header_image": "https://img/a.jpg",
                     "source": "appinfo+xiaoheihe"})
            s._save({"appid": 222, "name": "B", "source": "appinfo"})
            s.fetch_one = lambda *a, **kw: fetch_calls.append(a) or {}
            s._price_from_official = lambda aid, session=None: price_calls.append(aid) or {}
            s._save_price = lambda aid, data: None
            s.xiaoheihe_batch = lambda appids, session=None, sleep_between=True: (
                xhh_calls.append(list(appids)) or {})
            s.PRICE_INTERVAL_RANGE = (0.001, 0.002)
            out = s.fetch_metadata([111, 222], quiet=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            (schema.DB_PATH, s.init_db, s.get_connection, s._PRICE_COLUMN_READY,
             s.fetch_one, s.xiaoheihe_batch, s._price_from_official, s._save_price,
             s.PRICE_INTERVAL_RANGE) = original

    assert not fetch_calls, "两行都还在缓存有效期内，一个抓取请求都不该发"
    assert price_calls == [222], f"只该补缺价格的那行：{price_calls}"
    assert xhh_calls == [[111, 222]], f"小黑盒按批问（它只填空缺）：{xhh_calls}"
    assert out[111]["price"] == 14800 and out[222]["name"] == "B"
    print("PASS 跨次累积：命中缓存不重抓，只有缺价格的那行被问价")


def test_ip_history_persists_and_is_retried():
    """**历史成功 IP 落盘**（T11.3）：跨进程保留，新 IP 都不可用时被再试一次。"""
    import shutil
    with tempfile.TemporaryDirectory(prefix="gl_iphist_") as tmp:
        hist = Path(tmp) / "pinned_ips.json"
        original = (s.ip_history_path, s._doh_ips, s._system_ips, s._validate_ip)
        try:
            s.ip_history_path = lambda: hist
            s.reset_routes()
            s._doh_ips = lambda host, timeout=5.0: []
            s._system_ips = lambda host: []
            s._validate_ip = lambda host, ip, timeout: ip == "23.15.142.182"
            # 第一次：校验通过 -> 应落盘
            assert s._pick_pinned_ip("store.steampowered.com", 1.0) is None  # 候选里还没有它
            s._remember_ip("store.steampowered.com", "23.15.142.182")
            data = json.loads(hist.read_text(encoding="utf-8"))
            assert "store.steampowered.com" in data["hosts"], data
            ip, ts = data["hosts"]["store.steampowered.com"][0]
            assert ip == "23.15.142.182" and float(ts) > 0, data

            # 模拟「新进程」：清空内存 -> 从磁盘恢复；新解析的 IP 都不通，历史 IP 顶上
            s.reset_routes()
            s._doh_ips = lambda host, timeout=5.0: ["10.9.9.9"]
            got = s._pick_pinned_ip("store.steampowered.com", 1.0)
            assert got == "23.15.142.182", f"新 IP 不通时应回落到历史成功 IP：{got}"
            assert s.ip_history_status().get("store.steampowered.com") == ["23.15.142.182"]
        finally:
            s.ip_history_path, s._doh_ips, s._system_ips, s._validate_ip = original
            s.reset_routes()
            shutil.rmtree(tmp, ignore_errors=True)
    print("PASS 历史成功 IP 落盘 + 跨进程复用（新 IP 不通时再试一次）")


def main():
    tests = (
        test_price_state,
        test_from_store_price_parsing,
        test_price_for_and_backfill,
        test_fetch_one_takes_price_even_with_name,
        test_price_source_circuit_breaker,
        test_schema_migration_and_tristate_persistence,
        test_fetch_metadata_runs_price_pass,
        test_appdetails_node_trusts_inner_appid,
        test_store_price_with_scrambled_outer_key,
        test_steamcmd_node_validates_inner_appid,
        test_miss_negative_cache_is_short_and_self_healing,
        test_backfill_prices_multi_round_budget_and_resume,
        test_doh_ips_union_and_priority,
        test_pick_pinned_ip_validates_and_cools_down,
        test_host_cooldown_fails_fast_without_waiting,
        test_fen_from_yuan_uses_decimal,
        test_xhh_record_semantics_unknown_not_free,
        test_xiaoheihe_batch_chunks_and_validates,
        test_xiaoheihe_batch_failures_are_silent,
        test_price_priority_xiaoheihe_then_official,
        test_xiaoheihe_pass_fills_gaps_only,
        test_save_upsert_merges_text_columns_without_erasing,
        test_price_source_survives_merge_and_lands_in_db,
        test_fetch_metadata_with_price_false_passes_with_price_down,
        test_metadata_store_path_tags_price_source,
        test_cached_data_survives_total_source_outage,
        test_cached_gaps_fill_incrementally_without_refetching,
        test_ip_history_persists_and_is_retried,
    )
    for test in tests:
        test()
    print(f"\n全部 {len(tests)} 项价格路径验证通过")


if __name__ == "__main__":
    main()
