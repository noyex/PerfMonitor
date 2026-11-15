// Shared helpers: API, formatting, DOM, the agent card and game banners. Pages register themselves in window.Pages.
window.Pages = {};

// In the desktop shell the page is served by Tauri, window.__PERFMON_API__ is the origin of the window's own Python API
// server and window.__PERFMON_TOKEN__ the per-launch token its POSTs must carry. In a browser or pywebview the page
// comes from the API server itself: URLs stay relative and there is no token.
const API_BASE = window.__PERFMON_API__ || "";
const API_TOKEN = window.__PERFMON_TOKEN__ || "";
const apiUrl = (path) => `${API_BASE}${path}`;
// The shell starts its Python server together with the window (~1 s until it listens): during the first 10 s after page
// load, connection failures are retried quietly instead of reaching the page as errors. After that, or in a browser,
// a failure surfaces at once.
const API_START_DEADLINE = API_BASE ? 10000 : 0;   // ms since page load (performance.now)

const api = async (name, params = {}) => {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
  let r;
  for (let wait = 150; ; wait = Math.min(wait * 1.5, 800)) {
    try {
      r = await fetch(apiUrl(`/api/${name}?${q}`));
      break;
    } catch (e) {
      if (performance.now() >= API_START_DEADLINE) throw e;
      await new Promise((done) => setTimeout(done, Math.min(wait, API_START_DEADLINE - performance.now())));
    }
  }
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};

// The write endpoints (/api/game_target, /api/update): JSON body, JSON answer.
const apiPost = async (name, body) => {
  const headers = { "Content-Type": "application/json", ...(API_TOKEN && { "X-PerfMon-Token": API_TOKEN }) };
  const r = await fetch(apiUrl(`/api/${name}`), { method: "POST", headers, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};

// ---------- formatting ----------
const nf = (d = 0) => new Intl.NumberFormat("pl-PL", { maximumFractionDigits: d, minimumFractionDigits: d });
const fmt = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? "–" : nf(d).format(v));
const pad = (n) => String(n).padStart(2, "0");
const fmtTime = (ts, withDate = true) => {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return withDate ? `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${t}` : t;
};
const fmtDur = (s) => {
  if (s == null) return "–";
  s = Math.round(s);
  if (s < 60) return `${s} s`;
  const m = Math.round(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${pad(m % 60)} min`;
};
const gameName = (exe) => (exe || "").replace(/\.exe$/i, "");
// Session's game title: the Steam name when the session context has one, else the exe name.
const gameTitle = (s) => ((s.context && s.context.steam && s.context.steam.name) || "").replace(/[®™]/g, "").trim() || gameName(s.exe);
// "dziś, 21:08", "wczoraj, 21:08", "czw., 21:08", then "09.11, 21:08".
const DAYS = ["niedz.", "pon.", "wt.", "śr.", "czw.", "pt.", "sob."];
const whenDay = (ts) => {
  const d = new Date(ts * 1000);
  const diff = Math.round((new Date().setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86400000);
  return diff === 0 ? "dziś" : diff === 1 ? "wczoraj" : diff < 7 ? DAYS[d.getDay()] : `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
};
const when = (ts) => `${whenDay(ts)}, ${fmtTime(ts, false)}`;
// Stutters of a session per minute, and how that reads: ok | warn | bad.
const perMin = (s) => (s.duration_s > 0 ? (s.stutters || 0) / (s.duration_s / 60) : 0);
const stutterLevel = (s) => (perMin(s) >= 20 ? "bad" : perMin(s) >= 5 ? "warn" : "ok");
const signed = (v) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmt(Math.abs(v))}`;

// ---------- DOM ----------
const h = (tag, attrs = {}, ...children) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "html") e.innerHTML = v;
    else e.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) e.append(c.nodeType ? c : String(c));
  return e;
};

// el.append() that skips null / false: DOM append would print them as text.
const put = (el, ...nodes) => el.append(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false));

// Live temperatures in the dock; null while the collector is down.
const setTemps = (t) => {
  document.getElementById("temp-cpu").textContent = t && t.cpu != null ? `${fmt(t.cpu)}°` : "–";
  document.getElementById("temp-gpu").textContent = t && t.gpu != null ? `${fmt(t.gpu)}°` : "–";
};

// Live internet ping in the dock, only while netmon reports it; packet loss turns it amber.
const setPing = (n) => {
  const el = document.getElementById("ping");
  el.hidden = !n || n.inet_ms == null;
  if (el.hidden) return;
  el.lastChild.textContent = `${fmt(n.inet_ms)} ms`;
  el.classList.toggle("warn", n.inet_loss > 0);
  el.title = n.inet_loss > 0 ? `utrata pakietów ${fmt(n.inet_loss * 100)}%` : "";
};

const toast = (msg) => {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 1800);
};

// Copies a ready-made prompt for any AI agent with a terminal: it names the bundled guide and the pm CLI by full path,
// so nothing has to be installed on the agent's side.
const askAgent = async (text) => {
  let intro = "";
  try {
    const a = await api("agent");
    intro = `Przeczytaj instrukcję ${a.guide} i używaj CLI NX Frame: & "${a.cli}" <komenda>. `;
  } catch {}
  const prompt = `${intro}${text}`;
  try {
    await navigator.clipboard.writeText(prompt);
  } catch {
    const ta = h("textarea", {}, prompt);
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast("Skopiowano. Wklej agentowi AI.");
};

// What the user's agent wrote with `pm note` plus the open `pm test`; `ask` adds the "Zapytaj agenta" button.
// card = {note, test} from the API. Returns null when there is nothing to show.
const agentCard = (card, { ask, cls = "tile" } = {}) => {
  const { note, test } = card || {};
  if (!note && !test && !ask) return null;
  const done = test && !test.missing.length;
  return h("section", { class: `${cls} note` },
    h("div", { class: "who" }, h("b", {}, "Agent"), note ? h("span", {}, when(note.ts)) : null),
    note ? h("p", {}, note.text) : test ? null : h("p", { class: "hint" }, "Skopiuj gotowy prompt i wklej go swojemu agentowi AI."),
    test ? h("div", { class: `sev ${done ? "good" : "warning"}` },
      done ? `Test „${test.name}”: ${test.verdict}` : `Trwa test „${test.name}”: ${test.missing[0]}`) : null,
    ask ? h("button", { class: "btn primary", onclick: ask }, "Zapytaj agenta") : null);
};

// A game's Steam banner as a blob URL: the desktop shell's CSP allows blob: images, not the API origin. null = none.
const banners = new Map();
const bannerUrl = (appid) => {
  if (!appid) return Promise.resolve(null);
  if (!banners.has(appid)) {
    banners.set(appid, fetch(apiUrl(`/api/banner?appid=${appid}`))
      .then((r) => (r.ok ? r.blob() : null))
      .then((b) => b && URL.createObjectURL(b))
      .catch(() => null));
  }
  return banners.get(appid);
};
// Banner box of a fixed shape: the game's name, covered by the picture once (and if) it loads.
const banner = (g) => {
  const box = h("div", { class: "banner" }, h("span", {}, g.title));
  bannerUrl(g.appid).then((url) => {
    if (!url) return;
    const img = h("img", { src: url, alt: "" });
    img.onload = () => img.classList.add("in");
    box.append(img);
  });
  return box;
};
