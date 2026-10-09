"""G4 面板验收测试：六个可视化块「整块可点」+ 游戏族谱系列详情页（带封面）。

运行方式（仓库根目录）：
    python game-life/tests/test_webapp_series.py      # 独立跑
    pytest game-life/tests -q                         # 全量跑

约束（照 test_webapp.py 的约定）：
    - 不访问网络：封面上网在 pytest 下由 engine.series_tree._network_disabled() 自动关闭；
    - 不写真实 Steam 目录，只打桩；
    - 真实库相关断言一律跳过，不拖累整体通过。
"""

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_ROOT = REPO_ROOT / "game-life"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from webapp import api, server  # noqa: E402

# T6：测试进程里**绝不允许**自动补齐任务真的出网。
# standalone 跑本文件时 pytest 不在 sys.modules 里，光靠 api 自己的 pytest 探测挡不住，
# 所以这里显式拉闸（只影响后台补齐线程，面板渲染本来就不出网）。
os.environ.setdefault("GAMELIFE_JOB_NO_NETWORK", "1")

STATIC_DIR = APP_ROOT / "webapp" / "static"


class Panel:
    """后台线程里的真实面板实例（port=0 -> 随机空闲端口，不抢用户的 8765）。"""

    def __init__(self, **kwargs):
        self.httpd = server.build_server(port=0, **kwargs)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def request(self, path, timeout=120):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode("utf-8")

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)


# --------------------------------------------------------------------- 任务 1：整块可点

def test_frames_are_clickable_as_a_whole():
    """六个可视化块：整块可点 + 块内可滚（右侧滚动条可用），且不再依赖纯文字链接。

    回归防线（T2）——下面每条都只看**真实代码**，不看注释里出现过什么字符串：
      1. iframe 不设 pointer-events（设了 = 它自己的滚动条既拖不动、又收不到滚轮）；
      2. 遮罩 .frame-hit 显式留出右侧滚动条缝（不能 inset: 0 盖满整块，把滚动条埋掉）；
      3. app.js 有 SCROLLBAR_GUTTER 且与 CSS 留缝一致，滚轮转发 forwardWheelToFrame 已接线。

    反面教训：本用例以前断言 `"pointer-events: none" in css`，即「iframe 必须禁用指针事件，
    否则点击会被吞掉」—— 那正是 T2 要修掉的 bug（滚动条拖不动就是这么来的）。改成新契约后
    它仍能通过，仅仅因为那句话还留在 CSS 注释里；一旦有人清理注释，正确代码反而会误红。
    所以这里统一改成「先剥注释、再按声明/代码判断」。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    # 同样剥注释再看：否则这些也会变成「注释里写一句就永远绿」的假通过
    js_code_all = _strip_js_comments(js)
    assert "frame-hit" in js_code_all, "app.js 缺少 frame-hit 点击遮罩"
    assert 'target="_blank"' in js_code_all, "可视化块缺少新标签打开"
    assert 'window.open(href, "_blank"' in js_code_all, "标题栏区域点击未接线到新标签打开"
    assert "新标签打开 ↗" not in js_code_all, "旧的纯文字链接仍然存在（用户要求去掉）"

    # 遮罩仍要有「整块可点」的样子（去注释后看真实声明）
    hit = _css_declarations(css, ".frame-hit {")
    assert hit, "style.css 缺少 .frame-hit 样式"
    assert "cursor: pointer" in hit, f"整块缺少手型光标：{hit!r}"

    # ---- 滚动条契约（T2）--------------------------------------------------
    # 1) iframe **不**禁用指针事件：禁用后它自己的滚动条既拖不动、也收不到滚轮。
    #    只把 none 当回归（显式写 auto 无害，不该误红）；父级 .frame-body 也一并管住，
    #    因为父级设 none 时子元素照样收不到事件。
    iframe_decls = _css_declarations(css, ".frame-body iframe {")
    assert iframe_decls, "style.css 缺少 .frame-body iframe 规则"
    for selector in (".frame-body iframe {", ".frame-body {"):
        try:
            decls = _css_declarations(css, selector)
        except ValueError:
            continue
        assert _declared_pointer_events(decls) != "none", (
            f"{selector[:-2]} 又被设成 pointer-events: none：它的滚动条会拖不动、"
            f"滚轮也收不到（T2 回归）。实际声明：{decls!r}"
        )
    # 2) 遮罩必须给右侧滚动条留缝（0 = 盖满整块，把滚动条埋了 —— T2 的病根）
    #    只算「右侧实际留缝」，不禁止任何等价写法（right: 17px 与 inset: 0 17px 0 0 都算对）
    gap = _frame_hit_right_gap(hit)
    assert gap is not None, f"遮罩没有给出右侧留缝（right / inset 都没有）：{hit!r}"
    assert 8 <= gap <= 24, (
        f"遮罩右侧留缝 {gap}px 不合理：0 就是盖满整块把滚动条埋掉（T2 回归）；"
        f"Windows 经典滚动条约 17px。实际声明：{hit!r}"
    )

    # 3) app.js：常量 + 滚轮转发都要在，且常量与 CSS 留缝一致（两处必须同步）
    gutter = re.search(r"const\s+SCROLLBAR_GUTTER\s*=\s*([0-9]+)\s*;", js_code_all)
    assert gutter, "app.js 缺少 SCROLLBAR_GUTTER 常量"
    assert int(gutter.group(1)) == gap, (
        f"SCROLLBAR_GUTTER={gutter.group(1)} 与 style.css 的遮罩留缝 {gap}px 不一致（两处必须同步）"
    )
    assert "function forwardWheelToFrame(" in js_code_all, "app.js 缺少滚轮转发 forwardWheelToFrame()"
    assert re.search(r"forwardWheelToFrame\(\s*wrap\s*\)", js_code_all), (
        "铺块时没有调用 forwardWheelToFrame(wrap)：滚轮转发没接线"
    )
    print(
        f"PASS 任务1：整块可点（遮罩留缝 {gap}px = SCROLLBAR_GUTTER）+ "
        "iframe 未禁用指针事件 + 滚轮转发已接线；旧文字链接已移除"
    )


def test_frame_hint_is_hover_only():
    """「点击打开 ↗」提示默认不可见，悬停该块才淡入（用户不要常驻文字链接感）。"""
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8").replace("\n", " ")
    base = _rule_block(css, ".frame .frame-hint {")
    hover = _rule_block(css, ".frame.clickable:hover .frame-hint")
    assert "opacity: 0" in base and "visibility: hidden" in base, f"提示默认应不可见：{base}"
    assert "opacity: 1" in hover and "visibility: visible" in hover, f"悬停应淡入：{hover}"
    assert "focus-within" in css, "键盘聚焦也应能看到提示"
    print(f"PASS 悬停提示：默认 [{base.strip()}] · 悬停 [{hover.strip()}]")


def test_series_boxes_are_clickable():
    """族谱：每个系列一个框，整框是链接，点开系列详情页。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "series-grid" in js and "sbox" in js, "app.js 里族谱未改成卡片框"
    assert 'class="sbox" href="${esc(href)}" target="_blank"' in js, "系列框不是整框可点的链接"
    assert "/series?" in js, "系列框没有指向系列详情页"
    assert ".series-grid" in css and ".sbox" in css, "style.css 缺少族谱卡片样式"
    print("PASS 任务2：族谱每个系列一个框，整框点击在新标签打开系列详情页")


# --------------------------------------------------------------------- 任务 2：详情页与接口

def test_series_page_served():
    """GET /series 返回自包含 HTML（自己调 /api/series 渲染）。"""
    panel = Panel()
    try:
        status, ctype, body = panel.request("/series")
        assert status == 200, f"/series 状态码 {status}：{body[:200]}"
        assert "text/html" in ctype, f"/series Content-Type 异常：{ctype}"
        assert "系列详情" in body, "详情页缺少标志文案"
        assert "/api/series?" in body, "详情页没有调用 /api/series"
        assert "/static/style.css" in body, "详情页没有复用面板样式"
        print(f"PASS GET /series：200 + HTML（{len(body)} 字节）")
    finally:
        panel.close()


def test_series_api_bad_input():
    """未知系列 404、缺 name 400、特殊字符不崩不 500、不泄露 traceback。"""
    panel = Panel()
    try:
        q = urllib.parse.urlencode
        status, _ctype, body = panel.request("/api/series?" + q({"name": "绝对不存在的系列"}))
        assert status == 404, f"未知系列应 404，实际 {status}：{body[:200]}"
        assert "Traceback" not in body, "响应体泄露了 traceback"
        print(f"PASS /api/series 未知系列：404 + {json.loads(body)['error'][:20]}")

        status, _ctype, body = panel.request("/api/series")
        assert status == 400, f"缺 name 应 400，实际 {status}：{body[:200]}"
        print("PASS /api/series 缺 name：400")

        for weird in ("A/B 测试 & <x> 'y'", "  空格前后  ", "系列#1?%2F"):
            status, _ctype, body = panel.request("/api/series?" + q({"name": weird}))
            assert status == 404, f"特殊字符 {weird!r} 应 404，实际 {status}"
            assert "Traceback" not in body, f"特殊字符 {weird!r} 泄露 traceback"
        print("PASS /api/series 特殊字符（斜杠/与号/尖括号/井号/百分号/空格）：404 不崩")
    finally:
        panel.close()


FAKE_TREE = {
    "series_count": 1,
    "total_titles": 3,
    "series": [
        {
            # _series_section 负责把官方英文名本地化，打桩这里直接给本地化后的名字
            "series": "怪物猎人",
            "official_name": "Monster Hunter",
            "owned_count": 1,
            "total_count": 3,
            "completed_count": 0,
            "playtime_minutes": 600,
            "playtime_hours": 10.0,
            "games": [
                {"appid": 582010, "name": "Monster Hunter: World", "owned": True,
                 "status": "在玩", "playtime_minutes": 600, "playtime_hours": 10.0},
                {"appid": 1446780, "name": "MONSTER HUNTER RISE", "owned": False,
                 "status": "", "playtime_minutes": 0, "playtime_hours": 0.0},
                {"appid": 2246340, "name": "Monster Hunter Wilds", "owned": False,
                 "status": "", "playtime_minutes": 0, "playtime_hours": 0.0},
            ],
        }
    ],
}


def _fake_lib():
    return {
        "steam_path": r"C:\fake\steam",
        "account_id": "10001",
        "play_stats": {582010: {"playtime": 600, "last_played": 1700000000}},
        "games_meta": {582010: {"name": "Monster Hunter: World", "genres": ["动作"]}},
        "statuses": {582010: "在玩"},
        "classified": {582010: {"status": "在玩", "status_name": "在玩"}},
        "named": 1,
    }


def test_series_api_detail_shape():
    """详情接口：全部成员（含未拥有）、已拥有在前、封面字段齐全、计数正确。"""
    original_require = api._require_library
    original_section = api._series_section
    api._require_library = lambda account_id=None, with_metadata=False, steam_path=None: _fake_lib()
    api._series_section = lambda lib: json.loads(json.dumps(FAKE_TREE))
    panel = Panel()
    try:
        qs = urllib.parse.urlencode({"name": "怪物猎人", "account_id": "10001", "covers": "0"})
        status, _ctype, body = panel.request(f"/api/series?{qs}")
        assert status == 200, f"/api/series 状态码 {status}：{body[:300]}"
        data = json.loads(body)
        assert data["series"] == "怪物猎人", f"系列名未本地化：{data['series']}"
        assert data["total_count"] == 3, f"成员总数应为 3：{data}"
        assert data["owned_count"] == 1, f"已拥有应为 1：{data}"
        rows = data["games"]
        assert len(rows) == 3, "未拥有的作品也必须返回（用户要求展示整个系列）"
        assert rows[0]["appid"] == 582010 and rows[0]["owned"] is True, "已拥有的作品应排在最前"
        assert all({"appid", "name", "name_cn", "owned", "status", "playtime_hours",
                    "cover", "cover_kind", "genres"} <= set(r) for r in rows), "卡片行字段缺失"
        assert isinstance(data["with_cover"], int) and "covers" in data, "缺少封面统计"
        print(f"PASS /api/series：3 部（已拥有 {data['owned_count']}）· 有封面 {data['with_cover']} · 字段齐全")

        # 也支持用官方英文名查询（面板外的入口容错）
        qs2 = urllib.parse.urlencode({"name": "Monster Hunter", "account_id": "10001", "covers": "0"})
        status, _ctype, body = panel.request(f"/api/series?{qs2}")
        assert status == 200, f"按官方英文名查询应 200，实际 {status}"
        print("PASS /api/series：官方英文名查询同样可用")
    finally:
        api._require_library = original_require
        api._series_section = original_section
        panel.close()


def test_series_covers_offline_skips():
    """pytest 环境禁止出网：补齐封面直接 skipped，不发一个请求。"""
    original_net = api._series_network_disabled
    api._series_network_disabled = lambda: True          # 强制离线，两种跑法结果一致
    try:
        stats = api.ensure_series_covers([999000001, 999000002], None, budget=2.0)
    finally:
        api._series_network_disabled = original_net
    assert stats["requested"] == 2 and stats["skipped"] == 2, f"离线应全部 skipped：{stats}"
    assert stats["ok"] == 0 and stats["failed"] == 0, f"离线不应有成功/失败：{stats}"
    print(f"PASS 离线补齐：{stats}（不出网、不报错）")


def test_series_covers_bounded_and_degrades():
    """卡住/报错的图源：请求按预算返回、逐个降级、绝不抛异常、进程不被拖住。"""
    from core import artwork

    original = artwork.ensure_cover
    original_net = api._series_network_disabled
    api._series_network_disabled = lambda: False         # 强制允许联网，走真实补齐逻辑
    try:
        def hang(appid, steam_path=None, timeout=8.0, header_image_hint=None):
            time.sleep(30)
            return {"appid": appid, "ok": True}

        artwork.ensure_cover = hang
        started = time.time()
        stats = api.ensure_series_covers(
            [999100001 + i for i in range(8)], None, workers=3, budget=2.0, timeout=0.1
        )
        elapsed = time.time() - started
        assert elapsed < 6.0, f"卡住的下载把请求拖了 {elapsed:.1f}s（应有总耗时预算）"
        assert stats["requested"] == 8 and stats["ok"] == 0, f"卡住时不应有成功：{stats}"
        print(f"PASS 封面补齐有界：{stats} 用时 {elapsed:.1f}s（预算 2.0s）")

        def boom(appid, steam_path=None, timeout=8.0, header_image_hint=None):
            raise RuntimeError("打桩：图源挂了")

        artwork.ensure_cover = boom
        stats2 = api.ensure_series_covers([999200001, 999200002], None, workers=2, budget=3.0)
        assert stats2["failed"] == 2 and stats2["ok"] == 0, f"图源报错应逐个降级：{stats2}"
        print(f"PASS 封面补齐失败降级：{stats2}（不抛异常）")
    finally:
        artwork.ensure_cover = original
        api._series_network_disabled = original_net


def test_series_section_adds_local_thumbs():
    """面板系列框的缩略图只用本机已有封面（绝不为了画缩略图出网）。"""
    from engine import series_tree as st

    original_tree = st.compute_series_tree
    original_url = api._cover_url
    try:
        st.compute_series_tree = lambda *a, **k: {
            "series_count": 1,
            "total_titles": 2,
            "series": [{
                "series": "Monster Hunter",
                "owned_count": 1,
                "total_count": 2,
                "games": [
                    {"appid": 582010, "name": "MHW", "owned": True},
                    {"appid": 2246340, "name": "MH Wilds", "owned": False},
                ],
            }],
        }
        # 打桩：582010 本机有图，2246340 没有
        api._cover_url = lambda appid, steam_path=None: (
            f"/covers/{int(appid)}.jpg" if int(appid) == 582010 else ""
        )
        lib = {"play_stats": {582010: {}}, "games_meta": {}, "statuses": {}, "steam_path": None}
        section = api._series_section(lib)
        item = section["series"][0]
        assert item["thumbs"] == ["/covers/582010.jpg"], f"缩略图应只含本机已有封面：{item.get('thumbs')}"
        assert item["series"] == "怪物猎人", f"系列名未本地化：{item['series']}"
        assert item["games"][0]["name_cn"] is not None, "成员缺少中文名字段"
        print(f"PASS 族谱缩略图：{item['series']} thumbs={item['thumbs']}（纯本地）")
    finally:
        st.compute_series_tree = original_tree
        api._cover_url = original_url


def test_series_thumbs_uniform_max_four():
    """统一口径 (a)：作品数 >=4 且本机封面齐 -> 恒为 4 张（不是 5 张、不是 3 张）。"""
    from engine import series_tree as st

    original_tree, original_url = st.compute_series_tree, api._cover_url
    try:
        st.compute_series_tree = lambda *a, **k: {
            "series_count": 1,
            "total_titles": 5,
            "series": [{
                "series": "Five Titles",
                "owned_count": 1,
                "total_count": 5,
                "games": [{"appid": 900 + i, "name": f"T{i}", "owned": i == 0}
                          for i in range(5)],
            }],
        }
        api._cover_url = lambda appid, steam_path=None: f"/covers/{int(appid)}.jpg"
        lib = {"play_stats": {}, "games_meta": {}, "statuses": {}, "steam_path": None}
        item = api._series_section(lib)["series"][0]
    finally:
        st.compute_series_tree, api._cover_url = original_tree, original_url
    assert len(item["thumbs"]) == api.SERIES_THUMB_LIMIT == 4, \
        f"作品数够且封面齐时应恒为 4 张：{item['thumbs']}"
    assert len(item["thumbs_owned"]) == 4, "thumbs_owned 必须与 thumbs 一一对应"
    assert item["thumbs"][0] == "/covers/900.jpg" and item["thumbs_owned"][0] is True, \
        f"已拥有的作品仍排在最前：{item['thumbs']} / {item['thumbs_owned']}"
    print(f"PASS 统一上限 4 张：5 部作品 thumbs={item['thumbs']}")


def test_series_thumbs_show_all_when_fewer_than_four():
    """统一口径 (b)：不足 4 部 -> 有多少显示多少（2 部封面齐就是 2 张）。"""
    from engine import series_tree as st

    original_tree, original_url = st.compute_series_tree, api._cover_url
    try:
        st.compute_series_tree = lambda *a, **k: {
            "series_count": 1,
            "total_titles": 2,
            "series": [{
                "series": "Two Titles",
                "owned_count": 1,
                "total_count": 2,
                "games": [
                    {"appid": 810, "name": "A", "owned": False},
                    {"appid": 811, "name": "B", "owned": True},
                ],
            }],
        }
        api._cover_url = lambda appid, steam_path=None: f"/covers/{int(appid)}.jpg"
        lib = {"play_stats": {}, "games_meta": {}, "statuses": {}, "steam_path": None}
        item = api._series_section(lib)["series"][0]
    finally:
        st.compute_series_tree, api._cover_url = original_tree, original_url
    assert len(item["thumbs"]) == 2, f"只有 2 部就该显示 2 张：{item['thumbs']}"
    assert item["thumbs"][0] == "/covers/811.jpg", "已拥有的 811 仍排在最前"
    assert item["thumbs_owned"] == [True, False], f"一一对应标记：{item['thumbs_owned']}"
    print(f"PASS 不足 4 张：2 部作品 thumbs={item['thumbs']}")


def test_localize_covers_include_unowned_series_members():
    """统一口径 (c)：补抓封面的 appid 集合 = 本账号拥有的 ∪ 族谱系列框的封面缺口。

    缺口来自**未拥有**的系列成员（它们从来没抓过图，所以框里凑不满 4 张）；
    这里断言的就是「真的把未拥有的系列成员交给了 artwork.ensure_covers」。
    """
    from core import artwork
    from engine import series_tree as st

    lib = {
        "steam_path": r"C:\fake\steam",
        "account_id": "10001",
        "play_stats": {1001: {"playtime": 60, "last_played": 1700000000}},
        "games_meta": {1001: {"name": "Owned One", "genres": ["动作"]}},
        "statuses": {1001: "在玩"},
        "classified": {1001: {"status": "在玩", "status_name": "在玩"}},
        "named": 1,
    }
    tree = {"series_count": 1, "total_titles": 3, "series": [{
        "series": "Stub Series",
        "owned_count": 1,
        "total_count": 3,
        "games": [
            {"appid": 1001, "name": "Owned One", "owned": True},
            {"appid": 2002, "name": "Unowned A", "owned": False},
            {"appid": 3003, "name": "Unowned B", "owned": False},
        ],
    }]}
    originals = (api._require_library, api._has_cover, api._coverage,
                 st.compute_series_tree, artwork.ensure_covers, artwork.covers_dir)
    captured: dict = {}
    sandbox = Path(tempfile.mkdtemp(prefix="gl-no-art-"))
    try:
        artwork.covers_dir = lambda: sandbox   # 别把「官方无图名单」写进真实 data/covers
        api._require_library = lambda account_id=None, with_metadata=False, steam_path=None: lib
        api._coverage = lambda *a, **k: {}
        st.compute_series_tree = lambda *a, **k: tree
        # 只有「已拥有的 1001」本机有图：另两部就是系列框的封面缺口
        api._has_cover = lambda appid, steam_path=None: int(appid) == 1001

        def fake_ensure_covers(appids, **kwargs):
            captured["appids"] = [int(a) for a in appids]
            captured["limit"] = kwargs.get("limit")
            return {"total": len(captured["appids"]), "ok": 0, "failed": 1, "skipped": 0,
                    "unsupported": 0, "by_source": {},
                    "results": [{"appid": 2002, "ok": False, "error": "cdn: 404"}]}

        artwork.ensure_covers = fake_ensure_covers
        result = api.post_localize(account_id="10001", scope="covers", limit=50)
        recorded_no_art = artwork.no_art_appids()
    finally:
        (api._require_library, api._has_cover, api._coverage,
         st.compute_series_tree, artwork.ensure_covers, artwork.covers_dir) = originals
        shutil.rmtree(sandbox, ignore_errors=True)
    got = captured.get("appids") or []
    assert 2002 in got and 3003 in got, f"未拥有的系列成员必须进入补图集合：{got}"
    assert got[:2] == [2002, 3003], f"封面缺口应排在前面（每批上限内先补族谱）：{got}"
    assert 1001 in got, f"本账号拥有的也要保留：{got}"
    assert captured.get("limit") == 50, "limit 上限必须原样透传"
    assert result["ok"] is True and result["covers"]["not_found"] == 1, \
        f"404 应在统计里单列：{result.get('covers')}"
    assert "404" in result["message"], f"message 应说明 404（无图）属正常：{result['message']}"
    assert recorded_no_art == {2002}, f"抓到的 404 必须记进官方无图名单：{recorded_no_art}"
    print(f"PASS 补图集合 {got}（含未拥有成员）· 404 单列 {result['covers']['not_found']}")


# ------------------------------------------------- T5b：族谱「封面不全」提示改准确

def test_no_art_store_roundtrip_merge_and_corrupt():
    """T5b：官方无图名单的写入 / 读回 / 增量合并 / 损坏降级（走 core.artwork 的封面目录）。

    名单落在 ``covers_dir()/no_art.json``（打包态即 exe 旁的 data/covers），
    所以这里把 covers_dir 换成临时沙箱，绝不碰真实 data/covers。
    """
    from core import artwork

    sandbox = Path(tempfile.mkdtemp(prefix="gl-no-art-"))
    original_dir = artwork.covers_dir
    try:
        artwork.covers_dir = lambda: sandbox
        assert artwork.no_art_path() == sandbox / "no_art.json", \
            f"名单必须落在封面目录里：{artwork.no_art_path()}"
        assert artwork.no_art_appids() == set(), "名单不存在时应为空集（不抛）"

        assert artwork.mark_no_art([3454980, "2239770"]) == {3454980, 2239770}, "写入应返回合并后的全集"
        assert artwork.no_art_appids() == {3454980, 2239770}, "写入后应能读回"
        payload = json.loads((sandbox / "no_art.json").read_text(encoding="utf-8"))
        assert set(payload.get("appids") or {}) == {"3454980", "2239770"}, f"落盘结构不对：{payload}"
        assert not (sandbox / "no_art.json.tmp").exists(), "原子落盘不该留下 .tmp"

        artwork.mark_no_art([3802140])
        assert artwork.no_art_appids() == {3454980, 2239770, 3802140}, "必须增量合并，不能覆盖"

        artwork.mark_no_art([None, "abc", -5, "", True])
        assert artwork.no_art_appids() == {3454980, 2239770, 3802140}, "非法 appid 必须忽略"

        (sandbox / "no_art.json").write_text("{ 这不是 json", encoding="utf-8")
        assert artwork.no_art_appids() == set(), "损坏时必须降级为空集，而不是抛异常"
        assert artwork.mark_no_art([7]) == {7}, "损坏后仍应能重建名单"
    finally:
        artwork.covers_dir = original_dir
        shutil.rmtree(sandbox, ignore_errors=True)
    print("PASS 官方无图名单：写入 / 读回 / 增量合并 / 损坏降级（临时沙箱）")


def test_series_section_reports_missing_and_no_art():
    """T5b：``_series_section`` 回吐 thumbs_missing / thumbs_no_art，前端据此说两种话。

    四种情况：全无图 / 一张官方无图 + 一张没试过 / 都没记录（等于没试过）/ 封面齐。
    """
    from core import artwork
    from engine import series_tree as st

    tree = {"series_count": 1, "total_titles": 3, "series": [{
        "series": "Stub",
        "owned_count": 1,
        "total_count": 3,
        "games": [
            {"appid": 1001, "name": "Owned", "owned": True},
            {"appid": 2002, "name": "Unowned official-404", "owned": False},
            {"appid": 3003, "name": "Unowned never-tried", "owned": False},
        ],
    }]}
    lib = {"play_stats": {1001: {}}, "games_meta": {}, "statuses": {}, "steam_path": None}
    missing_ids = {2002, 3003}
    original = (st.compute_series_tree, api._cover_url, artwork.no_art_appids)
    try:
        st.compute_series_tree = lambda *a, **k: tree
        api._cover_url = lambda appid, steam_path=None: (
            "" if int(appid) in missing_ids else f"/covers/{int(appid)}.jpg"
        )
        # (1) 两张缺图都已确认「官方无图」-> 前端该说「另有 2 部官方暂无封面图」
        artwork.no_art_appids = lambda: {2002, 3003}
        item = api._series_section(lib)["series"][0]
        assert (item["thumbs_missing"], item["thumbs_no_art"]) == (2, 2), \
            f"全无图时应为 (2, 2)：{item['thumbs_missing']} / {item['thumbs_no_art']}"
        # (2) 只有一张确认无图、另一张没试过 -> 仍要给「可点抓取补齐」的指引
        artwork.no_art_appids = lambda: {2002}
        item = api._series_section(lib)["series"][0]
        assert (item["thumbs_missing"], item["thumbs_no_art"]) == (2, 1), \
            f"混合缺口时应为 (2, 1)：{item['thumbs_missing']} / {item['thumbs_no_art']}"
        # (3) 名单为空（都没试过）-> thumbs_no_art 必须为 0，不能虚报「官方无图」
        artwork.no_art_appids = lambda: set()
        item = api._series_section(lib)["series"][0]
        assert (item["thumbs_missing"], item["thumbs_no_art"]) == (2, 0), \
            f"没记录过时应为 (2, 0)：{item['thumbs_missing']} / {item['thumbs_no_art']}"
        # (4) 封面齐 -> 两个都是 0（前端不显示任何提示）
        missing_ids.clear()
        item = api._series_section(lib)["series"][0]
        assert (item["thumbs_missing"], item["thumbs_no_art"]) == (0, 0), \
            f"封面齐时应为 (0, 0)：{item['thumbs_missing']} / {item['thumbs_no_art']}"
    finally:
        st.compute_series_tree, api._cover_url, artwork.no_art_appids = original
    print("PASS 缺口两种情形：thumbs_missing / thumbs_no_art 取值正确 (2,2) (2,1) (2,0) (0,0)")


def test_localize_no_art_only_records_404():
    """T5b：只有「官方明确 404」进官方无图名单；超时 / 网络错误**不算**（下次还要再试）。"""
    from core import artwork

    lib = {
        "steam_path": r"C:\fake\steam",
        "account_id": "10001",
        "play_stats": {1001: {"playtime": 60}, 2002: {}, 3003: {}, 4004: {}},
        "games_meta": {}, "statuses": {}, "classified": {},
    }
    rows = [
        {"appid": 2002, "ok": False, "source": "none",
         "error": "steamcdn-a.akamaihd.net: HTTP 404; shared.akamai.steamstatic.com: HTTP 404"},
        {"appid": 3003, "ok": False, "source": "none", "error": "读取超时 timeout"},
        {"appid": 4004, "ok": True, "source": "cdn:media.st.dl.eccdnx.com", "error": ""},
    ]
    sandbox = Path(tempfile.mkdtemp(prefix="gl-no-art-"))
    original = (api._require_library, api._coverage, artwork.ensure_covers, artwork.covers_dir)
    try:
        api._require_library = lambda account_id=None, with_metadata=False, steam_path=None: lib
        api._coverage = lambda *a, **k: {}
        artwork.ensure_covers = lambda appids, **kw: {
            "total": len(list(appids)), "ok": 1, "failed": 2, "skipped": 0,
            "unsupported": 0, "by_source": {}, "results": rows,
        }
        artwork.covers_dir = lambda: sandbox
        result = api.post_localize(account_id="10001", scope="covers", limit=10)
        recorded = artwork.no_art_appids()
    finally:
        (api._require_library, api._coverage, artwork.ensure_covers, artwork.covers_dir) = original
        shutil.rmtree(sandbox, ignore_errors=True)
    assert recorded == {2002}, f"只该记 404 的 appid（超时不算），实际：{recorded}"
    assert result["covers"]["not_found"] == 1, f"404 应单列：{result.get('covers')}"
    print(f"PASS 官方无图名单只记 404：{sorted(recorded)}（超时/成功都不记）")


def test_no_art_lookup_is_local_only():
    """T5b：读官方无图名单**纯本地**（绝不出网），且路径走 core.artwork 的封面目录。"""
    from core import artwork

    calls: list[str] = []
    originals = (artwork.ensure_cover, artwork.ensure_covers, artwork._http_get)
    try:
        artwork.ensure_cover = lambda *a, **k: calls.append("ensure_cover") or {"ok": False}
        artwork.ensure_covers = lambda *a, **k: calls.append("ensure_covers") or {}
        artwork._http_get = lambda *a, **k: calls.append("http")
        path = artwork.no_art_path()
        ids = artwork.no_art_appids()
        section = api._series_section(
            {"play_stats": {582010: {"playtime": 600}}, "games_meta": {},
             "statuses": {}, "steam_path": r"C:\fake\steam"}
        )
    finally:
        (artwork.ensure_cover, artwork.ensure_covers, artwork._http_get) = originals
    assert calls == [], f"读官方无图名单 / 画族谱缩略图出网了：{calls}"
    assert path.parent == artwork.covers_dir(), f"名单没落在封面目录：{path}"
    assert isinstance(ids, set), f"应返回集合：{ids!r}"
    assert isinstance(section.get("series"), list) and section["series"], \
        f"离线也应能算出族谱：{section.get('series')}"
    print(f"PASS 官方无图名单纯本地：{path.name} · {sorted(ids)}（一笔网都没出）")


def test_series_render_never_goes_online():
    """统一口径 (d)：族谱渲染**绝不出网**（沿用现有离线断言风格）。

    画缩略图（``_series_section``）与算缺口（``_series_cover_gaps``）都只读本机封面；
    把四个联网入口换成「记账替身」，跑完应一笔都没记。
    （``maybe_refresh_async()`` 在 pytest 下本来就直接返回 False，不会起后台线程。）
    """
    from core import artwork
    from engine import series_tree as st

    calls: list[str] = []
    originals = (artwork.ensure_cover, artwork.ensure_covers,
                 st.fetch_franchise_members, st._http_get_json)
    try:
        artwork.ensure_cover = lambda *a, **k: calls.append("ensure_cover") or {"ok": False}
        artwork.ensure_covers = lambda *a, **k: calls.append("ensure_covers") or {"total": 0}
        st.fetch_franchise_members = lambda *a, **k: calls.append("franchise") or None
        st._http_get_json = lambda *a, **k: calls.append("http") or None
        # 582010 = 怪物猎人：世界，确实在 data/series_official.json 的系列目录里
        lib = {"play_stats": {582010: {"playtime": 600}},
               "games_meta": {582010: {"name": "Monster Hunter: World"}},
               "statuses": {}, "steam_path": r"C:\fake\steam"}
        section = api._series_section(lib)
        gaps = api._series_cover_gaps(lib)
    finally:
        (artwork.ensure_cover, artwork.ensure_covers,
         st.fetch_franchise_members, st._http_get_json) = originals
    assert calls == [], f"族谱渲染出网了：{calls}"
    assert isinstance(section.get("series"), list) and isinstance(gaps, list), \
        "离线渲染应正常返回（拿不到网也不许炸）"
    assert len(section["series"]) >= 1, f"离线也应能算出族谱：{section.get('series')}"
    print(f"PASS 族谱渲染不出网：series={len(section['series'])} 个 · 缺口 {len(gaps)} 款")


def test_page_get_reuses_existing_artifact():
    """/page/<kind>：产物指纹 == 当前渲染代码指纹 -> 直接发文件，**绝不重新渲染**。

    T3 契约变更：以前是「产物存在就复用」，现在是「产物存在**且指纹一致**才复用」。
    指纹对不上（改了渲染代码 / 老产物没记指纹）必须重画，否则用户看到的永远是老图。
    """
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="gl-t3-reuse-"))
    original_stable = api.cli._stable_output_dir
    api.cli._stable_output_dir = lambda: work
    kind = "starmap"
    account = "10000001"
    marker = "GL-T3-REUSE-MARKER"
    path = _make_fake_artifact(work, kind, account, body=f"<html><body>{marker}</body></html>")
    api._remember_page_artifact(kind, path, account)      # 记下「当前指纹」
    fresh_state = api.page_artifact_state(kind, account)
    assert fresh_state["file"] is not None, \
        "指纹一致却不可复用：新契约下应当直接发文件"
    assert fresh_state["stale"] is False and fresh_state["renderer_changed"] is False, \
        f"指纹一致却被误判过期（会让首屏白白重画一遍）：{fresh_state}"

    before = path.stat().st_mtime_ns
    original_render = api.render_page
    calls: list[str] = []

    def boom(*_a, **_k):                      # 一旦真的去渲染，立刻炸出来
        calls.append(kind)
        raise AssertionError("GET /page/<kind> 不该重新渲染指纹一致的产物")

    api.render_page = boom
    panel = Panel()
    try:
        status, ctype, body = panel.request(f"/page/{kind}?account_id={account}")
        assert status == 200, f"应直接发已有产物，实际 {status}：{body[:200]}"
        assert "text/html" in ctype, f"Content-Type 异常：{ctype}"
        assert marker in body, "发回来的不是那份已有产物"
        assert not calls, f"render_page 被调用了 {len(calls)} 次（应 0 次）"
        print(f"PASS GET /page/{kind}：200（{len(body)} 字节）· render_page 一次都没被调用")
    finally:
        api.render_page = original_render
        panel.close()
        api.cli._stable_output_dir = original_stable

    assert path.stat().st_mtime_ns == before, "产物被重写了：说明 GET /page/<kind> 仍在重新渲染"
    print("PASS 产物 mtime 未变化（指纹一致时首屏不重画，省掉约 10 秒）")


def test_page_get_rerenders_when_renderer_changed():
    """/page/<kind>：产物**缺指纹**（老产物）或指纹不符 -> 判过期 -> 重新渲染。

    这正是用户踩到的坑：T1 改了渲染代码（中文名优先），但面板首屏仍在发 10:54 的旧 HTML。
    """
    import tempfile

    for label, renderer in (("缺指纹（老产物）", None), ("指纹不符", "deadbeef" * 8)):
        work = Path(tempfile.mkdtemp(prefix="gl-t3-stale-"))
        original_stable = api.cli._stable_output_dir
        api.cli._stable_output_dir = lambda: work
        original_render = api.render_page
        kind = "graveyard"
        account = "10000001"
        path = _make_fake_artifact(work, kind, account, body="<html><body>OLD</body></html>")
        if renderer is not None:
            _write_fake_manifest(work, {kind: {"file": path.name, "account_id": account,
                                               "generated_at": "2026-09-24T10:54:44",
                                               "renderer": renderer}})
        try:
            state = api.page_artifact_state(kind, account)
            assert state["stale"] is True, f"[{label}] 过期产物没被识别：{state}"
            assert state["file"] is None, f"[{label}] 过期产物不能直接发出去：{state}"
            assert state["exists"] is True, f"[{label}] 文件明明在，exists 应为 True：{state}"

            rendered: list[str] = []

            def fake_render(k, **_kws):
                rendered.append(k)
                path.write_text("<html><body>NEW</body></html>", encoding="utf-8")
                return path

            api.render_page = fake_render
            html = api.read_page(kind, account_id=account)
            assert rendered == [kind], f"[{label}] 应重新渲染一次，实际 {rendered}"
            assert "NEW" in html, f"[{label}] 应返回重画后的内容：{html[:80]}"
            print(f"PASS [{label}] 判过期 -> 重新渲染 -> 返回新内容")
        finally:
            api.render_page = original_render
            api.cli._stable_output_dir = original_stable


def test_page_artifact_state_detects_other_account():
    """产物属于别的账号时不能被当成当前账号的图直接发出去。

    产物目录与清单都指向**临时目录**（自造 starmap.html），不依赖开发机 output/ 里
    恰好有没有旧图 —— 换台机器同样成立。
    """
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="gl-t3-owner-"))
    original = api._read_pages_manifest
    original_outdir = api._output_dir
    _make_fake_artifact(work, "starmap", "999888", body="<html><body>x</body></html>")
    # 注意：要带上当前渲染器指纹，否则「自己的账号」也会因为过期而不可用（T3 新契约）
    api._read_pages_manifest = lambda outdir=None: {
        "pages": {"starmap": {"file": "starmap.html", "account_id": "999888",
                              "renderer": api._renderer_fingerprint()}}
    }
    api._output_dir = lambda: work
    try:
        other = api.page_artifact_state("starmap", "10000001")
        assert other["other_account"] is True, f"应识别出是别的账号的图：{other}"
        assert other["file"] is None, "别的账号的产物不能直接拿来用"
        assert other["stale"] is False, "别的账号应走 other_account 分支，不该同时报过期"
        mine = api.page_artifact_state("starmap", "999888")
        assert mine["file"] is not None, f"自己的账号 + 指纹一致应该可直接用：{mine}"
        print("PASS 账号归属判断：别的账号 -> 重画；当前账号 + 指纹一致 -> 直接复用")
    finally:
        api._read_pages_manifest = original
        api._output_dir = original_outdir


def test_page_stale_artifact_is_flagged_in_listing():
    """首屏列表对过期产物给出**人能看懂**的状态（stale + 文案），不能冒充「已就绪」。

    用户看到的应该是「渲染逻辑已更新，将重新生成」，而不是一份旧代码画的图 +
    「已就绪」—— 否则他不知道要手点重新生成（T1 中文名修复就是这么被挡住的）。
    """
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="gl-t3-list-"))
    original_stable = api.cli._stable_output_dir
    api.cli._stable_output_dir = lambda: work
    account = "10000001"
    try:
        # 一个过期（老产物，没指纹）+ 一个指纹一致（刚生成）
        _make_fake_artifact(work, "graveyard", account, body="<html><body>OLD</body></html>")
        fresh = _make_fake_artifact(work, "starmap", account, body="<html><body>FRESH</body></html>")
        api._remember_page_artifact("starmap", fresh, account)

        data = api.list_existing_pages(account_id=account)
        by_kind = {p["kind"]: p for p in data["pages"]}
        assert set(by_kind) == set(api.PAGE_KINDS), f"六块应齐全：{sorted(by_kind)}"

        stale = by_kind["graveyard"]
        assert stale["ok"] is False, f"过期产物不能标成已就绪：{stale}"
        assert stale["stale"] is True, f"应显式标 stale：{stale}"
        assert stale["error"] == "渲染逻辑已更新，将重新生成", f"文案不对：{stale['error']}"

        ready = by_kind["starmap"]
        assert ready["ok"] is True and not ready.get("stale"), f"指纹一致的产物应直接可用：{ready}"

        for kind in ("report", "timeline", "today", "checkup"):
            assert by_kind[kind]["missing"] is True, f"没产物的应标 missing：{by_kind[kind]}"
        print("PASS 首屏列表：过期 -> stale + 「渲染逻辑已更新，将重新生成」；指纹一致 -> 已就绪")
    finally:
        api.cli._stable_output_dir = original_stable


def test_renderer_fingerprint_covers_code_not_volatile_data():
    """指纹必须覆盖渲染代码（桥接层/渲染模块/模板 **+ 引擎层**），且**不含**易变数据。

    含了 cache.db / covers 这类数据的话，每跑一次数据都会误判过期、首屏又慢回 ~10 秒。
    引擎层必须一起算：页面里的统计/画像/性价比/类型都是 render_bridge 调 engine 算出来的，
    漏了引擎就会出现「改了 engine 却仍发旧产物」—— 正是本任务要根治的问题。
    """
    files = api._renderer_source_files()
    rel = {f.relative_to(api.APP_ROOT).as_posix() for f in files}

    must_have = {
        "core/render_bridge.py",
        # 授权归属（T4）：决定六页的**库范围**，与 non_game 同级 —— 漏了它，
        # 改完 ownership 剔除某款游戏后六页指纹不变、面板继续发旧产物。
        "core/ownership.py",
        "render/annual_report.py", "render/graveyard.py", "render/starmap.py",
        "render/archaeologist.py", "render/timemachine.py", "render/checkup.py",
        "render/templates/base.html", "render/templates/graveyard.html",
        # 引擎层：render_bridge 直接调这些算页面内容
        "engine/stats.py", "engine/persona.py", "engine/value_ranking.py",
        "engine/display.py", "engine/__init__.py",
    }
    missing = must_have - rel
    assert not missing, f"指纹漏了这些渲染代码（改了它们不会触发重画）：{sorted(missing)}"

    volatile = [n for n in rel if "cache" in n.lower() or "covers" in n.lower() or n.endswith(".db")]
    assert not volatile, f"指纹混入了易变数据（会导致每次误判过期）：{volatile}"
    no_source = [n for n in rel if not n.endswith((".py", ".html"))]
    assert not no_source, f"指纹混入了非源码文件：{sorted(no_source)}"
    pycache = [n for n in rel if "__pycache__" in n]
    assert not pycache, f"指纹混入了字节码缓存（每次编译都变，会误判过期）：{pycache}"

    assert api._renderer_fingerprint() == api._renderer_fingerprint(force=True), \
        "指纹不稳定：同样的代码两次算出不同值"

    # 指纹缓存：首屏会反复问，必须命中缓存（否则每次都把全部源码读一遍）
    import time as _t
    api._renderer_fingerprint(force=True)
    started = _t.perf_counter()
    for _ in range(200):
        api._renderer_fingerprint()
    per_call_us = (_t.perf_counter() - started) / 200 * 1e6
    assert per_call_us < 1000, f"指纹缓存没生效：{per_call_us:.0f}us/次"
    print(f"PASS 指纹覆盖 {len(files)} 个代码文件（含引擎层）· 不含易变数据/字节码 · 稳定 · 缓存命中 {per_call_us:.2f}us/次")


def test_renderer_fingerprint_reacts_to_engine_changes():
    """改 ``engine/*.py`` **必须**让指纹变（否则改了引擎仍会发旧产物）；动数据必须不变。

    这条是回归闸门：如果有人把引擎层从 ``_renderer_source_files()`` 里拿掉，或在枚举时
    把 ``engine/`` 写错，本用例立刻变红。直接改写真实源码文件（改完逐字节还原）。
    """
    import os

    engine_dir = api.APP_ROOT / "engine"
    targets = ["stats.py", "display.py", "value_ranking.py", "persona.py"]
    existing = [engine_dir / n for n in targets if (engine_dir / n).is_file()]
    assert existing, "engine/ 下找不到要探测的源码文件（目录结构变了？）"

    baseline = api._renderer_fingerprint(force=True)
    try:
        for path in existing:
            # 先取 stat 再读字节：``read_bytes`` 会刷新 atime，
            # 取太晚就会把「已经被读过的 atime」当成原值还原回去。
            before = path.stat()
            original = path.read_bytes()
            try:
                path.write_bytes(original + b"\n# fingerprint probe\n")
                changed = api._renderer_fingerprint(force=True)
                assert changed != baseline, f"改了 {path.name} 指纹却没变（引擎层没进指纹）"
            finally:
                path.write_bytes(original)
                # 逐字节还原内容还不够：写入会把 mtime 刷成「现在」，
                # 而 启动面板.bat 是**按 mtime** 判「源码是否比 exe 新」的
                # （``for /r game-life\engine %%F ... do call :mark_newer``）。
                # 不还原 mtime 就会：跑一次测试 -> SRC_NEWER=1 -> 启动器弃用刚打好的 exe。
                # 用 ns 精度把 atime/mtime 一起还原（Windows 上 ``os.utime(ns=...)`` 可用且无损）。
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        restored = api._renderer_fingerprint(force=True)
        assert restored == baseline, "还原后指纹没回到基准（探测污染了文件）"

        # 新增引擎源码文件也要进指纹（防止新模块被漏掉）
        probe = engine_dir / "_fingerprint_probe_tmp.py"
        try:
            probe.write_text("# temporary probe\n", encoding="utf-8")
            assert api._renderer_fingerprint(force=True) != baseline, \
                "engine/ 下新增源码文件指纹却没变（新的引擎模块会被漏掉）"
        finally:
            if probe.exists():
                probe.unlink()
        assert api._renderer_fingerprint(force=True) == baseline, "删除探测文件后指纹没回到基准"

        # 易变数据 / 字节码缓存不得影响指纹
        # 注意：下面 data/ 与 output/ 下点到的都是**真实仓库文件**（不是 tmp_path），
        # 所以「动完必须把 mtime 还原」——与引擎源码同一个道理，别留下被测试动过的痕迹。
        volatile = [api.APP_ROOT / rel for rel in
                    ("data/names_cn.json", "data/cache.db", "data/genres_cn.json")]
        covers = api.APP_ROOT / "output" / "covers"
        touched: list[tuple[Path, os.stat_result]] = []
        try:
            for path in volatile:
                if path.is_file():
                    touched.append((path, path.stat()))
                    os.utime(path, None)
            if covers.is_dir():
                touched.append((covers, covers.stat()))
                os.utime(covers, None)
            cache_dir = engine_dir / "__pycache__"
            for pyc in cache_dir.glob("*.pyc") if cache_dir.is_dir() else []:
                # 字节码是 CPython 自管的重建产物：不匹配启动器扫描的 ``*.py``，
                # 且 pytest 导入时本来就会刷新，所以这里**有意不还原**它的 mtime。
                os.utime(pyc, None)
            assert api._renderer_fingerprint(force=True) == baseline, \
                "动了 data/ 数据或字节码缓存却让指纹变了（会导致每次误判过期）"
        finally:
            for path, st in touched:
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        print(f"PASS 引擎层敏感性：改 {len(existing)} 个 engine 源码 -> 指纹变；"
              "新增 engine 模块 -> 指纹变；动 data//__pycache__ -> 指纹不变")
    finally:
        api._renderer_fingerprint(force=True)


def test_renderer_fingerprint_reacts_to_ownership_changes():
    """改 ``core/ownership.py`` **必须**让六页指纹变（否则改了归属过滤仍发旧产物）。

    这条是 T4 的回归闸门。直接起因（主 agent 实测）：``ownership`` 已把 ``1091500``
    赛博朋克剔出游戏明细，但墓园 / 时光轴 / 星图里**仍然有它** —— 因为 ``ownership.py``
    当时不在 ``_renderer_source_files()`` 里，指纹没变，面板继续发旧产物。

    直接改写真实源码文件（改完逐字节还原，并把 mtime / atime 也还原回原值 ——
    ``启动面板.bat`` 按 mtime 判「源码比 exe 新」，留脏会让启动器弃用刚打好的 exe）。
    """
    import os

    path = api.APP_ROOT / "core" / "ownership.py"
    assert path.is_file(), "core/ownership.py 不存在（目录结构变了？）"

    baseline = api._renderer_fingerprint(force=True)
    before = path.stat()
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n# fingerprint probe\n")
        changed = api._renderer_fingerprint(force=True)
        assert changed != baseline, (
            "改了 core/ownership.py 指纹却没变（ownership 没进渲染指纹）—— "
            "用户会看到已剔除的游戏还留在墓园/时光轴/星图里"
        )
    finally:
        path.write_bytes(original)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.read_bytes() == original, "必须逐字节还原 ownership.py"
    restored = api._renderer_fingerprint(force=True)
    assert restored == baseline, "还原后指纹没回到基准（探测污染了文件）"
    print("PASS ownership 敏感性：改 core/ownership.py -> 指纹变；还原 -> 指纹回基准")


def test_pages_missing_when_no_artifacts(tmpdir=None):
    """一个产物都没有时：六块全部标 missing，提示「尚未生成」，不是报错。"""
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="gl-g5-"))
    original_stable = api.cli._stable_output_dir
    api.cli._stable_output_dir = lambda: work
    panel = Panel()
    try:
        data = api.list_existing_pages(account_id="10001")
        assert data["pages"], f"应仍然返回六个块：{data}"
        assert all(p["ok"] is False and p.get("missing") is True for p in data["pages"]), f"应全部 missing：{data}"
        assert all(p["error"] for p in data["pages"]), "缺产物应给出可读提示"
        assert all(p["url"].startswith("/page/") for p in data["pages"]), "块链接应保留"

        status, _ctype, body = panel.request("/api/pages?only_existing=1&account_id=10001")
        assert status == 200, f"没有产物也必须 200，实际 {status}：{body[:200]}"
        got = json.loads(body)
        assert all(p.get("missing") for p in got["pages"]), f"应全部 missing：{got['pages']}"
        assert "Traceback" not in body, "泄露了 traceback"
        print(f"PASS 无产物时：六块全部 missing + 提示，HTTP 200 不报错（{got['pages'][0]['error'][:16]}…）")
    finally:
        panel.close()
        api.cli._stable_output_dir = original_stable


def test_pages_auto_refresh_after_data_change():
    """run / 回填 / 抓取 / 换账号 之后，六块自动后台重画（用户不用自己去刷新）。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    assert "async function refreshPagesInBackground(" in js, "缺少后台重画函数"
    assert "_pagesBusy" in js, "缺少防叠加开关（连点会重复重画）"
    assert 'setFramesStatus("更新中…")' in js, "重画前没有「更新中…」状态"
    assert "function setFramesStatus(" in js, "缺少状态标记函数"
    assert "&_t=${Date.now()}" in js, "iframe 没有防缓存参数，换上新产物后可能还是旧图"
    assert ".frame-status" in css, "style.css 缺少状态小标样式"

    for trigger in ("分类扫描已完成", "快照回填已完成", "中文名与封面已更新", "已切换账号"):
        assert f'refreshPagesInBackground("{trigger}")' in js, f"缺少触发点：{trigger}"
    print("PASS 自动重画触发点：run / 回填 / 抓取中文名与封面 / 换账号（4 个）")

    run_block = js[js.index("async function doRun()"):js.index("async function doWriteback()")]
    assert "if (!dry)" in run_block, "run 的自动重画没有被 dry-run 条件包住"
    assert run_block.index("if (!dry)") < run_block.index("refreshPagesInBackground"), \
        "自动重画必须在 run 真正落库之后才触发"
    print("PASS 只在真实执行（非 dry-run）且落库之后才重画")
    print("PASS 六块状态标：「更新中…」-> 画完自动换成新 iframe")


def test_pages_render_race_guard():
    """首屏预填 / 切换账号 与后台重画并发时，晚到的旧响应不得覆盖更新的渲染。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "_pagesSeq" in js, "缺少渲染序号：慢的旧响应会盖掉新渲染（会闪「尚未生成」）"
    assert js.count("const seq = ++_pagesSeq;") == 3, \
        "prefill / 后台重画 / 手动生成 三处都要让旧请求失效"

    prefill = js[js.index("async function prefillPages("):js.index("async function doPages()")]
    assert "if (_pagesBusy) return;" in prefill, "后台重画进行中时 prefill 必须先让路"
    assert "if (seq !== _pagesSeq) return;" in prefill, "prefill 没有丢弃过期结果"

    bg = js[js.index("async function refreshPagesInBackground("):js.index("async function prefillPages(")]
    assert "if (seq !== _pagesSeq) return false;" in bg, "后台重画没有丢弃过期结果"
    assert "_pagesPending" in js, "重画期间的请求被丢掉了（数据变了画面却停在旧的）"
    assert "refreshPagesInBackground(next)" in bg, "忙完之后没有补画那一次被挤掉的请求"
    assert bg.index("_pagesPending = reason") < bg.index("_pagesBusy = true"), \
        "忙碌分支应先排队再返回，不能直接丢弃请求"

    handler = js[js.index('$("account").addEventListener("change"'):]
    handler = handler[:handler.index("});")]
    assert "prefillPages()" not in handler, \
        "切换账号不要再 prefill（产物属于上一个账号，只会闪「尚未生成」）"
    assert 'refreshPagesInBackground("已切换账号")' in handler, "切换账号必须后台重画六块"
    print("PASS 并发保护：prefill 让路 + 过期响应丢弃 + 切账号不闪「尚未生成」")


# ------------------------------------------------- 任务 G11：封面朝向不得靠参数推断

def _fake_jpeg(width: int, height: int, pad: int = 600) -> bytes:
    """造一张「能被解析出宽高」的最小 JPEG（同 test_artwork.fake_jpeg 手法）。"""
    body = bytearray()
    body += b"\xff\xd8"
    body += b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    body += b"\xff\xc0" + (17).to_bytes(2, "big") + bytes([8])
    body += height.to_bytes(2, "big") + width.to_bytes(2, "big")
    body += bytes([3]) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    body += b"\xff\xd9"
    if pad > len(body):
        body += b"\x00" * (pad - len(body))
    return bytes(body)


@contextlib.contextmanager
def _fake_steam_librarycache(tmp: Path):
    """伪造一份 Steam librarycache，只放 header.jpg（460x215 横版）或竖版图。"""
    steam = tmp / "steam"
    lc = steam / "appcache" / "librarycache"
    lc.mkdir(parents=True, exist_ok=True)

    def add(appid: int, name: str, width: int, height: int) -> Path:
        folder = lc / str(appid)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(_fake_jpeg(width, height))
        return path

    yield steam, add


def test_cover_kind_uses_real_pixels_not_size_param():
    """回归：460x215 的横版图绝不能被判成 portrait（否则前端按 2:3 cover 裁掉两侧文字）。

    真实 bug 的根因是旧实现「``cover_path_ex(..., "portrait")`` 有返回值 = 竖版」，
    而该函数在找不到真竖版时会回落横版（``_LIBRARY_PORTRAIT_NAMES`` 里就含
    ``header.jpg``）。这里用只含 header.jpg 的假 librarycache 构造同一场景。
    """
    import tempfile

    from core import artwork

    saved_memo = dict(api._COVER_KIND_MEMO)
    with tempfile.TemporaryDirectory(prefix="gl_g11_") as tmp:
        tmp_path = Path(tmp)
        with _fake_steam_librarycache(tmp_path) as (steam, add):
            land = add(900001, "header.jpg", 460, 215)          # 横版
            port = add(900002, "library_600x900.jpg", 600, 900)  # 真竖版（名字与像素都竖）
            weird = add(900003, "header.jpg", 200, 200)          # 方图 -> 按横版完整显示

            api._COVER_KIND_MEMO.clear()
            try:
                # 实际服务的文件就是 cover_for_panel（与 server._serve_cover 同源）
                served = api.cover_for_panel(900001, str(steam))
                assert served == land, f"应服务 header.jpg，实际 {served}"
                assert artwork.image_size(served) == (460, 215)

                kind_land = api._cover_kind(900001, str(steam))
                kind_port = api._cover_kind(900002, str(steam))
                kind_weird = api._cover_kind(900003, str(steam))
                assert kind_land == "landscape", f"460x215 必须是 landscape，实际 {kind_land}"
                assert kind_port == "portrait", f"600x900 必须是 portrait，实际 {kind_port}"
                assert kind_weird == "landscape", f"方图应按横版完整显示，实际 {kind_weird}"

                # 与真实像素判定一致（同一份 artwork.cover_kind 口径）
                assert kind_land == artwork.cover_kind(api.cover_for_panel(900001, str(steam)))
                assert kind_port == artwork.cover_kind(api.cover_for_panel(900002, str(steam)))
                print("PASS G11：朝向按真实像素判定 —— 460x215->landscape · 600x900->portrait")
            finally:
                api._COVER_KIND_MEMO.clear()
    api._COVER_KIND_MEMO.update(saved_memo)


def test_cover_kind_matches_served_file_and_is_absent_without_cover():
    """朝向必须与 ``/covers/<appid>.jpg`` 实际发的那个文件一致；没有封面时是 none。"""
    saved_memo = dict(api._COVER_KIND_MEMO)
    saved_for_panel = api.cover_for_panel
    try:
        # 用桩文件直接对齐「服务的那张图」：不管内部怎样选图，朝向只看最终文件
        fake_land = Path(__file__).resolve().parent / ".g11_land.jpg"
        fake_land.write_bytes(_fake_jpeg(460, 215))
        try:
            api._COVER_KIND_MEMO.clear()
            api.cover_for_panel = lambda aid, sp=None: fake_land if int(aid) == 900004 else None
            assert api._cover_kind(900004, None) == "landscape"
            assert api._cover_kind(900005, None) == "none", "没有封面应为 none"
        finally:
            fake_land.unlink(missing_ok=True)
    finally:
        api.cover_for_panel = saved_for_panel
        api._COVER_KIND_MEMO.clear()
        api._COVER_KIND_MEMO.update(saved_memo)
    print("PASS G11：朝向与「实际服务的文件」一致；无封面为 none")


def test_frontend_picks_contain_for_landscape():
    """前端确认：横版走 landscape(object-fit:contain 不裁切)，竖版走 portrait(cover 铺满)。"""
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    series = (APP_ROOT / "webapp" / "templates" / "series.html").read_text(encoding="utf-8")

    assert ".gc-img.landscape { width: 100%; height: 100%; object-fit: contain; }" in css
    assert ".gc-img.portrait { width: 100%; height: 100%; object-fit: cover; }" in css
    assert 'gc-img ${r.cover_kind === "landscape" ? "landscape" : "portrait"}' in js, \
        "游戏明细未按 cover_kind 选样式"
    assert 'row.cover_kind === "landscape" ? "landscape" : "portrait"' in series, \
        "系列详情未按 cover_kind 选样式"
    assert ".scard .s-cover img.landscape { object-fit: contain; }" in series
    assert ".scard .s-cover img.portrait { object-fit: cover; }" in series
    print("PASS G11：前端两处（游戏明细 / 系列详情）均按 cover_kind 选 contain/cover")

def test_log_api_tail_and_safety():
    """/api/log：只读 logs/ 内的日志尾巴，tail 有界，失败友好降级。"""
    import os
    import tempfile

    base = api.logs_dir()
    assert base.name == "logs", f"日志目录不对：{base}"

    data = api.get_log_tail(50)
    assert set(data) >= {"path", "lines"}, data
    assert Path(data["path"]).name == "game-life.log", data["path"]
    assert data.get("missing") or data.get("error") or Path(data["path"]).is_file(), data
    if data.get("lines"):
        assert len(data["lines"]) <= 50, f"tail 没生效：{len(data['lines'])} 行"
        assert data.get("tail") == 50, data
        before = Path(data["path"]).stat().st_mtime_ns
        api.get_log_tail(20)
        assert Path(data["path"]).stat().st_mtime_ns == before, "读日志竟然改动了文件"
    print(f"PASS /api/log 只读日志尾巴：{len(data.get('lines') or [])} 行 · {data['path']}")

    assert api.get_log_tail(0).get("tail") == 1
    assert api.get_log_tail(10 ** 9).get("tail") == api._LOG_TAIL_MAX
    assert api.get_log_tail("abc").get("tail") == api._LOG_TAIL_DEFAULT
    assert api.get_log_tail(None).get("tail") == api._LOG_TAIL_DEFAULT
    print(f"PASS tail 有界：0→1 · 超大→{api._LOG_TAIL_MAX} · 非法→{api._LOG_TAIL_DEFAULT}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        assert api._pick_log_file(tmp_dir / "missing") is None, "目录不存在不该报错"
        (tmp_dir / "notes.txt").write_text("x", encoding="utf-8")
        assert api._pick_log_file(tmp_dir) is None, "没有 *.log 时应返回 None"
        older = tmp_dir / "a.log"
        older.write_text("old", encoding="utf-8")
        newer = tmp_dir / "b.log"
        newer.write_text("new", encoding="utf-8")
        os.utime(older, (1, 1))
        assert api._pick_log_file(tmp_dir) == newer.resolve(), "应挑最新的 .log"

        (Path(tmp) / "outside.log").write_text("secret", encoding="utf-8")
        link = tmp_dir / "game-life.log"
        try:
            link.symlink_to(Path(tmp) / "outside.log")
            assert api._pick_log_file(tmp_dir) is None, "软链接指向 logs/ 之外时必须拒绝"
            print("PASS 路径安全：软链接逃逸被拒绝（绝不读 logs/ 之外的文件）")
        except (OSError, NotImplementedError) as exc:
            print(f"PASS 路径安全：解析后必须落在 logs/ 内（本机建不了软链接，跳过实测：{type(exc).__name__}）")
    print("PASS 日志接口只读 + 目录受限 + 失败降级")


def test_log_endpoint_http():
    """HTTP 层：GET /api/log 返回 JSON（path + lines），默认 200 行。"""
    panel = Panel()
    try:
        status, ctype, body = panel.request("/api/log?tail=5")
        assert status == 200, f"状态码 {status}：{body[:200]}"
        assert "application/json" in ctype, ctype
        data = json.loads(body)
        assert "path" in data and isinstance(data.get("lines"), list), data
        assert len(data["lines"]) <= 5, data["lines"]
        print(f"PASS GET /api/log?tail=5：200 · {len(data['lines'])} 行 · {Path(data['path']).name}")

        status2, _c, body2 = panel.request("/api/log")
        assert status2 == 200, body2[:200]
        lines2 = json.loads(body2).get("lines") or []
        assert len(lines2) <= api._LOG_TAIL_DEFAULT, len(lines2)
        print(f"PASS GET /api/log（默认 tail）：{len(lines2)} 行（上限 {api._LOG_TAIL_DEFAULT}）")
    finally:
        panel.close()


def _stub_handler(write_exc, path="/api/state"):
    """造一个「写响应时抛异常」的 handler，直接走 _dispatch（复现 wfile.write 抛错的真实路径）。"""
    class _Wfile:
        def write(self, data):
            raise write_exc

    handler = object.__new__(server.PanelHandler)
    handler.path = path
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.headers = {}
    handler.rfile = io.BytesIO(b"")
    handler.wfile = _Wfile()
    handler.send_response = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda *a, **k: None
    return handler


def test_client_gone_not_logged_as_error():
    """客户端提前断开静默（DEBUG）；真正的服务端异常照旧 ERROR + traceback。"""
    import errno
    import logging

    assert server._is_client_gone(ConnectionAbortedError(10053, "WinError 10053"))
    assert server._is_client_gone(ConnectionResetError(10054, "reset by peer"))
    assert server._is_client_gone(BrokenPipeError())
    assert server._is_client_gone(OSError(errno.EPIPE, "broken pipe"))
    assert not server._is_client_gone(ValueError("boom"))
    assert not server._is_client_gone(FileNotFoundError(2, "no such file"))
    assert not server._is_client_gone(None)
    print("PASS 客户端断开判定：Aborted/Reset/BrokenPipe → 静默；其它异常 → 照旧")

    src = (APP_ROOT / "webapp" / "server.py").read_text(encoding="utf-8")
    assert "except BrokenPipeError:  # 浏览器提前断开：静默" not in src, "旧的「只静默 BrokenPipeError」写法还在"
    assert 'logger.exception("处理 %s %s 失败"' in src, "兜底 ERROR + traceback 被吞掉了"

    records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record)

    root = logging.getLogger()
    cap = _Cap(level=logging.DEBUG)
    old_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    try:
        # 1) 用户报的那条：写响应时客户端已断开（wfile.write -> ConnectionAbortedError）
        for exc in (ConnectionAbortedError(10053, "WinError 10053"),
                    ConnectionResetError(10054, "reset"),
                    BrokenPipeError(32, "Broken pipe")):
            records.clear()
            _stub_handler(exc)._dispatch("GET")
            errors = [r for r in records if r.levelno >= logging.ERROR]
            assert not errors, f"{type(exc).__name__} 不该记 ERROR：{[r.getMessage() for r in errors]}"
            assert any(r.levelno == logging.DEBUG for r in records), f"{type(exc).__name__} 应留一条 DEBUG"
        print("PASS 写响应时客户端断开：Aborted/Reset/BrokenPipe 全部只留 DEBUG，0 条 ERROR")

        # 2) 真正的服务端异常：必须照旧记 ERROR + traceback
        for exc in (RuntimeError("boom"), FileNotFoundError(2, "no such file")):
            records.clear()
            _stub_handler(exc)._dispatch("GET")
            errors = [r for r in records if r.levelno >= logging.ERROR]
            assert errors, f"{type(exc).__name__} 必须记 ERROR"
            assert any(r.exc_info for r in errors), "ERROR 记录必须带 traceback"
        print("PASS 真正的服务端异常照旧 ERROR + traceback（兜底没被吞）")

        # 3) 端到端：真 handler 抛真异常 -> 500 + 服务内部错误
        records.clear()
        original = api.state
        try:
            api.state = lambda: (_ for _ in ()).throw(RuntimeError("真故障"))
            panel = Panel()
            try:
                status, _c, body = panel.request("/api/state")
                assert status == 500, f"真异常应回 500，实得 {status}"
                assert "服务内部错误" in body, body[:200]
            finally:
                panel.close()
        finally:
            api.state = original
        assert [r for r in records if r.levelno >= logging.ERROR], "真异常没记 ERROR"
        print("PASS 真异常端到端：HTTP 500 + ERROR 落日志")
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)


def _make_fake_artifact(outdir: Path, kind: str, account_id: str, body: str = "<html><body>x</body></html>") -> Path:
    """在临时 output 目录里造一个「看起来像真产物」的文件（按各 kind 的文件名约定）。"""
    name = {
        "report": "annual_report_2026.html",
        "graveyard": "graveyard.html",
        "starmap": "starmap.html",
        "timeline": "timeline.html",
        "today": "timemachine.html",
        "checkup": "checkup.html",
    }[kind]
    path = Path(outdir) / name
    path.write_text(body, encoding="utf-8")
    return path


def _write_fake_manifest(outdir: Path, pages: dict) -> Path:
    """直接写一份产物清单（模拟「老产物：没有 renderer 字段」或「指纹不符」）。"""
    path = Path(outdir) / api._PAGES_MANIFEST_NAME
    path.write_text(json.dumps({"pages": pages}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _declared_pointer_events(decls: str) -> str | None:
    """取某个声明块里 pointer-events 的值（小写）；没写就返回 None。"""
    m = re.search(r"(?<![-a-z])pointer-events\s*:\s*([a-zA-Z-]+)", decls)
    return m.group(1).lower() if m else None


def _frame_hit_right_gap(decls: str) -> int | None:
    """算出 .frame-hit 右侧的实际留缝（px）。

    同时支持两种等价写法：
      right: 17px             -> 17
      inset: 0 17px 0 0       -> 17（简写：上 右 下 左）
      inset: 0 17px           -> 17（简写：上下 左右）
      inset: 0                -> 0 （= 盖满整块，正是要防的回归）
    取不到 → None。
    """
    m = re.search(r"(?<![-a-z])right\s*:\s*([0-9.]+)px", decls)
    if m:
        return round(float(m.group(1)))
    m = re.search(r"(?<![-a-z])inset\s*:\s*([^;]+)", decls)
    if not m:
        return None
    parts = m.group(1).split()
    right = parts[0] if len(parts) == 1 else parts[1]      # 1 值=四边相同；>=2 值=第 2 个是 right
    mm = re.fullmatch(r"([0-9.]+)px", right.strip())
    return round(float(mm.group(1))) if mm else None


def _css_declarations(css: str, selector: str) -> str:
    """取出某个选择器的声明块，并**剥掉注释**后返回。

    必须剥注释：T2 那个「注释里出现 pointer-events: none 就让断言通过」的坑，
    就是因为拿裸文本做子串匹配。这里只看真实声明，注释里写什么都不算。
    """
    start = css.index(selector)
    open_brace = css.index("{", start)
    assert "/*" not in css[start:open_brace], f"选择器被写在注释里：{selector!r}"
    close_brace = css.index("}", open_brace)
    return re.sub(r"/\*.*?\*/", "", css[open_brace + 1:close_brace], flags=re.S)


def _strip_js_comments(js: str) -> str:
    """粗剥 JS 注释（/* */ 与 //），避免「注释里写了某字符串」导致假通过。"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", js)


def _rule_block(css: str, selector: str) -> str:
    """从 CSS 文本里取出某个选择器的声明块（够用的简易切片，不做完整解析）。"""
    start = css.index(selector)
    open_brace = css.index("{", start)
    close_brace = css.index("}", open_brace)
    return css[open_brace + 1:close_brace]


def test_pages_prefill_lists_existing_without_rendering():
    """首屏接口只列已有产物：毫秒级、不渲染、不重写文件（产物 mtime 不变）。

    T3：这里用临时目录造产物，**同时**覆盖两条路径 ——
      * 指纹一致 -> ``ok=True`` 可直接复用；
      * 旧指纹 -> ``stale=True`` 不可复用（但也不在这个只读接口里重画）。
    """
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="gl-t3-prefill-"))
    original_stable = api.cli._stable_output_dir
    api.cli._stable_output_dir = lambda: work
    account = "10000001"
    try:
        ready_kind, stale_kind = "starmap", "graveyard"
        ready_path = _make_fake_artifact(work, ready_kind, account, body="<html><body>READY</body></html>")
        _make_fake_artifact(work, stale_kind, account, body="<html><body>OLD</body></html>")
        api._remember_page_artifact(ready_kind, ready_path, account)     # 记下当前指纹

        before = {f.name: f.stat().st_mtime_ns for f in work.glob("*") if f.is_file()}
        original_render = api.render_page
        rendered: list[str] = []

        def boom(kind, **_kws):
            rendered.append(kind)
            raise AssertionError(f"只读接口不该渲染：{kind}")

        api.render_page = boom
        started = time.time()
        try:
            data = api.list_existing_pages(account_id=account)
        finally:
            api.render_page = original_render
        elapsed = time.time() - started

        assert not rendered, f"只列产物却触发了渲染：{rendered}"
        assert elapsed < 2.0, f"只列产物不该花 {elapsed:.1f}s（重新渲染才需要 ~10s）"
        assert data.get("only_existing") is True, f"缺少 only_existing 标记：{data}"
        kinds = [p["kind"] for p in data["pages"]]
        assert kinds == list(api.PAGE_KINDS), f"六块应齐全：{kinds}"

        by_kind = {p["kind"]: p for p in data["pages"]}
        for item in data["pages"]:
            assert {"kind", "label", "url", "ok"} <= set(item), f"字段缺失：{item}"
            assert item["url"] == f"/page/{item['kind']}", f"url 不对：{item}"
            if item["ok"]:
                assert Path(item["file"]).is_file(), f"报 ok 但文件不在：{item}"
            else:
                assert item.get("missing") is True, f"没产物应标 missing 而不是报错：{item}"

        assert by_kind[ready_kind]["ok"] is True, f"指纹一致的产物应可直接用：{by_kind[ready_kind]}"
        assert not by_kind[ready_kind].get("stale"), \
            f"指纹一致的产物被标成过期（会误导用户去点重新生成）：{by_kind[ready_kind]}"
        assert by_kind[stale_kind]["ok"] is False and by_kind[stale_kind].get("stale") is True, \
            f"旧指纹的产物应判过期：{by_kind[stale_kind]}"
        print(f"PASS /api/pages 只列已有产物：1/6 可复用 + 1/6 过期 · 用时 {elapsed * 1000:.1f}ms · 零渲染")

        after = {f.name: f.stat().st_mtime_ns for f in work.glob("*") if f.is_file()}
        changed = [k for k in before if k in after and before[k] != after[k]]
        assert not changed, f"只读接口却改动了产物：{changed}"
        assert set(after) - set(before) == set(), f"只读接口却新增了文件：{set(after) - set(before)}"
        print("PASS 产物未被重写/新增（没有偷偷重新渲染）")
    finally:
        api.cli._stable_output_dir = original_stable


def test_pages_api_only_existing():
    """HTTP 层：/api/pages?only_existing=1 快速返回，且不触发渲染。"""
    panel = Panel()
    try:
        started = time.time()
        status, _ctype, body = panel.request("/api/pages?only_existing=1&account_id=10001")
        elapsed = time.time() - started
        assert status == 200, f"状态码 {status}：{body[:200]}"
        data = json.loads(body)
        assert data.get("only_existing") is True, f"缺少 only_existing：{list(data)}"
        assert len(data.get("pages") or []) == 6, f"应返回六个块：{data.get('pages')}"
        assert elapsed < 3.0, f"首屏列表接口太慢：{elapsed:.1f}s（重新渲染才会 ~10s）"
        print(f"PASS GET /api/pages?only_existing=1：200 · 6 块 · {elapsed * 1000:.0f}ms")
    finally:
        panel.close()


def test_panel_autoloads_frames_on_open():
    """打开面板即自动铺六块；当前账号一张产物都没有时后台自动补画（用户不必点按钮）。"""
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "function prefillPages(" in js, "缺少 prefillPages"
    assert "/api/pages?only_existing=1" in js, "首屏预填没有走 only_existing，会白等 10 秒"
    init = js[js.index("(async function init()"):]
    assert "prefillPages();" in init, "init() 没有自动铺六块"
    assert "prefillPages(true);" in init, "账号就绪后那次没有开「一张产物都没有就后台补画」"

    prefill = js[js.index("async function prefillPages("):js.index("async function doPages()")]
    assert 'refreshPagesInBackground("首次生成")' in prefill, \
        "当前账号一张产物都没有时没有后台补画（用户还得手动点按钮）"
    assert "some((p) => p.ok)" in prefill, "后台补画前没有先判断「一张产物都没有」"
    print("PASS 首屏自动铺六块 + 无产物时后台自动补画（prefillPages(true)）")


def test_page_artifact_state_empty_account_ownership():
    """P1 回归：归属校验对「空 account_id」也必须生效（严格相等，双向都堵）。

    旧写法「两边都非空才比较」在任一侧为空时整段跳过校验，于是：
      * 清单记 A + 查询 ""（首启零账号）-> 把 A 的旧图当成「当前状态」发出去；
      * 清单记 "" + 查询 B -> 把空账号的旧图发给 B。
    两边都为空则视为同一个（空）账号，必须允许复用，否则每次打开都白重画六页。
    """
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="gl-p1-ownership-"))
    original_stable = api.cli._stable_output_dir
    api.cli._stable_output_dir = lambda: work
    original_manifest = api._read_pages_manifest
    kind, account_a, account_b = "starmap", "10000001", "10000003"
    renderer = api._renderer_fingerprint()
    path = _make_fake_artifact(work, kind, account_a, body="<html><body>OLD</body></html>")

    def use_manifest(recorded_account):
        api._read_pages_manifest = lambda outdir=None: {
            "pages": {kind: {"file": path.name, "account_id": recorded_account,
                             "renderer": renderer}}
        }

    try:
        cases = [
            ("记录 A + 查询空", account_a, "", True, False),
            ("记录空 + 查询 B", "", account_b, True, False),
            ("记录空 + 查询空", "", "", False, True),
            ("记录 A + 查询 A", account_a, account_a, False, True),
        ]
        for label, recorded_account, query, want_other, want_file in cases:
            use_manifest(recorded_account)
            state = api.page_artifact_state(kind, query or None)
            assert state["other_account"] is want_other, \
                f"[{label}] other_account 应为 {want_other}：{state}"
            assert (state["file"] is not None) is want_file, \
                f"[{label}] file 可用性应为 {want_file}：{state}"
            assert state["exists"] is True, f"[{label}] 文件在，exists 应为 True：{state}"
            print(f"PASS [{label}] other_account={state['other_account']} "
                  f"file={'有' if state['file'] else 'None'}")
    finally:
        api._read_pages_manifest = original_manifest
        api.cli._stable_output_dir = original_stable


def test_page_get_does_not_serve_other_account_artifact():
    """P1 端到端（隔离目录）：记录为账号 A 的产物，以 account_id="" 去取时不得直接返回旧图。"""
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="gl-p1-e2e-"))
    original_stable = api.cli._stable_output_dir
    api.cli._stable_output_dir = lambda: work
    kind, account = "starmap", "10000001"
    marker = "GL-P1-OTHER-ACCOUNT-MARKER"
    path = _make_fake_artifact(work, kind, account, body=f"<html><body>{marker}</body></html>")
    api._remember_page_artifact(kind, path, account)
    original_render = api.render_page
    rendered: list[str] = []

    def fake_render(k, **_kwargs):          # 不读真实库：只证明「走的是重画，不是发旧图」
        rendered.append(k)
        path.write_text("<html><body>REDRAWN</body></html>", encoding="utf-8")
        return path

    api.render_page = fake_render
    try:
        state = api.page_artifact_state(kind, "")
        assert state["other_account"] is True, f"空查询遇上 A 的产物必须判为别的账号：{state}"
        assert state["file"] is None, f"别的账号的产物不能直接拿来用：{state}"
        html = api.read_page(kind, account_id="")
        assert marker not in html, f"以空账号取页面时返回了其它账号的旧图：{html[:120]}"
        assert rendered == [kind], f"应改为重新渲染，实际 {rendered}"
        print("PASS E2E 隔离：account_id='' 时不会直接返回记录为 A 的旧产物")
    finally:
        api.render_page = original_render
        api.cli._stable_output_dir = original_stable


# --------------------------------------------------------------------- T6：账号数据自动补齐


def _job_sandbox() -> Path:
    """一个临时沙箱（把幂等状态与封面目录都指进去，绝不动真实 data/ ）。"""
    return Path(tempfile.mkdtemp(prefix="gl-localize-"))


def test_add_account_enqueues_auto_localize():
    """T6：/api/accounts/add 成功后会**自动**入队一个补齐任务（不阻塞这次响应）。

    只对本次**真正新增**的账号入队：重复添加已在列表里的账号不能再排一次。
    """
    calls: list[tuple] = []
    originals = (api._scan_local_accounts, api.load_added_accounts, api.save_added_accounts,
                 api.accounts_view, api.enqueue_localize)
    try:
        api._scan_local_accounts = lambda steam_path=None: [{"id": "900001", "name": "打桩账号甲"}]
        api.accounts_view = lambda steam_path=None, current=None: {
            "accounts": [{"id": "900001", "name": "打桩账号甲"}],
            "current": "900001", "needs_setup": False,
        }

        def fake_enqueue(account_id, steam_path=None, reason="", **kwargs):
            calls.append((str(account_id), reason))
            return {"ok": True, "queued": True}

        api.enqueue_localize = fake_enqueue
        api.load_added_accounts = lambda: []
        api.save_added_accounts = lambda entries: list(entries)
        result = api.add_accounts(["900001"], steam_path=r"C:\fake\steam")
        assert result["ok"] is True and result["changed"] == ["900001"], result
        assert calls and calls[0][0] == "900001", f"添加账号后应自动入队补齐：{calls}"
        assert "添加账号" in calls[0][1], f"入队原因应可读：{calls}"

        # 重复添加同一个已在列表里的账号 -> 不重复入队
        calls.clear()
        api.load_added_accounts = lambda: [{"id": "900001", "name": "打桩账号甲"}]
        api.add_accounts(["900001"], steam_path=r"C:\fake\steam")
        assert calls == [], f"重复添加不该再排一次：{calls}"
    finally:
        (api._scan_local_accounts, api.load_added_accounts, api.save_added_accounts,
         api.accounts_view, api.enqueue_localize) = originals
    print("PASS T6 添加账号自动入队（重复添加不重排）")


def test_localize_job_runs_all_stages_with_progress():
    """T6：后台补齐任务按「系列 -> 中文名 -> 价格 -> 封面 -> 刷缓存」跑完，每段都带进度回调。"""
    from core import artwork, store_api
    from engine import series_tree as st

    lib = {
        "steam_path": r"C:\fake\steam",
        "account_id": "10001",
        "play_stats": {1001: {"playtime": 60}, 2002: {"playtime": 0}},
        "games_meta": {1001: {"name": "One"}},
        "statuses": {},
        "classified": {},
        "named": 1,
    }
    seen = {"series": 0, "series_progress": 0, "meta": [], "price": [], "covers": []}
    sandbox = _job_sandbox()
    originals = (
        api.load_library, api._coverage, api.localize_state_path, api._series_cover_gaps,
        api._owned_series_targets,
        st.series_cache_is_stale, st._read_cache_file, st._refresh_targets, st.refresh_series_cache,
        store_api.fetch_metadata, store_api.backfill_prices,
        artwork.ensure_covers, artwork.covers_dir,
    )
    try:
        api.localize_state_path = lambda: sandbox / "localize_state.json"
        api.load_library = lambda **kwargs: lib
        api._coverage = lambda *a, **k: {
            "total": 2, "name_cn": 2, "cover": 2, "price": 1,
            "missing_name_cn": 0, "missing_cover": 0, "missing_price": 1,
        }
        api._series_cover_gaps = lambda lib_: [3003]        # 族谱缺口：未拥有的系列成员
        api._owned_series_targets = lambda appids: ["Stub Series"]
        artwork.covers_dir = lambda: sandbox                # no_art.json / 封面都写沙箱
        st.series_cache_is_stale = lambda *a, **k: True
        st._read_cache_file = lambda *a, **k: {"series": {}}
        st._refresh_targets = lambda *a, **k: ["Stub Series"]

        def fake_series(names=None, **kwargs):
            seen["series"] += 1
            callback = kwargs.get("on_progress")
            if callback:
                seen["series_progress"] += 1
                callback("Stub Series", 3)
            return {"fetched": ["Stub Series"], "kept": [], "failed": [], "unverified": []}

        st.refresh_series_cache = fake_series

        def fake_meta(appids, **kwargs):
            ids = [int(a) for a in appids]
            seen["meta"].append(len(ids))
            callback = kwargs.get("progress")
            for index, appid in enumerate(ids, 1):
                if callback:
                    callback(index, len(ids), {"appid": appid, "name_cn": "名字"})
            return {appid: {"appid": appid, "name_cn": "名字"} for appid in ids}

        store_api.fetch_metadata = fake_meta

        def fake_price(appids, **kwargs):
            ids = [int(a) for a in appids]
            seen["price"].append(len(ids))
            callback = kwargs.get("progress")
            for index, appid in enumerate(ids, 1):
                if callback:
                    callback(index, len(ids), {"appid": appid, "price": 100})
            return {ids[0]: {"price": 100}}

        store_api.backfill_prices = fake_price

        def fake_covers(appids, **kwargs):
            ids = [int(a) for a in appids]
            seen["covers"].append(ids)
            callback = kwargs.get("progress")
            for index, appid in enumerate(ids, 1):
                if callback:
                    callback(index, len(ids), {"appid": appid, "ok": True})
            return {"total": len(ids), "ok": len(ids), "failed": 0, "skipped": 0,
                    "unsupported": 0, "by_source": {}, "results": []}

        artwork.ensure_covers = fake_covers

        api._reset_localize_job()
        assert api.job_state()["running"] is False
        queued = api.enqueue_localize("10001", steam_path=lib["steam_path"],
                                      reason="打桩", force=True)
        assert queued["queued"] is True, queued
        assert api.job_state()["pending"] == 1, api.job_state()
        assert api._JOB_THREAD is None, "测试进程里绝不能起真实联网线程"
        api._drain_job_queue()
        state = api.job_state()
        recorded = api.load_localize_state()
    finally:
        (api.load_library, api._coverage, api.localize_state_path, api._series_cover_gaps,
         api._owned_series_targets,
         st.series_cache_is_stale, st._read_cache_file, st._refresh_targets, st.refresh_series_cache,
         store_api.fetch_metadata, store_api.backfill_prices,
         artwork.ensure_covers, artwork.covers_dir) = originals
        api._reset_localize_job()
        shutil.rmtree(sandbox, ignore_errors=True)

    assert seen["series"] == 1 and seen["series_progress"] == 1, f"系列阶段没跑/没进度：{seen}"
    assert seen["meta"] == [2], f"元数据阶段没带进度：{seen}"
    assert seen["price"] == [2], f"价格阶段没带进度：{seen}"
    covers = seen["covers"][0] if seen["covers"] else []
    assert covers[:1] == [3003], f"族谱缺口应排在最前：{covers}"
    assert 1001 in covers and 2002 in covers, f"本账号拥有的也要补：{covers}"
    assert state["ok"] is True, f"任务应成功：{state}"
    assert state["running"] is False and state["finished_at"], f"任务应结束：{state}"
    assert (state["coverage"] or {}).get("total") == 2, state["coverage"]
    assert recorded.get("10001", {}).get("coverage", {}).get("price") == 1, \
        f"跑完应记 localize_state.json：{recorded}"
    print(f"PASS T6 全阶段补齐：系列/中文名/价格/封面（{covers}）均带进度")


def test_localize_series_stage_is_bounded_and_isolated():
    """T6b：系列目录阶段（限时兜底）必须真的受**硬时间预算**约束，且超时 / 失败都不能
    连累中文名 / 价格 / 封面——任务整体仍 ``ok:true``，剩余系列记进 ``deferred`` 留待后台。"""
    from core import artwork, store_api
    from engine import series_tree as st

    lib = {
        "steam_path": r"C:\fake\steam", "account_id": "10002",
        "play_stats": {4004: {"playtime": 10}}, "games_meta": {}, "statuses": {},
        "classified": {}, "named": 0,
    }
    seen = {"budget": [], "order": []}
    sandbox = _job_sandbox()
    originals = (
        api.load_library, api._coverage, api.localize_state_path, api._series_cover_gaps,
        api._owned_series_targets,
        st.series_cache_is_stale, st._read_cache_file, st._refresh_targets, st.refresh_series_cache,
        store_api.fetch_metadata, store_api.backfill_prices,
        artwork.ensure_covers, artwork.covers_dir,
    )
    try:
        api.localize_state_path = lambda: sandbox / "localize_state.json"
        api.load_library = lambda **kwargs: lib
        api._coverage = lambda *a, **k: {
            "total": 1, "name_cn": 1, "cover": 1, "price": 1,
            "missing_name_cn": 0, "missing_cover": 0, "missing_price": 0}
        api._series_cover_gaps = lambda lib_: []
        api._owned_series_targets = lambda appids: ["Stub A", "Stub B"]
        artwork.covers_dir = lambda: sandbox
        st.series_cache_is_stale = lambda *a, **k: True
        st._read_cache_file = lambda *a, **k: {"series": {}}
        st._refresh_targets = lambda *a, **k: ["Stub A", "Stub B"]

        def fake_series(names=None, **kwargs):
            seen["order"].append("series")
            seen["budget"].append(kwargs.get("deadline_seconds"))
            callback = kwargs.get("on_progress")
            if callback:
                callback("Stub A", 1)
            return {"fetched": ["Stub A"], "kept": [], "failed": [],
                    "unverified": [], "deferred": ["Stub B"]}

        st.refresh_series_cache = fake_series

        def fake_meta(appids, **kwargs):
            seen["order"].append("names")
            return {int(a): {"appid": int(a), "name_cn": "名字"} for a in appids}

        store_api.fetch_metadata = fake_meta

        def fake_price(appids, **kwargs):
            seen["order"].append("price")
            return {int(appids[0]): {"price": 8}}

        store_api.backfill_prices = fake_price

        def fake_covers(appids, **kwargs):
            seen["order"].append("covers")
            return {"total": len(appids), "ok": len(appids), "failed": 0, "skipped": 0,
                    "unsupported": 0, "by_source": {}, "results": []}

        artwork.ensure_covers = fake_covers

        api._reset_localize_job()
        api.enqueue_localize("10002", reason="打桩预算", force=True)
        api._drain_job_queue()
        state_ok = api.job_state()
        order_ok = list(seen["order"])

        # 第二轮：系列阶段直接炸掉 —— 绝不能让整体判成失败
        def boom_series(*_a, **_k):
            seen["order"].append("series")
            raise RuntimeError("打桩：系列阶段炸了")

        st.refresh_series_cache = boom_series
        seen["order"] = []
        api._reset_localize_job()
        api.enqueue_localize("10002", reason="打桩炸系列", force=True)
        api._drain_job_queue()
        state_boom = api.job_state()
        order_boom = list(seen["order"])
    finally:
        (api.load_library, api._coverage, api.localize_state_path, api._series_cover_gaps,
         api._owned_series_targets,
         st.series_cache_is_stale, st._read_cache_file, st._refresh_targets, st.refresh_series_cache,
         store_api.fetch_metadata, store_api.backfill_prices,
         artwork.ensure_covers, artwork.covers_dir) = originals
        api._reset_localize_job()
        shutil.rmtree(sandbox, ignore_errors=True)

    assert seen["budget"][0] == api.JOB_SERIES_BUDGET_SECONDS, seen["budget"]
    assert 0 < api.JOB_SERIES_BUDGET_SECONDS <= 90.0, "系列阶段预算必须 ≤ 90 秒"
    assert order_ok == ["names", "price", "covers", "series"], \
        f"关键阶段必须排在系列目录前面：{order_ok}"
    assert order_boom == ["names", "price", "covers", "series"], order_boom
    # 限时兜底：只有部分系列抓到，剩下的记进 deferred / 状态字段，并写进人话文案
    assert state_ok["ok"] is True, f"系列目录被截断不能算失败：{state_ok}"
    assert state_ok["series_fetched"] == 1 and state_ok["series_deferred"] == 1, state_ok
    assert "后台会继续补" in (state_ok["message"] or ""), state_ok["message"]
    secs = state_ok["stage_seconds"]
    assert set(secs) == {"names", "price", "covers", "pages", "series"}, secs
    # 系列阶段炸掉：任务仍然 ok:true（中文名 / 价格 / 封面照样保留）
    assert state_boom["ok"] is True and state_boom["error"] == "", \
        f"系列阶段失败不能连累整体：{state_boom}"
    assert "系列目录刷新失败（已忽略" in (state_boom["message"] or ""), state_boom["message"]
    print(f"PASS T6b 系列阶段限时：预算 {seen['budget'][0]:.0f}s，"
          f"deferred={state_ok['series_deferred']}，整体 ok={state_ok['ok']}，"
          f"阶段耗时 {secs}；系列炸掉后仍 ok={state_boom['ok']}")


def test_localize_price_unavailable_is_partial_not_failure():
    """T6c：价格一款都没取到（大陆网络 / 本就无售价）**不能**判成失败。

    这时任务应是 ``ok:true + partial:true + retry_needed:["price"]``（前端黄色/中性），
    并把这个「半成品」记进 localize_state.json，让下次仍然会自动重试；
    只有中文名与封面**都**没拿到东西，才是真失败（ok:false，前端红色）。
    """
    from core import artwork, store_api
    from engine import series_tree as st

    lib = {
        "steam_path": r"C:\fake\steam", "account_id": "10003",
        "play_stats": {5005: {"playtime": 5}}, "games_meta": {}, "statuses": {},
        "classified": {}, "named": 0,
    }
    seen = {"order": []}
    sandbox = _job_sandbox()
    originals = (
        api.load_library, api._coverage, api.localize_state_path, api._series_cover_gaps,
        api._owned_series_targets,
        st.series_cache_is_stale, st._read_cache_file, st._refresh_targets, st.refresh_series_cache,
        store_api.fetch_metadata, store_api.backfill_prices,
        artwork.ensure_covers, artwork.covers_dir,
    )
    try:
        api.localize_state_path = lambda: sandbox / "localize_state.json"
        api.load_library = lambda **kwargs: lib
        api._coverage = lambda *a, **k: {
            "total": 1, "name_cn": 1, "cover": 1, "price": 0,
            "missing_name_cn": 0, "missing_cover": 0, "missing_price": 1}
        api._series_cover_gaps = lambda lib_: []
        api._owned_series_targets = lambda appids: ["Stub"]
        artwork.covers_dir = lambda: sandbox
        st.series_cache_is_stale = lambda *a, **k: False      # 系列缓存新鲜 -> 本阶段直接跳过
        st._read_cache_file = lambda *a, **k: {"series": {}}
        st._refresh_targets = lambda *a, **k: ["Stub"]

        def fake_meta(appids, **kwargs):
            seen["order"].append("names")
            return {int(a): {"appid": int(a), "name_cn": "名字"} for a in appids}

        def fake_price(appids, **kwargs):
            seen["order"].append("price")
            callback = kwargs.get("progress")
            if callback:                       # 确实问过商店（tot>0），但一款都没取到
                callback(1, len(list(appids)), {"appid": 5005})
            return {}

        def fake_covers(appids, **kwargs):
            seen["order"].append("covers")
            return {"total": len(appids), "ok": len(appids), "failed": 0, "skipped": 0,
                    "unsupported": 0, "by_source": {}, "results": []}

        store_api.fetch_metadata = fake_meta
        store_api.backfill_prices = fake_price
        artwork.ensure_covers = fake_covers

        api._reset_localize_job()
        api.enqueue_localize("10003", reason="打桩价格不通", force=True)
        api._drain_job_queue()
        partial_state = api.job_state()
        record_partial = api.load_localize_state().get("10003") or {}
        api._JOB_LAST_ATTEMPT.clear()          # 模拟「下次启动」：进程内的短时抑制不参与
        needed_again = api._localize_needed("10003", record_partial.get("coverage"))

        # 第二轮：中文名与封面都没拿到 -> 才是真失败（红）
        order_partial = list(seen["order"])
        seen["order"] = []

        def dead_meta(appids, **kwargs):
            seen["order"].append("names")
            return {int(a): {} for a in appids}

        def dead_covers(appids, **kwargs):
            seen["order"].append("covers")
            return {"total": len(appids), "ok": 0, "failed": len(appids), "skipped": 0,
                    "unsupported": 0, "by_source": {}, "results": []}

        store_api.fetch_metadata = dead_meta
        artwork.ensure_covers = dead_covers
        api._reset_localize_job()
        api.enqueue_localize("10003", reason="打桩全空", force=True)
        api._drain_job_queue()
        dead_state = api.job_state()
        order_dead = list(seen["order"])
    finally:
        (api.load_library, api._coverage, api.localize_state_path, api._series_cover_gaps,
         api._owned_series_targets,
         st.series_cache_is_stale, st._read_cache_file, st._refresh_targets, st.refresh_series_cache,
         store_api.fetch_metadata, store_api.backfill_prices,
         artwork.ensure_covers, artwork.covers_dir) = originals
        api._reset_localize_job()
        shutil.rmtree(sandbox, ignore_errors=True)

    assert order_partial == ["names", "price", "covers"], order_partial
    assert order_dead == ["names", "price", "covers"], order_dead
    # 价格 0 款 = 基本补齐 + 稍后自动重试（黄），绝不是失败（红）
    assert partial_state["ok"] is True, f"价格取不到不该算失败：{partial_state}"
    assert partial_state["partial"] is True, partial_state
    assert partial_state["retry_needed"] == ["price"], partial_state
    assert partial_state["stage"] != "error", partial_state["stage"]
    assert "稍后会自动重试" in (partial_state["message"] or ""), partial_state["message"]
    # partial 不能记成「已补齐」，否则下次不会重试
    assert record_partial.get("partial") is True, record_partial
    assert record_partial.get("retry_needed") == ["price"], record_partial
    assert record_partial.get("last_full_at") in (None, ""), record_partial
    assert needed_again is True, "partial 记录下次必须仍会重试"
    # 中文名与封面都没拿到 -> 真失败
    assert dead_state["ok"] is False and dead_state["partial"] is False, dead_state
    assert dead_state["stage"] == "error", dead_state
    assert "中文名和封面都没抓到" in (dead_state["error"] or ""), dead_state["error"]
    print("PASS T6c 三态：价格 0 款=黄(partial/稍后重试)，中文名+封面全空=红(ok:false)")


def test_localize_state_corrupt_and_no_repeat():
    """T6：localize_state.json 损坏 -> 降级为「需要补齐」；跑过且没变差 -> 不重复入队。"""
    sandbox = _job_sandbox()
    original = api.localize_state_path
    coverage = {"total": 2, "name_cn": 2, "cover": 2, "price": 1,
                "missing_name_cn": 0, "missing_cover": 0, "missing_price": 1}
    try:
        api.localize_state_path = lambda: sandbox / "localize_state.json"
        api._reset_localize_job()
        # 1) 缺失 -> 需要补齐
        assert api.load_localize_state() == {}
        assert api._localize_needed("10001", coverage) is True
        # 2) 损坏 -> 降级成空表（绝不抛），仍需要补齐
        (sandbox / "localize_state.json").write_text("{ 这不是 json", encoding="utf-8")
        assert api.load_localize_state() == {}
        assert api._localize_needed("10001", coverage) is True
        # 3) 跑过一次（写入）且覆盖率没变差 -> 不重复出网
        api._record_localize_state("10001", coverage)
        assert api._localize_needed("10001", coverage) is False
        assert api.enqueue_localize("10001", coverage=coverage)["queued"] is False
        # 4) 又出现了新缺口（比如刚买了游戏）-> 重新需要
        assert api._localize_needed("10001", dict(coverage, missing_name_cn=1)) is True
        # 5) 覆盖率全齐 -> 什么都不用做
        assert api._localize_needed("10001", dict(coverage, missing_price=0)) is False
        # 6) 太久没跑（40 天前）-> 需要重跑
        state = api.load_localize_state()
        state["10001"]["last_full_at"] = (
            datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
        api.save_localize_state(state)
        assert api._localize_needed("10001", coverage) is True
        # 7) 刚失败过的账号：同一进程内短时抑制（不反复重跑）
        api._JOB_LAST_ATTEMPT["10002"] = time.time()
        assert api._localize_needed("10002", coverage) is False
    finally:
        api.localize_state_path = original
        api._reset_localize_job()
        shutil.rmtree(sandbox, ignore_errors=True)
    print("PASS T6 幂等状态：损坏降级 / 跑过不重跑 / 新缺口重跑 / 40 天后重跑")


def test_job_api_http_and_failure_degrades():
    """T6：GET /api/job 结构正确且空闲不报错；补齐失败只标 ok:false，面板其它接口照常 200。"""
    panel = Panel()
    try:
        status, ctype, body = panel.request("/api/job")
        assert status == 200 and "json" in ctype.lower(), (status, ctype)
        data = json.loads(body)
        for key in ("running", "pending", "stage", "stage_label", "done", "total",
                    "message", "ok", "error", "coverage", "queue", "last",
                    "partial", "retry_needed", "stage_seconds"):
            assert key in data, f"/api/job 缺少字段 {key}：{sorted(data)}"
        assert data["running"] is False and data["pending"] == 0, data
        assert data["partial"] is False and data["retry_needed"] == [], data

        # 读库失败（打桩）：任务必须降级成 ok:false，绝不让异常冒出来
        original = api.load_library

        def boom(**kwargs):
            raise RuntimeError("打桩：读库炸了")

        try:
            api.load_library = boom
            api._reset_localize_job()
            api.enqueue_localize("10001", reason="打桩失败", force=True)
            api._drain_job_queue()
            failed = api.job_state()
        finally:
            api.load_library = original
            api._reset_localize_job()
        assert failed["ok"] is False and failed["error"], f"失败应记 ok:false：{failed}"
        assert "10001" in failed["error"] or "打桩" in failed["error"], failed["error"]
        assert "手动重试" in failed["message"], f"提示应人话：{failed['message']}"

        # 失败后面板其它接口仍然 200
        status, _ctype, _body = panel.request("/api/state")
        assert status == 200, status
        status, _ctype, _body = panel.request("/api/job")
        assert status == 200, status
    finally:
        panel.close()
    print("PASS T6 /api/job 结构 + 失败降级（面板其它接口仍 200）")



# ------------------------------------------------- 名字下方不再挂小字号英文名

def test_no_secondary_small_english_name_under_game_name():
    """游戏名下面**不再**挂一行小字英文名（用户反馈：游戏明细与智能推荐里那行小字去掉）。

    覆盖面板全部会渲染游戏名的地方：
      * ``app.js``：智能推荐卡片、游戏明细卡片；
      * ``series.html``：系列详情页卡片。

    只允许**一个**名字（中文名优先，没有中文名才回落原名），英文原名必须挪进
    悬浮提示（``title``），不能再占一行版面 —— 所以这里断言前端源码里
    ``.name-en`` / ``.s-en`` 这两个「名字下面的小字」类名彻底消失。
    """
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    series = (APP_ROOT / "webapp" / "templates" / "series.html").read_text(encoding="utf-8")

    assert "name-en" not in js, "app.js 还在渲染名字下面的小字号英文名"
    assert "name-en" not in css, "style.css 还留着 .name-en 的小字样式"
    assert "s-en" not in series, "系列详情页还在渲染名字下面的小字号英文名"

    # 一个名字：中文名优先（没有中文名才用原名），不再把两个名字都吐出来
    assert '<span class="name-cn">${esc(r.name_cn || r.name)}</span>' in js
    assert '<span class="s-name">${esc(row.name_cn || row.name)}</span>' in series
    # 英文原名没丢，挪进悬浮提示里可查
    assert 'title="${esc(r.name_cn && r.name && r.name_cn !== r.name' in js
    assert 'title="${esc(row.name_cn && row.name && row.name_cn !== row.name' in series
    print("PASS 名字只留一个（中文名优先），小字英文名已移除（面板 3 处）")



def main():
    tests = [
        test_frames_are_clickable_as_a_whole,
        test_frame_hint_is_hover_only,
        test_pages_prefill_lists_existing_without_rendering,
        test_pages_missing_when_no_artifacts,
        test_page_get_reuses_existing_artifact,
        test_page_get_rerenders_when_renderer_changed,
        test_page_artifact_state_detects_other_account,
        test_page_artifact_state_empty_account_ownership,
        test_page_get_does_not_serve_other_account_artifact,
        test_page_stale_artifact_is_flagged_in_listing,
        test_renderer_fingerprint_covers_code_not_volatile_data,
        test_renderer_fingerprint_reacts_to_engine_changes,
        test_renderer_fingerprint_reacts_to_ownership_changes,
        test_pages_api_only_existing,
        test_panel_autoloads_frames_on_open,
        test_pages_auto_refresh_after_data_change,
        test_pages_render_race_guard,
        test_log_api_tail_and_safety,
        test_log_endpoint_http,
        test_client_gone_not_logged_as_error,
        test_series_boxes_are_clickable,
        test_series_page_served,
        test_series_api_bad_input,
        test_series_api_detail_shape,
        test_series_covers_offline_skips,
        test_series_covers_bounded_and_degrades,
        test_series_section_adds_local_thumbs,
        test_series_thumbs_uniform_max_four,
        test_series_thumbs_show_all_when_fewer_than_four,
        test_localize_covers_include_unowned_series_members,
        test_series_render_never_goes_online,
        test_no_art_store_roundtrip_merge_and_corrupt,
        test_series_section_reports_missing_and_no_art,
        test_localize_no_art_only_records_404,
        test_no_art_lookup_is_local_only,
        test_add_account_enqueues_auto_localize,
        test_localize_job_runs_all_stages_with_progress,
        test_localize_series_stage_is_bounded_and_isolated,
        test_localize_price_unavailable_is_partial_not_failure,
        test_localize_state_corrupt_and_no_repeat,
        test_job_api_http_and_failure_degrades,
        test_no_secondary_small_english_name_under_game_name,
    ]
    for test in tests:
        test()
    print(f"\n全部 {len(tests)} 组验证通过")


if __name__ == "__main__":
    main()
