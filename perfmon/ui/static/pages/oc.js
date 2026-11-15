// "Podkręcanie": the active UV/OC profile, how far it is from "stable", and the one next step.
// NX Frame only reads; profiles are described by the user's agent with `pm oc`.
Pages.oc = (() => {
  const VERDICT_SEV = { niestabilny: "critical", "w trakcie testów": "warning", "spełnia kryteria stabilności": "good" };

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
        ? h("div", { class: "oc-fail" }, failed.map((c) => h("span", { class: "sev critical" }, `${c.check.replace(/^0 × /, "")}: ${c.value}${c.note ? `, ${c.note}` : ""}`)))
        : null,
      st.low_load_resets
        ? h("p", { class: "oc-note" }, `${st.low_load_resets} z ${st.resets.length} resetów przy niskim obciążeniu: typowe dla za mocnego Curve Optimizera na biegu jałowym.`)
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
    put(box,
      h("div", { class: "oc-next-title" }, s.title),
      h("p", { class: "oc-note" }, s.where),
      h("div", { class: "oc-cmdrow" }, h("code", { class: "oc-cmd" }, s.command), h("button", { class: "btn", onclick: copy }, "Kopiuj")),
      s.note ? h("p", { class: "oc-note" }, s.note) : null,
      r.test ? h("div", { class: "oc-checks" }, h("p", { class: "oc-note" }, r.test.what),
        r.test.criteria.map((c) => h("span", { class: `sev${c.ok ? " good" : ""}` }, c.check))) : null,
      r.rollback_if_bad ? h("p", { class: "oc-note" }, r.rollback_if_bad) : null);
    return box;
  };

  const render = async (view) => {
    const [ov, next, card] = await Promise.all([api("oc"), api("oc_next").catch(() => null), api("agent_card").catch(() => null)]);
    const active = ov.profiles.find((p) => p.id === ov.active_profile_id);
    const st = ov.stability;
    const ask = () => askAgent("Sprawdź stan podkręcania (pm oc list, pm oc stability, pm oc next). Czy aktywny profil jest stabilny "
      + "i jaki ma być następny krok? Profile zakładasz i aktywujesz przez pm oc add / pm oc activate, po moim potwierdzeniu. "
      + 'Wniosek zapisz: pm note add "...".');
    view.append(h("div", { class: "page-head" }, h("h1", {}, "Podkręcanie"),
      active ? h("span", { class: "oc-active" }, active.name, h("span", { class: "sub" }, `, ${active.summary}`)) : null,
      active && st && !st.error ? h("span", { class: `sev ${VERDICT_SEV[st.verdict] || ""}` }, st.verdict) : null,
      h("div", { class: "spacer" }), h("button", { class: "btn primary", onclick: ask }, "Zapytaj agenta")));
    if (ov.gpu_change) {
      view.append(h("p", { class: "oc-changed" }, `Ustawienia karty zmieniły się ${when(ov.gpu_change.ts)}: ${(ov.gpu_change.changes || []).slice(0, 3).join(", ")}. Poproś agenta o nowy profil.`));
    }
    put(view,
      agentCard(card),
      next ? nextBlock(next) : null,
      st && !st.error ? stabilityBlock(st) : null,
      !ov.profiles.length && !next ? h("div", { class: "empty" }, "Nie ma jeszcze profilu. Poproś agenta, żeby opisał obecne ustawienia jako profil Stock.") : null);
  };

  return { render, destroy: () => {} };
})();
