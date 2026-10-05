// Live updates over server-sent events.
// Every [data-fragment] element re-fetches its fragment URL when a run changes.
// When a run starts or finishes, items move between sections, so the page reloads
// (unless you are typing in a form).
(() => {
  const fragments = () => document.querySelectorAll("[data-fragment]");
  let pending = null;

  const refresh = () => {
    pending = null;
    for (const el of fragments()) {
      fetch(el.dataset.fragment, { headers: { Accept: "text/html" } })
        .then((r) => (r.ok ? r.text() : null))
        .then((html) => { if (html !== null) el.innerHTML = html; })
        .catch(() => {});
    }
  };

  const typing = () => {
    const a = document.activeElement;
    return a && ["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName) && a.value;
  };

  const source = new EventSource("/api/stream");
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
