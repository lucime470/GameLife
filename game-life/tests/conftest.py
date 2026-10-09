"""测试进程级隔离：**绝不读这台机器真实的 Steam 客户端缓存**。

T12 线起 :func:`core.ownership.client_owned_appids` 会去读 ``%LOCALAPPDATA%\\Steam\\htmlcache``
里的 CEF cache（本机实测 5300+ 文件 / 441 MB，一次扫盘约 0.4-3s）。测试用例大量调用
``classify`` / ``compute_series_tree``，若不隔离就会：

  * 把**开发者本机的真实游戏**混进断言（换台机器结果就变，CI 上直接红）；
  * 每个用例都扫一遍盘（本机实测 69 个用例从 38s 涨到 213s）。

这里把 ``core.ownership`` 的 ``_LOCALAPPDATA`` 指向一个不存在的目录：``client_owned_appids``
于是返回 ``None``（= 信息不足），**恰好还原 T12 之前的行为** —— 老用例断言的「只用票面」
语义保持不变；专门测客户端缓存的用例自行注入 ``client_owned`` / 直接造 blob 即可。
"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _disable_steam_live_source():
    """整个测试会话期间关掉 T22 的 Steam 客户端实时归属源。

    它经 ``127.0.0.1:8080`` 连本机 Steam 客户端，还会读 ``data`` 下的落盘快照 ——
    两者都是**这台机器**的状态：留着跑，断言就会随「开发者此刻开着哪个账号」变化。
    关掉之后 :func:`core.ownership.live_owned_appids` 一律返回 ``None``，
    恰好还原 T22 之前的行为，老用例语义（只用票面 / 客户端缓存）保持不变；
    专门测实时源的用例自行注入假快照即可（见 ``tests/test_steam_live.py``）。
    """
    from core import ownership, steam_live

    previous = os.environ.get(steam_live.ENV_DISABLE)
    os.environ[steam_live.ENV_DISABLE] = "0"
    ownership.reset_cache()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(steam_live.ENV_DISABLE, None)
        else:
            os.environ[steam_live.ENV_DISABLE] = previous
        ownership.reset_cache()


@pytest.fixture(scope="session", autouse=True)
def _isolate_steam_client_cache(tmp_path_factory):
    """整个测试会话期间，让客户端缓存根指向一个不存在的目录。"""
    from core import ownership

    bogus = tmp_path_factory.mktemp("no-steam-client") / "absent"
    original = ownership._LOCALAPPDATA
    ownership._LOCALAPPDATA = str(bogus)
    ownership.reset_cache()
    try:
        yield
    finally:
        ownership._LOCALAPPDATA = original
        ownership.reset_cache()
