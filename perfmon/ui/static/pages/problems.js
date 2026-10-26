// "Problemy": what went wrong, when, how bad, what it likely means.
// Deep link: #/problems?days=30
Pages.problems = (() => {
  const PERIODS = [
    [7, "7 dni"],
    [14, "14 dni"],
    [30, "30 dni"],
    [90, "90 dni"],
  ];
  const SEV_RANK = { critical: 0, serious: 1, warning: 2, info: 3 };
  const KIND = {
    ui_freeze: "Zawieszenie pulpitu",
    dwm_stall: "Zamrożony obraz (DWM)",
    app_not_responding: "Okno nie odpowiada",
  };
  const KEY_LABEL = { bios: "BIOS", os: "System", gpu_driver: "Sterownik GPU", gpu: "Karta graficzna", monitors: "Monitory", ram: "RAM", cpu: "Procesor" };
  const FREEZE_LIMIT = 15;

  const localTs = (s) => new Date(String(s).replace(" ", "T")).getTime() / 1000;
  const go = (from, to) => {
    location.hash = `#/timeline?from=${Math.round(from)}&to=${Math.round(to)}`;
  };
  const around = (ts, before, after = before) => go(ts - before, ts + after);
  const short = (v, n = 40) => {
    const s = typeof v === "string" ? v : JSON.stringify(v);
    return s == null ? "–" : s.length > n ? `${s.slice(0, n - 1)}…` : s;
  };

  const section = (title, body) => [h("h2", {}, title), body];

  // ---------- Wnioski ----------
  const findingsList = (findings) =>
    h("div", { class: "list" },
      findings
        .slice()
        .sort((a, b) => (SEV_RANK[a.severity] ?? 9) - (SEV_RANK[b.severity] ?? 9))
        .map((f) =>
          h("div", { class: "row finding" },
            h("span", { class: `sev ${f.severity}` }, sevLabel[f.severity] || f.severity),
            h("span", { class: "text" }, h("span", { class: "head" }, f.title), f.detail ? h("span", { class: "detail" }, f.detail) : null))
        ));

  // ---------- Twarde resety ----------
  const resetRow = (r) => {
    const bc = Number(r.bugcheck_code) || 0;
    const interp = r.interpretation || [];
    let title;
    if (interp.length) title = interp[0].replace(/^[^:]*:\s*/, "").split(" (")[0].split(" - ")[0];
    else title = r.source || "Nieoczekiwane wyłączenie";
    title = title.charAt(0).toUpperCase() + title.slice(1);
    const sample = r.last_sample_before;
    const sampleTs = sample && (sample.ts || (sample.time ? localTs(sample.time) : null));
    const flags = [
      bc ? `BugCheck 0x${bc.toString(16).toUpperCase()}` : null,
      r.power_button_ts && r.power_button_ts !== "0" ? "przycisk zasilania" : null,
      r.during_shutdown || (r.sleep_in_progress && r.sleep_in_progress !== "0") ? "podczas zamykania/usypiania" : null,
      sampleTs ? `ostatni pomiar ${fmtTime(sampleTs, false)}` : null,
    ].filter(Boolean);
    return h("div", { class: "row", title: interp.join("\n") || r.message || "", onclick: () => (sampleTs ? go(sampleTs - 900, sampleTs + 120) : around(r.ts, 900, 120)) },
      h("span", { class: "time" }, fmtTime(r.ts)),
      h("span", { class: "title" }, title),
      h("span", { class: "aside" }, flags.join(" · ")));
  };

  const resetsList = (resets) => {
    // An EventLog 6008 entry within minutes of a Kernel-Power entry that lists it in `also` is the same reset.
    const merged = new Set(resets.filter((r) => r.also).map((r) => r.also));
    const primary = resets.filter((r) => r.also || !merged.has(r.source) || !resets.some((o) => o !== r && o.also === r.source && Math.abs(o.ts - r.ts) < 900));
    return h("div", { class: "list" }, primary.slice().sort((a, b) => b.ts - a.ts).map(resetRow));
  };

  // ---------- Zawieszenia ----------
  const freezesList = (freezes) => {
    // ui_freeze + dwm_stall at the same moment are one event.
    const groups = new Map();
    for (const f of freezes) {
      const g = groups.get(f.time) || { time: f.time, items: [] };
      g.items.push(f);
      groups.set(f.time, g);
    }
    const events = [...groups.values()].sort((a, b) => (a.time < b.time ? 1 : -1));
    const box = h("div");
    const draw = (all) => {
      box.innerHTML = "";
      const list = h("div", { class: "list" });
      for (const g of all ? events : events.slice(0, FREEZE_LIMIT)) {
        const ts = localTs(g.time);
        const dur = g.items.map((i) => i.duration_s).find((d) => d != null);
        const title = g.items.some((i) => i.kind === "app_not_responding") ? g.items[0].title : g.items.map((i) => KIND[i.kind] || i.title).join(" · ");
        list.append(
          h("div", { class: "row", onclick: () => around(ts, 300) },
            h("span", { class: "time" }, fmtTime(ts)),
            h("span", { class: "title" }, title),
            h("span", { class: "aside" }, dur != null ? fmtDur(dur) : "do resetu")));
      }
      box.append(list);
      if (!all && events.length > FREEZE_LIMIT) box.append(h("button", { class: "btn more", onclick: () => draw(true) }, `Pokaż wszystkie (${events.length})`));
    };
    draw(false);
    return box;
  };

  // ---------- Powtarzające się crashe ----------
  const crashesTable = (crashes) =>
    h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Aplikacja"), h("th", {}, "Rodzaj"), h("th", { class: "num" }, "Ile razy"), h("th", { class: "num" }, "Ostatnio"))),
      h("tbody", {},
        crashes
          .slice()
          .sort((a, b) => b.n - a.n)
          .map((c) => {
            const last = localTs(c.last);
            return h("tr", { class: "click", onclick: () => around(last, 300) },
              h("td", {}, gameName(c.app)),
              h("td", {}, c.kind === "app_hang" ? "Zawieszenie" : "Crash"),
              h("td", { class: "num" }, c.n),
              h("td", { class: "num" }, fmtTime(last)));
          })));

  // ---------- Zmiany konfiguracji ----------
  const configList = (changes) =>
    h("div", { class: "list" },
      changes
        .slice()
        .sort((a, b) => b.ts - a.ts)
        .map((c) => {
          const keys = c.changed && c.changed.length ? c.changed : Object.keys(c.details || {});
          const text = keys
            .map((k) => {
              const d = (c.details || {})[k];
              const name = KEY_LABEL[k] || k;
              return d && typeof d === "object" && ("before" in d || "after" in d) ? `${name}: ${short(d.before)} → ${short(d.after)}` : name;
            })
            .join("  ·  ");
          return h("div", { class: "row", title: text, onclick: () => around(c.ts, 3600) },
            h("span", { class: "time" }, fmtTime(c.ts)),
            h("span", { class: "title" }, text || c.title));
        }));

  const render = async (view, params) => {
    let days = +params.days;
    if (!PERIODS.some(([d]) => d === days)) days = 30;

    const seg = segmented(PERIODS, days, (d) => {
      days = d;
      load();
    });
    view.append(
      h("div", { class: "page-head" }, h("h1", {}, "Problemy"), h("div", { class: "spacer" }), seg,
        h("button", { class: "btn", onclick: () => askAgent(`Przeanalizuj stabilność systemu z ostatnich ${days} dni (pm health --days ${days}). Co jest najpoważniejsze i co zrobić?`) }, "Zapytaj agenta"))
    );
    const body = h("div");
    view.append(body);

    let seq = 0;
    const load = async () => {
      const mine = ++seq;
      history.replaceState(null, "", `#/problems?days=${days}`);
      let d;
      try {
        d = await api("health", { days });
      } catch (e) {
        if (mine === seq) body.replaceChildren(h("div", { class: "empty" }, "Nie udało się wczytać danych"));
        return;
      }
      if (mine !== seq) return;

      const parts = [];
      const add = (title, items, build) => items && items.length && parts.push(...section(title, build(items)));
      add("Wnioski", d.findings, findingsList);
      add("Twarde resety", d.unclean_shutdowns, resetsList);
      add("Zawieszenia", d.freezes, freezesList);
      add("Powtarzające się crashe", d.recurring_crashes, crashesTable);
      add("Zmiany konfiguracji", d.config_changes, configList);

      body.replaceChildren(...(parts.length ? parts : [h("div", { class: "empty" }, "Brak problemów w tym okresie")]));
    };

    await load();
  };

  return { render, destroy: () => {} };
})();
