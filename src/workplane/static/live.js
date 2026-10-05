// Live updates over server-sent events.
// Every [data-fragment] element re-fetches its fragment URL when a run changes.
// When a run starts or finishes, items move between sections, so the page reloads
// (unless you are typing in a form). The header's [data-live] dot shows the connection state.
(() => {
  const fragments = () => document.querySelectorAll("[data-fragment]");
  const indicator = document.querySelector("[data-live]");
  const setLive = (state) => { if (indicator) indicator.dataset.state = state; };
  let pending = null;

  // A swapped fragment restarts its CSS animations. Start every spinner ring at the same wall-clock
  // phase (negative animation delay, see style.css) so the rings keep turning instead of jumping.
  const SPIN_MS = 900;
  const syncSpinners = () => {
    const phase = Date.now() % SPIN_MS;
    for (const el of document.querySelectorAll(".badge.run-starting, .badge.run-running")) {
      el.style.setProperty("--phase", phase);
    }
  };

  const refresh = () => {
    pending = null;
    for (const el of fragments()) {
      fetch(el.dataset.fragment, { headers: { Accept: "text/html" } })
        .then((r) => (r.ok ? r.text() : null))
        .then((html) => {
          if (html !== null) {
            el.innerHTML = html;
            syncSpinners();
          }
        })
        .catch(() => {});
    }
  };

  const typing = () => {
    const a = document.activeElement;
    return a && ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName) && a.value;
  };

  syncSpinners();
  const source = new EventSource("/api/stream");
  source.onopen = () => setLive("on");
  source.onerror = () => setLive("off"); // EventSource reconnects by itself; onopen flips it back
  source.onmessage = (msg) => {
    let data = {};
    try { data = JSON.parse(msg.data); } catch { return; }
    if ((data.kind === "run_created" || data.kind === "run_finished") && !typing()) {
      location.reload();
      return;
    }
    if (fragments().length && !pending) pending = setTimeout(refresh, 750);
  };
})();
