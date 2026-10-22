"use strict";
/* TradingAgents web UI. No build step, no dependencies: one file, plain DOM. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem("ta." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("ta." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opt);
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), err ? 7000 : 3500);
}
const say = (el, msg, kind = "") => { el.textContent = msg; el.className = "msg " + kind; };

/* ---------- markdown (escapes everything first, so model output can never inject markup) ---------- */
function inline(s) {
  s = esc(s);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^\w*])\*([^*\s][^*]*)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}
function md(src) {
  const lines = String(src || "").replace(/\r/g, "").split("\n");
  const out = []; let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`); continue;
    }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { const n = Math.min(m[1].length + 1, 4); out.push(`<h${n}>${inline(m[2])}</h${n}>`); i++; continue; }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^\s*>/.test(line)) {
      const buf = []; while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${md(buf.join("\n"))}</blockquote>`); continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const cells = l => l.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = cells(line); i += 2; const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(`<table><thead><tr>${head.map(c => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`); continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line); const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) items.push(inline(lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, "")));
      out.push(`<${ordered ? "ol" : "ul"}>${items.map(t => `<li>${t}</li>`).join("")}</${ordered ? "ol" : "ul"}>`); continue;
    }
    if (!line.trim()) { i++; continue; }
    const buf = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|```|\s*>|\s*([-*+]|\d+[.)])\s+|\s*\|.*\|\s*$)/.test(lines[i])) buf.push(lines[i++]);
    if (!buf.length) { buf.push(lines[i++]); }
    out.push(`<p>${inline(buf.join("\n")).replace(/\n/g, "<br>")}</p>`);
  }
  return out.join("\n");
}

const fmtTime = s => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const fmtTok = n => n >= 1e6 ? (n / 1e6).toFixed(2) + "M" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n || 0);
const ratingBadge = r => r ? `<span class="rating ${esc(r)}">${esc(r)}</span>` : "";
const pct = v => (v == null ? "—" : (v * 100).toFixed(0) + "%");
const signedPct = v => (v >= 0 ? "+" : "") + (v * 100).toFixed(2) + "%";
const when = ts => new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });

/* ---------- navigation ---------- */
const loaders = { history: loadHistory, backtest: loadBacktests, memory: loadMemory, settings: loadSettings };
function show(view) {
  $$(".view").forEach(v => v.classList.toggle("hidden", v.id !== "view-" + view));
  $$("#nav button").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  store.set("view", view);
  if (loaders[view]) loaders[view]().catch(e => toast(e.message, true));
}
$("#nav").addEventListener("click", e => { const b = e.target.closest("button[data-view]"); if (b) show(b.dataset.view); });
document.addEventListener("click", e => { const g = e.target.closest("[data-goto]"); if (g) show(g.dataset.goto); });
$("#theme").onclick = () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; store.set("theme", next);
};
if (store.get("theme")) document.documentElement.dataset.theme = store.get("theme");

/* ---------- new analysis form ---------- */
let META = null;
const form = { analysts: new Set(), depth: 1, available: null, thinking: {} };

async function init() {
  META = await api("/api/meta");
  $("#version").textContent = "v" + META.version;
  const d = { ...META.defaults, ...store.get("form", {}) };
  $("#ticker").value = d.ticker || "";
  $("#date").value = META.defaults.date; $("#date").max = META.defaults.date;
  $("#language").innerHTML = META.languages.map(l => `<option>${esc(l)}</option>`).join("");
  $("#language").value = META.languages.includes(d.language) ? d.language : "English";
  $("#checkpoint").checked = !!d.checkpoint;
  form.depth = [1, 3, 5].includes(d.depth) ? d.depth : 1;
  form.analysts = new Set((d.analysts || []).filter(a => META.analysts.some(x => x.value === a)));
  if (!form.analysts.size) META.analysts.forEach(a => form.analysts.add(a.value));
  $("#provider").innerHTML = META.providers.map(p => `<option value="${esc(p.key)}">${esc(p.name)}</option>`).join("");
  $("#provider").value = META.providers.some(p => p.key === d.provider) ? d.provider : META.providers[0].key;
  form.thinking = d.thinkingValues || { ...META.defaults.thinking };
  renderAnalysts(); renderDepths();
  onProvider(true, d);
  restorePortfolio();
  $("#bt-end").value = META.defaults.date; $("#bt-end").max = META.defaults.date;
  const s = new Date(); s.setDate(s.getDate() - 56); $("#bt-start").value = s.toISOString().slice(0, 10); $("#bt-start").max = META.defaults.date;
  updateBtCount();
  if (META.busy) { attachJob(META.busy.id, META.busy.kind); show(META.busy.kind === "backtest" ? "backtest" : "live"); }
  else show(store.get("view", "run") === "live" ? "run" : store.get("view", "run"));
}

function renderAnalysts() {
  $("#analysts").innerHTML = META.analysts.map(a => {
    const off = form.available && !form.available.includes(a.value);
    return `<button type="button" class="chip ${form.analysts.has(a.value) && !off ? "on" : ""} ${off ? "off" : ""}" data-a="${a.value}" ${off ? 'title="Not available for this asset"' : ""}>${esc(a.label)}</button>`;
  }).join("");
}
$("#analysts").addEventListener("click", e => {
  const b = e.target.closest("[data-a]"); if (!b || b.classList.contains("off")) return;
  const a = b.dataset.a; form.analysts.has(a) ? form.analysts.delete(a) : form.analysts.add(a);
  renderAnalysts(); saveForm();
});
function renderDepths() {
  $("#depths").innerHTML = META.depths.map(d => `<button type="button" data-d="${d.value}" class="${form.depth === d.value ? "on" : ""}"><b>${esc(d.label)}</b><span>${esc(d.hint)}</span></button>`).join("");
}
$("#depths").addEventListener("click", e => { const b = e.target.closest("[data-d]"); if (b) { form.depth = +b.dataset.d; renderDepths(); saveForm(); } });

let vTimer;
$("#ticker").addEventListener("input", () => { clearTimeout(vTimer); vTimer = setTimeout(validateTicker, 350); saveForm(); });
async function validateTicker() {
  const v = $("#ticker").value.trim(), hint = $("#ticker-hint");
  if (!v) { hint.textContent = ""; $("#ticker").classList.remove("bad"); form.available = null; renderAnalysts(); return null; }
  const r = await api("/api/validate?ticker=" + encodeURIComponent(v));
  $("#ticker").classList.toggle("bad", !r.ok);
  if (!r.ok) { hint.textContent = r.error; hint.style.color = "var(--danger)"; form.available = null; renderAnalysts(); return null; }
  hint.style.color = ""; hint.textContent = r.ticker !== v.toUpperCase() ? `Will use ${r.ticker} · ${r.asset_type}` : r.asset_type === "crypto" ? "Crypto · fundamentals analyst unavailable" : "";
  form.available = r.analysts; renderAnalysts(); return r;
}

const provider = () => META.providers.find(p => p.key === $("#provider").value);
function onProvider(initial = false, d = {}) {
  const p = provider();
  const fromPrefs = initial && d.provider === p.key;
  $("#url-row").classList.toggle("hidden", !(p.needs_url || p.key === "ollama"));
  $("#backend_url").value = fromPrefs && d.backend_url ? d.backend_url : (p.url || "");
  const pick = (mode, saved) => {
    const list = p.models[mode];
    $(`#${mode}_model`).value = fromPrefs && saved ? saved : (list[0] ? list[0].value : "");
    $(`#${mode}-list`).innerHTML = list.map(m => `<option value="${esc(m.value)}">${esc(m.label)}</option>`).join("");
  };
  pick("quick", d.quick_model); pick("deep", d.deep_model);
  if (!p.models.quick.length) $("#quick_model").placeholder = "Model ID or deployment name";
  if (!p.models.deep.length) $("#deep_model").placeholder = "Model ID or deployment name";
  renderModelChips();
  const t = p.thinking;
  $("#thinking-row").classList.toggle("hidden", !t);
  if (t) {
    $("#thinking-label").textContent = t.label;
    $("#thinking").innerHTML = `<option value="">Provider default</option>` + t.options.map(o => `<option value="${esc(o.value)}">${esc(o.label)}</option>`).join("");
    const kind = p.key === "google" ? "google" : p.key === "anthropic" ? "anthropic" : "openai";
    $("#thinking").value = form.thinking[kind] || "";
    $("#thinking").dataset.kind = kind;
  }
  renderProviderStatus();
  if (p.live_models) loadLive(p);
  saveForm();
}
async function loadLive(p) {
  try {
    const r = await api("/api/models?provider=" + p.key);
    if (provider().key !== p.key || !r.models.length) return;
    p.models.quick = p.models.deep = r.models;
    for (const mode of ["quick", "deep"]) {
      $(`#${mode}-list`).innerHTML = r.models.map(m => `<option value="${esc(m.value)}">${esc(m.label)}</option>`).join("");
      if (!$(`#${mode}_model`).value) $(`#${mode}_model`).value = r.models[0].value;
    }
    renderModelChips();
  } catch { /* typed model IDs still work */ }
}
function renderModelChips() {
  const p = provider(), el = $("#model-pick");
  const list = p.models.deep.slice(0, 6);
  el.innerHTML = list.length ? `<span class="hint" style="align-self:center">Suggestions:</span>` + list.map(m => `<button type="button" class="chip" data-m="${esc(m.value)}" title="${esc(m.label)}">${esc(m.value)}</button>`).join("") : "";
}
$("#model-pick").addEventListener("click", e => {
  const b = e.target.closest("[data-m]"); if (!b) return;
  const p = provider(), target = p.models.quick.some(m => m.value === b.dataset.m) && !p.models.deep.some(m => m.value === b.dataset.m) ? "quick" : "deep";
  $(`#${target}_model`).value = b.dataset.m; saveForm();
});
function renderProviderStatus() {
  const p = provider(), el = $("#provider-status");
  if (p.signed_in_plan) {
    const who = META.chatgpt.signed_in_as;
    el.innerHTML = who ? `<span class="ok">● Signed in as ${esc(who)}</span>` : `<span class="no">● Not signed in. Run <code>tradingagents login chatgpt</code> in a terminal.</span>`;
  } else if (!p.key_env) {
    el.innerHTML = `<span class="ok">● No API key needed</span>`;
  } else if (p.key_set) {
    el.innerHTML = `<span class="ok">● ${esc(p.key_env)} is set</span>`;
  } else if (p.key_optional) {
    el.innerHTML = `<span class="muted">${esc(p.key_env)} optional</span>`;
  } else {
    el.innerHTML = `<span class="no">● ${esc(p.key_env)} is not set.</span> <a data-goto="settings">Add it in Settings</a>`;
  }
}
$("#provider").onchange = () => onProvider(false);
["quick_model", "deep_model", "backend_url", "language", "checkpoint", "thinking", "date"].forEach(id => $("#" + id).addEventListener("change", () => {
  if (id === "thinking") form.thinking[$("#thinking").dataset.kind] = $("#thinking").value || null;
  saveForm();
}));

function saveForm() {
  if (!META) return;
  store.set("form", {
    ticker: $("#ticker").value, analysts: [...form.analysts], depth: form.depth, language: $("#language").value,
    provider: $("#provider").value, backend_url: $("#backend_url").value, quick_model: $("#quick_model").value,
    deep_model: $("#deep_model").value, checkpoint: $("#checkpoint").checked, thinkingValues: form.thinking,
  });
}

/* portfolio */
function addPosition(p = {}) {
  const tr = document.createElement("tr");
  tr.innerHTML = `<td><input class="pf-t" value="${esc(p.ticker || "")}" placeholder="AAPL"></td><td><input class="pf-q" type="number" step="any" value="${esc(p.quantity ?? "")}" placeholder="100"></td><td><input class="pf-p" type="number" step="any" value="${esc(p.average_price ?? "")}" placeholder="optional"></td><td><button type="button" class="ghost pf-x" title="Remove">✕</button></td>`;
  $("#pf-table tbody").appendChild(tr);
}
$("#pf-add").onclick = () => addPosition();
$("#pf-table").addEventListener("click", e => { if (e.target.closest(".pf-x")) { e.target.closest("tr").remove(); savePortfolio(); } });
$("#portfolio-card").addEventListener("input", savePortfolio);
function readPortfolio() {
  return {
    cash: $("#pf-cash").value, currency: $("#pf-currency").value,
    positions: $$("#pf-table tbody tr").map(tr => ({ ticker: $(".pf-t", tr).value, quantity: $(".pf-q", tr).value, average_price: $(".pf-p", tr).value })),
  };
}
function savePortfolio() { store.set("portfolio", readPortfolio()); }
function restorePortfolio() {
  const p = store.get("portfolio"); if (!p) return;
  $("#pf-cash").value = p.cash ?? ""; $("#pf-currency").value = p.currency ?? "";
  (p.positions || []).forEach(addPosition);
  if ((p.positions || []).length || p.cash) $("#portfolio-card").open = true;
}

function runPayload() {
  return {
    ticker: $("#ticker").value, date: $("#date").value, analysts: [...form.analysts].filter(a => !form.available || form.available.includes(a)),
    depth: form.depth, language: $("#language").value, provider: $("#provider").value, backend_url: $("#backend_url").value,
    quick_model: $("#quick_model").value, deep_model: $("#deep_model").value, checkpoint: $("#checkpoint").checked,
    thinking: $("#thinking-row").classList.contains("hidden") ? null : ($("#thinking").value || null),
    portfolio: readPortfolio(),
  };
}

$("#start").onclick = async () => {
  const msg = $("#start-msg"); say(msg, "");
  if (!$("#ticker").value.trim()) { say(msg, "Enter a ticker to analyze.", "err"); $("#ticker").focus(); return; }
  $("#start").disabled = true;
  try {
    const v = await validateTicker(); if (!v) throw new Error("Fix the ticker first.");
    const { id } = await api("/api/runs", runPayload());
    saveForm(); attachJob(id, "analysis"); show("live");
    api("/api/meta").then(m => { META.providers = m.providers; });
  } catch (e) { say(msg, e.message, "err"); }
  finally { $("#start").disabled = false; }
};
$("#ticker").addEventListener("keydown", e => { if (e.key === "Enter") $("#start").click(); });

/* ---------- live run ---------- */
const AGENT_ORDER = ["market_report", "sentiment_report", "news_report", "fundamentals_report", "bull", "bear", "research_manager", "trader", "aggressive", "conservative", "neutral", "portfolio_manager"];
let live = null, es = null, followTab = true, rafPending = false;

function newLive(id, kind) {
  return { id, kind, title: "", status: "running", agents: null, reports: {}, order: [], fresh: new Set(), tab: null, stats: null, result: null, error: null, startedAt: Date.now() };
}
function attachJob(id, kind) {
  if (es) es.close();
  live = newLive(id, kind); followTab = true;
  if (kind === "backtest") { btReset(); } else { $("#live-empty").classList.add("hidden"); $("#live-body").classList.remove("hidden"); resetLiveDom(); }
  $("#live-dot").classList.remove("hidden");
  es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = m => { handleEvent(JSON.parse(m.data)); };
  es.onerror = () => { if (live && live.status !== "running") es.close(); };
}
function resetLiveDom() {
  $("#feed").innerHTML = ""; $("#pipeline").innerHTML = ""; $("#report-tabs").innerHTML = "";
  $("#report-view").innerHTML = `<p class="muted">Reports appear here as each agent files them.</p>`;
  $("#error-box").classList.add("hidden"); $("#result-banner").classList.add("hidden"); $("#final-wrap").classList.add("hidden"); $("#final").innerHTML = "";
  $("#live-badge").className = "badge"; $("#live-badge").textContent = "running"; $("#cancel").classList.remove("hidden");
  $(".progress").classList.add("indet"); $("#progress-bar").style.width = "30%";
}

function handleEvent(ev) {
  const L = live; if (!L) return;
  if (L.kind === "backtest") return btEvent(ev);
  switch (ev.type) {
    case "started": L.title = ev.title; $("#live-title").textContent = ev.title; break;
    case "agents": L.agents = ev; break;
    case "report": if (!(ev.key in L.reports)) L.order.push(ev.key); L.reports[ev.key] = ev; if (L.tab !== ev.key) L.fresh.add(ev.key); if (followTab) { L.tab = ev.key; L.fresh.delete(ev.key); } break;
    case "message": addFeed("msg", ev.clock, ev.kind, ev.content); break;
    case "tool": addFeed("tool", ev.clock, "Tool", `${ev.name}  ${ev.args}`); break;
    case "log": addFeed("msg", "", "Log", ev.message); break;
    case "stats": L.stats = ev; break;
    case "result": L.result = ev; break;
    case "error": L.error = ev; break;
    case "finished": L.status = ev.status; if (es) es.close(); $("#live-dot").classList.add("hidden"); break;
  }
  schedule();
}
const schedule = () => { if (!rafPending) { rafPending = true; requestAnimationFrame(() => { rafPending = false; renderLive(); }); } };

function addFeed(kind, clock, label, text) {
  const feed = $("#feed"), atEnd = $("#feed-follow").checked;
  const ln = document.createElement("div"); ln.className = "ln"; ln.dataset.kind = kind;
  if ((kind === "tool" && !$("#feed-tools").checked) || (kind === "msg" && !$("#feed-msgs").checked)) ln.classList.add("hidden");
  ln.innerHTML = `<span class="t">${esc(clock || "")}</span><span class="k ${esc(label)}">${esc(label)}</span><span class="c">${esc(text)}</span>`;
  feed.appendChild(ln);
  while (feed.childElementCount > 600) feed.firstChild.remove();
  if (atEnd) feed.scrollTop = feed.scrollHeight;
}
$("#feed").addEventListener("click", e => { const c = e.target.closest(".c"); if (c) c.classList.toggle("open"); });
[["feed-tools", "tool"], ["feed-msgs", "msg"]].forEach(([id, k]) => $("#" + id).onchange = e => $$(`#feed .ln[data-kind="${k}"]`).forEach(l => l.classList.toggle("hidden", !e.target.checked)));

function renderLive() {
  const L = live; if (!L || L.kind === "backtest") return;
  // header
  const badge = $("#live-badge"); badge.textContent = L.status; badge.className = "badge " + L.status;
  $("#cancel").classList.toggle("hidden", L.status !== "running");
  const s = L.stats;
  if (s) {
    $("#st-time").textContent = fmtTime(s.elapsed); $("#st-agents").textContent = `${s.agents_done}/${s.agents_total}`;
    $("#st-llm").textContent = s.llm_calls; $("#st-tools").textContent = s.tool_calls;
    $("#st-tok").textContent = s.tokens_in || s.tokens_out ? `${fmtTok(s.tokens_in)} / ${fmtTok(s.tokens_out)}` : "—";
    const bar = $("#progress-bar"), prog = $(".progress");
    prog.classList.toggle("indet", L.status === "running" && !s.agents_done);
    if (!prog.classList.contains("indet")) bar.style.width = (s.agents_total ? 100 * s.agents_done / s.agents_total : 0) + "%";
  }
  if (L.status !== "running") { $(".progress").classList.remove("indet"); if (L.status === "completed") $("#progress-bar").style.width = "100%"; }
  // pipeline
  if (L.agents) {
    $("#pipeline").innerHTML = Object.entries(L.agents.teams).map(([team, agents]) =>
      `<div class="team"><h3>${esc(team)}</h3>${agents.map(a => { const st = L.agents.status[a] || "pending"; return `<div class="agent ${st}"><span class="ico"></span><span>${esc(a)}</span></div>`; }).join("")}</div>`).join("");
  }
  // reports
  const keys = AGENT_ORDER.filter(k => k in L.reports);
  $("#report-tabs").innerHTML = keys.map(k => `<button data-k="${k}" class="${L.tab === k ? "active" : ""} ${L.fresh.has(k) ? "fresh" : ""}">${esc(L.reports[k].title)}</button>`).join("");
  if (L.tab && L.reports[L.tab]) {
    const html = md(L.reports[L.tab].content), view = $("#report-view");
    if (view.dataset.k !== L.tab || view.dataset.len !== String(L.reports[L.tab].content.length)) {
      const top = view.dataset.k === L.tab ? view.scrollTop : 0;
      view.innerHTML = html; view.dataset.k = L.tab; view.dataset.len = L.reports[L.tab].content.length; view.scrollTop = top;
    }
  }
  // error
  const eb = $("#error-box");
  if (L.error) {
    eb.classList.remove("hidden");
    eb.innerHTML = `${esc(L.error.message)}<details><summary>Details</summary><pre>${esc(L.error.detail || "")}</pre></details>`;
  }
  // result
  if (L.result && !$("#result-banner").dataset.done) {
    const r = L.result; $("#result-banner").dataset.done = "1";
    const dl = `/api/history/${encodeURIComponent(r.ticker)}/${r.date}/export`;
    $("#result-banner").className = "banner";
    $("#result-banner").innerHTML = `${ratingBadge(r.rating)}<div><b>${esc(r.ticker)}</b> · ${esc(r.date)}<br><span class="muted small">${esc(r.review ? "No rating could be read from the final decision; read the text below and judge it yourself." : r.wall_time)}</span></div><span class="spacer"></span><a href="${dl}?format=html"><button class="ghost">Download HTML</button></a> <a href="${dl}?format=md"><button class="ghost">Markdown</button></a>`;
    $("#final-wrap").classList.remove("hidden"); $("#final").innerHTML = sectionsHtml(r.sections);
    if (r.saved_to) toast("Report saved to " + r.saved_to);
  }
  if (L.status === "cancelled") toast("Run stopped.");
}
$("#report-tabs").addEventListener("click", e => {
  const b = e.target.closest("[data-k]"); if (!b) return;
  live.tab = b.dataset.k; live.fresh.delete(b.dataset.k); followTab = false; renderLive();
});
$("#cancel").onclick = async () => { if (live) { try { await api(`/api/jobs/${live.id}/cancel`, {}); toast("Stopping after the current step…"); } catch (e) { toast(e.message, true); } } };
$("#live-sub").textContent = "";

function sectionsHtml(sections) {
  return sections.map(sec => `<div class="card"><h2>${esc(sec.heading)}</h2>${sec.items.map((it, i) => `<details ${sec.items.length === 1 || i === 0 ? "open" : ""} class="sub"><summary><b>${esc(it.agent)}</b></summary><div class="md">${md(it.text)}</div></details>`).join("")}</div>`).join("");
}

/* ---------- reports (history) ---------- */
let HIST = [];
async function loadHistory() {
  $("#hist-detail").classList.add("hidden"); $("#hist-list").classList.remove("hidden"); $(".toolbar", $("#view-history")).classList.remove("hidden");
  HIST = (await api("/api/history")).runs;
  const ratings = [...new Set(HIST.map(r => r.rating).filter(Boolean))];
  const sel = $("#hist-rating"), cur = sel.value;
  sel.innerHTML = `<option value="">Any rating</option>` + ratings.map(r => `<option>${esc(r)}</option>`).join(""); sel.value = cur;
  renderHistory();
}
function renderHistory() {
  const q = $("#hist-filter").value.trim().toUpperCase(), rt = $("#hist-rating").value;
  const rows = HIST.filter(r => (!q || String(r.ticker).toUpperCase().includes(q)) && (!rt || r.rating === rt));
  $("#hist-list").innerHTML = rows.length ? rows.map(r => `<div class="item" data-t="${esc(r.ticker)}" data-d="${esc(r.date)}"><span class="tk">${esc(r.ticker)}</span><span>${esc(r.date)}</span>${ratingBadge(r.rating)}<span class="meta">${esc([r.provider, r.model].filter(Boolean).join(" · "))}${r.analysts ? " · " + esc(r.analysts.join(", ")) : ""}</span><span class="muted small">${esc(when(r.saved))}</span></div>`).join("")
    : `<div class="empty"><p class="muted">${HIST.length ? "Nothing matches that filter." : "No saved runs yet. Finish an analysis and it appears here."}</p></div>`;
}
$("#hist-filter").oninput = renderHistory; $("#hist-rating").onchange = renderHistory; $("#hist-refresh").onclick = () => loadHistory().catch(e => toast(e.message, true));
$("#hist-list").addEventListener("click", async e => {
  const it = e.target.closest(".item"); if (!it) return;
  try {
    const r = await api(`/api/history/${encodeURIComponent(it.dataset.t)}/${it.dataset.d}`);
    const dl = `/api/history/${encodeURIComponent(r.ticker)}/${r.date}/export`;
    $("#hist-list").classList.add("hidden"); $(".toolbar", $("#view-history")).classList.add("hidden");
    const el = $("#hist-detail"); el.classList.remove("hidden");
    const s = r.settings || {};
    el.innerHTML = `<button class="ghost" id="hist-back">← All reports</button><div class="banner" style="margin-top:14px">${ratingBadge(r.rating)}<div><b>${esc(r.ticker)}</b> · ${esc(r.date)}<br><span class="muted small">${esc([s.llm_provider, s.deep_think_llm && "deep " + s.deep_think_llm, s.quick_think_llm && "quick " + s.quick_think_llm].filter(Boolean).join(" · "))}</span></div><span class="spacer"></span><a href="${dl}?format=html"><button class="ghost">Download HTML</button></a> <a href="${dl}?format=md"><button class="ghost">Markdown</button></a></div>${sectionsHtml(r.sections)}`;
    $("#hist-back").onclick = loadHistory; window.scrollTo(0, 0);
  } catch (err) { toast(err.message, true); }
});

/* ---------- backtest ---------- */
function updateBtCount() {
  const t = $("#bt-tickers").value.split(/[,\s]+/).filter(Boolean).length, a = new Date($("#bt-start").value), b = new Date($("#bt-end").value), e = +$("#bt-every").value || 7;
  if (!t || isNaN(a) || isNaN(b) || b < a) { $("#bt-count").textContent = ""; return; }
  const dates = Math.floor((b - a) / 864e5 / e) + 1;
  $("#bt-count").textContent = `${t} ticker${t > 1 ? "s" : ""} × ${dates} date${dates > 1 ? "s" : ""} = ${t * dates} full analyses.`;
}
["bt-tickers", "bt-start", "bt-end", "bt-every"].forEach(id => $("#" + id).addEventListener("input", updateBtCount));
$("#bt-start-btn").onclick = async () => {
  const msg = $("#bt-msg"); say(msg, "");
  try {
    const body = { ...runPayload(), tickers: $("#bt-tickers").value, start: $("#bt-start").value, end: $("#bt-end").value, every: +$("#bt-every").value || 7, asset_type: $("#bt-asset").value, run_id: $("#bt-runid").value, analysts: [...form.analysts] };
    const crypto = $("#bt-asset").value === "crypto"; if (crypto) body.analysts = body.analysts.filter(a => a !== "fundamentals");
    const { id } = await api("/api/backtests", body); attachJob(id, "backtest");
  } catch (e) { say(msg, e.message, "err"); }
};
function btReset() {
  $("#bt-live").classList.remove("hidden"); $("#bt-log").innerHTML = ""; $("#bt-result").innerHTML = ""; $("#bt-bar").style.width = "0";
  $("#bt-badge").className = "badge"; $("#bt-badge").textContent = "running"; $("#bt-cancel").classList.remove("hidden"); $("#bt-label").textContent = "";
}
function btLog(text) { const d = document.createElement("div"); d.className = "ln"; d.innerHTML = `<span class="t"></span><span class="k Log">·</span><span class="c open">${esc(text)}</span>`; $("#bt-log").appendChild(d); $("#bt-log").scrollTop = 1e9; }
function btEvent(ev) {
  switch (ev.type) {
    case "started": $("#bt-title").textContent = ev.title; break;
    case "log": btLog(ev.message); break;
    case "progress": $("#bt-bar").style.width = (ev.total ? 100 * ev.done / ev.total : 0) + "%"; $("#bt-label").textContent = `${ev.done} of ${ev.total} · ${ev.label}`; if (ev.label && ev.label !== "Done" && ev.label !== "Starting") btLog("Analyzing " + ev.label); break;
    case "backtest_result": $("#bt-bar").style.width = "100%"; $("#bt-result").innerHTML = backtestHtml(ev, true); break;
    case "error": btLog("Error: " + ev.message); $("#bt-result").innerHTML = `<div class="alert">${esc(ev.message)}</div>`; break;
    case "finished": live.status = ev.status; $("#bt-badge").textContent = ev.status; $("#bt-badge").className = "badge " + ev.status; $("#bt-cancel").classList.add("hidden"); $("#live-dot").classList.add("hidden"); if (es) es.close(); loadBacktests().catch(() => { }); break;
  }
}
$("#bt-cancel").onclick = async () => { if (live) { try { await api(`/api/jobs/${live.id}/cancel`, {}); toast("Stopping after the current cell…"); } catch (e) { toast(e.message, true); } } };

function backtestHtml(r, fresh) {
  const s = r.summary || r, rows = s.by_rating || [];
  const max = Math.max(0.05, ...rows.map(x => Math.abs(x.mean_alpha)));
  const bars = rows.map(x => {
    const w = 50 * Math.abs(x.mean_alpha) / max, left = x.mean_alpha >= 0 ? 50 : 50 - w;
    return `<div class="rate-row"><span>${ratingBadge(x.rating)}</span><div class="bar"><i style="left:${left}%;width:${w}%;background:${x.mean_alpha >= 0 ? "var(--buy)" : "var(--sell)"}"></i></div><span class="nums small">n=${x.count} · ${x.hit_rate == null ? "no direction" : "hit rate " + pct(x.hit_rate)} · α ${signedPct(x.mean_alpha)}</span></div>`;
  }).join("");
  const fails = (r.failures || []).map(f => `<li>${esc(f.ticker)} ${esc(f.date)}: ${esc(f.reason)}</li>`).join("");
  return `<div class="stats" style="margin-top:14px"><div><b>${s.resolved}</b><span>resolved</span></div><div><b>${s.pending}</b><span>pending</span></div>${s.unscored ? `<div><b>${s.unscored}</b><span>unscored</span></div>` : ""}${fresh ? `<div><b>${r.cells_run}</b><span>cells run</span></div><div><b>${r.skipped}</b><span>skipped</span></div>` : ""}</div>
  ${rows.length ? `<div class="rate-bars">${bars}</div>` : `<p class="muted">No decision has settled yet, so there is nothing to score. Decisions settle once the holding window has traded; run the sweep again later.</p>`}
  <p class="muted small">${esc((s.text || "").split("\n").slice(-1)[0])}</p>
  ${fresh ? `<p class="small">Continue or settle this sweep with ID <code>${esc(r.run_id)}</code>.</p>` : ""}
  ${fails ? `<details><summary class="small">${r.failures.length} cell(s) failed</summary><ul class="small">${fails}</ul></details>` : ""}`;
}
async function loadBacktests() {
  const runs = (await api("/api/backtests")).runs;
  $("#bt-past").innerHTML = runs.length ? runs.map(r => `<div class="card" style="margin:0"><div class="feed-head"><h2>${esc(r.run_id)}</h2><span class="muted small">${esc(when(r.modified))}</span><button class="ghost" data-reuse="${esc(r.run_id)}">Continue this sweep</button></div>${backtestHtml(r, false)}</div>`).join("")
    : `<p class="muted">No sweeps yet.</p>`;
}
$("#bt-past").addEventListener("click", e => { const b = e.target.closest("[data-reuse]"); if (b) { $("#bt-runid").value = b.dataset.reuse; window.scrollTo(0, 0); toast("Sweep ID filled in. Set the same tickers and dates, then run."); } });

/* ---------- decision log ---------- */
async function loadMemory() {
  const entries = (await api("/api/memory")).entries;
  const pending = entries.filter(e => e.pending).length;
  const resolved = entries.filter(e => !e.pending && e.alpha);
  const wins = resolved.filter(e => !String(e.alpha).startsWith("-")).length;
  $("#mem-summary").innerHTML = `<div><b>${entries.length}</b><span>decisions logged</span></div><div><b>${pending}</b><span>awaiting outcome</span></div><div><b>${resolved.length ? pct(wins / resolved.length) : "—"}</b><span>beat the benchmark</span></div>`;
  $("#mem-list").innerHTML = entries.length ? entries.map(e => {
    const cls = e.pending ? "pend" : String(e.alpha || "").startsWith("-") ? "neg" : "pos";
    return `<div class="mem"><div class="mem-head"><span class="tk">${esc(e.ticker)}</span><span>${esc(e.date)}</span>${ratingBadge(e.rating)}<span class="outcome ${cls}">${e.pending ? "pending" : `return ${esc(e.raw || "n/a")} · α ${esc(e.alpha || "n/a")}${e.holding ? " over " + esc(e.holding) : ""}`}</span></div>
    ${e.reflection ? `<div class="md" style="margin-top:8px"><b>Lesson:</b> ${inline(e.reflection)}</div>` : ""}
    ${e.decision ? `<details><summary>Decision text</summary><div class="md">${md(e.decision)}</div></details>` : ""}</div>`;
  }).join("") : `<div class="empty"><p class="muted">The log fills as analyses finish. Past outcomes then feed back into later runs on the same ticker.</p></div>`;
}

/* ---------- settings ---------- */
async function loadSettings() {
  META = { ...META, ...(await api("/api/meta")) };
  const keys = (await api("/api/keys")).keys;
  $("#keys").innerHTML = keys.map(k => `<div class="key" data-env="${esc(k.env)}"><div><span class="name">${esc(k.env)}</span><span class="for">${esc(k.label)}</span></div><input type="password" autocomplete="off" placeholder="${k.set ? "•••••••• (set; paste to replace)" : "Paste key"}"><button class="primary save">Save</button>${k.set ? `<button class="ghost rm" title="Remove this key">Remove</button>` : `<span class="pill unset">not set</span>`}</div>`).join("");
  $("#chatgpt-state").textContent = META.chatgpt.signed_in_as ? `Signed in as ${META.chatgpt.signed_in_as}.` : "Not signed in.";
  $("#paths").innerHTML = Object.entries(META.paths).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}
$("#keys").addEventListener("click", async e => {
  const row = e.target.closest(".key"); if (!row) return;
  const input = $("input", row), env = row.dataset.env;
  try {
    if (e.target.closest(".save")) { if (!input.value.trim()) return toast("Paste a key first.", true); await api("/api/keys", { env, value: input.value }); input.value = ""; toast(env + " saved."); }
    else if (e.target.closest(".rm")) { if (!confirm(`Remove ${env} from this session and your .env file?`)) return; await api("/api/keys", { env, value: "" }); toast(env + " removed."); }
    else return;
    META.providers = (await api("/api/meta")).providers; await loadSettings(); if (provider()) renderProviderStatus();
  } catch (err) { toast(err.message, true); }
});
$("#clear-cp").onclick = async () => {
  if (!confirm("Delete all saved checkpoints? Interrupted runs will start over.")) return;
  try { const r = await api("/api/checkpoints/clear", {}); say($("#cp-msg"), `Cleared ${r.cleared} checkpoint database(s).`, "ok"); } catch (e) { say($("#cp-msg"), e.message, "err"); }
};

init().catch(e => { document.body.insertAdjacentHTML("afterbegin", `<div class="alert" style="margin:20px">Could not load the app: ${esc(e.message)}</div>`); });
