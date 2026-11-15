// "Gry": one tile per game, and the game's own page. Deep link: #/games?exe=<exe>
Pages.games = (() => {
  const STATUS = {
    test: ["warning", () => "trwa test agenta"],
    crash: ["critical", (s) => `awaria ${whenDay(s.ts)}`],
    live: ["good", () => "gra trwa"],
    slower: ["warning", (s) => (s.cause === "driver" ? "wolniej od nowego sterownika" : s.cause === "game" ? "wolniej od aktualizacji gry" : "wolniej niż zwykle")],
    ok: ["good", () => "jak zwykle"],
    unknown: ["", () => "za mało sesji do porównania"],
  };
  const STUTTERS = { ok: "Mało", warn: "Sporo", bad: "Dużo" };
  const ENDED = { crash: "awaria", unknown: "przerwana" };
  const SETTINGS = [["resolution", "Rozdzielczość"], ["preset", "Jakość"], ["upscaler", "Upscaler"], ["frame_gen", "Generator klatek"], ["fps_cap", "Limit FPS"], ["vsync", "VSync"], ["ray_tracing", "Ray tracing"]];
  const VERSIONS = { game: "Gra", driver: "Sterownik", os: "Windows" };

  const fps = (v) => [v == null ? "–" : fmt(v), h("small", {}, "fps")];
  const status = (st) => {
    const [sev, text] = STATUS[st.kind] || STATUS.unknown;
    return h("span", { class: `sev ${sev}` }, text(st));
  };
  const tile = (label, value, meta, cls) =>
    h("div", { class: "tile" }, h("div", { class: "label" }, label), h("div", { class: `value${cls ? ` ${cls}` : ""}` }, value), h("div", { class: "meta" }, meta || ""));
  const kv = (title, rows) =>
    h("section", { class: "tile kv" }, h("h2", {}, title), rows.map(([k, v, note]) => h("div", {}, h("span", {}, k), h("span", {}, v, note ? h("em", {}, note) : null))));

  // ---------- tiles ----------
  const renderList = async (view) => {
    const games = await api("games");
    view.append(h("div", { class: "page-head" }, h("h1", {}, "Gry")));
    if (!games.length) {
      view.append(h("div", { class: "empty" }, "Gry pojawią się tu po pierwszej sesji."));
      return;
    }
    view.append(h("div", { class: "cards" }, games.map((g) =>
      h("a", { class: "tile card", href: `#/games?exe=${encodeURIComponent(g.exe)}` },
        banner(g),
        h("div", { class: "card-body" }, h("b", {}, g.title), h("i", {}, fps(g.last.fps)), status(g.status))))));
  };

  // ---------- one game ----------
  const setting = (g, key) => {
    const v = g[key];
    if (v == null || v === "") return null;
    if (key === "resolution") return String(v).replace(/\s*x\s*/i, " × ");
    if (key === "upscaler") return [v, g.upscaler_mode].filter(Boolean).join(", ");
    return v === true ? "włączony" : v === false || v === "off" ? "wyłączony" : v;
  };

  const rate = (s) => {
    const m = perMin(s);
    return !m ? "żadnych" : m < 1 ? `jedno na ${fmt(1 / m)} min` : `${fmt(m)} na minutę`;
  };

  // "Cel": the FPS the user wants here. Click to type it; Enter or leaving the field saves, Escape cancels.
  const targetTile = (g) => {
    const value = h("div", { class: "value" });
    const meta = h("div", { class: "meta" });
    const box = h("div", { class: "tile target", role: "button", tabindex: "0", "aria-label": "Zmień cel FPS" }, h("div", { class: "label" }, "Cel"), value, meta);
    const show = () => {
      const t = g.target_fps, now = g.last.fps;
      value.replaceChildren(...(t ? fps(t) : ["Ustaw"]));
      meta.textContent = !t ? "ile FPS chcesz mieć" : now == null ? "" : now >= t ? "osiągnięty" : `brakuje ${fmt(Math.ceil(t - now))}`;
    };
    const edit = () => {
      const input = h("input", { type: "number", min: "1", max: "1000", value: g.target_fps ?? "", "aria-label": "Cel FPS" });
      let cancelled = false;
      input.onkeydown = (e) => {
        if (e.key === "Escape") cancelled = true;
        if (e.key === "Enter" || e.key === "Escape") input.blur();
        e.stopPropagation();
      };
      input.onblur = async () => {
        if (!cancelled && String(g.target_fps ?? "") !== input.value.trim()) {
          try {
            g.target_fps = (await apiPost("game_target", { exe: g.exe, fps: input.value.trim() || null })).target_fps;
          } catch (e) {
            toast(e.message);
          }
        }
        show();
      };
      value.replaceChildren(input);
      input.focus();
      input.select();
    };
    box.onclick = () => box.querySelector("input") || edit();
    box.onkeydown = (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      edit();
    };
    show();
    return box;
  };

  const sessionRow = (s) => {
    const level = stutterLevel(s);
    const end = ENDED[s.end_reason];
    const sev = s.end_reason === "crash" ? "critical" : level === "bad" || end ? "serious" : level === "warn" ? "warning" : "good";
    const notes = [s.running ? "na żywo" : fmtDur(s.duration_s), level === "warn" ? "sporo przycięć" : level === "bad" ? "dużo przycięć" : null];
    return h("div", { class: "row" },
      h("span", { class: "time" }, when(s.start_ts)),
      h("span", { class: `sev ${sev} title` }, h("span", {}, notes.filter(Boolean).join(", "), end ? [", ", h("span", { class: "bad" }, end)] : null)),
      h("span", { class: "fps" }, fps(s.fps)));
  };

  const renderGame = async (view, exe) => {
    const g = await api("game", { exe });
    const back = h("a", { class: "back", href: "#/games" }, "← Gry");
    if (g.error) {
      view.append(h("div", { class: "page-head" }, back), h("div", { class: "empty" }, "Nie ma jeszcze sesji tej gry."));
      return;
    }
    const last = g.last;
    const ask = () => askAgent(`Przeanalizuj grę ${g.title} (pm regress --exe ${g.exe}, pm sessions, pm session <id> dla ostatnich sesji). `
      + "Jak chodzi względem wcześniejszych sesji, co powoduje spadki FPS i przycięcia, co zmienić? "
      + (g.target_fps ? `Mój cel to ${g.target_fps} FPS. ` : "")
      + `Wniosek zapisz: pm note add --exe ${g.exe} "...".`);
    const settings = g.settings ? SETTINGS.map(([k, label]) => [label, setting(g.settings, k)]).filter(([, v]) => v != null) : [];
    const versions = g.versions.map((v) => [VERSIONS[v.key], v.value, v.since_ts ? `od ${whenDay(v.since_ts)}` : null]);
    const level = stutterLevel(last);
    view.append(
      h("div", { class: "page-head" }, back, h("h1", {}, g.title), h("div", { class: "spacer" }), h("button", { class: "btn primary", onclick: ask }, "Zapytaj agenta")),
      h("div", { class: "game" },
        h("div", { class: "game-col" },
          g.appid ? h("div", { class: "tile game-banner" }, banner(g)) : null,
          settings.length ? kv("Ustawienia gry", settings) : null,
          versions.length ? kv("Wersje", versions) : null),
        h("div", { class: "game-col" },
          h("div", { class: "tiles" },
            tile("Średnio", fps(last.fps), g.vs_usual != null ? `${signed(g.vs_usual)} względem zwykle` : ""),
            targetTile(g),
            tile("1% low", last.fps_1low == null ? "–" : fmt(last.fps_1low), "najgorsze chwile"),
            last.frames ? tile("Przycięcia", STUTTERS[level], rate(last), level) : null),
          agentCard(g.agent),
          h("section", {}, h("h2", {}, "Ostatnie sesje"), h("div", { class: "list" }, g.recent.map(sessionRow))))));
  };

  return {
    render: (view, params) => (params.exe ? renderGame(view, params.exe) : renderList(view)),
    destroy: () => {},
  };
})();
