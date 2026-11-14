// Custom title bar for the desktop shell (window has no native frame): the three window controls in the .titlebar row.
// Does nothing in a browser or pywebview.
(() => {
  if (!window.__TAURI__ || !window.__TAURI__.window) return;
  const win = window.__TAURI__.window.getCurrentWindow();
  const svg = (inner) => `<svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1" aria-hidden="true">${inner}</svg>`;
  const GLYPH = {
    min: svg('<path d="M0 5.5h10"/>'),
    max: svg('<rect x=".5" y=".5" width="9" height="9" rx="1.5"/>'),
    restore: svg('<rect x=".5" y="2.5" width="7" height="7" rx="1"/><path d="M2.5 2.5v-1a1 1 0 0 1 1-1h5a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1h-1"/>'),
    close: svg('<path d="M0 0l10 10M10 0L0 10"/>'),
  };
  const btn = (cls, label, onclick) => h("button", { type: "button", class: `wc ${cls}`, "aria-label": label, onclick });

  const max = btn("max", "Maksymalizuj", () => win.toggleMaximize());
  const sync = async () => {
    let on = false;
    try {
      on = await win.isMaximized();
    } catch {}
    max.innerHTML = on ? GLYPH.restore : GLYPH.max;
    max.setAttribute("aria-label", on ? "Przywróć" : "Maksymalizuj");
  };
  const min = btn("min", "Minimalizuj", () => win.minimize());
  const close = btn("close", "Zamknij", () => win.close());
  min.innerHTML = GLYPH.min;
  close.innerHTML = GLYPH.close;

  // The row itself (data-tauri-drag-region in index.html) drags the window, and a double click toggles maximize.
  document.querySelector(".titlebar").append(min, max, close);
  // The state flips after the resize event that announced it, so read it once the resizing has settled.
  let settle;
  window.addEventListener("resize", () => {
    clearTimeout(settle);
    settle = setTimeout(sync, 100);
  });
  sync();
})();
