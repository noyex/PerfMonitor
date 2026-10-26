// "Teraz": live tiles + last 10 minutes.
Pages.now = (() => {
  let timer = null;
  let charts = [];
  const WINDOW = 600;

  const tile = (label, value, unit, meta) =>
    h("div", { class: "tile" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value, unit ? h("small", {}, unit) : null), h("div", { class: "meta" }, meta || ""));

  const deg = (v) => (v ? `${fmt(v)}°C` : null);

  const render = async (view) => {
    const hero = h("div");
    const tiles = h("div", { class: "tiles" });
    const grid = h("div", { class: "charts two" });
    view.append(hero, tiles, h("h2", {}, "Ostatnie 10 minut"), grid);

    const defs = [
      { title: "FPS", key: "fps", frames: true, series: [{ label: "FPS" }] },
      { title: "Procesor", unit: "%", keys: ["cpu_total", "cpu_core_max"], series: [{ label: "Średnio" }, { label: "Najbardziej obciążony wątek" }], yMax: 100 },
      { title: "Karta graficzna", unit: "%", keys: ["gpu_load"], series: [{ label: "GPU" }], yMax: 100 },
      { title: "Temperatury", unit: "°C", keys: ["cpu_temp", "gpu_hotspot"], series: [{ label: "CPU" }, { label: "GPU hotspot" }] },
      { title: "Opóźnienie dysku", unit: "ms", decimals: 1, keys: ["disk_lat_ms"], series: [{ label: "Dysk" }], yFloor: 5 },
    ];
    const allKeys = [...new Set(defs.flatMap((d) => d.keys || []))];
    let fpsChart = null;

    const tick = async () => {
      const now = Date.now() / 1000;
      const [l, s] = await Promise.all([api("latest"), api("series", { from: now - WINDOW, to: now, cols: allKeys.join(","), points: 600 })]);
      const x = l.sample || {};
      const sess = l.open_session;
      const f = sess ? await api("frames", { from: now - WINDOW, to: now, session: sess.id, points: 600 }) : null;

      // Hero only while a game is running
      hero.innerHTML = "";
      if (sess) {
        const fps = f.fps && f.fps.length ? f.fps[f.fps.length - 1] : null;
        hero.append(h("div", { class: "hero" }, h("span", { class: "big" }, fmt(fps)), h("span", { class: "unit" }, "FPS"), h("span", { class: "game" }, `${gameName(sess.exe)} · ${fmtDur(now - sess.start_ts)}`)));
      }

      tiles.innerHTML = "";
      const vramPct = x.gpu_vram_total_mb ? (100 * x.gpu_vram_mb) / x.gpu_vram_total_mb : null;
      tiles.append(
        tile("Procesor", fmt(x.cpu_total), "%", [deg(x.cpu_temp), x.cpu_clock_mhz ? `${fmt(x.cpu_clock_mhz / 1000, 2)} GHz` : null].filter(Boolean).join(" · ")),
        tile("Karta graficzna", fmt(x.gpu_load), "%", [deg(x.gpu_temp), x.gpu_power_w ? `${fmt(x.gpu_power_w)} W` : null].filter(Boolean).join(" · ")),
        tile("Pamięć RAM", fmt(x.ram_used_gb, 1), "GB", `${fmt(x.ram_pct)}% z 32 GB`),
        tile("VRAM", fmt((x.gpu_vram_mb || 0) / 1024, 1), "GB", vramPct != null ? `${fmt(vramPct)}% z ${fmt(x.gpu_vram_total_mb / 1024)} GB` : ""),
        tile("Dysk", fmt(x.disk_lat_ms, 1), "ms", `${fmt((x.disk_read_mbs || 0) + (x.disk_write_mbs || 0), 1)} MB/s`)
      );

      // Charts are created once, then only fed new data
      if (!charts.length) {
        for (const d of defs) {
          if (d.frames) continue;
          charts.push({ d, c: chart(grid, { ...d, height: 140 }, cols(s, d.keys)) });
        }
      } else {
        for (const { d, c } of charts) c.setData(cols(s, d.keys));
      }
      if (sess) {
        const data = cols(f, ["fps"]);
        if (!fpsChart) {
          fpsChart = chart(grid, { title: "FPS", series: [{ label: "FPS" }], height: 140 }, data);
          grid.prepend(fpsChart.el);
        } else fpsChart.setData(data);
      }
    };

    await tick();
    timer = setInterval(() => tick().catch(() => {}), 2000);
    Pages.now._cleanup = () => {
      fpsChart && fpsChart.destroy();
      fpsChart = null;
    };
  };

  return {
    render,
    destroy: () => {
      clearInterval(timer);
      charts.forEach(({ c }) => c.destroy());
      charts = [];
      Pages.now._cleanup && Pages.now._cleanup();
    },
  };
})();
