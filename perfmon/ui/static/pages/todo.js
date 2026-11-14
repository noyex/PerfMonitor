// "Co zrobić": ranked changes backed by the user's data, and before/after tests proving what helped.
// PerfMonitor changes nothing itself: the user applies a change, then starts a test here.
Pages.todo = (() => {
  const VERDICT_SEV = { pomogło: "good", pogorszyło: "critical", mieszane: "warning", "bez zmian": "muted", "za mało danych": "muted" };
  const KIND = { stability: "Stabilność", smoothness: "Płynność", latency: "Opóźnienie", fps: "FPS" };
  let view = null;

  const post = (body) => apiPost("test", body);
  const act = (body) => async () => {
    try {
      await post(body);
      render(view);
    } catch (e) {
      toast(e.message);
    }
  };
  const verdict = (v) => h("span", { class: `td-verdict ${VERDICT_SEV[v] || "muted"}` }, v || "w toku");

  // ---------- running test ----------
  const metric = (m) => h("tr", {},
    h("td", {}, m.label),
    h("td", { class: "num" }, fmt(m.before, 1)),
    h("td", { class: "num" }, fmt(m.after, 1)),
    h("td", { class: `num${m.significant ? (m.better ? " up" : " down") : ""}` }, m.change_pct == null ? "–" : `${m.change_pct > 0 ? "+" : "−"}${fmt(Math.abs(m.change_pct), 1)}%`));

  const openTest = (t) => h("section", { class: "tile td-open" },
    h("div", { class: "td-open-hd" },
      h("div", {}, h("div", { class: "label" }, `Test w toku · ${gameName(t.exe)}`), h("div", { class: "td-name" }, t.name)),
      verdict(t.verdict)),
    t.metrics.length
      ? h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", { class: "num" }, "Przed"), h("th", { class: "num" }, "Po"), h("th", { class: "num" }, "Zmiana"))),
        h("tbody", {}, t.metrics.map(metric)))
      : h("p", { class: "td-note" }, `Graj normalnie, najlepiej te same mapy co zwykle. Do werdyktu: ${(t.missing || []).join(", ")}.`),
    (t.warnings || []).map((w) => h("p", { class: "td-note warn" }, w)),
    h("div", { class: "td-actions" },
      h("button", { class: "btn", onclick: act({ action: "stop" }) }, "Zakończ test"),
      h("button", { class: "btn ghost", onclick: act({ action: "cancel" }) }, "Anuluj")));

  // ---------- changes ----------
  const fact = (k, v) => (v ? h("div", { class: "td-fact" }, h("span", {}, k), h("span", {}, v)) : null);

  const item = (i, n, testRunning, exe) => h("details", { class: "td-item" },
    h("summary", {},
      h("span", { class: "td-n" }, fmt(n)),
      h("span", { class: "td-main" },
        h("span", { class: "td-title" }, i.title),
        h("span", { class: "td-why" }, i.why)),
      i.test ? verdict(i.test.verdict) : h("span", { class: "td-kind" }, KIND[i.kind] || "")),
    h("div", { class: "td-body" },
      fact("Jak", i.how), fact("Efekt", i.expected), fact("Ryzyko", i.risk), fact("Cofnięcie", i.undo),
      testRunning ? null : h("button", { class: "btn", onclick: act({ action: "start", name: i.title, key: i.key, exe }) }, "Zmieniłem to, zacznij test")));

  const simple = (title, rows) => (rows.length
    ? h("section", { class: "td-sec" }, h("h2", {}, title),
      h("div", { class: "list static" }, rows.map(([t, d]) => h("div", { class: "row td-plain" }, h("span", { class: "td-title" }, t), h("span", { class: "td-why" }, d)))))
    : null);

  const render = async (el, params) => {
    view = el;
    const [a, t] = await Promise.all([api("advise"), api("tests")]);
    el.innerHTML = "";
    const b = a.bottleneck || {};
    el.append(h("div", { class: "page-head" }, h("h1", {}, "Co zrobić"),
      a.game ? h("span", { class: "sub" }, `${gameName(a.game)}${b.kind ? ` · ogranicza ${b.kind.toUpperCase().replace("_", " ")} przez ${fmt(b.share_pct)}% grania` : ""}`) : null));
    if (t.open) el.append(openTest(t.open));
    if (a.items.length) {
      el.append(h("div", { class: "list" }, a.items.map((i, n) => item(i, n + 1, !!t.open, a.test_exe))));
    } else {
      el.append(h("div", { class: "empty" }, a.game ? "Nic nie wymaga zmiany." : "Za mało rozegranych sesji, żeby coś doradzić."));
    }
    const done = t.tests.filter((x) => x.status === "done");
    if (done.length) {
      el.append(h("section", { class: "td-sec" }, h("h2", {}, "Wyniki testów"),
        h("div", { class: "list static" }, done.map((x) => h("div", { class: "row td-result" },
          h("span", { class: "time" }, x.time), h("span", { class: "title" }, x.name), verdict(x.verdict))))));
    }
    const notWorth = simple("Nie warto", (a.not_worth || []).map((x) => [x.title, x.why]));
    const unknown = simple("Do sprawdzenia ręcznie", (a.unknown || []).map((x) => [x.title, x.check]));
    if (notWorth) el.append(notWorth);
    if (unknown) el.append(unknown);
  };

  return { render };
})();
