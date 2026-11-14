// "Podkręcanie": active UV/OC profile + stability, profile comparison for one game, V/F curves, per-core view.
// Profiles are only described here (BIOS / Adrenalin are changed by the user); the side panel edits them.
// Deep link: #/oc?exe=cod.exe
Pages.oc = (() => {
  let charts = [];

  const STATUS = { testing: "Testowany", stable: "Stabilny", rejected: "Odrzucony" };
  const VERDICT_SEV = { niestabilny: "critical", "w trakcie testów": "warning", "spełnia kryteria stabilności": "good" };
  const pct = (v) => (v == null ? "–" : v === 0 ? "0%" : `${v > 0 ? "+" : "−"}${fmt(Math.abs(v), 1)}%`);
  const deltaCell = (v, goodUp = true) =>
    h("td", { class: `num${v ? ((v > 0) === goodUp ? " up" : " down") : ""}` }, pct(v));

  const post = async (body) => {
    const r = await fetch("/api/oc_profile", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || r.statusText);
    return j;
  };

  // ---------- stability ----------
  const check = (st, prefix) => st.checks.find((c) => c.check.startsWith(prefix)) || {};
  const tile = (label, value, meta, sev) =>
    h("div", { class: "tile" }, h("div", { class: "label" }, label), h("div", { class: `value${sev ? ` ${sev}` : ""}` }, value), h("div", { class: "meta" }, meta || ""));

  const stabilityBlock = (st) => {
    const errs = Object.values(st.errors || {}).reduce((a, b) => a + b, 0);
    const game = check(st, "≥ 10 h gry");
    const days = check(st, "≥ 3 dni");
    const tests = st.checks.filter((c) => / bez błędów$/.test(c.check));
    const failed = st.checks.slice(0, 4).filter((c) => !c.ok);
    return h("div", {},
      h("div", { class: "tiles" },
        tile("Błędy od aktywacji", fmt(errs), st.errors_per_h != null ? `${fmt(st.errors_per_h, 2)} / h` : `${fmt(st.hours_collected, 1)} h danych`, errs ? "bad" : null),
        tile("Gra", h("span", {}, fmt(game.value, 1), h("small", {}, " / 10 h")), null),
        tile("Dni pracy", h("span", {}, fmt(days.value), h("small", {}, " / 3")), null),
        tile("Testy obciążeniowe", h("span", {}, fmt(tests.filter((c) => c.ok).length), h("small", {}, ` / ${tests.length}`)),
          tests.filter((c) => !c.ok).map((c) => c.check.split(" ")[0]).join(", "))),
      failed.length
        ? h("div", { class: "oc-fail" }, failed.map((c) => h("span", { class: "sev critical" }, `${c.check.replace(/^0 × /, "")}: ${c.value}${c.note ? ` – ${c.note}` : ""}`)))
        : null,
      st.low_load_resets
        ? h("p", { class: "oc-note" }, `${st.low_load_resets} z ${st.resets.length} resetów przy niskim obciążeniu – typowe dla za mocnego Curve Optimizera na biegu jałowym.`)
        : null);
  };

  // ---------- next step (advice only: nothing here changes BIOS / Adrenalin) ----------
  const nextBlock = (r) => {
    const box = h("section", { class: "tile oc-next" }, h("div", { class: "label" }, "Następny krok"));
    if (r.blocked) {
      const why = r.blockers.map((b) => b.what).join(", ");
      box.append(h("p", { class: "oc-next-text" }, `${r.until ? `Strojenie poczeka do ${r.until.slice(0, 10)}` : "Strojenie jeszcze poczeka"}: ${why}.`));
      return box;
    }
    const s = r.step;
    const copy = () => navigator.clipboard.writeText(s.command).then(() => toast("Skopiowano"), () => toast(s.command));
    box.append(
      h("div", { class: "oc-next-title" }, s.title),
      h("p", { class: "oc-note" }, s.where),
      h("div", { class: "oc-cmdrow" }, h("code", { class: "oc-cmd" }, s.command), h("button", { class: "btn", onclick: copy }, "Kopiuj")),
      s.note ? h("p", { class: "oc-note" }, s.note) : null,
      r.test ? h("div", { class: "oc-checks" }, h("p", { class: "oc-note" }, r.test.what),
        r.test.criteria.map((c) => h("span", { class: `sev${c.ok ? " good" : ""}` }, c.check))) : null,
      r.rollback_if_bad ? h("p", { class: "oc-note" }, r.rollback_if_bad) : null);
    return box;
  };

  // ---------- profile comparison ----------
  const compareTable = (cmp, activeId) => {
    if (!cmp.rows.length) return h("div", { class: "empty" }, "Brak sesji tej gry z danymi UV/OC.");
    const t = h("table", {},
      h("thead", {}, h("tr", {},
        ["Profil", "Gra", "FPS", "", "1% low", "FPS/W", "", "Moc", "Hotspot", "Błędy / h"].map((c, i) => h("th", { class: i > 0 ? "num" : "" }, c)))),
      h("tbody", {}, cmp.rows.map((r) =>
        h("tr", { class: `${r.small_sample ? "small" : ""}${r.profile_id === activeId ? " cur" : ""}`, title: r.small_sample ? "Mała próba (< 30 min) – wynik orientacyjny" : "" },
          h("td", {}, r.profile, r.profile_id === activeId ? h("span", { class: "note" }, "aktywny") : null),
          h("td", { class: "num" }, `${r.sessions} · ${fmtDur(r.minutes * 60)}`),
          h("td", { class: "num" }, fmt(r.fps_avg)),
          deltaCell(r.fps_avg_delta_pct),
          h("td", { class: "num" }, fmt(r.fps_1low)),
          h("td", { class: "num" }, fmt(r.fps_per_w, 2)),
          deltaCell(r.fps_per_w_delta_pct),
          h("td", { class: "num" }, r.power_w != null ? `${fmt(r.power_w)} W` : "–"),
          h("td", { class: "num" }, r.gpu_hotspot != null ? `${fmt(r.gpu_hotspot)}°` : "–"),
          h("td", { class: `num${r.errors_per_h ? " down" : ""}` }, r.errors_per_h == null ? "–" : fmt(r.errors_per_h, 2))))));
    return h("div", { class: "tbl" }, t);
  };

  // ---------- V/F ----------
  const vfChart = (wrap, title, a, b, labels) => {
    const box = h("div", { class: "chart" });
    const read = h("div", { class: "chart-read" });
    box.append(h("div", { class: "chart-head" }, h("div", { class: "chart-title" }, title), read));
    const el = h("div");
    box.append(el);
    wrap.append(box);
    // a curve needs at least two clock buckets (uPlot cannot range a single x value)
    [a, b] = [a, b].map((s) => (s && s.length >= 2 ? s : null));
    const sets = [a, b].filter(Boolean);
    if (!sets.length) {
      el.append(h("div", { class: "empty small" }, "Za mało pomiarów pod obciążeniem."));
      return;
    }
    const data = uPlot.join(sets.map((s) => [s.map((p) => p.mhz), s.map((p) => p.v)]));
    const colors = [css("--s1"), css("--s2")];
    const names = [a, b].map((s, i) => (s && s.length ? labels[i] : null)).filter(Boolean);
    const legend = (idx) => {
      read.innerHTML = "";
      names.forEach((n, k) =>
        read.append(h("span", { class: "k" }, h("span", { class: "sw", style: `background:${colors[k]}` }), n,
          idx == null ? null : h("span", { class: "v" }, ` ${fmt(data[k + 1][idx], 3)} V`))));
      if (idx != null) read.append(h("span", { class: "t" }, `${fmt(data[0][idx])} MHz`));
    };
    const axis = { stroke: css("--muted"), grid: { stroke: css("--grid"), width: 1 }, ticks: { show: false }, font: `13px Nohemi, system-ui, sans-serif` };
    const u = new uPlot({
      width: Math.max(300, el.clientWidth || wrap.clientWidth / 2 - 40), height: 190, padding: [6, 8, 0, 0],
      legend: { show: false }, cursor: { y: false, points: { size: 7, width: 2, fill: css("--surface") } },
      // pad the x range: a single 50 MHz bucket would otherwise stretch the axis to 0..10000
      scales: { x: { time: false, range: (u, mn, mx) => [mn - 100, mx + 100] } },
      axes: [{ ...axis, values: (u, v) => v.map((x) => fmt(x)) }, { ...axis, size: 48, values: (u, v) => v.map((x) => fmt(x, 2)) }],
      series: [{}, ...names.map((n, i) => ({ label: n, stroke: colors[i], width: 2, spanGaps: true, points: { show: true, size: 4 } }))],
      hooks: { setCursor: [(u) => legend(u.cursor.idx)] },
    }, data, el);
    legend(null);
    const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: 190 }));
    ro.observe(el);
    charts.push({ destroy: () => { ro.disconnect(); u.destroy(); } });
  };

  // ---------- cores ----------
  const coresGrid = (pc) => {
    if (!pc.cores.length) return h("div", { class: "empty small" }, "Brak danych per rdzeń (wymaga kolektora z uprawnieniami admina).");
    return h("div", {},
      h("div", { class: "oc-cores" }, pc.cores.map((c) =>
        h("div", { class: `tile oc-core${c.suspect ? " bad" : ""}`, title: `Pod pełnym obciążeniem: ${fmt(c.loaded_min, 0)} min, śr. zegar efektywny ${fmt(c.eff_loaded_avg)} MHz, maks. ${fmt(c.eff_max)} MHz` },
          h("div", { class: "n" }, `Rdzeń ${c.core}`),
          h("div", { class: "v" }, c.stretch_pct == null ? "–" : `${fmt(c.stretch_pct, 1)}%`),
          h("div", { class: "m" }, c.whea19 || c.whea18 ? `WHEA ${c.whea19 + c.whea18}×` : "")))),
      pc.note ? h("p", { class: "oc-note" }, pc.note) : null);
  };

  // ---------- editor (side panel) ----------
  const num = (v) => (v === "" || v == null ? undefined : Number(v));
  const field = (label, input, hint) => h("label", { class: "oc-field" }, h("span", {}, label), input, hint ? h("small", {}, hint) : null);
  const input = (name, value, attrs = {}) => h("input", { class: "btn", name, value: value ?? "", inputmode: "decimal", ...attrs });

  const editor = (p, st, onSaved) => {
    const cpu = (p && p.cpu) || {};
    const gpu = (p && p.gpu) || {};
    const pbo = cpu.pbo || {};
    const co = cpu.co_per_core || [];
    const panel = h("aside", { class: "slab oc-panel" });
    const close = () => { panel.remove(); veil.remove(); };
    const veil = h("div", { class: "oc-veil", onclick: close });
    const coInputs = Array.from({ length: 8 }, (_, i) => input(`co${i}`, co[i], { placeholder: "0", title: `Rdzeń ${i}` }));
    const coAll = input("coall", "", { placeholder: "wszystkie" });
    coAll.oninput = () => coInputs.forEach((x) => (x.value = coAll.value));
    const expo = h("select", { class: "btn", name: "expo" },
      [["", "–"], ["on", "włączone"], ["off", "wyłączone"]].map(([v, l]) => h("option", { value: v, selected: (cpu.expo === true && v === "on") || (cpu.expo === false && v === "off") || null }, l)));
    const isActive = !!(p && p.id && st && st.profile && st.profile.id === p.id);
    const stableOk = !isActive || st.can_mark_stable;
    const status = h("select", { class: "btn", name: "status" },
      Object.entries(STATUS).map(([v, l]) => h("option", { value: v, selected: ((p && p.status) || "testing") === v || null, disabled: v === "stable" && !stableOk && (p && p.status) !== "stable" ? true : null }, l)));
    const f = {
      name: input("name", p ? p.name : "", { inputmode: "text", placeholder: "np. CO −15, GPU −60 mV" }),
      notes: h("textarea", { class: "btn", name: "notes", rows: 2 }, (p && p.notes) || ""),
      ppt: input("ppt", pbo.ppt), tdc: input("tdc", pbo.tdc), edc: input("edc", pbo.edc),
      scalar: input("scalar", pbo.scalar), boost: input("boost", pbo.boost_override), soc: input("soc", cpu.soc_v, { placeholder: "V" }),
      uv: input("uv", gpu.voltage_offset_mv, { placeholder: "mV" }), pl: input("pl", gpu.power_limit_pct, { placeholder: "%" }),
      clk: input("clk", gpu.max_clock_offset, { placeholder: "MHz" }), vram: input("vram", gpu.vram_mhz, { placeholder: "MHz" }),
      fast: h("input", { type: "checkbox", name: "fast", checked: gpu.vram_fast_timing ? true : null }),
    };
    const err = h("div", { class: "oc-err" });
    const collect = () => {
      const coVals = coInputs.map((x) => x.value.trim());
      const c = {
        co_per_core: coVals.some((v) => v !== "") ? coVals.map((v) => Number(v || 0)) : undefined,
        pbo: Object.fromEntries(Object.entries({ ppt: num(f.ppt.value), tdc: num(f.tdc.value), edc: num(f.edc.value), scalar: num(f.scalar.value), boost_override: num(f.boost.value) }).filter(([, v]) => v !== undefined)),
        expo: expo.value === "" ? undefined : expo.value === "on",
        soc_v: num(f.soc.value),
      };
      if (!Object.keys(c.pbo).length) delete c.pbo;
      const g = { voltage_offset_mv: num(f.uv.value), power_limit_pct: num(f.pl.value), max_clock_offset: num(f.clk.value), vram_mhz: num(f.vram.value), vram_fast_timing: f.fast.checked || undefined };
      const clean = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined));
      return { id: p && p.id ? p.id : undefined, name: f.name.value.trim(), notes: f.notes.value.trim(), status: status.value, cpu: clean(c), gpu: clean(g) };
    };
    const save = async (activate) => {
      err.textContent = "";
      try {
        await post({ ...collect(), activate });
        close();
        toast(activate ? "Zapisano i aktywowano" : "Zapisano");
        onSaved();
      } catch (e) {
        err.textContent = e.message;
      }
    };
    panel.append(
      h("div", { class: "oc-panel-head" }, h("h2", {}, p && p.id ? "Edytuj profil" : "Nowy profil"), h("button", { class: "btn", onclick: close }, "Zamknij")),
      field("Nazwa", f.name),
      h("h2", {}, "CPU (BIOS)"),
      field("Curve Optimizer per rdzeń", h("div", { class: "oc-co" }, coInputs), "ujemne = mniejsze napięcie"),
      field("Ustaw wszystkie", coAll),
      h("div", { class: "oc-grid" }, field("PPT W", f.ppt), field("TDC A", f.tdc), field("EDC A", f.edc), field("Scalar", f.scalar), field("Boost +MHz", f.boost), field("VSoC", f.soc)),
      field("EXPO", expo),
      h("h2", {}, "GPU (Adrenalin)"),
      h("div", { class: "oc-grid" }, field("Offset napięcia", f.uv), field("Limit mocy", f.pl), field("Offset zegara", f.clk), field("VRAM", f.vram)),
      h("label", { class: "oc-check" }, f.fast, "Szybkie timingi VRAM"),
      h("h2", {}, "Notatki i status"),
      f.notes,
      field("Status", status, stableOk ? null : "„Stabilny” po spełnieniu kryteriów stabilności"),
      err,
      h("div", { class: "oc-actions" },
        isActive ? null : h("button", { class: "btn primary", onclick: () => save(true) }, "Zapisz i aktywuj od teraz"),
        h("button", { class: isActive ? "btn primary" : "btn", onclick: () => save(false) }, isActive ? "Zapisz" : "Tylko zapisz")));
    document.body.append(veil, panel);
    f.name.focus();
  };

  // ---------- page ----------
  // A new profile starts as a copy of the active one: change one setting at a time.
  const copyOf = (p) => (p ? { ...p, id: undefined, name: "", notes: "", status: "testing" } : null);

  const render = async (view, params) => {
    destroy();
    const ov = await api("oc");
    const profiles = ov.profiles;
    const active = profiles.find((p) => p.id === ov.active_profile_id);
    const st = ov.stability;
    const reload = () => render(view, params);
    view.innerHTML = "";

    const head = h("div", { class: "page-head" }, h("h1", {}, "Podkręcanie"));
    if (active) {
      head.append(
        h("span", { class: "oc-active" }, active.name, h("span", { class: "sub" }, ` · ${active.summary}`)),
        st && !st.error ? h("span", { class: `sev ${VERDICT_SEV[st.verdict] || ""}` }, st.verdict) : null);
    }
    head.append(h("div", { class: "spacer" }));
    if (profiles.length) {
      const pick = h("select", { class: "btn", title: "Aktywuj inny profil od teraz" },
        h("option", { value: "" }, "Aktywuj profil…"),
        profiles.filter((p) => !p.active).map((p) => h("option", { value: p.id }, `${p.name} · ${STATUS[p.status] || p.status}`)));
      pick.onchange = async () => {
        const p = profiles.find((x) => x.id === Number(pick.value));
        if (!p) return;
        try {
          await post({ id: p.id, activate: true });
          toast(`Aktywny: ${p.name}`);
          reload();
        } catch (e) {
          toast(e.message);
        }
      };
      head.append(pick);
    }
    if (active) head.append(h("button", { class: "btn", onclick: () => editor(active, st, reload) }, "Edytuj"));
    head.append(h("button", { class: `btn${profiles.length ? "" : " primary"}`, onclick: () => editor(copyOf(active), st, reload) }, "Nowy profil"));
    view.append(head);

    if (!profiles.length) {
      view.append(h("div", { class: "empty" },
        h("p", {}, "Zacznij od profilu „Stock” z obecnymi ustawieniami – to punkt odniesienia dla każdej zmiany."),
        h("button", { class: "btn primary", onclick: () => editor({ name: "Stock", cpu: { expo: true }, gpu: {} }, null, reload) }, "Utwórz profil Stock")));
      return;
    }
    if (ov.gpu_change) {
      view.append(h("div", { class: "tile oc-banner" },
        h("span", {}, `Ustawienia GPU zmieniły się ${fmtTime(ov.gpu_change.ts)}`, h("span", { class: "sub" }, ` · ${(ov.gpu_change.changes || []).slice(0, 3).join(", ")}`)),
        h("button", { class: "btn", onclick: () => editor(copyOf(active), st, reload) }, "Oznacz nowym profilem")));
    }
    const next = await api("oc_next").catch(() => null);
    if (next) view.append(nextBlock(next));
    if (st && !st.error) view.append(stabilityBlock(st));
    else if (!active) view.append(h("p", { class: "oc-note" }, "Żaden profil nie jest aktywny – wybierz go z listy „Aktywuj profil…”."));

    // comparison
    const cmpHead = h("h2", {}, "Porównanie profili");
    view.append(h("div", { class: "oc-cmp-head" }, cmpHead));
    const cmpBox = h("div");
    view.append(cmpBox);
    const loadCmp = async (exe) => {
      const cmp = await api("oc_compare", { exe });
      cmpHead.textContent = `Porównanie profili · ${gameName(cmp.exe) || "brak gier"}`;
      cmpBox.innerHTML = "";
      cmpBox.append(compareTable(cmp, ov.active_profile_id));
      return cmp;
    };
    const games = await api("games").catch(() => []);
    const cmp = await loadCmp(params.exe);
    if (games.length > 1) {
      const sel = h("select", { class: "btn" }, games.map((g) => h("option", { value: g.exe, selected: g.exe === cmp.exe || null }, gameName(g.exe))));
      sel.onchange = () => {
        history.replaceState(null, "", `#/oc?exe=${encodeURIComponent(sel.value)}`);
        loadCmp(sel.value);
      };
      cmpHead.after(sel);
    }

    // V/F + cores: active profile vs baseline (oldest profile)
    const base = profiles.find((p) => p.id !== ov.active_profile_id);
    const vfHead = h("h2", {}, base && active ? `Krzywa V/F · ${active.name} vs ${base.name}` : "Krzywa V/F");
    const vfWrap = h("div", { class: "charts two" });
    view.append(vfHead, vfWrap);
    const [vfA, vfB, cores] = await Promise.all([
      active ? api("oc_vf", { profile: active.id }) : api("oc_vf"),
      base && active ? api("oc_vf", { profile: base.id }) : Promise.resolve(null),
      api("oc_cores", active ? { profile: active.id } : {}),
    ]);
    const labels = [active ? active.name : "Wszystkie dane", base ? base.name : ""];
    vfChart(vfWrap, "GPU · napięcie rdzenia przy obciążeniu", vfA.gpu, vfB && vfB.gpu, labels);
    vfChart(vfWrap, "CPU · Vcore przy obciążeniu wszystkich rdzeni", vfA.cpu_all, vfB && vfB.cpu_all, labels);
    view.append(h("h2", {}, "Rdzenie CPU · clock stretching i błędy WHEA"), coresGrid(cores));
  };

  const destroy = () => {
    charts.forEach((c) => c.destroy());
    charts = [];
    document.querySelectorAll(".oc-panel, .oc-veil").forEach((e) => e.remove());
  };

  return { render, destroy };
})();
