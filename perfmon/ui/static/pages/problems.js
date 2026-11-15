// "Problemy": what went wrong in the last two weeks, in plain words. The why is the agent's job.
Pages.problems = (() => {
  const DAYS = 14;
  const TITLE = {
    reset: (r) => (r.game ? "Komputer zresetował się w trakcie gry" : "Komputer wyłączył się nagle"),
    bsod: () => "Niebieski ekran",
    gpu_driver: () => "Sterownik karty graficznej przestał odpowiadać",
    whea: () => "Błąd sprzętowy (WHEA)",
    crash: (r) => (r.app ? "Program się wysypał" : "Gra się wysypała"),
    hang: (r) => (r.app ? "Program przestał odpowiadać" : "Gra przestała odpowiadać"),
    freeze: (r) => (r.duration_s ? `Obraz zawiesił się na ${fmtDur(r.duration_s)}` : "Obraz się zawiesił"),
    disk: () => "Problem z dyskiem",
    memory: () => "Zabrakło pamięci",
    leak: () => "Gra zajmuje coraz więcej pamięci",
    malware: () => "Defender wykrył zagrożenie",
    net: () => "Internet przestał działać",
  };

  const row = (r) =>
    h("div", { class: "row problem" },
      h("span", { class: `sev ${r.severity}` }),
      h("span", { class: "title" }, TITLE[r.group](r),
        h("small", {}, [r.app || r.game || "poza grą", r.count > 1 ? `${r.count} razy` : null].filter(Boolean).join(", "))),
      h("span", { class: "aside" }, when(r.ts)));

  const render = async (view) => {
    const ask = () => askAgent(`Przeanalizuj stabilność komputera z ostatnich ${DAYS} dni (pm health --days ${DAYS}, pm incidents). `
      + 'Co jest najpoważniejsze, jaka jest przyczyna i co zrobić? Wniosek zapisz: pm note add "...".');
    view.append(h("div", { class: "page-head" }, h("h1", {}, "Problemy"), h("span", { class: "sub" }, `ostatnie ${DAYS} dni`),
      h("div", { class: "spacer" }), h("button", { class: "btn primary", onclick: ask }, "Zapytaj agenta")));
    const d = await api("problems", { days: DAYS });
    put(view,
      agentCard(d.agent),
      d.items.length ? h("div", { class: "list" }, d.items.map(row)) : h("div", { class: "empty" }, `Bez problemów w ostatnich ${DAYS} dniach.`));
  };

  return { render, destroy: () => {} };
})();
