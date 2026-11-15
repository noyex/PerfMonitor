// Hash router + collector status and temperatures in the dock.
let current = null;

const route = async () => {
  const [, name = "overview", ...rest] = location.hash.split("/");
  const params = Object.fromEntries(new URLSearchParams(rest.join("/").split("?")[1] || location.hash.split("?")[1] || ""));
  const key = window.Pages[name.split("?")[0]] ? name.split("?")[0] : "overview";  // unknown and old #/now -> overview
  document.querySelectorAll(".dock nav a").forEach((a) => {
    const on = a.getAttribute("href").split("/")[1] === key;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (current && current.destroy) current.destroy();
  const view = document.getElementById("view");
  view.innerHTML = "";
  view.dataset.page = key;
  const page = window.Pages[key];
  current = page;
  try {
    await page.render(view, params);
  } catch (e) {
    view.append(h("div", { class: "empty" }, `Błąd: ${e.message}`));
  }
};

// The dock shows the collector state only when something is wrong.
const status = async () => {
  const box = document.getElementById("status");
  let problem = null;
  try {
    const s = await api("latest");
    const alive = s.collector_alive ?? s.alive;
    setTemps(alive && s.sample ? { cpu: s.sample.cpu_temp, gpu: s.sample.gpu_temp } : null);
    setPing(alive ? s.net : null);
    if (!alive) problem = ["bad", "Kolektor nie działa"];
    else if (s.admin === false) problem = ["warn", "Zbieranie bez uprawnień administratora"];
  } catch {
    setTemps(null);
    setPing(null);
    problem = ["bad", "Brak danych"];
  }
  box.hidden = !problem;
  if (problem) {
    box.className = `status ${problem[0]}`;
    box.textContent = problem[1];
  }
};

// A newer release shows one button in the dock; the installer it runs (UAC) closes and restarts the app.
const update = async () => {
  const btn = document.getElementById("update");
  let s;
  try {
    s = await api("update");
  } catch {
    return;
  }
  btn.hidden = !s.available;
  if (!s.available) return;
  btn.disabled = s.state === "downloading" || s.state === "installing";
  btn.textContent = s.state === "downloading" ? `Pobieranie ${fmt(s.progress * 100)}%`
    : s.state === "installing" ? "Instalowanie…" : `Aktualizuj do ${s.available}`;
  if (s.state === "error" && update.shown !== s.error) toast((update.shown = s.error));
  if (btn.disabled) setTimeout(update, 1000);
};
document.getElementById("update").addEventListener("click", async () => {
  const r = await fetch("/api/update", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  if (!r.ok) toast((await r.json()).error || r.statusText);
  update.shown = null;
  update();
});

window.addEventListener("hashchange", route);
// uPlot paints its axes on a canvas, which does not repaint when a web font arrives late.
const TEXT = css("--font-text"), DISPLAY = css("--font-display");
Promise.all([`400 13px ${TEXT}`, `600 13px ${TEXT}`, `400 19px ${DISPLAY}`, `500 21px ${DISPLAY}`, `600 19px ${DISPLAY}`].map((f) => document.fonts.load(f)))
  .catch(() => {})
  .then(route);
status();
setInterval(status, 5000);
update();
setInterval(update, 3600 * 1000);   // the server caches the GitHub answer
