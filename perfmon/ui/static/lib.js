// Shared helpers: API, formatting, DOM, charts. Pages register themselves in window.Pages.
window.Pages = {};

const api = async (name, params = {}) => {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
  const r = await fetch(`/api/${name}?${q}`);
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
        ctx.globalAlpha = 0.75;
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

const syncKey = "pm";

/**
 * chart(container, {title, unit, decimals, series:[{label, color?}], height, markers:()=>[], yMin, yMax, onSelect})
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
    font: `11px system-ui, "Segoe UI", sans-serif`,
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
        x: { time: true },
        y: { range: (u, mn, mx) => [opt.yMin ?? Math.min(0, mn ?? 0), opt.yMax ?? Math.max((mx ?? 1) * 1.08, opt.yFloor ?? 1)] },
      },
      axes: [
        { ...axis, space: 70 },
        { ...axis, size: 44, values: (u, vals) => vals.map((v) => fmt(v, opt.decimals && v < 10 ? 1 : 0)) },
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
