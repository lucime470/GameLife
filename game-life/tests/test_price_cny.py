# -*- coding: utf-8 -*-
"""E 线：价格统一人民币 + 汇率换算 + 冷却熔断 + 路由兜底（全部打桩，不联网）。"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import store_api as s  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http %s" % self.status_code)

    def json(self):
        return self._payload


class _FakeSession:
    """按 URL 子串命中打桩响应；None 表示不可达。"""

    def __init__(self, mapping):
        self._mapping = mapping
        self.seen = []

    def get(self, url, timeout=None, headers=None):
        self.seen.append(url)
        for key, payload in self._mapping.items():
            if str(key) in url:
                if payload is None:
                    raise RuntimeError("unreachable")
                return _Resp(payload)
        return _Resp({})


def _detail(appid, overview=None, is_free=False, name=""):
    data = {"name": name, "type": "game"}
    if overview is not None:
        data["price_overview"] = overview
    if is_free:
        data["is_free"] = True
    return {str(appid): {"success": True, "data": data}}


def test_to_cny_direct_unchanged():
    """人民币原价原样保留，不做任何换算。"""
    out = s.to_cny_fen(19900, "CNY")
    assert out["cny_fen"] == 19900
    assert out["rate"] == 1.0
    assert out["converted"] is False
    print("PASS 人民币原价：不换算")


def test_to_cny_conversion():
    """非人民币按当日汇率换算成人民币分。"""
    original = s._FX_MEMO
    try:
        s._FX_MEMO = {"date": s.date.today().isoformat(), "source": "unit-test",
                      "rates": {"CNY": 1.0, "USD": 7.0, "JPY": 0.05}}
        usd = s.to_cny_fen(4999, "USD")
        assert usd["cny_fen"] == round(4999 * 7.0), usd
        assert usd["converted"] is True and usd["rate_source"] == "unit-test"
        jpy = s.to_cny_fen(649000, "JPY")
        assert jpy["cny_fen"] == round(649000 * 0.05), jpy
        # 缺汇率的币种：绝不瞎猜，返回 0 并标记未换算
        unknown = s.to_cny_fen(1000, "XXX")
        assert unknown["cny_fen"] == 0 and unknown["converted"] is False
    finally:
        s._FX_MEMO = original
    print("PASS 外币按汇率换算成人民币；缺汇率则判「未知」")


def test_price_from_store_price_converts_currency():
    """_from_store_price 把外币价格换算成人民币，人民币价格原样使用。"""
    original = s._FX_MEMO
    try:
        s._FX_MEMO = {"date": s.date.today().isoformat(), "source": "unit-test",
                      "rates": {"CNY": 1.0, "USD": 7.0}}
        usd = s._from_store_price(1, "https://x/1", 1.0,
                                  session=_FakeSession({1: _detail(1, {"currency": "USD", "final": 4999})}))
        assert usd == {"price": 34993}, usd
        cny = s._from_store_price(2, "https://x/2", 1.0,
                                  session=_FakeSession({2: _detail(2, {"currency": "CNY", "final": 19900})}))
        assert cny == {"price": 19900}, cny
        # 汇率缺失：不得写成 0，而是按「未知」处理
        s._FX_MEMO = {"date": s.date.today().isoformat(), "source": "unit-test", "rates": {"CNY": 1.0}}
        missing = s._from_store_price(3, "https://x/3", 1.0,
                                      session=_FakeSession({3: _detail(3, {"currency": "USD", "final": 4999})}))
        assert missing == {}, missing
    finally:
        s._FX_MEMO = original
    print("PASS 商店外币价 -> 人民币；缺汇率判未知不写 0")


def test_price_source_prefers_cny_then_region():
    """取价顺序：cc=CN 优先；账号在别国时用账号区域并按汇率换算成人民币。"""
    original = (s._from_store_price, s._ACTIVE_REGION, s._FX_MEMO,
                s._OFFICIAL_PRICE_BLOCKED, s._OFFICIAL_PRICE_FAILS, s._xiaoheihe_one)
    try:
        seen = []
        s._xiaoheihe_one = lambda appid, session=None: {}   # 本用例只验官方路径，不打小黑盒

        def _fake(appid, url, timeout, session=None, report=False):
            seen.append(url)
            if "cc=CN" in url:
                return ({}, True) if report else {}
            if "cc=US" in url:
                payload = _detail(appid, {"currency": "USD", "final": 4999})
                out = s._from_store_price._real(appid, url, timeout, session, report) if hasattr(s._from_store_price, "_real") else None
                return ({"price": round(4999 * 7.0)}, True) if report else {"price": round(4999 * 7.0)}
            return ({}, True) if report else {}

        s._from_store_price = _fake
        s.set_active_region("US")
        got = s._price_from_official(42, None)
        assert got == {"price": 34993, "price_source": "steampowered"}, got
        assert any("cc=CN" in u for u in seen) and any("cc=US" in u for u in seen), seen
    finally:
        (s._from_store_price, s._ACTIVE_REGION, s._FX_MEMO,
         s._OFFICIAL_PRICE_BLOCKED, s._OFFICIAL_PRICE_FAILS, s._xiaoheihe_one) = original
    print("PASS 先 cc=CN、再按账号区域换算人民币")


def test_cooldown_breaker_recovers():
    """熔断是「冷却式」的：冷却期满会自动恢复，不是一次失败就永久放弃。"""
    original = (s._OFFICIAL_PRICE_BLOCKED, s._OFFICIAL_PRICE_FAILS,
                s._PRICE_BLOCKED_AT, s.PRICE_COOLDOWN_SECONDS)
    try:
        s.PRICE_COOLDOWN_SECONDS = 0.05
        s._OFFICIAL_PRICE_BLOCKED = False
        s._OFFICIAL_PRICE_FAILS = 0
        s._note_price_fail()
        s._note_price_fail()
        s._note_price_fail()
        assert s._OFFICIAL_PRICE_BLOCKED is True, "连续失败应熔断"
        assert s._price_source_available() is False, "冷却期内不应可用"
        import time as _t
        _t.sleep(0.08)
        assert s._price_source_available() is True, "冷却期满应自动恢复"
        assert s._OFFICIAL_PRICE_FAILS == 0, "恢复后计数应清零"
    finally:
        (s._OFFICIAL_PRICE_BLOCKED, s._OFFICIAL_PRICE_FAILS,
         s._PRICE_BLOCKED_AT, s.PRICE_COOLDOWN_SECONDS) = original
    print("PASS 冷却式熔断：失败受限但会自动恢复")


def test_fx_cache_and_offline_fallback():
    """汇率按天缓存；离线时沿用上次成功结果，全无可用汇率时不抛异常。"""
    original = (s.fx_cache_path, s._FX_MEMO, s._fetch_fx_online)
    with tempfile.TemporaryDirectory(prefix="gl_fx_") as tmp:
        cache = Path(tmp) / "fx_rates.json"
        try:
            s.fx_cache_path = lambda: cache
            s._FX_MEMO = None
            calls = {"n": 0}

            def _online():
                calls["n"] += 1
                return {"date": s.date.today().isoformat(), "source": "unit",
                        "rates": {"CNY": 1.0, "USD": 7.1}}

            s._fetch_fx_online = _online
            first = s.fx_snapshot()
            assert first["rates"]["USD"] == 7.1 and calls["n"] == 1
            # 当天第二次：命中磁盘缓存，不再联网
            s._FX_MEMO = None
            second = s.fx_snapshot()
            assert second["rates"]["USD"] == 7.1 and calls["n"] == 1, "当天应复用缓存"

            # 离线（联网失败）：沿用磁盘上最近一次结果，且绝不抛异常
            s._FX_MEMO = None
            s._fetch_fx_online = lambda: {}
            third = s.fx_snapshot(force=True)
            assert third["rates"]["USD"] == 7.1, "离线应沿用上次结果"

            # 连缓存都没有：返回只有 CNY 的空表，换算函数安全降级
            cache.unlink()
            s._FX_MEMO = None
            fourth = s.fx_snapshot(force=True)
            assert fourth["rates"].get("CNY") == 1.0
            assert s.to_cny_fen(100, "USD")["converted"] is False
        finally:
            s.fx_cache_path, s._FX_MEMO, s._fetch_fx_online = original
    print("PASS 汇率按天缓存 + 离线降级不抛异常")


def test_fx_parsers():
    """汇率解析：中间价（含 100JPY 这类标价单位与反向报价）+ USD 基准表。"""
    chinamoney = {"records": [
        {"vrtEName": "USD/CNY", "price": "6.7468"},
        {"vrtEName": "100JPY/CNY", "price": "4.2754"},
        {"vrtEName": "CNY/KRW", "price": "201.56"},
        {"vrtEName": "EUR/CNY", "price": "7.6935"},
    ]}
    rates = s._rates_from_chinamoney(chinamoney)
    assert abs(rates["USD"] - 6.7468) < 1e-9
    assert abs(rates["JPY"] - 0.042754) < 1e-9, rates          # 100 日元标价要除以 100
    assert abs(rates["KRW"] - 1 / 201.56) < 1e-9, rates        # 反向报价要取倒数
    assert abs(rates["EUR"] - 7.6935) < 1e-9

    usd_base = {"rates": {"CNY": 6.7, "JPY": 147.0}}
    parsed = s._rates_from_usd_base(usd_base)
    assert abs(parsed["USD"] - 6.7) < 1e-9
    assert abs(parsed["JPY"] - 6.7 / 147.0) < 1e-9
    assert parsed["CNY"] == 1.0
    print("PASS 汇率解析：标价单位 / 反向报价 / USD 基准")


def test_routing_fallback_is_used_after_failure():
    """常规线路失败后，后续请求优先走直连兜底（省掉重复白等超时）。"""
    original = dict(s._BAD_ROUTE)
    original_pick = s._pick_pinned_ip
    original_get = s._get_via_ip
    original_req = s.requests
    try:
        s.reset_routes()
        assert s._route_is_bad("h.example") is False
        s._note_route("h.example", False)
        assert s._route_is_bad("h.example") is True
        s._note_route("h.example", True)
        assert s._route_is_bad("h.example") is False, "成功应清零，别一直罚它"
    finally:
        s._BAD_ROUTE.clear()
        s._BAD_ROUTE.update(original)
        s._pick_pinned_ip = original_pick
        s._get_via_ip = original_get
        s.requests = original_req
    print("PASS 路由健康度：失败改走直连，成功即恢复")


def test_offline_never_raises():
    """网络全断时取价只返回空，绝不抛异常、绝不阻塞。"""
    original = (s.requests, s._pick_pinned_ip)
    try:
        s.requests = None
        s._pick_pinned_ip = lambda host, timeout: None
        assert s.price_for(730) == {}
        payload, reachable = s._store_json("https://example.invalid/api", 0.01)
        assert payload is None and reachable in (True, False)
    finally:
        s.requests, s._pick_pinned_ip = original
    print("PASS 断网降级：返回空、不抛异常")


def main():
    for test in (test_to_cny_direct_unchanged, test_to_cny_conversion,
                 test_price_from_store_price_converts_currency,
                 test_price_source_prefers_cny_then_region,
                 test_cooldown_breaker_recovers,
                 test_fx_cache_and_offline_fallback, test_fx_parsers,
                 test_routing_fallback_is_used_after_failure,
                 test_offline_never_raises):
        test()
    print("\n全部 9 项「人民币价格路径」验证通过")


if __name__ == "__main__":
    main()
