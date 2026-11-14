// "Oś czasu": synced charts over a chosen range, incident markers, drag to zoom.
// Deep link: #/timeline?from=<ts>&to=<ts>
Pages.timeline = (() => {
  let charts = [];

  const PRESETS = [
    [3600, "1 h"],
    [6 * 3600, "6 h"],
    [86400, "24 h"],
    [7 * 86400, "7 dni"],
    [30 * 86400, "30 dni"],
  ];

  const DEFS = [
    { title: "Procesor", unit: "%", keys: ["cpu_total", "cpu_core_max"], series: [{ label: "Średnio" }, { label: "Najbardziej obciążony wątek" }], yMax: 100 },
    { title: "Karta graficzna", unit: "%", keys: ["gpu_load"], series: [{ label: "GPU" }], yMax: 100 },
    { title: "Temperatury", unit: "°C", keys: ["cpu_temp", "gpu_hotspot"], series: [{ label: "CPU" }, { label: "GPU hotspot" }] },
    { title: "Pamięć", unit: "%", keys: ["ram_pct", "vram_pct"], series: [{ label: "RAM" }, { label: "VRAM" }], yMax: 100 },
    { title: "Opóźnienie dysku", unit: "ms", decimals: 1, keys: ["disk_lat_ms"], series: [{ label: "Dysk" }], yFloor: 5 },
    { title: "Responsywność pulpitu", unit: "ms", keys: ["ui_ms", "dwm_ms"], series: [{ label: "Explorer" }, { label: "DWM" }], yFloor: 100 },
  ];
  const KEYS = ["cpu_total", "cpu_core_max", "gpu_load", "cpu_temp", "gpu_hotspot", "ram_pct", "gpu_vram_mb", "gpu_vram_total_mb", "disk_lat_ms", "ui_ms", "dwm_ms"];

  const render = async (view, params) => {
    const now = Date.now() / 1000;
    let t1 = params.to ? +params.to : now;
    let t0 = params.from ? +params.from : now - 6 * 3600;
    let incidents = [];

    const rangeLabel = h("span", { class: "sub" });
    const presets = segmented(PRESETS, params.from ? null : 6 * 3600, (sec) => {
      t1 = Date.now() / 1000;
      t0 = t1 - sec;
      load();
    });
    view.append(
      h("div", { class: "page-head" }, h("h1", {}, "Oś czasu"), rangeLabel, h("div", { class: "spacer" }), presets,
        h("button", { class: "btn", onclick: () => askAgent(`Przeanalizuj okres ${fmtTime(t0)} – ${fmtTime(t1)} (pm range ${Math.round(t0)} ${Math.round(t1)}). Co się wtedy działo i czy coś wymaga optymalizacji?`) }, "Zapytaj agenta"))
    );
    const wrap = h("div", { class: "charts" });
    const listHead = h("h2", {}, "Problemy w tym okresie");
    const list = h("div", { class: "list" });
    view.append(wrap, listHead, list);

    const zoom = (a, b) => {
      t0 = a;
      t1 = b;
      presets.querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      load();
    };

    const load = async () => {
      rangeLabel.textContent = `${fmtTime(t0)} – ${fmtTime(t1)}`;
      history.replaceState(null, "", `#/timeline?from=${Math.round(t0)}&to=${Math.round(t1)}`);
      const long = t1 - t0 > 86400;
      const [s, f, inc, diag, net] = await Promise.all([
        api("series", { from: t0, to: t1, cols: KEYS.join(","), points: 1200 }),
        api("frames", { from: t0, to: t1, points: 1200 }),
        api("incidents", { since: t0 }),
        diagMarkers(t0, t1),
        api("net_series", { from: t0, to: t1, points: 1200 }),
      ]);
      s.vram_pct = (s.gpu_vram_mb || []).map((v, i) => (v != null && s.gpu_vram_total_mb[i] ? (100 * v) / s.gpu_vram_total_mb[i] : null));
      incidents = (inc.items || inc).filter((i) => i.ts <= t1 && i.kind !== "config_change");
      const markers = () => [...incidents.filter((i) => i.severity === "critical" || i.severity === "serious"), ...diag.lines];

      charts.forEach((c) => c.destroy());
      charts = [];
      wrap.innerHTML = "";
      const hasFrames = (f.ts || []).length > 0;
      const xRange = [t0, t1];
      if (hasFrames) charts.push(chart(wrap, { title: "FPS", series: [{ label: "FPS" }], height: 150, markers, onSelect: zoom, longRange: long, xRange }, cols(f, ["fps"])));
      if (!(s.ts || []).length) {
        wrap.append(h("div", { class: "empty" }, "Brak danych w tym okresie"));
      } else {
        for (const d of DEFS) charts.push(chart(wrap, { ...d, height: 130, markers, onSelect: zoom, longRange: long, xRange }, cols(s, d.keys)));
        const ping = pingChart(wrap, net, { height: 130, onSelect: zoom, longRange: long, xRange });
        if (ping) charts.push(ping);
      }
      if (diag.legend && charts.length) charts[0].el.append(diag.legend);

      list.innerHTML = "";
      listHead.style.display = incidents.length ? "" : "none";
      list.style.display = incidents.length ? "" : "none";
      for (const i of incidents.slice().reverse().slice(0, 100)) {
        list.append(
          h("div", { class: "row", onclick: () => zoom(i.ts - 120, (i.end_ts || i.ts) + 120) },
            h("span", { class: "time" }, fmtTime(i.ts)),
            h("span", { class: "title" }, i.title),
            h("span", { class: `sev ${i.severity}` }, sevLabel[i.severity] || i.severity))
        );
      }
    };

    // Double-click on any chart resets to the last 6 hours
    wrap.addEventListener("dblclick", () => {
      t1 = Date.now() / 1000;
      t0 = t1 - 6 * 3600;
      load();
    });
    await load();
  };

  return {
    render,
    destroy: () => {
      charts.forEach((c) => c.destroy());
      charts = [];
    },
  };
})();
