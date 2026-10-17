/* GameLife 本地面板前端逻辑（零依赖，原生 fetch） */
const $ = (id) => document.getElementById(id);
const state = { account: "", withMeta: false, mergedOption: null };

/* 当前选的是不是「合流库」（把已添加的账号当作同一个账号统计）。
   静默兜底：/api/accounts 没返回 merged 时按账号 id 判断，避免状态错位。 */
function isMergedAccount() {
  const id = String(state.account || "").toLowerCase();
  return state.account === "all" || id === "merged" || id === "confluence" || id === "合流" || id === "合流库";
}
// 非游戏条目（Steam 客户端组件 / 工具 / 性能测试工具…）由**服务端**统一剔除：
// 判定口径唯一一份在 core/non_game.py，/api/summary 的 games 与 /api/recommend
// 的 recommendations 回来时都已经干净。前端不再自己维护黑名单 —— 那会让
// 「哪个条目算游戏」在两处各有一份答案，改一处漏一处。

function toast(msg, kind) {
  const el = $("toast");
  el.textContent = msg;
  // kind: 省略 = 普通（绿/中性）；"warn" = 黄（基本补齐、稍后自动重试）；true/"err" = 红（真失败）
  const cls = (kind === true || kind === "err" || kind === "error") ? " err"
    : (kind === "warn" ? " warn" : "");
  el.className = "toast" + cls;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 6200);
}

function setStatus(text) { $("status").textContent = text; }

async function getJSON(url) {
  const res = await fetch(url);
  let data = null;
  try { data = await res.json(); } catch (e) { data = { error: "响应不是 JSON" }; }
  if (!res.ok || (data && data.error)) {
    throw new Error((data && data.error) || ("HTTP " + res.status));
  }
  return data;
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await res.json(); } catch (e) { data = { error: "响应不是 JSON" }; }
  if (!res.ok || (data && data.error)) {
    throw new Error((data && data.error) || ("HTTP " + res.status));
  }
  return data;
}

function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function q(extra) {
  const p = new URLSearchParams({ account_id: state.account, with_metadata: state.withMeta ? 1 : 0 });
  Object.entries(extra || {}).forEach(([k, v]) => { if (v != null && v !== "") p.set(k, v); });
  return p.toString();
}

/* ---------------- 账号（先添加再用） ---------------- */

function accountLabel(a) {
  return (a && a.name) ? `${a.name}（${a.id}）` : String((a && a.id) || "");
}

function fillAccountSelect(accounts, current) {
  const sel = $("account");
  sel.innerHTML = "";
  (accounts || []).forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = accountLabel(a) + (a.present === false ? "（本机已找不到）" : "");
    sel.appendChild(opt);
  });
  // 合流库：把已添加的账号当作**同一个账号**统计。至少两个账号才有意义，
  // 故排在最后且默认不选中（用户主动选它才生效）。
  if (state.mergedOption) {
    const opt = document.createElement("option");
    opt.value = state.mergedOption.id;
    opt.textContent = `${state.mergedOption.name} · 合并统计`;
    sel.appendChild(opt);
  }
  if (current && (accounts || []).some((a) => a.id === current)) sel.value = current;
  return sel.value || "";
}

async function loadAccounts() {
  const data = await getJSON("/api/accounts");
  const accounts = data.accounts || [];
  state.mergedOption = data.merged || null;
  state.account = fillAccountSelect(accounts, data.current || state.account);
  renderAddedAccounts(accounts);
  renderSetup(!!data.needs_setup, accounts.length);
  if (data.suggested && data.suggested.id) {
    toast(`检测到账号「${data.suggested.name || data.suggested.id}」，可在「添加账号」里加入`);
  }
  if (!accounts.length) {
    setStatus("请先添加账号");
  }
  return data;
}

function renderSetup(needsSetup, count) {
  const box = $("setupCard");
  if (!box) return;
  box.classList.toggle("hidden", !needsSetup);
  const hint = $("setupHint");
  if (hint && needsSetup) hint.textContent = "还没有添加账号，点左边的按钮开始";
  if (needsSetup) $("accountCard").classList.add("hidden");
  if (!needsSetup && count) $("accountCount").textContent = `已添加 ${count} 个`;
}

function renderAddedAccounts(accounts) {
  const box = $("addedAccounts");
  const count = $("accountCount");
  if (!box) return;
  if (count) count.textContent = accounts.length ? `已添加 ${accounts.length} 个` : "还没有添加账号";
  if (!accounts.length) {
    box.innerHTML = '<span class="muted">还没有添加账号。</span>';
    return;
  }
  box.innerHTML = accounts.map((a) => `
    <div class="item">
      <span class="name">${esc(accountLabel(a))}${a.present === false ? ' <span class="pill">本机已找不到</span>' : ""}</span>
      <button class="btn" data-remove="${esc(a.id)}">移除</button>
    </div>`).join("");
  box.querySelectorAll("button[data-remove]").forEach((btn) => {
    btn.addEventListener("click", () => removeAccount(btn.getAttribute("data-remove")));
  });
}

/* ---- 添加账号弹窗 ---- */
let _addCandidates = [];

async function openAddModal() {
  try {
    const data = await getJSON("/api/accounts");
    _addCandidates = data.available || [];
    const box = $("addList");
    if (!_addCandidates.length) {
      box.innerHTML = '<span class="muted">本机没有其它可添加的账号。</span>';
    } else {
      box.innerHTML = _addCandidates.map((a) => `
        <label class="item add-row">
          <span class="name"><input type="checkbox" value="${esc(a.id)}"> ${esc(accountLabel(a))}</span>
        </label>`).join("");
    }
    $("addHint").textContent = _addCandidates.length ? `共 ${_addCandidates.length} 个可添加` : "";
    $("addModal").classList.remove("hidden");
    if ($("btnAddConfirm")) $("btnAddConfirm").disabled = !_addCandidates.length;
  } catch (e) {
    toast("读取账号失败：" + e.message, true);
  }
}

function closeAddModal() { $("addModal").classList.add("hidden"); }

async function confirmAddAccounts() {
  const ids = Array.from($("addList").querySelectorAll("input[type=checkbox]"))
    .filter((c) => c.checked).map((c) => c.value);
  if (!ids.length) { toast("请先勾选要添加的账号", true); return; }
  const btn = $("btnAddConfirm");
  btn.disabled = true;
  try {
    const d = await postJSON("/api/accounts/add", {
      account_ids: ids, current: state.account || null,
    });
    closeAddModal();
    toast(d.message || "已添加账号");
    state.account = fillAccountSelect(d.accounts || [], d.current || state.account);
    renderAddedAccounts(d.accounts || []);
    renderSetup(!!d.needs_setup, (d.accounts || []).length);
    if (state.account) await refresh();
    startJobPolling();   // T6：添加账号后自动补齐数据（后台跑），立刻开始轮询进度
  } catch (e) {
    toast("添加失败：" + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function removeAccount(id) {
  const label = accountLabel((await getJSON("/api/accounts")).accounts.find((a) => a.id === id));
  if (!confirm(`从面板列表移除「${label}」？\n\n不会删除 Steam 里的任何文件。`)) return;
  try {
    const d = await postJSON("/api/accounts/remove", { account_id: id, current: state.account });
    toast(d.message || "已移除");
    state.account = fillAccountSelect(d.accounts || [], d.current || "");
    renderAddedAccounts(d.accounts || []);
    renderSetup(!!d.needs_setup, (d.accounts || []).length);
    if (state.account) await refresh(); else clearDashboard();
  } catch (e) {
    toast("移除失败：" + e.message, true);
  }
}

function clearDashboard() {
  setStatus("请先添加账号");
  ["statusDist", "challenge", "persona", "value", "series", "wishlist", "radar"].forEach((id) => {
    const el = $(id);
    if (el) el.innerHTML = '<span class="muted">还没有添加账号</span>';
  });
  const games = $("games");
  if (games) games.innerHTML = '<div class="muted" style="padding:10px">还没有添加账号</div>';
  const notice = $("sharedNotice");
  if (notice) notice.textContent = "";
  const wrap = $("sharedToggleWrap");
  if (wrap) wrap.style.display = "none";
  const rec = $("rec");
  if (rec) rec.innerHTML = '<span class="muted">还没有添加账号</span>';
}

/* ---------------- 汇总 ---------------- */

function renderOverview(d) {
  const s = d.stats || {};
  $("mTotal").textContent = s.total_games != null ? s.total_games : "-";
  $("mHours").innerHTML = s.total_playtime_hours != null ? `${s.total_playtime_hours}<small>h</small>` : "-";
  $("mAvg").innerHTML = s.avg_playtime_hours != null ? `${s.avg_playtime_hours}<small>h</small>` : "-";
  $("mDust").innerHTML = s.dust_rate != null ? `${s.dust_rate}<small>%</small>` : "-";
  $("mPlayed").innerHTML = s.played_ratio != null ? `${s.played_ratio}<small>%</small>` : "-";
  const metaText = d.named ? `${d.named} 款有元数据` : "无元数据";
  $("metaTag").textContent = d.merged ? `合流库 · ${metaText}` : metaText;

  const dist = $("statusDist");
  dist.innerHTML = "";
  const counts = s.status_counts || {};
  const pct = s.status_percent || {};
  const keys = Object.keys(counts);
  if (!keys.length) { dist.innerHTML = '<span class="muted">暂无数据</span>'; return; }
  keys.forEach((k) => {
    const row = document.createElement("div");
    row.className = "stack";
    row.innerHTML = `
      <div class="item">
        <span class="name"><span class="badge s${esc(k)}">${esc(k)}</span></span>
        <span class="val">${counts[k]} 款 · ${(pct[k] != null ? pct[k] : 0)}%</span>
      </div>
      <div class="bar"><i style="width:${Math.max(2, Number(pct[k] || 0))}%"></i></div>`;
    dist.appendChild(row);
  });
}

function renderChallenge(d) {
  const box = $("challenge");
  if (!d || d.available === false) {
    box.innerHTML = `<span class="muted">${esc((d && d.message) || "暂无")}</span>`;
    return;
  }
  const mc = d.monthly_challenge || {};
  const prog = mc.progress || {};
  const goal = mc.goal || "-";
  const doneText = prog.needed != null
    ? `已完成 ${prog.done || 0} / ${prog.needed} 款`
    : `已玩 ${prog.done_minutes || 0} / ${prog.needed_minutes || 0} 分钟`;
  const games = (mc.games || []).map((g) => esc(g.name)).join("、") || "（无候选）";
  box.innerHTML = `
    <div><b>${esc(d.month || "")} 目标：</b>${esc(goal)} ${mc.completed ? '<span class="badge s通关">已完成</span>' : '<span class="badge">进行中</span>'}</div>
    <div class="sec">${esc(doneText)} · 候选：${games}</div>
    <div class="kv">
      <span class="k">本月通关</span><span>${d.completed_count || 0} 款</span>
      <span class="k">本月回归</span><span>${d.resume_count || 0} 款</span>
      <span class="k">本月新玩</span><span>${d.new_playing_count || 0} 款</span>
      <span class="k">搁置游玩时长</span><span>${d.shelved_hours || 0} h</span>
      <span class="k">本月 XP</span><span>+${d.month_xp || 0}</span>
      <span class="k">累计 XP / 等级</span><span>${d.cumulative_xp || 0} · Lv.${d.level || 0} ${esc(d.title || "")}</span>
    </div>
    <div class="hint">面板只计算不落库；要记账请运行 CLI 的 challenge 命令。</div>`;
}

function renderPersona(d) {
  const box = $("persona");
  if (!d) { box.innerHTML = '<span class="muted">暂无</span>'; return; }
  const dims = d.dimensions || {};
  const names = { purchase: "购买力", execution: "执行力", focus: "专注度", breadth: "口味广度", courage: "勇气值" };
  let html = `<div><b>${esc(d.primary_label || d.label || "未知人格")}</b></div>`;
  html += `<div class="sec">${esc(d.summary || "")}</div><div class="stack" style="margin-top:8px">`;
  Object.entries(dims).forEach(([k, v]) => {
    const val = Math.round(Number(v) || 0);
    html += `<div><div class="item"><span class="k">${esc(names[k] || k)}</span><span class="val">${val}</span></div>
      <div class="bar"><i style="width:${Math.min(100, Math.max(2, val))}%"></i></div></div>`;
  });
  html += `</div>`;
  if ((d.labels || []).length) html += `<div class="hint">标签：${esc((d.labels || []).join("、"))}</div>`;
  box.innerHTML = html;
}

function coverImg(url, kind, cls) {
  if (!url) return "";
  const k = (kind === "landscape") ? "landscape" : "portrait";
  return `<img class="${cls || "thumb"} ${k}" src="${esc(url)}" alt="" loading="lazy" onerror="this.style.display='none'">`;
}

function genreBadges(list) {
  return (list || []).map((g) => `<span class="badge">${esc(g)}</span>`).join(" ");
}

function renderRec(d) {
  const box = $("rec");
  // 服务端已剔除非游戏条目；这里只做结构兜底
  const items = ((d && d.recommendations) || []).filter((it) => it && it.appid != null);
  if (!items.length) { box.innerHTML = '<span class="muted">没有候选（试试放开类型筛选）</span>'; return; }
  box.innerHTML = items.map((it) => `
    <div class="item">
      <span class="rowleft">
        ${coverImg(it.cover, it.cover_kind, "thumb")}
        <span style="min-width:0">
          <span class="name" title="${esc(it.name_cn && it.name && it.name_cn !== it.name ? `${it.name_cn}（${it.name}）` : (it.name_cn || it.name))}">${esc(it.name_cn || it.name)}</span>
          <div class="why">${esc(it.reason || "")}</div>
          <div class="why">${genreBadges(it.genres_cn)}</div>
        </span>
      </span>
      <span class="recmatch">
        <span class="val">${it.score}</span>
        <span class="scorelab">${esc(it.score_label || "")}</span>
      </span>
    </div>`).join("");
}

function renderValue(d) {
  const box = $("value");
  if (!d) { box.innerHTML = '<span class="muted">暂无</span>'; return; }
  const top = (d.value_top || []).slice(0, 5);
  const waste = (d.waste_top || []).slice(0, 5);
  const free = (d.zero_cost_top || []).slice(0, 5);
  const stats = d.stats || {};
  const unknown = stats.unknown_price_count != null ? Number(stats.unknown_price_count) : null;
  const line = (list, cls) => list.map((g) => `
    <div class="item">
      <span class="name">${esc(g.name_cn || g.name)}</span>
      <span class="val" style="${cls === "waste" ? "color:var(--accent-red)" : ""}">${g.cost_per_hour != null ? g.cost_per_hour : "-"} 元/时</span>
    </div>`).join("") || '<span class="muted">暂无</span>';
  const freeLine = free.map((g) => `
    <div class="item">
      <span class="name">${esc(g.name_cn || g.name)}</span>
      <span class="val">已玩 ${g.playtime_hours != null ? g.playtime_hours : 0} h</span>
    </div>`).join("");

  // 单价榜为空时给出有意义的说明，而不是干瘪的「暂无」或看起来像坏了
  const emptyHint = (label) =>
    `<span class="muted">价格数据不足，暂无法计算${label}。${unknown ? `（当前有 ${unknown} 款价格未知）` : ""}</span>`;
  const emptyBoth = !top.length && !waste.length;

  box.innerHTML = `
    <div class="sec" style="margin-bottom:6px">最划算</div>${top.length ? line(top, "value") : emptyHint("性价比")}
    <div class="sec" style="margin:10px 0 6px">最亏（买了没玩）</div>${waste.length ? line(waste, "waste") : emptyHint("最浪费")}
    ${freeLine ? `<div class="sec" style="margin:10px 0 6px">免费游戏时长榜</div>${freeLine}` : ""}
    ${emptyBoth ? '<div class="hint">拿不到价格的游戏暂不参与排名，补齐价格后会自动出现。</div>' : ""}`;
}

/* 族谱每个系列框**统一最多 4 张**封面（与后端 webapp/api.py 的 SERIES_THUMB_LIMIT 一致）；
   不足 4 部的系列有多少显示多少。 */
const SERIES_THUMB_LIMIT = 4;

function seriesURL(name) {
  /* 系列详情页地址：系列名是中文，必须整体编码，服务端 parse_qs 会自动解码 */
  const p = new URLSearchParams({ name: name });
  if (state.account) p.set("account_id", state.account);
  if (state.withMeta) p.set("with_metadata", "1");
  return "/series?" + p.toString();
}

function renderSeries(d) {
  const box = $("series");
  // 让这张卡片纵向撑满左栏（与性价比榜等高），滚动区吃掉剩余高度
  const fillCard = box.closest(".card");
  if (fillCard) fillCard.classList.add("card-fill");
  if (!d) { box.innerHTML = '<span class="muted">暂无</span>'; return; }
  // 显示全部系列：不再静默截断（列表可滚动查看）
  // 展示「确实拥有」或「未拥有但在家庭库里玩过」的系列；两者都没有的与他无关。
  let list = (d.series || []).filter(
    (g) => (g.owned_count || 0) > 0 || (g.played_count || 0) > 0);
  if (!list.length) { list = d.series || []; }
  if (!list.length) { box.innerHTML = '<span class="muted">没有识别到系列作品</span>'; return; }

  const cards = list.map((g) => {
    const name = g.series || "";
    const href = seriesURL(name);
    const owned = g.thumbs_owned || [];
    const played = g.thumbs_played || [];
    // 缩略图顺序由服务端决定：**已拥有**排在前、**玩过的**次之。
    // 已拥有的加一圈亮边 + 左下角小圆点（亮绿）；玩过（家庭库）的用蓝色细边区分。
    const thumbs = (g.thumbs || []).slice(0, SERIES_THUMB_LIMIT);
    const covers = thumbs
      .map((u, i) => {
        const mine = owned.length ? !!owned[i] : false;
        const wasPlayed = !mine && (played.length ? !!played[i] : false);
        return `<span class="ci${mine ? " mine" : (wasPlayed ? " played" : "")}">`
          + `<img src="${esc(u)}" alt="" loading="lazy"></span>`;
      }).join("");
    const placeholder = !covers ? `<span class="ph">${esc((name || "?").slice(0, 1))}</span>` : "";
    // 该系列本该显示 min(4, 作品总数) 张却凑不够 = 本机还缺封面（系列里未拥有的作品
    // 从没抓过图）。缺口分两种、说两种话，**都只在确有缺口时出现**，不常驻：
    //   · 缺的全是「官方 CDN 确认无图（404）」的（后端 thumbs_no_art）—— 再点抓取也补不上，
    //     只能如实说「官方暂无封面图」，不许叫用户去点；
    //   · 还有没试过的缺口 —— 才给「可点抓取」的指引。
    // 这两种都必须**只占一行**：卡片内容宽只有约 150px（10px 字号约 15 个汉字），
    // 实测「封面不全（缺 1 张），可点「抓取中文名与封面」补齐」要 241px，会折成两行/被截断，
    // 所以这里用短句 + 把完整指引放进 title 悬浮；overflow/ellipsis 只是极窄窗口的兜底。
    const want = Math.min(SERIES_THUMB_LIMIT, g.total_count || 0);
    const missingThumbs = Math.max(0, (g.thumbs_missing === undefined || g.thumbs_missing === null)
      ? want - thumbs.length
      : Number(g.thumbs_missing) || 0);
    const noArtThumbs = Math.min(missingThumbs, Math.max(0, Number(g.thumbs_no_art) || 0));
    let gapHint = "";
    if (missingThumbs > 0) {
      const hintStyle = "display:block;font-size:10px;color:var(--text-muted);line-height:1.2;"
        + "white-space:nowrap;overflow:hidden;text-overflow:ellipsis";
      gapHint = noArtThumbs >= missingThumbs
        ? `<span style="${hintStyle}" title="官方图源没有这几张封面（404），抓取也补不上">另有 ${missingThumbs} 部官方暂无封面图</span>`
        : `<span style="${hintStyle}" title="点操作区的「抓取中文名与封面」可以补齐">封面不全（缺 ${missingThumbs} 张），可点抓取</span>`;
    }
    const playedCount = g.played_count || 0;
    const tip = `打开「${esc(name)}」系列的全部作品（已拥有 ${g.owned_count || 0}/${g.total_count || 0} 部`
      + (playedCount ? `，另玩过（家庭库）${playedCount} 部` : "") + "）"
      + "（每个系列最多 4 部代表作；亮绿点 = 已拥有，蓝边 = 家庭库玩过）";
    const playedBadge = playedCount ? ` · 玩过 ${playedCount} 部` : "";
    return `
    <a class="sbox" href="${esc(href)}" target="_blank" rel="noopener" title="${tip}">
      <span class="sbox-covers">${covers}${placeholder}</span>
      <span class="sbox-name">${esc(name)}</span>
      ${gapHint}
      <span class="sbox-foot"><span class="badge">已拥有 ${g.owned_count || 0}/${g.total_count || 0} 部${playedBadge}</span>
        <span class="sbox-open">查看全部 ↗</span></span>
    </a>`;
  }).join("");

  box.innerHTML = `<div class="hint" style="margin:0 0 8px">共 ${list.length} 个系列 · 每个系列最多 4 部代表作 · 亮绿点 = 已拥有，蓝边 = 家庭库玩过</div>
    <div class="series-grid">${cards}</div>`;
}

function renderWishlist(d) {
  const box = $("wishlist");
  if (!d) { box.innerHTML = '<span class="muted">暂无</span>'; return; }
  const warns = (d.warnings || []).slice(0, 6);
  const unknown = (d.stats && d.stats.unknown_price_count) || 0;
  const priceLess = (d.genres || []).every((g) => !g.avg_price_yuan);
  if (!warns.length) {
    const extra = priceLess
      ? `（价格数据不足，均价暂按 0 计${unknown ? `，有 ${unknown} 款价格未知` : ""}；补齐后判定更准）`
      : "";
    box.innerHTML = `<span class="muted">暂无明显「买了不玩」的类型，保持冷静。${esc(extra)}</span>`;
    return;
  }
  box.innerHTML = warns.map((w) => `
    <div class="item">
      <span class="name">${esc(w.genre || "")}</span>
      <span class="val" style="color:var(--accent-yellow);max-width:60%;text-align:right">${esc(w.warning_message || w.message || "")}</span>
    </div>`).join("");
}

function renderRadar(d) {
  const box = $("radar");
  if (!d) { box.innerHTML = '<span class="muted">暂无</span>'; return; }
  const sum = d.summary || {};
  const list = (d.candidates || []).slice(0, 6);
  const scanned = sum.total || 0;
  const withData = sum.with_data || 0;
  const incomplete = (sum.incomplete != null) ? sum.incomplete : withData;
  const completed = sum.completed || 0;
  // 已 100%（剩余 0 个）的进不了候选榜；标题里如实说明有多少款已全成就。
  let html = `<div class="sec">共 ${incomplete} 款可冲刺`
    + (completed ? ` · ${completed} 款已 100% 全成就` : "")
    + ` / ${withData} 款有数据 / ${scanned} 款扫描</div>`;
  if (!list.length) {
    const empty = completed
      ? "有数据的游戏都已全成就，无需冲刺"
      : (d.hint || "暂无成就数据，先跑一次 stats 或 radar");
    html += `<span class="muted">${esc(empty)}</span>`;
  } else {
    html += list.map((c) => `
      <div class="item">
        <span class="name">${esc(c.name_cn || c.name)} <span class="muted">${c.achieved}/${c.total}</span></span>
        <span class="val">${c.rate}% · 剩 ${c.remaining} 个</span>
      </div>`).join("");
  }
  box.innerHTML = html;
}

function initialOf(row) {
  const s = String(row.name_cn || row.name || "").trim();
  return s ? s.slice(0, 1).toUpperCase() : "?";
}

const AUDIT_REASON_CN = {
  "shared-free-game": "免费游戏被当家庭共享剔除",
  "trace-but-free": "免费游戏被当残留痕迹剔除",
  "dropped-despite-playtime": "玩过却被剔除",
  "owned-but-refunded": "疑似已退款/退库的残留",
  "owned-without-evidence": "判为已拥有但查不到出处",
  "index-entry-not-classified": "在库里却没被判定",
  "free-in-library-not-listed": "在库的免费游戏没有出现在明细里",
};

function renderSharedNotice(own) {
  // 家庭共享过滤的自述信息：服务端（core.ownership）是唯一口径，这里只做展示。
  // A4：shared 只统计「共享且从未玩过、被剔除」的；playedShared 是「共享但玩过、已保留」的。
  const notice = $("sharedNotice"), wrap = $("sharedToggleWrap"), count = $("sharedCount");
  if (!notice) return;
  // 合流库：说明「哪几个账号被当成一个账号」，逐账号小计 + 重叠/剔除情况
  if (own && own.merged) {
    const rows = (own.accounts || []).map((a) =>
      `${esc(a.name || a.id)} ${a.games || 0} 款 / ${Number(a.hours || 0).toFixed(1)}h`);
    const parts = [`${own.account_count || rows.length} 个账号合为一个：${rows.join("；")}`];
    if (own.overlap_games) {
      parts.push(`${own.overlap_games} 款在多个账号都有记录，时长已相加（额外并入 ${(Number(own.overlap_minutes || 0) / 60).toFixed(1)}h）`);
    }
    if (own.played_shared) parts.push(`已保留 ${own.played_shared} 款家庭共享但玩过的游戏`);
    if (own.shared) parts.push(`另有 ${own.shared} 款共享且未玩过，已剔除`);
    if (own.trace) parts.push(`另有 ${own.trace} 款是 Steam 残留痕迹，未计入`);
    notice.textContent = parts.join("；");
    if (wrap && count) { count.textContent = own.shared || 0; wrap.style.display = own.shared ? "" : "none"; }
    return;
  }
  const shared = own && own.shared ? own.shared : 0;
  const playedShared = own && own.played_shared ? own.played_shared : 0;
  const trace = own && own.trace ? own.trace : 0;
  const degraded = !!(own && own.degraded);
  if (degraded) {
    notice.textContent = "没读到授权票，本次不剔除家庭共享游戏。";
  } else {
    const parts = [];
    if (playedShared > 0) parts.push(`已保留 ${playedShared} 款家庭共享但你玩过的游戏`);
    if (shared > 0) parts.push(`另有 ${shared} 款共享且未玩过，已剔除`);
    if (trace > 0) parts.push(`另有 ${trace} 款是 Steam 残留痕迹，未计入`);
    // 归属自动体检（T15）：把工具自己发现的问题直接摆出来，不用用户逐个点名
    const audit = own && own.audit;
    if (audit && !audit.degraded && audit.errors > 0) {
      parts.push(`⚠ 自动体检发现 ${audit.errors} 条显示错标（见下方说明）`);
    } else if (audit && !audit.degraded && audit.warnings > 0) {
      parts.push(`自动体检：${audit.checked} 条已核对，另有 ${audit.warnings} 条存疑`);
    }
    // parts 为空（全自有、无 shared、无 trace）时 join 回空串，提示区整体为空，不留孤立分号
    notice.textContent = parts.join("；");
  }
  if (!wrap || !count) return;
  count.textContent = shared;
  wrap.style.display = shared > 0 && !degraded ? "" : "none";
}

function renderGames(rows) {
  const box = $("games");
  // 服务端（_games_rows）已剔除非游戏条目与「共享且未玩过」条目；这里只做结构兜底
  rows = (Array.isArray(rows) ? rows : []).filter((r) => r && r.appid != null);
  // 计数只算**正常参与统计**的（自己拥有 + 共享但玩过 + 免费）；
  // 灰显的「共享且未玩过」行绝不改变这个数字
  const keptCount = rows.filter((r) => !r.shared).length;
  $("gamesCount").textContent = keptCount + " 款";
  if (!rows.length) {
    box.innerHTML = '<div class="muted" style="padding:10px">无数据</div>';
    return;
  }
  box.innerHTML = rows.slice(0, 400).map((r) => {
    // 统一卡片：竖版铺满；横版在固定框内完整居中（不拉伸、不裁切）；缺图用统一占位
    const initial = esc(initialOf(r));
    const cover = r.cover
      ? `<img class="gc-img ${r.cover_kind === "landscape" ? "landscape" : "portrait"}"
             src="${esc(r.cover)}" alt="${initial}" loading="lazy"
             onerror="this.dataset.failed='1';this.parentNode.classList.add('noimg')">`
      : `<span class="gc-ph">${initial}</span>`;
    // 卡片正文只留一个名字（中文名优先）：不在名字下面再挂一行小字英文名。
    // 英文原名仍在悬浮提示（title）里可查，不占版面。
    const title = `<span class="name-cn">${esc(r.name_cn || r.name)}</span>`;
    // 两种「家庭共享」徽标语义完全不同：
    //   r.shared        = 共享且未玩过 -> 灰显、追加在末尾、不参与统计；
    //   r.played_shared = 共享但玩过   -> 同样的低调徽标，但**正常排序、正常统计**（不灰显）
    // 徽标固定在封面左上角：正文那行只有 24px，塞三块（共享+状态+时长）会换行撑高卡片。
    // 共享标记放封面角标后，正文行永远只有「状态 + 时长」两块，高度恒定。
    const sharedBadge = (r.shared || r.played_shared)
      ? '<span class="gc-chip">家庭共享</span>' : "";
    const statusBadge = r.shared
      ? ""
      : `<span class="badge s${esc(r.status || "")}">${esc(r.status_name || r.status || "")}</span>`;
    return `
    <div class="gcard${r.shared ? " shared" : ""}" title="${esc(r.name_cn && r.name && r.name_cn !== r.name ? `${r.name_cn}（${r.name}）` : (r.name_cn || r.name))}">
      <div class="gc-cover" data-initial="${initial}">${cover}${sharedBadge}</div>
      <div class="gc-body">
        ${title}
        <div class="gc-genres">${genreBadges(r.genres)}</div>
        <div class="gc-meta">
          ${statusBadge}
          <span class="val">${r.playtime_hours} h</span>
        </div>
        <div class="gc-last">最后游玩：${esc(r.last_played || "从未")}</div>
      </div>
    </div>`;
  }).join("");
}

function renderCoverage(c) {
  const box = $("coverage");
  if (!c) { box.textContent = "-"; return; }
  const total = c.total || 0;
  let text = `中文名 ${c.name_cn || 0}/${total} · 封面 ${c.cover || 0}/${total}`;
  if (c.price != null) text += ` · 价格 ${c.price || 0}`;
  box.textContent = text;
  box.title = "中文名 / 封面 / 价格的覆盖率；添加账号后会自动补齐，缺的可点「抓取中文名与封面」";
}

async function refresh() {
  setStatus("加载中…");
  try {
    const d = await getJSON("/api/summary?" + q());
    renderOverview(d);
    renderChallenge(d.challenge);
    renderPersona(d.persona);
    renderValue(d.value);
    renderSeries(d.series);
    renderWishlist(d.wishlist);
    renderRadar(d.radar);
    renderSharedNotice(d.ownership);
    // 勾上「显示家庭共享」时，把灰显行追加在明细末尾；上面的统计数字一格都不动
    const showShared = $("showShared");
    const own = d.ownership || {};
    const sharedRows = (showShared && showShared.checked && Array.isArray(own.shared_games))
      ? own.shared_games : [];
    renderGames((Array.isArray(d.games) ? d.games : []).concat(sharedRows));
    renderCoverage(d.coverage);
    if (d.errors) toast("部分区块降级：" + Object.keys(d.errors).join("、"), true);
    // 归属自动体检：有确证错标时，把具体条目列出来（工具自己发现的，不是用户点名的）
    const auditInfo = d.ownership && d.ownership.audit;
    if (auditInfo && !auditInfo.degraded && auditInfo.errors > 0) {
      const detail = (auditInfo.issues || [])
        .filter((it) => it.level === "error")
        .slice(0, 5)
        .map((it) => `${it.name || it.appid}（${AUDIT_REASON_CN[it.reason] || it.reason}）`).join("、");
      toast(`归属体检发现 ${auditInfo.errors} 条错标：${detail}`, true);
    }
    setStatus(`${d.account_id} · ${new Date().toLocaleTimeString()}`);
    try {   // 推荐卡片用独立接口，必须跟着一起刷新（用户第 6 项）
      const rec = await getJSON("/api/recommend?" + q({
        time: $("time").value, mood: $("moodSel").value, genre: $("genre").value,
      }));
      renderRec(rec);
    } catch (e) {
      $("rec").innerHTML = '<span class="muted">推荐加载失败：' + esc(e.message) + '</span>';
    }
  } catch (e) {
    setStatus("加载失败");
    toast("加载失败：" + e.message, true);
  }
  try {
    const st = await getJSON("/api/state");
    setWritebackUI(st.writeback);
  } catch (e) { /* 忽略 */ }
}

/* ---------------- 动作 ---------------- */

function log(text) {
  $("log").textContent = text || "（无输出）";
}

function setWritebackUI(on) {
  const btn = $("btnWriteback");
  btn.textContent = "分类写回 Steam：" + (on ? "已开启" : "关闭");
  btn.className = "btn" + (on ? " on" : "");
  $("writebackHint").textContent = on
    ? "已开启：run 时把状态标签写进 Steam 分类，再点一次可关闭"
    : "写回会改 Steam 分类；Steam 运行时会被拦截";
}

async function doRun() {
  const dry = $("dryRun").checked;
  if (!dry && !confirm("将真实执行：写快照 + 导出分类（写回开启还会改 Steam 分类）。继续？")) return;
  const btn = $("btnRun");
  btn.disabled = true; btn.textContent = "执行中…";
  try {
    const d = await postJSON("/api/run", { account_id: state.account, dry_run: dry, with_metadata: state.withMeta });
    log(d.output || "");
    toast(d.ok ? (dry ? "预览完成（未落库）" : "执行完成") : "执行结束但有告警", !d.ok);
    if (!dry) {
      await refresh();
      refreshPagesInBackground("分类扫描已完成");   // 数据变了：六块自动重画
    }
  } catch (e) {
    toast("执行失败：" + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "执行分类扫描 run";
  }
}

async function doWriteback() {
  if (isMergedAccount()) {
    toast("合流库不能写回 Steam 分类，请切回单个账号", true);
    return;
  }
  if (!confirm("切换 Steam 分类写回状态？")) return;
  const btn = $("btnWriteback");
  btn.disabled = true;
  try {
    const d = await postJSON("/api/writeback", { account_id: state.account });
    setWritebackUI(d.writeback);
    log(d.output || d.message || "");
    toast(d.message || "已切换", !d.ok);
  } catch (e) {
    toast("写回失败：" + e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function doBackfill() {
  if (isMergedAccount()) {
    toast("快照按账号保存，合流库不能回填，请切回单个账号", true);
    return;
  }
  try {
    const d = await getJSON("/api/backfill?" + q({ dry_run: 1 }));
    const s = d.stats || {};
    log(`[预览] 策略：${s.strategy || ""}\n可回填日期数：${s.dates != null ? s.dates : "-"}\n说明：${d.note || ""}`);
    if (confirm("真正执行回填？（只补缺，不覆盖今天）")) {
      const r = await postJSON("/api/backfill", { account_id: state.account, dry_run: false });
      log(JSON.stringify(r.stats || r, null, 2));
      toast("回填完成");
      await refresh();   // 回填会改历史数据，概览/族谱/推荐一并刷新
      refreshPagesInBackground("快照回填已完成");
    }
  } catch (e) {
    toast("回填失败：" + e.message, true);
  }
}

async function doConfig() {
  try {
    const d = await getJSON("/api/config");
    $("configCard").classList.remove("hidden");
    $("configPath").textContent = d.source + " · " + d.path;
    $("configOut").textContent = JSON.stringify(d.file || d.merged || {}, null, 2);
  } catch (e) {
    toast("读取配置失败：" + e.message, true);
  }
}

async function autoRecommend() {
  /* 首屏静默预加载一次默认推荐（时间=不限 / 心情=随便 / 类型=不限），失败不报错 */
  try {
    const d = await getJSON("/api/recommend?" + q({
      time: $("time").value, mood: $("moodSel").value, genre: $("genre").value,
    }));
    renderRec(d);
  } catch (e) {
    const box = $("rec");
    if (box) box.innerHTML = '<span class="muted">暂无候选（试试放开类型筛选）</span>';
  }
}

async function doRec() {
  try {
    const d = await getJSON("/api/recommend?" + q({
      time: $("time").value, mood: $("moodSel").value, genre: $("genre").value,
    }));
    renderRec(d);
  } catch (e) {
    toast("推荐失败：" + e.message, true);
  }
}

async function doLocalize() {
  const btn = $("btnLocalize");
  const scope = $("localizeScope").value;
  const limit = parseInt($("localizeLimit").value, 10) || 20;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "抓取中…（可能几十秒）";
  setStatus("抓取中文名与封面…");
  try {
    const d = await postJSON("/api/localize", {
      account_id: state.account, scope, limit, with_metadata: state.withMeta,
    });
    toast(d.message || "抓取完成");
    log(JSON.stringify({ names: d.names, covers: d.covers, coverage: d.coverage }, null, 2));
    await refresh();
    await doRec();
    refreshPagesInBackground("中文名与封面已更新");
  } catch (e) {
    toast("抓取失败：" + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    setStatus("就绪");
  }
}

/* 遮罩右侧留给滚动条的宽度：必须与 style.css 里 .frame-hit 的 `right` 保持一致 */
const SCROLLBAR_GUTTER = 17;

/* 让块内滚动**立刻**生效。可视化页的 <html> 上有 `scroll-behavior: smooth`，
   若直接给 scrollTop 赋值会走平滑动画：读回的值滞后于动画目标，连续滚轮时
   在底部会被误判成「没滚动」，于是把滚轮吃掉、外层跟着卡住（滚轮陷阱）。
   这里用 behavior:"instant" 覆盖 CSS，保证 1:1 跟手，判定也准确。 */
function scrollFrameTo(win, top) {
  const doc = win.document;
  const el = doc.scrollingElement || doc.documentElement;
  const max = Math.max(0, el.scrollHeight - el.clientHeight);
  const next = Math.max(0, Math.min(max, top));
  try { el.scrollTo({ top: next, behavior: "instant" }); } catch (e) { el.scrollTop = next; }
  return el;
}

/* 遮罩接管点击后，滚轮事件会落在遮罩上（同源 iframe 收不到）。这里把滚轮
   手动转发给块内可视化页，做到「鼠标悬停在块上滚轮 = 滚块内内容」。
   只转发纵向；块内已到边界时**放行**，让外层仪表盘接着滚（不产生滚轮陷阱）。 */
function forwardWheelToFrame(frame) {
  const hit = frame.querySelector(".frame-hit");
  const iframe = frame.querySelector("iframe");
  if (!hit || !iframe) return;
  hit.addEventListener("wheel", (ev) => {
    if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) return;   // 横向为主：不拦
    let win = null;
    try { win = iframe.contentWindow; } catch (e) { win = null; }   // 同源才有；跨源时退回默认行为
    if (!win) return;
    const el = win.document.scrollingElement || win.document.documentElement;
    const max = Math.max(0, el.scrollHeight - el.clientHeight);
    const atTop = el.scrollTop <= 0;
    const atBottom = el.scrollTop >= max - 1;
    if ((ev.deltaY < 0 && atTop) || (ev.deltaY > 0 && atBottom)) return;   // 已在边界：交给外层
    const before = el.scrollTop;
    scrollFrameTo(win, before + ev.deltaY);
    if (Math.round(el.scrollTop) !== Math.round(before)) ev.preventDefault();   // 块内确实滚了：别再让外层跟着滚
  }, { passive: false });
}
function renderFrames(d) {
  /* 六个可视化块：整块可点、透明遮罩接点击、失败/未生成都不接线 */
  $("pagesInfo").textContent = `产物目录：${d.output_dir}`;
  const box = $("frames");
  box.innerHTML = "";
  (d.pages || []).forEach((p) => {
    const wrap = document.createElement("div");
    wrap.className = "frame";
    if (!p.ok) {
      // 「尚未生成」（还没画过）和「生成失败」（画过但报错）分开说，别让用户以为坏了
      const tag = p.missing ? "尚未生成" : "生成失败";
      wrap.innerHTML = `<header><span>${esc(p.label)}</span><span class="pill">${tag}</span></header>
        <div style="padding:12px" class="muted">${esc(p.error || "点「生成 / 刷新全部页面」即可生成")}</div>`;
      box.appendChild(wrap);
      return;
    }
    const href = `${p.url}?${q()}`;
    const live = `${href}&_t=${Date.now()}`;     // 防缓存：重画后 iframe 一定是新内容
    wrap.classList.add("clickable");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "link");
    wrap.title = `点击整块在新标签打开「${p.label}」`;
    // iframe 会吞掉点击：整块盖一层透明链接遮罩，点哪都能开
    wrap.innerHTML = `<header><span>${esc(p.label)}</span>
        <span class="frame-meta"><span class="frame-hint">点击打开 ↗</span></span></header>
      <div class="frame-body">
        <iframe src="${esc(live)}" loading="lazy" tabindex="-1"></iframe>
        <a class="frame-hit" href="${esc(live)}" target="_blank" rel="noopener"
           aria-label="在新标签打开「${esc(p.label)}」"></a>
      </div>`;
    // 标题栏等非链接区域也整块可点（键盘回车同样打开）
    const open = () => window.open(href, "_blank", "noopener");
    wrap.addEventListener("click", (ev) => {
      if (ev.target.closest("a")) return;                 // 遮罩自己会开
      const body = wrap.querySelector(".frame-body");
      if (body) {
        // 删掉遮罩右侧 17px 的「滚动条缝」：点这里是在操作系统滚动条（被改成手动滚动），
        // 不算「点整块」，不能开新标签。
        const r = body.getBoundingClientRect();
        if (ev.clientX >= r.right - SCROLLBAR_GUTTER) return;
      }
      open();
    });
    // 键盘：遮罩获得焦点时，↑/↓/PageUp/PageDown/Home/End 滚块内内容，Enter/空格才打开
    wrap.addEventListener("keydown", (ev) => {
      const iframe = wrap.querySelector("iframe");
      let win = null;
      try { win = iframe && iframe.contentWindow; } catch (e) { win = null; }
      if (win) {
        const el = win.document.scrollingElement || win.document.documentElement;
        const page = Math.max(40, el.clientHeight - 40);
        const step = {
          ArrowDown: 40, ArrowUp: -40,
          PageDown: page, PageUp: -page,
          Home: -el.scrollTop, End: el.scrollHeight,
        }[ev.key];
        if (step !== undefined) {
          const before = el.scrollTop;
          scrollFrameTo(win, before + step);
          if (el.scrollTop !== before) { ev.preventDefault(); return; }
        }
      }
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
    });
    forwardWheelToFrame(wrap);   // 滚轮：遮罩收到后转发给块内可视化页
    box.appendChild(wrap);
  });
}

let _pagesBusy = false;
let _pagesSeq = 0;   // 渲染序号：晚到的旧响应（如切换账号时的 prefill）不得覆盖更新的渲染
let _pagesPending = null;   // 重画期间又来了一次请求（如刚 run 完就换账号）：忙完补画，别把新数据丢掉

function setFramesStatus(text, cls) {
  /* 六块标题栏上的状态小标（「更新中…」/「更新失败」），空字符串=清掉 */
  document.querySelectorAll("#frames .frame").forEach((f) => {
    const head = f.querySelector("header");
    if (!head) return;
    let tag = head.querySelector(".frame-status");
    if (!text) { if (tag) tag.remove(); return; }
    if (!tag) {
      tag = document.createElement("span");
      const meta = head.querySelector(".frame-meta");
      (meta || head).appendChild(tag);
    }
    tag.className = "pill frame-status" + (cls ? " " + cls : "");
    tag.textContent = text;
  });
}

async function refreshPagesInBackground(reason) {
  /* run / 回填 / 抓取 / 换账号之后数据已经变了：后台把六张页面重画一遍再换上去，
     这样用户看到的数字和块里内容一致，不用自己去点「生成 / 刷新全部页面」。
     注意：只是**不阻塞**用户——先打「更新中…」，画完直接换 iframe。 */
  if (_pagesBusy) { _pagesPending = reason || _pagesPending; return false; }   // 正在重画：记下来，忙完补画
  _pagesBusy = true;
  const seq = ++_pagesSeq;                      // 作废所有在飞的旧请求（如切换账号时的 prefill）
  setFramesStatus("更新中…");
  try {
    const d = await getJSON("/api/pages?" + q());
    if (seq !== _pagesSeq) return false;        // 已被更新的渲染取代：别覆盖它
    renderFrames(d);                            // 重新铺块（iframe 是新的，绕开缓存）
    const ok = (d.pages || []).filter((p) => p.ok).length;
    toast(`${reason || "数据已更新"} · ${ok} 张可视化页面已自动重画`);
    return true;
  } catch (e) {
    setFramesStatus("更新失败");
    toast("可视化页面更新失败：" + e.message + "（可点「生成 / 刷新全部页面」重试）", true);
    return false;
  } finally {
    _pagesBusy = false;
    if (_pagesPending) {                        // 重画期间又来了请求：补画一次，保证画面跟数据一致
      const next = _pagesPending;
      _pagesPending = null;
      refreshPagesInBackground(next);
    }
  }
}

async function prefillPages(autoGenerate) {
  /* 首屏自动铺满六块：只列**已经有产物**的页面，毫秒级返回，不重新渲染（重画要 ~10 秒）。
     autoGenerate=true（账号就绪后的那次）：当前账号一张产物都没有就后台补画，
     用户不必自己去点「生成 / 刷新全部页面」。 */
  if (_pagesBusy) return;                 // 后台正在重画：别用旧/空结果盖掉它（否则会闪成「尚未生成」）
  const seq = ++_pagesSeq;
  try {
    const d = await getJSON("/api/pages?only_existing=1&" + q());
    if (seq !== _pagesSeq) return;        // 期间又发起了新的渲染：丢弃这份过期结果
    renderFrames(d);
    if (autoGenerate && !(d.pages || []).some((p) => p.ok)) {
      refreshPagesInBackground("首次生成");   // 当前账号一张都没有：后台补画（不阻塞用户）
    }
  } catch (e) {
    // 首屏预填失败不打扰用户：点「生成 / 刷新全部页面」仍然可用
    if (seq === _pagesSeq) $("pagesInfo").textContent = "尚未生成";
  }
}

/* ---------------- 日志 ---------------- */

async function loadLogPath() {
  /* 只取路径显示在底部小字里（tail=1 很便宜）；读不到就保持原样，不打扰用户 */
  try {
    const d = await getJSON("/api/log?tail=1");
    if ($("logPath")) $("logPath").textContent = d.path || "（未找到日志文件）";
  } catch (e) {
    if ($("logPath")) $("logPath").textContent = "（日志不可读）";
  }
}

async function doLog() {
  const btn = $("btnLog");
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "读取中…"; }
  try {
    const d = await getJSON("/api/log?tail=200");
    const lines = d.lines || [];
    $("logPath").textContent = d.path || "-";
    $("logFile").textContent = d.path ? `文件：${d.path}` : "";
    $("logTailTag").textContent = `最近 ${d.tail || 200} 行`;
    $("logHint").textContent = d.error || d.message
      || `${lines.length} 行 · 共 ${(d.total_bytes || 0).toLocaleString()} 字节`;
    $("logBody").textContent = lines.length
      ? lines.join("\n")
      : (d.error || d.message || "（日志为空）");
    $("logModal").classList.remove("hidden");
    $("logBody").scrollTop = $("logBody").scrollHeight;   // 默认停在最新一行
    if (d.error) toast(d.error, true);
  } catch (e) {
    toast("读取日志失败：" + e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || "查看最近日志"; }
  }
}

function closeLogModal() { $("logModal").classList.add("hidden"); }

async function doPages() {
  const btn = $("btnPages");
  btn.disabled = true; btn.textContent = "生成中…（首次约 10 秒）";
  const seq = ++_pagesSeq;
  try {
    const d = await getJSON("/api/pages?" + q());
    if (seq !== _pagesSeq) return;
    renderFrames(d);
    toast("页面已生成，共 " + (d.pages || []).filter((p) => p.ok).length + " 张");
  } catch (e) {
    toast("生成页面失败：" + e.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "生成 / 刷新全部页面";
  }
}

/* ---------------- T6：添加账号后自动补齐数据（后台跑 + 进度可见 + 完成后自动刷新） ----------------
   服务端 /api/accounts/add 成功后会把该账号排进后台补齐队列（中文名 / 封面 / 价格 / 系列目录），
   这里只负责**轮询进度**（GET /api/job）并在完成后自动刷新全部数据；页面永远不卡。 */

let _jobTimer = null;
let _jobSeen = null;   // null = 还没看过第一帧（进页面前的历史任务不弹提示）
let _jobDataReady = false;   // 中文名/价格/封面已跑完（只剩系列目录在补）时先自动刷新一次

function renderJob(d) {
  const card = $("jobCard");
  if (!card) return;
  const total = d.total || 0, done = d.done || 0;
  const fill = $("jobBarFill");
  if (fill) {
    fill.style.width = (total > 0)
      ? Math.max(3, Math.min(100, Math.round(done * 100 / total))) + "%"
      : "100%";    // 总数未知（如系列目录）：给一条满条，别卡在 0 看着像死了
  }
  if ($("jobTitle")) {
    $("jobTitle").textContent = d.stage_label ? ("正在为你补齐数据：" + d.stage_label) : "正在为你补齐数据…";
  }
  if ($("jobText")) $("jobText").textContent = d.message || (total ? (done + "/" + total) : "进行中…");
  if ($("jobHint")) {
    $("jobHint").textContent = (d.pending || 0) > 0
      ? `后面还有 ${d.pending} 个账号排队（同时只跑一个）`
      : "首次添加账号会在后台自动抓取中文名 / 封面 / 价格 / 系列，可以先逛别的";
  }
  card.classList.remove("hidden");
}

async function pollJob() {
  let d = null;
  try {
    d = await getJSON("/api/job");
  } catch (e) {
    stopJobPolling();
    return;
  }
  const active = !!(d.running || (d.pending || 0) > 0);
  // 数据阶段（中文名 / 价格 / 封面）已经跑完、只剩系列目录在补时，先自动刷新一次：
  // 用户不用等系列目录（大陆时通时断，会分几次慢慢补）就能看到抓好的数据。
  if (active && d.stage === "series" && !_jobDataReady) {
    _jobDataReady = true;
    try { await refresh(); } catch (e) { /* refresh 自己会提示 */ }
    refreshPagesInBackground("数据已抓齐（系列目录后台继续补）");
  }
  const stamp = (d.last && d.last.finished_at) ? String(d.last.finished_at) : "";
  if (_jobSeen === null) {
    _jobSeen = stamp;            // 首帧：只记录「进页面前已经发生的」，不弹历史提示
  } else if (stamp && stamp !== _jobSeen) {
    _jobSeen = stamp;
    if (d.last.ok) await afterJobDone(d.last);
    else toast("自动补齐没完成：" + (d.last.error || d.last.message || "未知原因")
      + "（可点「抓取中文名与封面」重试）", true);
  }
  if (active) { renderJob(d); ensureJobPolling(); }
  else stopJobPolling();
}

function ensureJobPolling() {
  if (_jobTimer) return;
  _jobTimer = setInterval(pollJob, 2000);     // 1~2 秒一次，别太密
}

function startJobPolling() {
  _jobDataReady = false;                      // 新一轮任务：允许中途再先刷一次数据
  pollJob();                                  // 立刻看一眼：添加账号后马上把进度条打出来
  ensureJobPolling();
}

function stopJobPolling() {
  if (_jobTimer) { clearInterval(_jobTimer); _jobTimer = null; }
  const card = $("jobCard");
  if (card) card.classList.add("hidden");
}

async function afterJobDone(d) {
  const c = d.coverage || {};
  const bits = [];
  if (c.total) {
    bits.push(`中文名 ${c.name_cn || 0}/${c.total}`);
    bits.push(`封面 ${c.cover || 0}/${c.total}`);
    bits.push(`价格 ${c.price || 0}`);
  }
  const head = bits.length ? "数据已补齐：" + bits.join(" · ") : "数据已补齐";
  if (d.partial) {
    // 黄色/中性：数据基本补齐了，只是有个别项（通常是价格）稍后会自动重试——别画成红色。
    const retry = (d.retry_needed || []).join("/");
    const miss = c.missing_price || 0;
    const extra = retry.indexOf("price") >= 0
      ? `价格${miss ? "有 " + miss + " 款" : ""}没取到，稍后自动重试`
      : "个别项稍后会自动重试";
    toast(`${head}；${extra}`, "warn");
  } else {
    toast(bits.length ? "数据已自动补齐：" + bits.join(" · ") : "数据已自动补齐");
  }
  try { await refresh(); } catch (e) { /* refresh 自己会提示 */ }
  refreshPagesInBackground("数据已自动补齐");   // 数据变了：六张页面自动重画
}

/* ---------------- 绑定 ---------------- */

function bind() {
  const showShared = $("showShared");
  if (showShared) showShared.addEventListener("change", () => refresh());
  $("account").addEventListener("change", (e) => {
    state.account = e.target.value;
    refresh();               // 保留旧画面 + 「更新中…」，后台重画完成后整批替换
    refreshPagesInBackground("已切换账号");   // 再后台按新账号重画六张页面
  });
  $("btnAddAccount").addEventListener("click", openAddModal);
  $("btnSetupAdd").addEventListener("click", openAddModal);
  if ($("btnAddMore")) $("btnAddMore").addEventListener("click", openAddModal);
  if ($("btnAddClose")) $("btnAddClose").addEventListener("click", closeAddModal);
  if ($("btnAddConfirm")) $("btnAddConfirm").addEventListener("click", confirmAddAccounts);
  if ($("addModal")) $("addModal").addEventListener("click", (e) => {
    if (e.target === $("addModal")) closeAddModal();
  });
  $("btnManageAccounts").addEventListener("click", () => {
    $("accountCard").classList.toggle("hidden");
  });
  $("withMeta").addEventListener("change", (e) => { state.withMeta = e.target.checked; });
  $("btnRefresh").addEventListener("click", refresh);
  $("btnRec").addEventListener("click", doRec);
  $("btnRun").addEventListener("click", doRun);
  $("btnWriteback").addEventListener("click", doWriteback);
  $("btnBackfill").addEventListener("click", doBackfill);
  $("btnConfig").addEventListener("click", doConfig);
  $("btnPages").addEventListener("click", doPages);
  $("btnLocalize").addEventListener("click", doLocalize);
  if ($("btnLog")) $("btnLog").addEventListener("click", doLog);
  if ($("btnLogReload")) $("btnLogReload").addEventListener("click", doLog);
  if ($("btnLogClose")) $("btnLogClose").addEventListener("click", closeLogModal);
  if ($("logModal")) $("logModal").addEventListener("click", (e) => {
    if (e.target === $("logModal")) closeLogModal();
  });
}

(async function init() {
  bind();
  prefillPages();          // 不 await：先把六块铺出来，别挡着后面的数据加载
  loadLogPath();           // 底部小字：日志文件在哪（不读内容）
  try {
    await loadAccounts();
    if (state.account) {
      prefillPages(true);  // 账号就绪后再刷一次（毫秒级）：链接带上 account_id；没有产物就后台补画
      await refresh();
      await autoRecommend();
      startJobPolling();  // T6：若后台正在（或刚刚）补齐数据，接着显示进度 / 弹完成提示
    } else {
      clearDashboard();
    }
  } catch (e) {
    toast("初始化失败：" + e.message, true);
    setStatus("初始化失败");
  }
})();
