// Hash router + collector status in the header.
let current = null;

const route = async () => {
  const [, name = "overview", ...rest] = location.hash.split("/");
  const params = Object.fromEntries(new URLSearchParams(rest.join("/").split("?")[1] || location.hash.split("?")[1] || ""));
  const key = window.Pages[name.split("?")[0]] ? name.split("?")[0] : "overview";  // unknown and old #/now -> overview
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href").split("/")[1] === key));
  if (current && current.destroy) current.destroy();
  const view = document.getElementById("view");
  view.innerHTML = "";
  const page = window.Pages[key];
  current = page;
  try {
    await page.render(view, params);
  } catch (e) {
    view.append(h("div", { class: "empty" }, `Błąd: ${e.message}`));
  }
};

// The header shows the collector state only when something is wrong.
const status = async () => {
  const box = document.getElementById("status");
  const text = document.getElementById("st-text");
  let problem = null;
  try {
    const s = await api("latest");
    if (s.host) document.getElementById("host").textContent = s.host.toUpperCase();
    if (!(s.collector_alive ?? s.alive)) problem = ["bad", "Kolektor nie działa"];
    else if (s.admin === false) problem = ["warn", "Zbieranie bez uprawnień administratora"];
  } catch {
    problem = ["bad", "Brak danych"];
  }
  box.hidden = !problem;
  if (problem) {
    box.querySelector(".dot").className = `dot ${problem[0]}`;
    text.textContent = problem[1];
  }
};

window.addEventListener("hashchange", route);
route();
status();
setInterval(status, 5000);
