// Shared helpers: API, formatting, DOM, charts. Pages register themselves in window.Pages.
window.Pages = {};

// In the desktop shell the page is served by Tauri, window.__PERFMON_API__ is the origin of the window's own Python API
// server and window.__PERFMON_TOKEN__ the per-launch token its POSTs must carry. In a browser or pywebview the page
// comes from the API server itself: URLs stay relative and there is no token.
const API_BASE = window.__PERFMON_API__ || "";
const API_TOKEN = window.__PERFMON_TOKEN__ || "";
const apiUrl = (path) => `${API_BASE}${path}`;
const API_START_MS = 10000;   // the shell may have just started the Python server: ~1 s for it to listen
let apiAnswered = false;

const api = async (name, params = {}) => {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
  // Until the API has answered once, connection failures are retried quietly instead of reaching the page as errors.
  const giveUp = Date.now() + (API_BASE && !apiAnswered ? API_START_MS : 0);
  let r;
  for (let wait = 150; ; wait = Math.min(wait * 1.5, 800)) {
    try {
      r = await fetch(apiUrl(`/api/${name}?${q}`));
      break;
    } catch (e) {
      if (Date.now() >= giveUp) throw e;
      await new Promise((done) => setTimeout(done, wait));
    }
  }
  apiAnswered = true;
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};

// The write endpoints (/api/oc_profile, /api/test): JSON body, JSON answer.
const apiPost = async (name, body) => {
  const headers = { "Content-Type": "application/json", ...(API_TOKEN && { "X-PerfMon-Token": API_TOKEN }) };
  const r = await fetch(apiUrl(`/api/${name}`), { method: "POST", headers, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const SERIES = ["--s1", "--s2", "--s3", "--s4"];

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
const fmtDate = (ts) => {
  const d = new Date(ts * 1000);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
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
// Polish plural: plural(n, "przycięcie", "przycięcia", "przycięć") -> 1 / 2-4, 22-24... / 0, 5-21, 25...
const plural = (n, one, few, many) => {
  if (n === 1) return one;
  const d = n % 10, t = n % 100;
  return d >= 2 && d <= 4 && (t < 12 || t > 14) ? few : many;
};

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

// Copies a ready-made prompt for the T3 agent (perf-analyst skill).
const askAgent = async (text) => {
  const prompt = `Użyj skilla perf-analyst. ${text}`;
  try {
    await navigator.clipboard.writeText(prompt);
  } catch {
    const ta = h("textarea", {}, prompt);
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast("Skopiowano – wklej w T3");
};

const segmented = (options, value, onChange) => {
  const wrap = h("div", { class: "seg" });
  for (const [v, label] of options) {
    const b = h("button", { class: v === value ? "on" : "" }, label);
    b.onclick = () => {
      wrap.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
      onChange(v);
    };
    wrap.append(b);
  }
  return wrap;
};

const sevLabel = { critical: "Krytyczne", serious: "Poważne", warning: "Ostrzeżenie", info: "Info" };

const BOTTLENECK = { vram: "VRAM", gpu_power: "Limit mocy GPU", cpu_thread: "Wątek CPU", cpu: "CPU", limit: "Limit FPS", gpu: "GPU", mixed: "Mieszane" };

// ---------- charts ----------
// Vertical markers (incidents, driver updates). marker = {ts, severity|color}
const markersPlugin = (getMarkers) => ({
  hooks: {
    draw: (u) => {
      const ms = getMarkers() || [];
      if (!ms.length) return;
      const { ctx } = u;
      const { top, height } = u.bbox;
      ctx.save();
      ctx.lineWidth = 1.5 * devicePixelRatio;
      for (const m of ms) {
        const x = Math.round(u.valToPos(m.ts, "x", true));
        if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
        ctx.strokeStyle = m.color || css(`--${m.severity || "muted"}`) || css("--muted");
        ctx.globalAlpha = ms.length > 40 ? 0.3 : 0.75;  // hundreds of incidents would paint a solid block
        ctx.setLineDash(m.dash ? [4 * devicePixelRatio, 4 * devicePixelRatio] : []);
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, top + height);
        ctx.stroke();
      }
      ctx.restore();
    },
  },
});

// User marks, Defender scans and background process starts within [t0, t1]: chart lines + a one-row legend (null if none).
const DIAG = { mark: ["--s3", false], scan: ["--muted", true], start: ["--warning", true] };
const diagMarkers = async (t0, t1) => {
  const ms = (await api("diag_markers", { from: t0, to: t1 })).filter((m) => DIAG[m.kind]);
  const item = (kind, text) => h("span", { class: "mark" }, h("i", { class: DIAG[kind][1] ? "dash" : "", style: `border-left-color:var(${DIAG[kind][0]})` }), text);
  const has = (kind) => ms.some((m) => m.kind === kind);
  const legend = [
    ...ms.filter((m) => m.kind === "mark").slice(-6).map((m) => item("mark", `${fmtTime(m.ts, t1 - t0 > 86400)} ${m.label}`)),
    has("scan") ? item("scan", "Skan Defendera") : null,
    has("start") ? item("start", "Start procesu w tle") : null,
  ].filter(Boolean);
  return {
    lines: ms.map((m) => ({ ts: m.ts, color: css(DIAG[m.kind][0]), dash: DIAG[m.kind][1] })),
    legend: legend.length ? h("div", { class: "marks" }, legend) : null,
  };
};

const syncKey = "pm";

// 24 h Polish tick labels (uPlot defaults to US "9:20pm" + "10/18/26").
const timeTicks = (u, vals) => {
  const span = u.scales.x.max - u.scales.x.min;
  return vals.map((v) => {
    const d = new Date(v * 1000);
    const day = `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
    if (span > 3 * 86400) return day;
    const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return span > 86400 ? `${day} ${t}` : t;
  });
};

/**
 * chart(container, {title, unit, decimals, series:[{label, color?}], height, markers:()=>[], yMin, yMax, onSelect, xRange:[t0,t1]})
 * data = [xs, ys1, ys2...]. Returns {u, setData(data)}.
 * Single series: no legend, title names it; hover readout shows the value. 2+ series: swatch legend with values.
 */
const chart = (container, opt, data) => {
  const box = h("div", { class: "chart" });
  const read = h("div", { class: "chart-read" });
  box.append(h("div", { class: "chart-head" }, h("div", { class: "chart-title" }, opt.title), read));
  const plot = h("div");
  box.append(plot);
  container.append(box);

  const colors = opt.series.map((s, i) => s.color || css(SERIES[i]));
  const unit = opt.unit ? ` ${opt.unit}` : "";
  const multi = opt.series.length > 1;

  const readout = (idx) => {
    read.innerHTML = "";
    const d = u.data;
    const i = idx ?? (d[0].length ? d[0].length - 1 : null);
    if (i === null) return;
    opt.series.forEach((s, k) => {
      const v = d[k + 1][i];
      read.append(
        multi
          ? h("span", { class: "k" }, h("span", { class: "sw", style: `background:${colors[k]}` }), s.label, " ", h("span", { class: "v" }, `${fmt(v, opt.decimals ?? 0)}${unit}`))
          : h("span", { class: "v" }, `${fmt(v, opt.decimals ?? 0)}${unit}`)
      );
    });
    if (idx !== null && idx !== undefined) read.append(h("span", { class: "t" }, fmtTime(d[0][i], opt.longRange)));
  };

  const axis = {
    stroke: css("--muted"),
    grid: { stroke: css("--grid"), width: 1 },
    ticks: { show: false },
    font: `13px ${css("--font-text")}`,
  };
  const u = new uPlot(
    {
      width: Math.max(300, plot.clientWidth || container.clientWidth - 32),
      height: opt.height || 150,
      padding: [6, 4, 0, 0],
      legend: { show: false },
      cursor: {
        sync: { key: syncKey, setSeries: false },
        points: { size: 7, width: 2, fill: css("--surface") },
        drag: { x: !!opt.onSelect, y: false, setScale: false },
        y: false,
      },
      scales: {
        // xRange pins every chart of a page to the same window, even when a series covers only part of it
        x: opt.xRange ? { time: true, range: () => opt.xRange } : { time: true },
        y: { range: (u, mn, mx) => [opt.yMin ?? Math.min(0, mn ?? 0), opt.yMax ?? Math.max((mx ?? 1) * 1.08, opt.yFloor ?? 1)] },
      },
      axes: [
        { ...axis, space: 70, values: timeTicks },
        { ...axis, size: 44, values: (u, vals) => vals.map((v) => (v === 0 ? "0" : fmt(v, opt.decimals && v < 10 ? 1 : 0))) },
      ],
      series: [
        {},
        ...opt.series.map((s, i) => ({
          label: s.label,
          stroke: colors[i],
          width: 2,
          points: { show: false },
          spanGaps: false,
          fill: !multi && opt.fill !== false ? colors[i] + "1f" : undefined,
        })),
      ],
      hooks: {
        setCursor: [(u) => readout(u.cursor.idx)],
        setSelect: [
          (u) => {
            if (!opt.onSelect || u.select.width < 4) return;
            const a = u.posToVal(u.select.left, "x");
            const b = u.posToVal(u.select.left + u.select.width, "x");
            u.setSelect({ width: 0, height: 0 }, false);
            opt.onSelect(a, b);
          },
        ],
      },
      plugins: opt.markers ? [markersPlugin(opt.markers)] : [],
    },
    data,
    plot
  );
  readout(null);
  const ro = new ResizeObserver(() => u.setSize({ width: plot.clientWidth, height: opt.height || 150 }));
  ro.observe(plot);
  return {
    u,
    el: box,
    setData: (d) => {
      u.setData(d);
      readout(null);
    },
    destroy: () => {
      ro.disconnect();
      u.destroy();
    },
  };
};

// Builds uPlot data arrays from analyze.series() output ({ts:[], col:[]}), optional value transform.
const cols = (s, keys, map = {}) => [s.ts || [], ...keys.map((k) => (s[k] || []).map((v) => (v == null ? null : map[k] ? map[k](v) : v)))];

// "Ping" chart from net_series() output: router / internet / game server lines, packet loss as markers. Returns null without data.
const pingChart = (container, n, opt = {}) => {
  const has = (k) => (n[k] || []).some((v) => v != null);
  if (!has("gw_ms") && !has("inet_ms") && !has("game_ms")) return null;
  const keys = [["gw_ms", "Router"], ["inet_ms", "Internet"], ["game_ms", "Serwer gry"]].filter(([k]) => has(k));
  // one mark where a loss streak starts: a mark per bucket would bury the diag lines under amber
  const loss = (n.ts || []).filter((_, i) => n.inet_loss[i] > 0 && !(n.inet_loss[i - 1] > 0)).map((ts) => ({ ts, color: css("--warning") }));
  return chart(container, { title: "Ping", unit: "ms", series: keys.map(([, label]) => ({ label })), yFloor: 50, ...opt, markers: () => [...loss, ...(opt.markers ? opt.markers() : [])] }, cols(n, keys.map(([k]) => k)));
};
