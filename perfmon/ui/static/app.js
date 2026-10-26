// Hash router + collector status in the header.
let current = null;

const route = async () => {
  const [, name = "now", ...rest] = location.hash.split("/");
  const params = Object.fromEntries(new URLSearchParams(rest.join("/").split("?")[1] || location.hash.split("?")[1] || ""));
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href").split("/")[1] === name.split("?")[0]));
  if (current && current.destroy) current.destroy();
  const view = document.getElementById("view");
  view.innerHTML = "";
  const page = window.Pages[name.split("?")[0]] || window.Pages.now;
  current = page;
  try {
    await page.render(view, params);
  } catch (e) {
    view.append(h("div", { class: "empty" }, `Błąd: ${e.message}`));
  }
};

const status = async () => {
  const dot = document.getElementById("st-dot");
  const text = document.getElementById("st-text");
  try {
    const s = await api("latest");
    const alive = s.collector_alive ?? s.alive;
    dot.className = `dot ${alive ? "ok" : "bad"}`;
    text.textContent = alive ? (s.admin === false || s.admin === 0 || s.admin === "0" ? "Zbieranie (bez admina)" : "Zbieranie aktywne") : "Kolektor nie działa";
  } catch {
    dot.className = "dot bad";
    text.textContent = "Brak danych";
  }
};

window.addEventListener("hashchange", route);
route();
status();
setInterval(status, 5000);
