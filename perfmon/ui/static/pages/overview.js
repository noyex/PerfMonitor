// "Przegląd": one session over the mercury drop; the glass buttons switch the whole view, the agent card sits under
// them. The title opens the game.
Pages.overview = (() => {
  const SESSIONS = 4;
  const LIVE_POLL_MS = 2000;
  const IDLE_POLL_MS = 10000;
  const LONG_TITLE = 24;
  const ENDED = { crash: ["bad", "awaria"], unknown: ["warn", "przerwana"] };
  const VERDICT = { ok: "Mało przycięć", warn: "Sporo przycięć", bad: "Dużo przycięć" };
  const calm = matchMedia("(prefers-reduced-motion: reduce)");

  let timer = null;
  let run = 0;         // bumped on destroy so an in-flight tick stops rescheduling
  let picked = null;   // session chosen by hand, kept across tabs; null follows the current one
  let liveId = null;

  const severity = stutterLevel;
  const hasFrames = (s) => !!s.frames || s.fps_live != null;
  // with frame generation the game's own frames are the number to judge; the presented ones go in the stat row
  const frameGen = (s) => s.frame_gen_pct > 0 && s.app_fps_avg != null;
  // a finished session shows its gameplay FPS (s.play: menus and loading left out), the number "względem zwykle" compares
  const fpsOf = (s) => (s.running ? (frameGen(s) ? s.app_fps_live : null) ?? s.fps_live ?? s.fps_avg : s.play.fps);
  const shownOf = (s) => (s.running ? s.fps_live ?? s.fps_avg : s.fps_avg);
  const count = (v) => String(v).replace(/\B(?=(\d{3})+$)/g, " ");
  const label = (s) => `${s.running ? "na żywo" : when(s.start_ts)}, ${fmtDur(s.duration_s)}`;

  // Counts a number from its current value to `to`.
  const roll = (el, to, dur = 520) => {
    const from = +el.dataset.v || 0;
    const token = (el.rollToken = (el.rollToken || 0) + 1);
    el.dataset.v = to ?? "";
    if (to == null) {
      el.textContent = "–";
      return;
    }
    if (calm.matches || !from || to === from || liveId) {   // nothing moves during a game
      el.textContent = fmt(to);
      return;
    }
    const t0 = performance.now();
    const step = (t) => {
      if (token !== el.rollToken) return;
      const k = Math.min(1, (t - t0) / dur);
      el.textContent = fmt(from + (to - from) * (1 - Math.pow(1 - k, 4)));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  const stat = (value, text, cls) => h("div", { class: cls }, h("b", {}, value), text);

  const pair = (s) => [
    ...(frameGen(s) ? [stat(fmt(shownOf(s)), "wyświetlane")] : []),
    stat(fmt(s.running ? (frameGen(s) ? s.app_fps_1low ?? s.fps_1low : s.fps_1low) : s.play.fps_1low), "1% low"),
    stat(count(s.stutters || 0), "przycięcia", severity(s)),
    ...(s.vs_usual != null ? [stat(`${signed(s.vs_usual)} fps`, "względem zwykle")] : []),
  ];

  const ask = (s) => askAgent(`Przeanalizuj sesję #${s.id} ${gameTitle(s)} (pm session ${s.id}) i stan komputera (pm health). `
    + "Co powodowało spadki FPS i przycięcia, co poprawić? Wniosek zapisz przez pm note add.");

  const render = async (view) => {
    const r = {
      title: h("a"),
      meta: h("div", { class: "ov-meta" }),
      verdict: h("div"),
      big: h("span"),
      pair: h("div", { class: "ov-pair" }),
      note: h("div", { class: "ov-note" }),
      side: h("aside", { class: "ov-side", "aria-label": "Ostatnie sesje" }),
    };
    const heading = h("h1", {}, r.title);
    r.number = h("div", { class: "ov-big" }, r.big, h("small", {}, "fps"));
    r.fps = h("section", { class: "ov-fps" }, r.verdict, r.number, r.pair, r.note);
    view.append(h("div", { class: "ov" }, h("section", { class: "ov-top" }, heading, r.meta), r.fps, r.side));

    let sessions = [];
    let agent = null;
    let agentKey = null;
    let buttons = new Map();
    let sideKey = null;
    let loaded = false;

    const setTitle = (text, href) => {
      r.title.textContent = text;
      if (href) r.title.href = href;
      else r.title.removeAttribute("href");
      heading.classList.toggle("long", text.length > LONG_TITLE);
    };

    // States without a session: only the title and one line stay on the metal.
    const blank = (title, line) => {
      setTitle(title);
      r.meta.textContent = line;
      r.fps.hidden = true;
      r.side.replaceChildren();
      sideKey = null;
      Mercury.surface(0);
    };

    const paint = (s) => {
      const frames = hasFrames(s);
      const end = ENDED[s.end_reason];
      setTitle(gameTitle(s), `#/games?exe=${encodeURIComponent(s.exe)}`);
      r.meta.replaceChildren(...(s.running
        ? [h("span", { class: "live" }, "Na żywo"), `, od ${fmtTime(s.start_ts, false)}, ${fmtDur(s.duration_s)}`]
        : [`${when(s.start_ts)}, ${fmtDur(s.duration_s)}`, ...(end ? [", ", h("span", { class: end[0] }, end[1])] : [])]));
      r.fps.hidden = false;
      r.verdict.className = `ov-verdict ${frames ? severity(s) : ""}`;
      r.verdict.textContent = frames ? VERDICT[severity(s)] : s.running ? "Czekam na klatki" : "Brak danych o klatkach";
      r.number.hidden = !frames;
      if (frames) roll(r.big, fpsOf(s));
      else delete r.big.dataset.v;
      r.pair.hidden = !frames;
      if (frames) r.pair.replaceChildren(...pair(s));
      r.note.hidden = frames;
      r.note.textContent = s.running ? "Pierwszy odczyt pojawi się, gdy gra zacznie rysować." : "PresentMon nie zebrał klatek z tej sesji.";
      Mercury.surface(frames ? perMin(s) : 0);
    };

    const select = () => {
      const s = sessions.find((x) => x.id === picked) || sessions[0];
      buttons.forEach((b, id) => b.el.setAttribute("aria-pressed", id === s.id));
      paint(s);
    };

    // Buttons are patched in place: rebuilding them every 2 s would drop keyboard focus.
    const drawSide = () => {
      const key = sessions.map((s) => s.id).join();
      if (key !== sideKey) {
        sideKey = key;
        buttons = new Map(sessions.map((s) => {
          const b = { name: h("b"), fps: h("i"), when: h("span") };
          b.el = h("button", { class: "slab ov-ses", onclick: () => { picked = s.id; select(); } }, b.name, b.fps, b.when);
          return [s.id, b];
        }));
        r.agent = h("div", { class: "ov-agent" });
        agentKey = null;
        r.side.replaceChildren(...[...buttons.values()].map((b) => b.el), r.agent);
      }
      for (const s of sessions) {
        const b = buttons.get(s.id);
        const end = ENDED[s.end_reason];
        b.name.textContent = gameTitle(s);
        b.fps.textContent = hasFrames(s) ? fmt(fpsOf(s)) : "–";
        b.when.textContent = `${label(s)}${end ? `, ${end[1]}` : ""}`;
      }
      // rebuilt only when the note or the test changed: the button keeps its focus between polls
      const card = JSON.stringify(agent);
      if (card !== agentKey) {
        agentKey = card;
        r.agent.replaceChildren(agentCard(agent, { cls: "slab", ask: () => ask(sessions.find((x) => x.id === picked) || sessions[0]) }));
      }
    };

    const tick = async () => {
      const o = await api("overview");
      if (me !== run) return;
      setTemps(o.temps);
      const cur = o.current;
      const live = cur && cur.running ? cur.id : null;
      if (live && live !== liveId) picked = null;   // a game that just started takes the screen
      liveId = live;
      Mercury.pause(o.game_running);   // the drop costs ~1.5% GPU and ~15% of a core animated, ~0 as a still frame
      sessions = [cur, ...o.recent].filter(Boolean).slice(0, SESSIONS);
      agent = o.agent;
      loaded = true;
      if (!sessions.length) {
        blank("Czekam na grę", "Pomiar zacznie się sam, gdy włączysz grę.");
        return;
      }
      drawSide();
      select();
    };

    const me = run;
    const loop = async () => {
      try {
        await tick();
      } catch (e) {
        // a passing failure keeps the last data on screen, the dock reports it
        if (me === run && !loaded) {
          if (e.message === "no data yet") blank("Czekam na dane", "Kolektor nie zapisał jeszcze pomiarów.");
          else blank("Brak danych", "Nie mogę odczytać pomiarów. Próbuję ponownie.");
        }
      }
      if (me !== run) return;
      timer = setTimeout(loop, liveId ? LIVE_POLL_MS : IDLE_POLL_MS);
    };
    Mercury.show();
    await loop();
  };

  return {
    render,
    destroy: () => {
      run++;
      clearTimeout(timer);
      timer = null;
      Mercury.hide();
    },
  };
})();
