# -*- coding: utf-8 -*-
r"""E4 验收：族谱成员 / 自有库的中文名缺口统计 + 抽样对照表（只读，不联网）。

用法（GameLife 根目录）：
    .\.venv\Scripts\python.exe .scratch\e4_report.py [账号ID]
"""
import json
import os
import re
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
APP_ROOT = Path(__file__).resolve().parent.parent
ROOT = str(APP_ROOT.parent)
S = Path(ROOT) / ".scratch"
sys.path.insert(0, str(APP_ROOT))
os.chdir(str(APP_ROOT))
from webapp import api

CJK = re.compile(r"[\u4e00-\u9fff]")
ACC = sys.argv[1] if len(sys.argv) > 1 else "10000001"
lib = api.load_library(account_id=ACC)
sec = api._series_section(lib)

total = miss = vis_en = 0
visible = []
for s in sec.get("series") or []:
    for g in s.get("games") or []:
        total += 1
        if str(g.get("name_cn") or "").strip():
            continue
        miss += 1
        if not CJK.search(str(g.get("name") or "")):
            vis_en += 1
            visible.append((s.get("series"), int(g.get("appid")), g.get("name")))

ps = lib.get("play_stats") or {}
own_miss = []
for aid in ps:
    try:
        i = int(aid)
    except Exception:
        continue
    if not str(api._name_cn(i) or "").strip():
        own_miss.append(i)
own_real = [a for a in own_miss if not api.is_non_game(a)]

print(f"账号 {lib.get('account_id')}")
print(f"族谱：{len(sec.get('series') or [])} 个系列 / {total} 个成员")
print(f"  缺 name_cn：{miss}    其中仍会显示英文名：{vis_en}")
print(f"自有库：{len(ps)} 款   缺 name_cn：{len(own_miss)}   其中真游戏：{len(own_real)}")

filled = {}
try:
    filled = (json.load(open(os.path.join(S, "e4_fill_result.json"), encoding="utf-8"))
              .get("filled") or {})
except Exception:
    pass
cached = json.load(open(str(APP_ROOT / "data" / "names_cn.json"),
                        encoding="utf-8"))["items"]
en_of = {}
for series, aid, name in visible:
    en_of.setdefault(str(aid), name)
for _s, a, n in (json.load(open(os.path.join(S, "e4_gap.json"), encoding="utf-8")).get("missing") or []):
    en_of.setdefault(str(a), n)

print("\n抽样 10 条（appid / 英文名 / 中文名 / 来源站点）")
ids = [int(a) for a in json.load(open(os.path.join(S, "e4_ids.json"), encoding="utf-8"))]
by_source = {}
for a in ids:
    entry = cached.get(str(a)) or {}
    cn = str(entry.get("name_cn") or "").strip()
    if cn and str(entry.get("name_en") or "").strip():
        by_source.setdefault(str(entry.get("source") or "?"), []).append((a, entry))
order = ["steam_store", "steamcmd", "series_cache", "baike", "steam_client"]
shown = 0
for src in order + [k for k in by_source if k not in order]:
    for a, entry in sorted(by_source.get(src) or [])[:3]:
        print(f"  {a:>8}  {str(entry['name_en'])[:40]:<40} {str(entry['name_cn'])[:26]:<26} {src}")
        shown += 1
        if shown >= 10:
            break
    if shown >= 10:
        break

if vis_en:
    print(f"\n仍显示英文名：{vis_en} 条（官方商店 cc=CN/cc=US + 百科都没有中文名，按任务书「缺失允许」保留）")
    for series, aid, name in visible[:10]:
        print(f"  {aid:>8}  {series:<12} {name[:48]}")