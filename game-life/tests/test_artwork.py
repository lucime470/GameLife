r"""封面模块（core/artwork.py）的最小自测。

运行方式（game-life 目录）：
    ..\.venv\Scripts\python.exe tests\test_artwork.py
或（仓库根目录）：
    .\.venv\Scripts\python.exe game-life\tests\test_artwork.py

零额外依赖，纯标准库 + 打桩；**全部使用临时目录**，不写真实 data/covers、
不发起任何真实网络请求（只有最后一项「联网冒烟」会真跑，且失败不算错）。

覆盖：
    1. cover_url_candidates：顺序（eccdnx 第一）、四个镜像齐全、size 变体、
       带哈希的官方路径、非法 appid 返回空；
    2. 缓存优先：data/covers/<appid>.jpg 命中时**完全不联网**（断言打桩未被调用）；
    3. 本机 Steam librarycache 命中（临时造假目录，断言选中的就是那个文件）；
    4. 40 位哈希命名 jpg 兜底：优先横版、优先更大的；
    5. 坏响应优雅降级：HTTP 500 / Content-Type 非图片 / 体积 < 1000B / 直接抛异常，
       一律 ok=False 且**绝不抛异常**；
    6. 第一个源失败后自动换下一个源并成功（source=cdn:<host>）；
    7. 原子落盘：文件非空、无 .tmp 残留；
    8. 清单一 index.json：正常可读、损坏时能自动重建；
    9. cover_for_panel 只查本地、不联网；
   10. appid 安全：畸形/非数字/负数/0/路径穿越一律被拒，不产生越界文件；
   11. ensure_covers 批量统计：total / ok / failed / skipped / unsupported 与 by_source；
   12. 哈希命名的**无扩展名**图片（Steam 真实形态）：按魔数识别，32x32 占位图被跳过；
   13. 哈希**子目录** <appid>/<40位哈希>/ 里的 library_header.jpg 等也能命中，
       含 _schinese 本地化变体；顶层具名图优先级不被破坏；
   14. logo.png 不会被兜底选中，也不会被当成封面；
   15. 联网冒烟（可跳过）：真实拉一张 appid=570 的封面头。
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import artwork  # noqa: E402

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


class FakeResponse:
    """够用的假响应对象（模拟 requests.Response 的三个关键属性）。"""

    def __init__(self, status_code=200, content=b"", content_type="image/jpeg"):
        self.status_code = status_code
        self.content = content
        self.headers = {"Content-Type": content_type} if content_type is not None else {}
        self.closed = False

    @property
    def text(self):  # pragma: no cover - 用不到
        return ""

    def close(self):
        self.closed = True


def fake_png(width: int = 460, height: int = 215, pad: int = 2000) -> bytes:
    """造一张「能被解析出宽高」的最小 PNG 头（魔数合法，不是真图）。"""
    body = bytearray()
    body += b"\x89PNG\r\n\x1a\n"
    body += (13).to_bytes(4, "big") + b"IHDR"
    body += width.to_bytes(4, "big") + height.to_bytes(4, "big")
    body += bytes([8, 2, 0, 0, 0])
    body += b"\x00\x00\x00\x00"
    body += (0).to_bytes(4, "big") + b"IEND" + b"\x00\x00\x00\x00"
    if pad > len(body):
        body += b"\x00" * (pad - len(body))
    return bytes(body)


def fake_webp(width: int = 460, height: int = 215, pad: int = 2000) -> bytes:
    """造一张「能被解析出宽高」的最小 WebP 头（RIFF/WEBP/VP8）。"""
    payload = bytearray()
    payload += b"\x00\x00\x00"                 # frame tag
    payload += b"\x9d\x01\x2a"                 # start code
    payload += (width & 0x3FFF).to_bytes(2, "little")
    payload += (height & 0x3FFF).to_bytes(2, "little")
    chunk = b"VP8 " + len(payload).to_bytes(4, "little") + bytes(payload)
    riff = b"RIFF" + (len(chunk) + 4).to_bytes(4, "little") + b"WEBP" + chunk
    if pad > len(riff):
        riff += b"\x00" * (pad - len(riff))
    return riff


def fake_jpeg(width: int = 460, height: int = 215, pad: int = 2000) -> bytes:
    """造一张「能被解析出宽高」的最小 JPEG 头（不是真图，但魔数/结构合法）。"""
    body = bytearray()
    body += b"\xff\xd8"                      # SOI
    body += b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    body += b"\xff\xc0" + (17).to_bytes(2, "big") + bytes([8])
    body += height.to_bytes(2, "big") + width.to_bytes(2, "big")
    body += bytes([3]) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    body += b"\xff\xd9"
    if pad > len(body):
        body += b"\x00" * (pad - len(body))
    return bytes(body)


@contextlib.contextmanager
def temp_covers():
    """把封面缓存目录指向临时目录（绝不碰真实 data/covers）。"""
    with tempfile.TemporaryDirectory(prefix="gl_covers_") as tmp:
        original = artwork._COVERS_DIR
        artwork._COVERS_DIR = Path(tmp) / "covers"
        try:
            yield artwork._COVERS_DIR
        finally:
            artwork._COVERS_DIR = original


@contextlib.contextmanager
def temp_steam():
    """造一个假的 Steam 根目录，带 appcache/librarycache。"""
    with tempfile.TemporaryDirectory(prefix="gl_steam_") as tmp:
        root = Path(tmp)
        (root / "appcache" / "librarycache").mkdir(parents=True, exist_ok=True)
        yield root


@contextlib.contextmanager
def stub_http(handler):
    """替换 artwork._http_get，返回调用过的 url 列表。"""
    original = artwork._http_get
    calls: list[str] = []

    def fake(url, timeout):
        calls.append(url)
        return handler(url, timeout)

    artwork._http_get = fake
    try:
        yield calls
    finally:
        artwork._http_get = original


def ok_response(_url, _timeout):
    return FakeResponse(200, fake_jpeg(), "image/jpeg")


# --------------------------------------------------------------------------
# 1. 候选地址
# --------------------------------------------------------------------------

def test_cover_url_candidates():
    urls = artwork.cover_url_candidates(570)
    check(len(urls) == 5, f"默认应给出 5 个候选（header + 5 镜像去重后），实际 {len(urls)}: {urls}")
    check("media.st.dl.eccdnx.com" in urls[0], f"第一个候选应是 eccdnx（实测最快），实际 {urls[0] if urls else None}")
    for host in ("media.st.dl.eccdnx.com", "shared.cdn.steamchina.eccdnx.com",
                 "steamcdn-a.akamaihd.net",
                 "shared.akamai.steamstatic.com", "cdn.cloudflare.steamstatic.com"):
        check(any(host in u for u in urls), f"缺少镜像 {host}")
    # 蒸汽平台 CDN 的路径带 /store_item_assets 前缀（与其它镜像不同），单独校验
    china = [u for u in urls if "shared.cdn.steamchina.eccdnx.com" in u]
    check(china and "/store_item_assets/steam/apps/570/header.jpg" in china[0],
          f"蒸汽平台 CDN 路径前缀不对：{china[:1]}")
    check(all("/steam/apps/570/header.jpg" in u for u in urls),
          f"候选地址应包含 appid 与 header.jpg: {urls}")
    check(all(u.startswith("https://") for u in urls), "候选地址必须走 https")

    portrait = artwork.cover_url_candidates(570, "library_600x900")
    check(any(u.endswith("library_600x900.jpg") for u in portrait), "size 变体应命中对应用户文件")
    check(any(u.endswith("header.jpg") for u in portrait), "竖版找不到时应回退到 header.jpg")

    hint = {"english": "e9047d8ec47ae3d94bb8b464fb0fc9e9972b4ac7/header.jpg"}
    hashed = artwork.cover_url_candidates(570, "header", hint)
    check(hashed and "e9047d8ec47ae3d94bb8b464fb0fc9e9972b4ac7/header.jpg" in hashed[0],
          f"给了哈希线索时，带哈希的官方路径应排最前，实际 {hashed[:1]}")
    plain = "e9047d8ec47ae3d94bb8b464fb0fc9e9972b4ac7/header.jpg"
    check(artwork.cover_url_candidates(570, "header", plain)[0].endswith(plain),
          "字符串形态的哈希线索也要支持")
    check(artwork.cover_url_candidates(570, "header", "header.jpg")[0].endswith("/header.jpg"),
          "无哈希线索时不该拼出哈希目录")

    check(artwork.cover_url_candidates("not-an-id") == [], "非法 appid 应返回空列表")
    check(artwork.cover_url_candidates(0) == [], "appid=0 应返回空列表")


# --------------------------------------------------------------------------
# 2. 缓存优先（不联网）
# --------------------------------------------------------------------------

def test_cache_hit_never_touches_network():
    with temp_covers() as covers, temp_steam() as steam:
        covers.mkdir(parents=True, exist_ok=True)
        cached = covers / "730.jpg"
        cached.write_bytes(fake_jpeg())

        def boom(_url, _timeout):
            raise AssertionError("缓存已命中，不应该联网")

        with stub_http(boom) as calls:
            result = artwork.ensure_cover(730, steam_path=str(steam))

        check(calls == [], f"缓存命中时不应发起网络请求，实际请求了 {calls}")
        check(result["ok"] is True, f"缓存命中应 ok=True，实际 {result}")
        check(result["source"] == "cache", f"来源应为 cache，实际 {result['source']}")
        check(result["path"] == str(cached), f"应返回缓存文件本身，实际 {result['path']}")
        check(result["bytes"] > 0, "应报告文件字节数")
        check(artwork.cover_for_panel(730, steam_path=str(steam)) == cached,
              "cover_for_panel 应返回缓存路径")

        # 字符串形态的 appid 也要能命中
        check(artwork.local_cover("730", str(steam)) == cached, "字符串 appid 应同样命中缓存")


# --------------------------------------------------------------------------
# 3/4. 本机 librarycache + 哈希兜底
# --------------------------------------------------------------------------

def test_local_librarycache_and_hash_fallback():
    with temp_covers(), temp_steam() as steam:
        lc = steam / "appcache" / "librarycache"
        check(artwork.find_librarycache_dir(str(steam)) == lc,
              f"find_librarycache_dir 应返回 {lc}")
        check(artwork.find_librarycache_dir(str(steam / "nope")) is None,
              "不存在的 Steam 路径应返回 None，而不是抛异常")
        check(artwork.find_librarycache_dir("") is not None or True, "空路径不应抛异常（允许 None）")

        # 只有 header.jpg
        folder = lc / "111"
        folder.mkdir(parents=True)
        header = folder / "header.jpg"
        header.write_bytes(fake_jpeg(460, 215))
        path, kind = artwork.local_cover_ex(111, str(steam))
        check(path == header, f"应选中 header.jpg，实际 {path}")
        check(kind == "header", f"来源标记应为 header，实际 {kind}")
        check(artwork.cover_for_panel(111, str(steam)) == header, "cover_for_panel 应命中 librarycache")

        # 只有竖版图
        folder2 = lc / "222"
        folder2.mkdir(parents=True)
        portrait = folder2 / "library_600x900.jpg"
        portrait.write_bytes(fake_jpeg(600, 900))
        path2, kind2 = artwork.local_cover_ex(222, str(steam))
        check(path2 == portrait, f"应选中 library_600x900.jpg，实际 {path2}")
        check(artwork.cover_kind(portrait) == "portrait", "竖版图应被识别为 portrait")

        # 只有 40 位哈希命名的 jpg —— 优先横版、优先更大的
        folder3 = lc / "333"
        folder3.mkdir(parents=True)
        small_land = folder3 / ("a" * 40 + ".jpg")
        big_portrait = folder3 / ("b" * 40 + ".jpg")
        small_land.write_bytes(fake_jpeg(300, 100, pad=1200))
        big_portrait.write_bytes(fake_jpeg(300, 900, pad=4000))
        path3, kind3 = artwork.local_cover_ex(333, str(steam))
        check(path3 == small_land,
              f"哈希图里应优先横版，实际选中 {path3.name if path3 else None}")
        check(kind3 == "hash", f"来源标记应为 hash，实际 {kind3}")

        # 目录存在但没有任何图 -> None
        (lc / "444").mkdir(parents=True)
        check(artwork.local_cover(444, str(steam)) is None, "空目录应返回 None")

        # 完全没有该 appid 目录 -> None，且不抛异常
        check(artwork.local_cover(555, str(steam)) is None, "缺失目录应返回 None")
        check(artwork.local_cover(555, str(steam / "nope")) is None, "坏 Steam 路径应返回 None")


# --------------------------------------------------------------------------
# 5. 坏响应优雅降级
# --------------------------------------------------------------------------

def test_bad_responses_degrade_gracefully():
    cases = [
        ("HTTP 500", lambda u, t: FakeResponse(500, fake_jpeg(), "image/jpeg")),
        ("Content-Type 非图片", lambda u, t: FakeResponse(200, fake_jpeg(), "text/html")),
        ("体积过小", lambda u, t: FakeResponse(200, b"\xff\xd8" + b"\x00" * 100, "image/jpeg")),
        ("连接异常", lambda u, t: (_ for _ in ()).throw(ConnectionError("模拟连接失败"))),
        ("超时", lambda u, t: (_ for _ in ()).throw(TimeoutError("模拟超时"))),
        ("返回 None", lambda u, t: None),
    ]
    for label, handler in cases:
        with temp_covers() as covers, temp_steam() as steam:
            try:
                with stub_http(handler):
                    result = artwork.ensure_cover(999999, steam_path=str(steam), timeout=0.5)
            except Exception as exc:  # 绝不应该走到这里
                check(False, f"[{label}] ensure_cover 抛异常了: {type(exc).__name__}: {exc}")
                continue
            check(result["ok"] is False, f"[{label}] 应 ok=False，实际 {result}")
            check(not result["path"], f"[{label}] 失败时不应给出路径，实际 {result['path']}")
            check(bool(result["error"]), f"[{label}] 应带中文错误说明，实际 {result['error']!r}")
            check(result["source"] == "none", f"[{label}] 失败时来源应为 none")
            leftover = list(covers.glob("*")) if covers.exists() else []
            check(leftover == [], f"[{label}] 失败不应留下垃圾文件，实际 {leftover}")


def test_second_source_succeeds_and_writes_atomically():
    hosts = [u.split("/")[2] for u in artwork.cover_url_candidates(570)]
    with temp_covers() as covers, temp_steam() as steam:
        state = {"n": 0}

        def handler(url, timeout):
            state["n"] += 1
            if state["n"] == 1:
                raise ConnectionError("第一个镜像挂了")
            return FakeResponse(200, fake_jpeg(), "image/jpeg")

        with stub_http(handler) as calls:
            result = artwork.ensure_cover(570, steam_path=str(steam), timeout=1.0)

        check(result["ok"] is True, f"第二个源应成功，实际 {result}")
        check(result["source"] == f"cdn:{hosts[1]}",
              f"来源应为第二个镜像 {hosts[1]}，实际 {result['source']}")
        target = covers / "570.jpg"
        check(target.is_file() and target.stat().st_size > 0, f"应落盘到 {target}")
        check(result["path"] == str(target), "返回路径应指向缓存文件")
        check(result["bytes"] == target.stat().st_size, "bytes 应等于真实文件大小")
        check(list(covers.glob("*.tmp")) == [], "落盘后不应残留 .tmp 文件")
        check(len(calls) >= 2, "失败后应换下一个源继续尝试")

        # 第二次调用直接吃缓存，不再联网
        def boom(_u, _t):
            raise AssertionError("不该联网")

        with stub_http(boom) as calls2:
            again = artwork.ensure_cover(570, steam_path=str(steam))
        check(calls2 == [], "第二次应命中缓存，不再联网")
        check(again["source"] == "cache", f"第二次来源应为 cache，实际 {again['source']}")


def test_all_sources_fail_returns_error():
    with temp_covers(), temp_steam() as steam:
        def handler(_url, _timeout):
            raise ConnectionError("全部镜像都不可达")

        with stub_http(handler):
            result = artwork.ensure_cover(570, steam_path=str(steam), timeout=0.5)
        check(result["ok"] is False, "全源失败应 ok=False")
        check("不可达" in (result["error"] or "") or bool(result["error"]),
              f"应给出失败原因，实际 {result['error']!r}")


# --------------------------------------------------------------------------
# 6. 索引
# --------------------------------------------------------------------------

def test_index_is_readable_and_rebuildable():
    with temp_covers() as covers, temp_steam() as steam:
        check(artwork.cover_index() == {}, "空目录应返回空索引")

        with stub_http(ok_response):
            artwork.ensure_cover(570, steam_path=str(steam))

        index_path = covers / "index.json"
        check(index_path.is_file(), f"应生成清单 {index_path}")
        data = json.loads(index_path.read_text(encoding="utf-8"))
        check("570" in data, f"清单里应有 570，实际 {list(data)}")
        entry = data["570"]
        for key in ("source", "bytes", "sha256", "updated_at"):
            check(key in entry, f"清单条目缺少字段 {key}: {entry}")
        check(len(entry["sha256"]) == 64, "sha256 应为 64 位十六进制")
        check(entry["bytes"] > 0, "bytes 应大于 0")
        check(artwork.cover_index().get("570", {}).get("sha256") == entry["sha256"],
              "cover_index() 读回的应与磁盘一致")

        # 损坏的清单 -> 自动重建，不抛异常
        index_path.write_text("{ 这不是合法 JSON ", encoding="utf-8")
        rebuilt = artwork.cover_index()
        check("570" in rebuilt, f"损坏后应能按磁盘重建索引，实际 {list(rebuilt)}")
        check(json.loads(index_path.read_text(encoding="utf-8")).get("570") is not None,
              "重建结果应回写磁盘")

        # 完全不是对象的清单
        index_path.write_text("[1, 2, 3]", encoding="utf-8")
        check(isinstance(artwork.cover_index(), dict), "非对象清单也应被安全处理")


# --------------------------------------------------------------------------
# 7. 面板接口 & appid 安全
# --------------------------------------------------------------------------

def test_cover_for_panel_is_local_only():
    with temp_covers() as covers, temp_steam() as steam:
        def boom(_u, _t):
            raise AssertionError("面板取图不允许联网")

        with stub_http(boom) as calls:
            check(artwork.cover_for_panel(570, str(steam)) is None, "没有本地图应返回 None")
            covers.mkdir(parents=True, exist_ok=True)
            (covers / "570.jpg").write_bytes(fake_jpeg())
            path = artwork.cover_for_panel(570, str(steam))
        check(calls == [], f"cover_for_panel 不应发起网络请求，实际 {calls}")
        check(path == covers / "570.jpg", f"应返回缓存路径，实际 {path}")
        check(artwork.mime_for_cover(path) == "image/jpeg", "mime 应识别 jpeg")


def test_appid_safety():
    bad = ["..\\..\\evil", "../../evil", "abc", "", "  ", None, 0, -1, 3.5, "1e3",
           "１２３", True, {"a": 1}, [1]]
    with temp_covers() as covers, temp_steam() as steam:
        with stub_http(ok_response) as calls:
            for raw in bad:
                result = artwork.ensure_cover(raw, steam_path=str(steam))
                check(result["ok"] is False, f"畸形 appid {raw!r} 应 ok=False，实际 {result}")
                check(bool(result["error"]), f"畸形 appid {raw!r} 应给出错误说明")
                check(artwork.local_cover(raw, str(steam)) is None,
                      f"畸形 appid {raw!r} 的本地查询应为 None")
                check(artwork.cover_url_candidates(raw) == [],
                      f"畸形 appid {raw!r} 不应生成任何 URL")
        check(calls == [], f"畸形 appid 不应触发网络请求，实际 {calls}")
        written = list(covers.glob("**/*")) if covers.exists() else []
        check(written == [], f"畸形 appid 不应写出任何文件，实际 {written}")

    # 缓存目录必须落在 data/covers 之内
    root = Path(artwork.__file__).resolve().parent.parent
    check(artwork._COVERS_DIR == root / "data" / "covers",
          f"缓存目录应在 game-life/data/covers 内，实际 {artwork._COVERS_DIR}")
    check(".." not in str(artwork._COVERS_DIR.resolve()), "缓存目录不应含路径穿越")


# --------------------------------------------------------------------------
# 8. 批量统计
# --------------------------------------------------------------------------

def test_ensure_covers_batch_stats():
    with temp_covers() as covers, temp_steam() as steam:
        covers.mkdir(parents=True, exist_ok=True)
        (covers / "100.jpg").write_bytes(fake_jpeg())
        (covers / "200.jpg").write_bytes(fake_jpeg())

        def handler(_url, _timeout):
            raise ConnectionError("这个测试里不联网")

        seen: list[tuple[int, int, str]] = []

        def progress(done, total, result):
            seen.append((done, total, str(result.get("appid"))))

        with stub_http(handler):
            report = artwork.ensure_covers(
                [100, 200, "bad-id", 300],
                steam_path=str(steam),
                progress=progress,
            )

        check(report["total"] == 4, f"total 应为 4，实际 {report['total']}")
        check(report["ok"] == 2, f"ok 应为 2（缓存的 100/200），实际 {report['ok']}")
        check(report["failed"] == 1, f"failed 应为 1（300 拉不到），实际 {report['failed']}")
        check(report["unsupported"] == 1, f"unsupported 应为 1（bad-id），实际 {report['unsupported']}")
        check(report["skipped"] == 0, f"skipped 应为 0，实际 {report['skipped']}")
        check(report["by_source"].get("cache") == 2, f"来源统计应含 cache=2，实际 {report['by_source']}")
        check(len(report["results"]) == 4, "results 应与输入等长")
        check([s[0] for s in seen] == [1, 2, 3, 4], f"progress 应按顺序回调，实际 {seen}")
        check(all(s[1] == 4 for s in seen), "progress 的 total 应恒定")

        # limit：超出的计入 skipped，不再尝试
        with stub_http(handler) as calls:
            limited = artwork.ensure_covers([100, 200, 300], steam_path=str(steam), limit=1)
        check(limited["total"] == 3, f"limit 下 total 仍为 3，实际 {limited['total']}")
        check(limited["ok"] == 1, f"limit=1 应只成功 1 个，实际 {limited['ok']}")
        check(limited["skipped"] == 2, f"limit=1 应跳过 2 个，实际 {limited['skipped']}")
        check(calls == [], "被 limit 挡下的项不应联网（100 命中缓存，其余跳过）")

        # 接受 dict 输入（游戏库本身就是 {appid: {...}}）
        with stub_http(handler):
            as_dict = artwork.ensure_covers({100: {"name": "a"}, 200: {"name": "b"}},
                                            steam_path=str(steam))
        check(as_dict["total"] == 2 and as_dict["ok"] == 2, f"应支持 dict 输入，实际 {as_dict}")


# --------------------------------------------------------------------------
# 9. 联网冒烟（可选，失败不算错）
# --------------------------------------------------------------------------

def test_network_smoke():
    if os.environ.get("GL_SKIP_NETWORK") == "1":
        print("      - 联网冒烟被 GL_SKIP_NETWORK=1 跳过")
        return
    with temp_covers() as covers, temp_steam() as steam:
        if covers.exists() and any(covers.iterdir()):
            pass
        started = time.time()
        result = artwork.ensure_cover(570, steam_path=str(steam), timeout=8.0)
        cost = time.time() - started
        if not result["ok"]:
            print(f"      - 联网冒烟未成功（跳过，不算失败）: {result['error']}（{cost:.1f}s）")
            return
        check(result["bytes"] > _MIN_OK, f"抓到的封面应大于 1000 字节，实际 {result['bytes']}")
        check(result["source"].startswith("cdn:"), f"联网成功时来源应为 cdn:<host>，实际 {result['source']}")
        path = Path(result["path"])
        check(path.is_file() and path.stat().st_size > _MIN_OK, "落盘文件应存在且非空")
        head = path.read_bytes()[:3]
        check(head[:2] == b"\xff\xd8", f"文件头应是 JPEG，实际 {head!r}")
        tiny = artwork.image_size(path)
        check(tiny is not None, "image_size 应能解析出真实封面尺寸")
        print(f"      - 联网冒烟: appid=570 source={result['source']} "
              f"bytes={result['bytes']} size={tiny} 用时={cost:.1f}s")


# --------------------------------------------------------------------------
# 13. 哈希命名的无扩展名图片（Steam 真实形态）
# --------------------------------------------------------------------------

def test_hash_named_files_without_extension():
    """Steam 用内容哈希命名素材，很多文件**没有扩展名**，必须按魔数识别。"""
    with temp_covers(), temp_steam() as steam:
        lc = steam / "appcache" / "librarycache"

        # 40 位十六进制 + 无扩展名的真实（结构合法）JPEG
        folder = lc / "8001"
        folder.mkdir(parents=True)
        noext = folder / ("a" * 40)
        noext.write_bytes(fake_jpeg(460, 215, pad=3000))
        check(artwork.looks_like_image_file(noext), "无扩展名的 JPEG 应被魔数识别为图片")
        check(artwork.image_size(noext) == (460, 215), "无扩展名文件也应能解析出尺寸")
        path, kind = artwork.local_cover_ex(8001, str(steam))
        check(path == noext, f"无扩展名的哈希图应被选中，实际 {path}")
        check(kind == "hash", f"来源标记应为 hash，实际 {kind}")
        check(artwork.cover_for_panel(8001, str(steam)) == noext, "cover_for_panel 也应命中")

        # 40 位十六进制 + .png / .webp 也要认
        folder2 = lc / "8002"
        folder2.mkdir(parents=True)
        png = folder2 / ("b" * 40 + ".png")
        png.write_bytes(fake_png(600, 900, pad=3000))
        check(artwork.local_cover(8002, str(steam)) == png, "哈希命名的 .png 应被选中")

        folder3 = lc / "8003"
        folder3.mkdir(parents=True)
        webp = folder3 / ("c" * 40 + ".webp")
        webp.write_bytes(fake_webp(460, 215, pad=3000))
        check(artwork.image_size(webp) == (460, 215), "webp 应能解析出尺寸")
        check(artwork.local_cover(8003, str(steam)) == webp, "哈希命名的 .webp 应被选中")

        # 只有 32x32 占位图 -> 判定为不可用，返回 None
        folder4 = lc / "8004"
        folder4.mkdir(parents=True)
        (folder4 / ("d" * 40 + ".jpg")).write_bytes(fake_jpeg(32, 32, pad=1200))
        check(artwork.local_cover(8004, str(steam)) is None,
              "只有 32x32 占位图时不应被当作封面")

        # 占位图 + 真图并存 -> 跳过占位图，选真图
        folder5 = lc / "8005"
        folder5.mkdir(parents=True)
        (folder5 / ("e" * 40 + ".jpg")).write_bytes(fake_jpeg(32, 32, pad=1200))
        real = folder5 / ("f" * 40 + ".jpg")
        real.write_bytes(fake_jpeg(1920, 620, pad=5000))
        check(artwork.local_cover(8005, str(steam)) == real,
              "占位图与真图并存时应选真图")

        # 名字像 40 位哈希、但内容不是图片 -> 不能当封面
        folder6 = lc / "8006"
        folder6.mkdir(parents=True)
        (folder6 / ("1" * 40 + ".jpg")).write_bytes(b"not an image at all" + b"x" * 2000)
        check(artwork.local_cover(8006, str(steam)) is None,
              "哈希命名但内容不是图片的文件不应被选中")


# --------------------------------------------------------------------------
# 14. 哈希子目录（新版 Steam 把素材放在 <appid>/<40位哈希>/ 里）
# --------------------------------------------------------------------------

def test_hash_subdirectory_covers():
    with temp_covers(), temp_steam() as steam:
        lc = steam / "appcache" / "librarycache"
        h1 = "a" * 40
        h2 = "b" * 40

        # 子目录里的 library_header.jpg —— 这就是本机真实命中形态
        d1 = lc / "9001" / h1
        d1.mkdir(parents=True)
        header = d1 / "library_header.jpg"
        header.write_bytes(fake_jpeg(460, 215, pad=3000))
        path, kind = artwork.local_cover_ex(9001, str(steam))
        check(path == header, f"应命中哈希子目录里的 library_header.jpg，实际 {path}")
        check(artwork.cover_for_panel(9001, str(steam)) == header, "cover_for_panel 也应命中")

        # 子目录里的 library_capsule.jpg（竖版一族）
        d2 = lc / "9002" / h2
        d2.mkdir(parents=True)
        capsule = d2 / "library_capsule.jpg"
        capsule.write_bytes(fake_jpeg(300, 450, pad=3000))
        check(artwork.local_cover(9002, str(steam)) == capsule,
              "子目录里的 library_capsule.jpg 应被选中")

        # 本地化变体 _schinese（中文版素材）
        d3 = lc / "9003" / h1
        d3.mkdir(parents=True)
        schinese = d3 / "library_header_schinese.jpg"
        schinese.write_bytes(fake_jpeg(460, 215, pad=3000))
        check(artwork.local_cover(9003, str(steam)) == schinese,
              "应能识别 _schinese 中文本地化变体")

        # 顶层 header.jpg 优先于哈希子目录（历史优先级不被破坏）
        d4 = lc / "9004" / h1
        d4.mkdir(parents=True)
        (d4 / "library_header.jpg").write_bytes(fake_jpeg(1920, 620, pad=6000))
        top_header = lc / "9004" / "header.jpg"
        top_header.write_bytes(fake_jpeg(460, 215, pad=3000))
        check(artwork.local_cover(9004, str(steam)) == top_header,
              "顶层 header.jpg 应优先于哈希子目录里的图")

        # 子目录里只有 logo.png -> 不算封面
        d5 = lc / "9005" / h1
        d5.mkdir(parents=True)
        (d5 / "logo.png").write_bytes(fake_png(640, 360, pad=3000))
        check(artwork.local_cover(9005, str(steam)) is None,
              "子目录里只有 logo.png 时不应返回封面")

        # 顶层只有 32x32 占位图，真图在子目录 -> 应选子目录里的真图
        d6 = lc / "9006" / h2
        d6.mkdir(parents=True)
        real = d6 / "library_600x900.jpg"
        real.write_bytes(fake_jpeg(300, 450, pad=4000))
        (lc / "9006" / (h1 + ".jpg")).write_bytes(fake_jpeg(32, 32, pad=1200))
        check(artwork.local_cover(9006, str(steam)) == real,
              "顶层只有占位图时应回退到哈希子目录里的真图")


# --------------------------------------------------------------------------
# 15. logo 排除 & 任意图兜底
# --------------------------------------------------------------------------

def test_logo_is_never_used_as_cover():
    with temp_covers(), temp_steam() as steam:
        lc = steam / "appcache" / "librarycache"

        # 目录里只有 logo.png（体积很大）-> 兜底也不该选它
        folder = lc / "9101"
        folder.mkdir(parents=True)
        (folder / "logo.png").write_bytes(fake_png(640, 360, pad=8000))
        check(artwork._pick_any_image([folder]) is None, "logo.png 不应被兜底选中")
        check(artwork.local_cover(9101, str(steam)) is None, "只有 logo 时不应返回封面")

        # logo.png + 一张散图 -> 应选散图
        folder2 = lc / "9102"
        folder2.mkdir(parents=True)
        (folder2 / "logo.png").write_bytes(fake_png(640, 360, pad=9000))
        shot = folder2 / "screenshot.png"
        shot.write_bytes(fake_png(1280, 720, pad=3000))
        picked = artwork._pick_any_image([folder2])
        check(picked == shot, f"应选非 logo 的散图，实际 {picked}")
        check(artwork.local_cover(9102, str(steam)) == shot, "散图兜底应被 cover_for_panel 认同")

        # mime：无扩展名文件也要按魔数给出正确 MIME（否则浏览器不显示）
        noext = lc / "9103" / ("a" * 40)
        noext.parent.mkdir(parents=True)
        noext.write_bytes(fake_jpeg(460, 215, pad=3000))
        check(artwork.mime_for_cover(noext) == "image/jpeg",
              f"无扩展名 JPEG 的 MIME 应为 image/jpeg，实际 {artwork.mime_for_cover(noext)}")
        webp = lc / "9103" / ("b" * 40)
        webp.write_bytes(fake_webp(460, 215, pad=3000))
        check(artwork.mime_for_cover(webp) == "image/webp",
              f"无扩展名 WebP 的 MIME 应为 image/webp，实际 {artwork.mime_for_cover(webp)}")


_MIN_OK = 1000


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tests = [
        test_cover_url_candidates,
        test_cache_hit_never_touches_network,
        test_local_librarycache_and_hash_fallback,
        test_bad_responses_degrade_gracefully,
        test_second_source_succeeds_and_writes_atomically,
        test_all_sources_fail_returns_error,
        test_index_is_readable_and_rebuildable,
        test_cover_for_panel_is_local_only,
        test_appid_safety,
        test_ensure_covers_batch_stats,
        test_hash_named_files_without_extension,
        test_hash_subdirectory_covers,
        test_logo_is_never_used_as_cover,
        test_network_smoke,
    ]
    started = time.time()
    for index, test in enumerate(tests, 1):
        name = test.__name__
        print(f"  [{index}/{len(tests)}] {name} ...", end="", flush=True)
        before = len(FAILURES)
        try:
            test()
        except Exception as exc:  # 测试自身炸了也要报出来
            import traceback
            FAILURES.append(f"{name} 抛异常: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        delta = len(FAILURES) - before
        print(" 失败" if delta else " 通过")
        # 让联网冒烟等真网络测试把 print 刷出来
        sys.stdout.flush()

    if FAILURES:
        print(f"\n共 {len(FAILURES)} 项断言失败：")
        for item in FAILURES:
            print(f"  - {item}")
        print(f"\n失败 {len(FAILURES)} 项，未通过")
        return 1

    print(f"\n全部 {len(tests)} 项验证通过（用时 {time.time() - started:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())