// "Przegląd": the running game session (or the last one) as one clickable card + the latest sessions.
Pages.overview = (() => {
  const LIVE_WINDOW = 120;
  const CHART_H = 240;
  const LIMIT = {
    gpu: "Karta graficzna", gpu_power: "Limit mocy karty", cpu: "Procesor", cpu_thread: "Jeden wątek procesora",
    limit: "Limit FPS", vram: "Pamięć VRAM", mixed: "Różne",
  };
  const ENDED = { crash: ["critical", "Awaria"], unknown: ["warning", "Przerwana"] };
  const DAYS = ["niedz.", "pon.", "wt.", "śr.", "czw.", "pt.", "sob."];

  let timer = null;
  let run = 0;       // bumped on destroy so an in-flight tick stops rescheduling
  let card = null;   // {key, refs, plot, ro}

  const when = (ts) => {
    const d = new Date(ts * 1000);
    const day = new Date(d).setHours(0, 0, 0, 0);
    const diff = Math.round((new Date().setHours(0, 0, 0, 0) - day) / 86400000);
    const label = diff === 0 ? "dziś" : diff === 1 ? "wczoraj" : diff < 7 ? DAYS[d.getDay()] : `${pad(d.getDate())}.${pad(d.getMonth() + 1)}`;
    return `${label}, ${fmtTime(ts, false)}`;
  };
  const cutsText = (n) => (n ? `${n} ${plural(n, "przycięcie", "przycięcia", "przycięć")}` : "Brak przycięć");
  const tag = (s) => {
    const t = ENDED[s.end_reason];
    return t ? h("span", { class: `ov-tag ${t[0]}` }, h("span", { class: "dot" }), t[1]) : null;
  };

  // Red dots on the FPS line at stutter timestamps.
  const stutterDots = (getTs) => ({
    hooks: {
      draw: (u) => {
        const [xs, ys] = u.data;
        if (!xs.length) return;
        const { ctx } = u;
        ctx.save();
        ctx.fillStyle = css("--critical");
        ctx.strokeStyle = css("--page");
        ctx.lineWidth = 2 * devicePixelRatio;
        for (const t of getTs()) {
          if (t < xs[0] || t > xs[xs.length - 1]) continue;
          // frames are per-second buckets starting at xs[i]; take the bucket holding t (or the nearest lower fps)
          let i = xs.findIndex((x) => x > t) - 1;
          if (i < 0) i = xs.length - 1;
          if (i + 1 < xs.length && ys[i + 1] != null && (ys[i] == null || ys[i + 1] < ys[i]) && xs[i + 1] - t < 1) i++;
          if (ys[i] == null) continue;
          ctx.beginPath();
          ctx.arc(u.valToPos(t, "x", true), u.valToPos(ys[i], "y", true), 4 * devicePixelRatio, 0, 2 * Math.PI);
          ctx.fill();
          ctx.stroke();
        }
        ctx.restore();
      },
    },
  });

  const p98 = (ys) => {
    const v = (ys || []).filter((y) => y != null).sort((a, b) => a - b);
    return v.length ? v[Math.floor(0.98 * (v.length - 1))] : 0;
  };
  // x is seconds relative to `ref`: now for a live session (negative values), the start for a finished one.
  const INCRS = [15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
  const tickLabel = (v, live) => {
    if (live) return v > -5 ? "teraz" : v % 60 ? `−${-v} s` : `−${-v / 60} min`;
    if (v >= 3600) return `${Math.floor(v / 3600)} h ${pad(Math.round(v / 60) % 60)}`;
    return v < 60 ? `${v} s` : v % 60 ? `${Math.floor(v / 60)} min ${v % 60} s` : `${v / 60} min`;
  };

  const makePlot = (el, state) => {
    const axis = { stroke: css("--muted"), grid: { stroke: css("--grid"), width: 1 }, ticks: { show: false }, font: "13px Geist, system-ui, sans-serif" };
    const u = new uPlot(
      {
        width: Math.max(300, el.clientWidth),
        height: CHART_H,
        padding: [8, 0, 0, 24],
        legend: { show: false },
        cursor: { show: false },
        // y up to the 98th percentile: loading screens at 700+ fps would flatten the gameplay line
        scales: { x: { time: false, range: (u, mn, mx) => (state.live ? [-LIVE_WINDOW, 0] : [0, Math.max(mx || 0, 60)]) }, y: { range: (u) => [0, Math.max(60, p98(u.data[1]) * 1.2)] } },
        axes: [
          { ...axis, space: 100, incrs: INCRS, values: (u, vals) => vals.map((v) => tickLabel(v, state.live)) },
          { ...axis, side: 1, size: 44, values: (u, vals) => vals.map((v) => fmt(v)) },
        ],
        series: [{}, { stroke: css("--ink"), width: 1.5, fill: "rgba(255,255,255,.06)", points: { show: false }, spanGaps: false }],
        plugins: [markersPlugin(() => state.marks), stutterDots(() => state.stutters)],
      },
      [[], []],
      el
    );
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: CHART_H }));
    ro.observe(el);
    return { u, ro };
  };

  const buildCard = (s) => {
    const live = s.running;
    const r = {};
    const state = { stutters: [], marks: [], live };
    const chartEl = h("div", { class: "ov-chart" });
    r.big = h("span");
    r.sub = h("div", { class: "ov-sub" });
    r.marksLeg = h("span", { hidden: true }, h("i", { class: "sw mark" }), "znacznik");
    r.game = h("span");
    r.gameSub = h("span", { class: "m" });
    r.cuts = h("span");
    r.cutsSub = h("span", { class: "m" });
    r.low = h("div", { class: "x" });
    r.limit = h("div", { class: "x" });
    r.advice = h("div", { class: "ov-adv" });
    const el = h("a", { class: "ov-card", href: `#/games?id=${s.id}` },
      h("div", { class: "ov-live" },
        h("div", { class: "ov-lhd" },
          h("div", {}, h("div", { class: "ov-big" }, r.big, h("small", {}, "fps")), r.sub),
          h("div", { class: "ov-leg" },
            h("span", {}, h("i", { class: "sw line" }), "FPS"),
            h("span", {}, h("i", { class: "sw cut" }), "przycięcie"),
            r.marksLeg)),
        chartEl),
      h("div", { class: "ov-dl" },
        h("div", { class: "ov-hd" },
          live ? h("span", { class: "on" }, h("span", { class: "dot live" }), "Na żywo") : h("span", {}, "Ostatnia sesja", tag(s)),
          h("span", { class: "ov-go" }, "Szczegóły sesji →")),
        h("div", {}, h("div", { class: "k" }, "Gra"), h("div", { class: "x" }, r.game, r.gameSub)),
        h("div", { class: "ov-hi" }, h("div", { class: "k" }, "Przycięcia"), h("div", { class: "x" }, r.cuts, r.cutsSub)),
        h("div", {}, h("div", { class: "k" }, "1% low"), r.low),
        h("div", {}, h("div", { class: "k" }, "Ogranicza"), r.limit, r.advice)));
    return { el, chartEl, r, state };
  };

  const fill = (c, s, f, marks, now) => {
    const { r, state } = c;
    const live = s.running;
    const fps = (f.fps || []).filter((v) => v != null);
    const avg = live ? (fps.length ? fps.reduce((a, b) => a + b, 0) / fps.length : null) : s.fps_avg;
    r.big.textContent = fmt(avg);
    r.sub.textContent = live ? "średnio z ostatnich 2 minut" : "średnio w całej sesji";
    r.game.textContent = gameTitle(s);
    r.gameSub.textContent = live ? `od ${fmtTime(s.start_ts, false)} · ${fmtDur(s.duration_s)}` : `${when(s.start_ts)} · ${fmtDur(s.duration_s)}`;
    const n = s.stutters || 0;
    r.cuts.textContent = cutsText(n);
    r.cutsSub.textContent = !n ? "" : live
      ? (now - s.last_stutter_ts < 60 ? "ostatnie przed chwilą" : `ostatnie ${fmtDur(now - s.last_stutter_ts)} temu`)
      : `1 na ${fmtDur(s.duration_s / n)}`;
    r.low.textContent = s.fps_1low != null ? `${fmt(s.fps_1low)} fps` : "–";
    const b = s.bottleneck;
    r.limit.replaceChildren(b ? LIMIT[b.kind] || b.kind : "–", b && b.share != null && b.kind !== "mixed" ? h("span", { class: "m" }, `${fmt(b.share)}% czasu`) : "");
    r.advice.textContent = b && b.kind !== "mixed" ? b.advice : "";
    const ref = live ? now : s.start_ts;
    state.stutters = (s.stutter_ts || []).map((t) => t - ref);
    state.marks = marks.map((m) => ({ ts: m.ts - ref, color: css("--s3") }));
    r.marksLeg.hidden = !marks.length;
    const [xs, ys] = cols(f, ["fps"]);
    c.plot.u.setData([xs.map((t) => t - ref), ys]);
  };

  const rows = (list) =>
    h("div", { class: "ov-rows" },
      h("div", { class: "ov-r th" }, h("span", {}, "Gra"), h("span", {}, "Kiedy"), h("span", { class: "num" }, "Czas"),
        h("span", { class: "num" }, "Śr. FPS"), h("span", { class: "num" }, "1% low"), h("span", { class: "num" }, "Przycięcia"), h("span")),
      list.map((s) =>
        h("a", { class: "ov-r", href: `#/games?id=${s.id}` },
          h("span", { class: "g" }, h("span", { class: "name" }, gameTitle(s)), tag(s)),
          h("span", { class: "m" }, when(s.start_ts)),
          h("span", { class: "num m" }, fmtDur(s.duration_s)),
          h("span", { class: "num" }, fmt(s.fps_avg)),
          h("span", { class: "num" }, fmt(s.fps_1low)),
          h("span", { class: "num" }, s.frames ? fmt(s.stutters || 0) : "–"),
          h("span", { class: "chev" }, "›"))));

  const render = async (view) => {
    const top = h("div");
    const list = h("div");
    view.append(top, list);

    const tick = async () => {
      const o = await api("overview");
      if (me !== run) return;
      const s = o.current;
      const key = s ? `${s.id}:${s.running}` : "none";
      if (!card || card.key !== key) {
        destroyCard();
        top.replaceChildren();
        if (!s || !s.running)
          top.append(h("div", { class: "ov-idle" }, h("span", { class: "dot" }), "Żadna gra nie jest uruchomiona. Pomiar zacznie się sam, gdy włączysz grę."));
        if (s) {
          card = { key, ...buildCard(s) };
          top.append(card.el);
          card.plot = makePlot(card.chartEl, card.state);
        } else card = { key };
        list.replaceChildren(...(o.recent.length
          ? [h("div", { class: "ov-lh" }, h("h2", {}, "Ostatnie sesje"), h("a", { href: "#/games" }, "Wszystkie sesje →")), rows(o.recent)]
          : []));
      }
      if (!s) return;
      const t0 = s.running ? o.now - LIVE_WINDOW : s.start_ts;
      const t1 = s.running ? o.now : s.end_ts;
      const [f, dm] = await Promise.all([
        api("frames", { from: t0, to: t1, session: s.id, points: s.running ? LIVE_WINDOW * 2 : 600 }),
        api("diag_markers", { from: t0, to: t1 }),
      ]);
      if (me === run && card && card.plot) fill(card, s, f, dm.filter((m) => m.kind === "mark"), o.now);
    };

    const me = run;
    const loop = async () => {
      try {
        await tick();
      } catch (e) {
        if (me === run && !card) view.replaceChildren(h("div", { class: "empty" }, `Błąd: ${e.message}`));
      }
      if (me !== run) return;
      // live: refresh every 2 s; otherwise only watch for a game to start
      timer = setTimeout(loop, card && card.key.endsWith(":true") ? 2000 : 10000);
    };
    await loop();
  };

  const destroyCard = () => {
    if (card && card.plot) {
      card.plot.ro.disconnect();
      card.plot.u.destroy();
    }
    card = null;
  };

  return {
    render,
    destroy: () => {
      run++;
      clearTimeout(timer);
      timer = null;
      destroyCard();
    },
  };
})();
