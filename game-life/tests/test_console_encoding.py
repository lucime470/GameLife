r"""F6 回归闸门：控制台编码兜底——游戏名含 GBK 编不出的字符时**绝不崩溃**。

真实 bug（主 agent 实测，默认中文 Windows 控制台 cp936 / GBK）::

    cd game-life
    ..\.venv\Scripts\python.exe main.py stats --account-id 10000002
    -> UnicodeEncodeError: 'gbk' codec can't encode character '\xae'
       （游戏名《暗黑破坏神® IV》里的 ®），退出码 1、输出半截。

根因：游戏名是**外部数据**，随时可能出现 GBK 编不出的字符（® / ™ / emoji /
繁体生僻字……），``print`` 直接抛异常，整个命令带着半截输出死掉。

修复：``main._make_console_encoding_safe()`` 把 ``sys.stdout`` / ``sys.stderr``
配成「**保持控制台原有编码**，只把 errors 从 strict 降级为 replace」——编不出的
字符变成 ``?``，命令永不因编码失败。**绝不改成 encoding="utf-8"**：那会把中文
Windows 控制台变成乱码；``PYTHONIOENCODING=utf-8`` 场景必须原样透传、不得丢字。

本文件**全部使用合成数据**：不跑子进程、不读真实 Steam 数据、不联网。
"""

import io
import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
APP_ROOT = TESTS_DIR.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import main as cli  # noqa: E402

# 合成样本：GBK 编不出 ®（U+00AE 不在 GBK 里），其余中文都编得出。
GAME_NAME = "《暗黑破坏神® IV》"
GBK_FALLBACK = "《暗黑破坏神? IV》"
RICH_TEXT = "《暗黑破坏神® IV》 · ™ · 🎮 中文"


def _gbk_stream(errors="strict"):
    """返回 (底层 BytesIO, 以 gbk 编码的文本流)——模拟默认中文控制台。"""
    buf = io.BytesIO()
    return buf, io.TextIOWrapper(buf, encoding="gbk", errors=errors)


def _stats_payload(name=GAME_NAME):
    """最小合成 stats 结构，字段与 main._print_stats_table 读取的一一对应。"""
    entry = {"name": name, "playtime_hours": 228.8, "last_played": 1758643200}
    return {
        "total_games": 24,
        "total_playtime_hours": 371.58,
        "avg_playtime_hours": 15.48,
        "dust_rate": 75.0,
        "played_ratio": 75.0,
        "played_count": 18,
        "unplayed_count": 6,
        "status_counts": {"未玩": 6},
        "status_percent": {"未玩": 25.0},
        "playtime_distribution": [{"label": "1-10小时", "count": 12, "percent": 50.0}],
        "top_genres": [{"name": "动作", "count": 10}],
        "most_forgotten": [entry],
        "recent_played": [entry],
    }


# ------------------------------------------------------- 保护函数本身：不抛 + 不丢内容

def test_guard_makes_gbk_stream_replace_instead_of_raise():
    """GBK 流写 ®：保护后不抛异常，字节仍可用 gbk 解出，且编码保持 gbk。"""
    buf, stream = _gbk_stream(errors="strict")

    # 先证明这真是「编不出」：未保护的严格流写入必抛。
    with pytest.raises(UnicodeEncodeError):
        stream.write(GAME_NAME)

    guarded = cli._guard_stream(stream)
    assert guarded is stream, "带 reconfigure 的流应原地改 errors，不换对象"
    assert guarded.errors == "replace"
    assert (guarded.encoding or "").lower().replace("-", "") == "gbk", (
        "必须保持控制台原有编码，绝不改成 utf-8"
    )

    guarded.write(GAME_NAME)  # 关键：不再抛
    guarded.flush()
    assert buf.getvalue().decode("gbk") == GBK_FALLBACK


def test_utf8_stream_keeps_text_intact():
    """能编码时内容不丢：utf-8 场景（PYTHONIOENCODING=utf-8）原样透传。"""
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="utf-8", errors="strict")
    guarded = cli._guard_stream(stream)

    guarded.write(RICH_TEXT)
    guarded.flush()
    decoded = buf.getvalue().decode("utf-8")
    assert decoded == RICH_TEXT
    assert "?" not in decoded, "能编码的场景不得出现任何降级占位符"


def test_stream_without_reconfigure_does_not_raise():
    """没有 reconfigure 的流（StringIO / 桩对象 / None）：绝不抛异常。"""
    # (a) io.StringIO：无 reconfigure，原样返回、内容完整
    text = io.StringIO()
    assert cli._guard_stream(text) is text
    text.write(RICH_TEXT)
    assert text.getvalue() == RICH_TEXT

    # (b) 只有 .buffer、没有 reconfigure：退回包一层，按 replace 编码
    class _Buffered:
        def __init__(self, buffer):
            self.buffer = buffer
            self.encoding = "gbk"
            self.line_buffering = False

    buf = io.BytesIO()
    wrapped = cli._guard_stream(_Buffered(buf))
    assert wrapped is not None
    wrapped.write(GAME_NAME)  # 不抛
    wrapped.flush()
    assert buf.getvalue().decode("gbk") == GBK_FALLBACK

    # (c) None（windowed 解释器可能根本没有标准流）：原样返回
    assert cli._guard_stream(None) is None


# ------------------------------------------------- main() 入口：同时保护 stdout/stderr

def test_make_console_encoding_safe_guards_both_streams(monkeypatch):
    """_make_console_encoding_safe()：stdout/stderr 一并保护、幂等、保留原编码。"""
    out_buf, out = _gbk_stream()
    err_buf, err = _gbk_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    monkeypatch.setattr(cli, "_CONSOLE_ENCODING_GUARDED", False, raising=False)

    cli._make_console_encoding_safe()
    cli._make_console_encoding_safe()  # 幂等：重复调用不报错、不重复包装

    assert sys.stdout.errors == "replace" and sys.stderr.errors == "replace"
    assert (sys.stdout.encoding or "").lower().replace("-", "") == "gbk"

    print(GAME_NAME)  # 走被保护的 stdout：不抛
    sys.stderr.write(GAME_NAME + "\n")  # stderr 同样受保护
    sys.stdout.flush()
    sys.stderr.flush()

    assert out_buf.getvalue().decode("gbk") == GBK_FALLBACK + "\r\n"
    assert GBK_FALLBACK in err_buf.getvalue().decode("gbk")


# --------------------------------------- 崩溃点本身：合成数据打印统计表（含游戏名）

def test_stats_table_prints_unencodable_name_without_crashing(monkeypatch):
    """直接覆盖崩溃点（_print_stats_table 的 print）：GBK 流 + 合成数据不抛异常。"""
    buf, stream = _gbk_stream()
    guarded = cli._guard_stream(stream)
    monkeypatch.setattr(sys, "stdout", guarded)

    cli._print_stats_table(_stats_payload(), show_meta=True)  # 必须不抛异常
    guarded.flush()

    result = buf.getvalue().decode("gbk")
    assert GBK_FALLBACK in result, "游戏名照常显示，只是 ® 降级为 ?"
    assert "最近游玩 TOP：" in result, "崩溃点所在的那一节必须完整打印出来"
    assert "类型分布 TOP：" in result
