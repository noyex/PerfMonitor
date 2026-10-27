// "Gry": list of game sessions + session detail. Deep link: #/games?id=<id>
Pages.games = (() => {
  let charts = [];

  const CAUSES = {
    disk: "Dysk",
    vram: "VRAM",
    ram: "RAM",
    cpu_thread: "Wątek CPU",
    alt_tab: "Alt+Tab",
    dpc: "Sterownik (DPC)",
    thermal: "Temperatura",
    shader_or_unknown: "Kompilacja shaderów / nieznane",
  };
  const causeLabel = (c) => (c.startsWith("background:") ? `Proces w tle: ${c.slice(11)}` : CAUSES[c] || c);

  const END = { crash: ["critical", "Crash"], unknown: ["serious", "Nieznany koniec"] };

  const tile = (label, value, unit, meta) =>
    h("div", { class: "tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value, unit ? h("small", {}, unit) : null), h("div", { class: "meta" }, meta || ""));

  // ---------- list ----------
  const renderList = async (view) => {
    const sessions = await api("sessions", { limit: 50 });
    const games = [...new Set(sessions.map((s) => gameName(s.exe)))];
    let filter = "all";

    const head = h("div", { class: "page-head" }, h("h1", {}, "Gry"), h("div", { class: "spacer" }));
    const body = h("div");
    view.append(head, body);

    const draw = () => {
      body.innerHTML = "";
      const rows = sessions.filter((s) => filter === "all" || gameName(s.exe) === filter);
      if (!rows.length) {
        body.append(h("div", { class: "empty" }, "Brak sesji gier – pojawią się po uruchomieniu gry"));
        return;
      }
      const tbody = h("tbody");
      for (const s of rows) {
        const end = END[s.end_reason];
        tbody.append(
          h("tr", { onclick: () => (location.hash = `#/games?id=${s.id}`) },
            h("td", { class: "muted" }, fmtTime(s.start_ts)),
            h("td", {}, gameName(s.exe),
              s.running ? h("span", { class: "live" }, "trwa") : null,
              end ? h("span", { class: `sev ${end[0]}` }, end[1]) : null),
            h("td", { class: "num" }, s.duration_s > 0 || s.running ? fmtDur(s.duration_s) : "–"),
            h("td", { class: "num" }, fmt(s.fps_avg)),
            h("td", { class: "num" }, fmt(s.fps_1low)))
        );
      }
      body.append(
        h("table", { class: "games-table" },
          h("thead", {}, h("tr", {}, h("th", {}, "Start"), h("th", {}, "Gra"), h("th", { class: "num" }, "Czas"), h("th", { class: "num" }, "Śr. FPS"), h("th", { class: "num" }, "1% low"))),
          tbody)
      );
    };

    if (games.length > 1) head.append(segmented([["all", "Wszystkie"], ...games.map((g) => [g, g])], filter, (v) => { filter = v; draw(); }));
    draw();
  };

  // ---------- detail ----------
  const COLS = ["cpu_total", "cpu_core_max", "gpu_load", "cpu_temp", "gpu_hotspot"];
  const DEFS = [
    { title: "Procesor", unit: "%", keys: ["cpu_total", "cpu_core_max"], series: [{ label: "Średnio" }, { label: "Najbardziej obciążony wątek" }], yMax: 100 },
    { title: "Karta graficzna", unit: "%", keys: ["gpu_load"], series: [{ label: "GPU" }], yMax: 100 },
    { title: "Temperatury", unit: "°C", keys: ["cpu_temp", "gpu_hotspot"], series: [{ label: "CPU" }, { label: "GPU hotspot" }] },
  ];

  const renderDetail = async (view, id) => {
    const d = await api("session", { id });
    if (d.error) {
      view.append(h("div", { class: "page-head" }, h("a", { class: "back", href: "#/games" }, "← Gry")), h("div", { class: "empty" }, d.error));
      return;
    }
    const s = d.session;
    const game = gameName(s.exe);
    const now = Date.now() / 1000;
    const t0 = s.start_ts;

    // end_ts is already the effective end (last data) for sessions cut short by a reset
    const t1 = s.running ? now : Math.max(s.end_ts || t0, t0 + 1);
    const f = await api("frames", { from: t0, to: t1, session: id, points: 1200 });
    const hasFrames = (f.ts || []).length > 0;
    const dur = d.duration_s || t1 - t0;
    const known = t1 - t0 > 1;

    const zoom = (a, b) => (location.hash = `#/timeline?from=${Math.round(a)}&to=${Math.round(b)}`);
    const end = END[s.end_reason];

    view.append(
      h("div", { class: "page-head" },
        h("a", { class: "back", href: "#/games" }, "← Gry"),
        h("h1", {}, game),
        h("span", { class: "sub" }, `${fmtDate(t0)} · ${fmtTime(t0, false)}${known ? ` – ${fmtTime(t1, false)}` : ""}${dur ? ` · ${fmtDur(dur)}` : ""}${s.running ? " · trwa" : ""}`),
        end ? h("span", { class: `sev ${end[0]}` }, end[1]) : null,
        h("div", { class: "spacer" }),
        known ? h("a", { class: "btn", href: `#/timeline?from=${Math.round(t0)}&to=${Math.round(t1)}` }, "Oś czasu") : null,
        h("button", { class: "btn", onclick: () => askAgent(`Przeanalizuj sesję #${id} ${game} (pm session ${id}). Co powodowało spadki FPS i stuttery oraz co warto zoptymalizować?`) }, "Zapytaj agenta"))
    );

    const st = d.stutters || {};
    if (hasFrames) {
      view.append(
        h("div", { class: "tiles" },
          tile("Średni FPS", fmt(s.fps_avg)),
          tile("1% low", fmt(s.fps_1low)),
          tile("0,1% low", fmt(s.fps_01low)),
          tile("Przycięcia", fmt(st.per_min, 2), "/min", `${fmt(st.count)} łącznie · najgorsze ${fmt(st.worst_ft_ms)} ms`))
      );
    }

    if ((d.findings || []).length) {
      view.append(
        h("h2", {}, "Wnioski"),
        h("div", { class: "list static" },
          d.findings.map((x) =>
            h("div", { class: "row find" },
              h("span", { class: `sev ${x.severity}` }, ""),
              h("div", { class: "title" }, x.title, x.detail ? h("div", { class: "detail" }, x.detail) : null)))
        )
      );
    }

    // Charts
    const wrap = h("div", { class: "charts" });
    view.append(h("h2", {}, "Przebieg sesji"), wrap);
    if (!known) {
      wrap.append(h("div", { class: "empty" }, "Brak danych sesji"));
    } else {
      const worst = (st.worst || []).map((w) => ({ ts: w.ts, severity: "serious" }));
      const markers = () => worst;
      const xRange = [t0, t1];
      const sr = await api("series", { from: t0, to: t1, cols: COLS.join(","), points: 1200 });
      if (hasFrames) {
        charts.push(chart(wrap, { title: "FPS", series: [{ label: "FPS" }], height: 150, markers, onSelect: zoom, longRange: false, xRange }, cols(f, ["fps"])));
        charts.push(chart(wrap, { title: "Czas klatki", unit: "ms", decimals: 1, series: [{ label: "Średni" }, { label: "99. percentyl" }], height: 130, onSelect: zoom, xRange }, cols(f, ["ft_avg", "ft_p99"])));
      } else {
        wrap.append(h("div", { class: "empty" }, "Brak danych o klatkach (PresentMon)"));
      }
      if ((sr.ts || []).length) for (const def of DEFS) charts.push(chart(wrap, { ...def, height: 130, onSelect: zoom, xRange }, cols(sr, def.keys)));
    }

    // Stutters
    const causes = Object.entries(st.cause_counts || {}).sort((a, b) => b[1] - a[1]);
    if (causes.length) {
      view.append(
        h("h2", {}, "Przyczyny przycięć"),
        h("div", { class: "list static" }, causes.map(([c, n]) => h("div", { class: "row" }, h("span", { class: "title" }, causeLabel(c)), h("span", { class: "aside" }, fmt(n)))))
      );
    }
    const worst = (st.worst || []).slice(0, 5);
    if (worst.length) {
      view.append(
        h("h2", {}, "Najgorsze przycięcia"),
        h("div", { class: "list" },
          worst.map((w) =>
            h("div", { class: "row go", onclick: () => zoom(w.ts - 30, w.ts + 30) },
              h("span", { class: "time" }, fmtTime(w.ts, false)),
              h("span", { class: "title" }, (w.causes || []).map(causeLabel).join(", ") || "Nieznana przyczyna"),
              h("span", { class: "aside" }, `${fmt(w.ft_ms)} ms`))))
      );
    }

    const b = d.bound_pct;
    if (b && hasFrames) {
      view.append(h("div", { class: "line" }, `Ograniczenie: GPU ${fmt(b.GPU)}% · CPU ${fmt(b.CPU)}% · limit FPS ${fmt(b.limit)}% czasu`));
    }

    const inc = d.incidents || [];
    if (inc.length) {
      view.append(
        h("h2", {}, "Zdarzenia w trakcie sesji"),
        h("div", { class: "list" },
          inc.map((i) =>
            h("div", { class: "row go", onclick: () => zoom(i.ts - 120, (i.end_ts || i.ts) + 120) },
              h("span", { class: "time" }, fmtTime(i.ts, false)),
              h("span", { class: "title" }, i.title),
              h("span", { class: `sev ${i.severity}` }, sevLabel[i.severity] || i.severity)))
        )
      );
    }

    // Context
    const c = s.context || {};
    const kv = [
      ["Sterownik GPU", [c.adrenalin || s.gpu_driver, c.gpu_driver ? `(${c.gpu_driver})` : null].filter(Boolean).join(" ")],
      ["Wersja gry", s.game_version],
      ["Windows", c.os_build],
      ["BIOS", c.bios],
    ].filter(([, v]) => v);
    if (kv.length) {
      view.append(h("h2", {}, "Środowisko"), h("div", { class: "kv" }, kv.map(([k, v]) => [h("span", { class: "k" }, k), h("span", { class: "v" }, v)])));
    }
  };

  const render = async (view, params) => {
    if (params.id) await renderDetail(view, params.id);
    else await renderList(view);
  };

  return {
    render,
    destroy: () => {
      charts.forEach((c) => c.destroy());
      charts = [];
    },
  };
})();
