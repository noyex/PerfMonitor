// "Porównania": FPS per session over time + groups table (week / day / month / driver / game version) for one game.
// Deep link: #/compare?exe=cod.exe&by=driver, or with a split point: #/compare?exe=cod.exe&split=m3 (before/after marker m3)
Pages.compare = (() => {
  let plot = null;

  const BY = [
    ["week", "Tydzień"],
    ["day", "Dzień"],
    ["month", "Miesiąc"],
    ["driver", "Sterownik"],
    ["game_version", "Wersja gry"],
  ];
  const SINCE = "180d";
  const MIN_MINUTES = 30;
  const MARKER_KINDS = {
    driver: { name: "Sterownik", dash: false },
    game_update: { name: "Gra", dash: true },
  };

  const pct = (v) => (v == null ? "–" : v === 0 ? "0%" : `${v > 0 ? "+" : "−"}${fmt(Math.abs(v), 1)}%`);
  const deltaCell = (v, muted) => h("td", { class: `num${!muted && v ? (v > 0 ? " up" : " down") : ""}` }, pct(v));

  // "Sterownik GPU 26.9.1 -> 26.10.1" -> "26.10.1"
  // "exe: wersja A -> B (Steam build X -> Y)" -> "B"; "26.9.1 -> 26.10.1" -> "26.10.1"
  const markerValue = (m) => {
    const v = m.label.match(/wersja .*?->\s*([^\s(]+)/);
    return v ? v[1] : (m.label.split("->").pop() || "").replace(/\)\s*$/, "").trim();
  };
  const shortDate = (ts) => fmtDate(ts).slice(0, 5);

  const SPLIT_LABEL = { przed: "Przed", po: "Po" };
  const topBottleneck = (g) => {
    const t = Object.entries(g.bottleneck || {}).sort((a, b) => b[1] - a[1])[0];
    return t ? `${BOTTLENECK[t[0]] || t[0]} ${fmt(t[1])}%` : "–";
  };

  const groupLabel = (g, by) => {
    if (by === "week") return `${g.group.replace(/^\d+-/, "")} · ${shortDate(g.from)} – ${shortDate(g.to)}`;
    return g.group;
  };

  const plotFps = (wrap, pts, markers) => {
    const colors = [css("--s1"), css("--s2")];
    const labels = ["Średnie FPS", "1% low"];
    const box = h("div", { class: "chart" });
    const read = h("div", { class: "chart-read" });
    box.append(h("div", { class: "chart-head" }, h("div", { class: "chart-title" }, "FPS w czasie"), read));
    const el = h("div");
    box.append(el);

    const ms = markers.map((m) => ({ ts: m.ts, dash: MARKER_KINDS[m.kind].dash, color: css("--ink-2") }));
    if (markers.length) {
      box.append(
        h("div", { class: "marks" }, markers.map((m) =>
          h("span", { class: "mark" }, h("i", { class: MARKER_KINDS[m.kind].dash ? "dash" : "" }), `${MARKER_KINDS[m.kind].name} ${markerValue(m)} · ${shortDate(m.ts)}`)))
      );
    }
    wrap.append(box);

    const data = [pts.map((p) => p.start_ts), pts.map((p) => p.fps_avg), pts.map((p) => p.fps_1low)];
    const readout = (idx) => {
      read.innerHTML = "";
      // Without hover the readout doubles as the legend (2 series must always be identifiable)
      labels.forEach((l, k) =>
        read.append(h("span", { class: "k" }, h("span", { class: "sw", style: `background:${colors[k]}` }), l, idx == null ? null : " ", idx == null ? null : h("span", { class: "v" }, fmt(data[k + 1][idx], 0))))
      );
      if (idx != null) read.append(h("span", { class: "t" }, fmtTime(data[0][idx])));
    };
    const axis = { stroke: css("--muted"), grid: { stroke: css("--grid"), width: 1 }, ticks: { show: false }, font: `13px ${css("--font-text")}` };
    const H = 220;
    const u = new uPlot(
      {
        width: Math.max(300, el.clientWidth || wrap.clientWidth - 32),
        height: H,
        padding: [6, 4, 0, 0],
        legend: { show: false },
        cursor: { points: { show: false }, drag: { x: false, y: false }, y: false },
        scales: { x: { time: true }, y: { range: (u, mn, mx) => [0, Math.max((mx ?? 1) * 1.08, 1)] } },
        axes: [{ ...axis, space: 70, values: timeTicks }, { ...axis, size: 44, values: (u, vals) => vals.map((v) => fmt(v, 0)) }],
        series: [
          {},
          ...labels.map((label, i) => ({
            label,
            stroke: colors[i],
            paths: () => null,
            points: { show: true, size: 7, width: 0, fill: colors[i] },
            spanGaps: false,
          })),
        ],
        hooks: { setCursor: [(u) => readout(u.cursor.idx)], ready: [() => readout(null)] },
        plugins: ms.length ? [markersPlugin(() => ms)] : [],
      },
      data,
      el
    );
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: H }));
    ro.observe(el);
    return {
      destroy: () => {
        ro.disconnect();
        u.destroy();
      },
    };
  };

  const headline = (groups, by) => {
    if ((by !== "driver" && by !== "game_version") || groups.length < 2) return null;
    const g = groups[groups.length - 1];
    if (g.minutes < MIN_MINUTES || (g.fps_1low_delta_pct == null && g.fps_avg_delta_pct == null)) return null;
    const what = by === "driver" ? "zmianie sterownika" : "aktualizacji gry";
    const part = (label, v) => (v == null ? null : h("span", {}, `${label} `, h("span", { class: v > 0 ? "up" : v < 0 ? "down" : "" }, pct(v))));
    const parts = [part("1% low", g.fps_1low_delta_pct), part("śr. FPS", g.fps_avg_delta_pct)].filter(Boolean);
    return h("p", { class: "headline" }, `Po ${what} ${g.group}: `, parts[0], parts[1] ? ", " : null, parts[1] || null);
  };

  const table = (groups, by, split) => {
    const first = split ? "Okres" : { week: "Tydzień", day: "Dzień", month: "Miesiąc", driver: "Sterownik", game_version: "Wersja gry" }[by];
    const time = by === "week" || by === "day" || by === "month";
    const extra = [];
    if (split) extra.push(["Sterownik", (g) => g.drivers.join(", ")], ["Wersja gry", (g) => g.game_versions.join(", ")], ["Wąskie gardło", topBottleneck]);
    else if (time) extra.push(["Sterownik", (g) => g.drivers.join(", ")], ["Wersja gry", (g) => g.game_versions.join(", ")]);
    else if (by === "driver") extra.push(["Wersja gry", (g) => g.game_versions.join(", ")]);
    else extra.push(["Sterownik", (g) => g.drivers.join(", ")]);

    const head = h("tr", {}, h("th", {}, first), h("th", { class: "num" }, "Sesje"), h("th", { class: "num" }, "Minuty"), h("th", { class: "num" }, "Śr. FPS"), h("th", { class: "num" }, "Δ"), h("th", { class: "num" }, "1% low"), h("th", { class: "num" }, "Δ"), h("th", { class: "num" }, "Stutter/min"), extra.map(([l]) => h("th", {}, l)));
    const body = groups.map((g) => {
      const small = g.small_sample ?? g.minutes < MIN_MINUTES;
      // FPS columns count the game's own frames; a group played with frame generation says so. What changed since
      // the previous group (settings, driver, system) is in the row's tooltip: the table has no room for it
      const changes = g.changes || [];
      return h("tr", { class: small ? "small" : "", title: changes.join("\n"), onclick: () => (location.hash = `#/games?id=${g.session_ids[0]}`) },
        h("td", {}, split ? SPLIT_LABEL[g.group] || g.group : groupLabel(g, by), small ? h("span", { class: "note" }, "mała próba") : null,
          g.frame_gen_pct >= 5 ? h("span", { class: "note" }, `gen. klatek ${fmt(g.frame_gen_pct)}%`) : null),
        h("td", { class: "num" }, fmt(g.sessions)),
        h("td", { class: "num" }, fmt(g.minutes)),
        h("td", { class: "num" }, fmt(g.fps_avg, 1)),
        deltaCell(g.fps_avg_delta_pct, small),
        h("td", { class: "num" }, fmt(g.fps_1low, 1)),
        deltaCell(g.fps_1low_delta_pct, small),
        h("td", { class: "num" }, fmt(g.stutters_per_min, 2)),
        extra.map(([, f]) => h("td", {}, f(g))));
    });
    return h("div", { class: "tbl" }, h("table", {}, h("thead", {}, head), h("tbody", {}, body)));
  };

  const render = async (view, params) => {
    const exes = (await api("games")).map((g) => g.exe);
    if (!exes.length) {
      view.append(h("div", { class: "page-head" }, h("h1", {}, "Porównania")), h("div", { class: "empty" }, "Brak sesji do porównania"));
      return;
    }
    let exe = exes.includes(params.exe) ? params.exe : exes[0];
    let by = BY.some(([v]) => v === params.by) ? params.by : "week";
    let split = params.split || "";

    const select = h("select", { class: "btn" }, exes.map((e) => h("option", { value: e }, gameName(e))));
    select.value = exe;
    select.onchange = () => {
      exe = select.value;
      split = "";
      load();
    };
    const seg = segmented(BY, by, (v) => {
      by = v;
      load();
    });
    const splitSel = h("select", { class: "btn" });
    splitSel.onchange = () => {
      split = splitSel.value;
      load();
    };
    view.append(
      h("div", { class: "page-head" }, h("h1", {}, "Porównania"), select, seg, splitSel, h("div", { class: "spacer" }),
        h("button", { class: "btn", onclick: () => askAgent(`Porównaj wydajność ${gameName(exe)} (pm compare --exe ${exe} ${split ? `--before ${split}` : `--by ${by}`}). Co się zmieniło i czy coś wymaga optymalizacji?`) }, "Zapytaj agenta"))
    );
    const content = h("div");
    view.append(content);

    let token = 0;
    const load = async () => {
      const mine = ++token;
      const marks = await api("markers", { exe, since: SINCE });
      if (mine !== token) return;
      // Split points are the numbered change markers of this game
      splitSel.replaceChildren(
        h("option", { value: "" }, "Bez podziału"),
        ...marks.filter((m) => m.n).map((m) => h("option", { value: `m${m.n}` }, `m${m.n} · ${shortDate(m.ts)} · ${m.label.length > 50 ? `${m.label.slice(0, 49)}…` : m.label}`))
      );
      if (![...splitSel.options].some((o) => o.value === split)) split = "";
      splitSel.value = split;
      splitSel.style.display = splitSel.options.length > 1 ? "" : "none";
      seg.style.display = split ? "none" : "";
      history.replaceState(null, "", `#/compare?exe=${encodeURIComponent(exe)}&by=${by}${split ? `&split=${split}` : ""}`);
      const [groups, pts] = await Promise.all([api("compare", { exe, by, since: SINCE, before: split }), api("points", { exe, since: SINCE })]);
      if (mine !== token) return;
      plot && plot.destroy();
      plot = null;
      content.innerHTML = "";
      if (!groups.length || !pts.length) {
        content.append(h("div", { class: "empty" }, "Brak sesji tej gry w tym okresie"));
        return;
      }
      const hl = split ? null : headline(groups, by);
      if (hl) content.append(hl);
      const wrap = h("div", { class: "charts" });
      content.append(wrap);
      plot = plotFps(wrap, pts, marks.filter((m) => MARKER_KINDS[m.kind]));
      content.append(h("h2", {}, "Grupy"), table(groups, by, split));
    };
    await load();
  };

  return {
    render,
    destroy: () => {
      plot && plot.destroy();
      plot = null;
    },
  };
})();
