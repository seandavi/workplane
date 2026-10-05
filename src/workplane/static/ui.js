// Light/dark switch and busy buttons. The saved theme is applied before first paint by the inline
// script in base.html; this file only reacts to clicks.
(() => {
  const root = document.documentElement;
  const toggle = document.querySelector("[data-theme-switch]");
  if (toggle) {
    const sync = () => toggle.setAttribute("aria-checked", String(root.dataset.theme !== "light"));
    sync();
    toggle.addEventListener("click", () => {
      root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
      try { localStorage.setItem("wp-theme", root.dataset.theme); } catch { /* private mode: lasts until reload */ }
      sync();
    });
  }

  // Any form: show a spinner on the submit button until the next page loads, and block double submits.
  document.addEventListener("submit", (event) => {
    if (event.defaultPrevented) return;
    const button = event.submitter;
    if (!button || button.disabled) return;
    // Disable after the event has finished so the browser still submits the form.
    setTimeout(() => {
      button.disabled = true;
      button.classList.add("busy");
      if (button.dataset.busyLabel) {
        button.dataset.label = button.textContent;
        button.textContent = button.dataset.busyLabel;
      }
    }, 0);
  });

  // The back/forward cache restores the page as it was left; undo the busy state.
  addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    for (const button of document.querySelectorAll("button.busy")) {
      button.disabled = false;
      button.classList.remove("busy");
      if (button.dataset.label) button.textContent = button.dataset.label;
    }
  });
})();
