// Render <time data-ts="unix seconds"> as local time + relative age.
(function () {
  function rel(sec) {
    const a = Math.abs(sec), sign = sec < 0 ? "in " : "";
    const tail = sec < 0 ? "" : " ago";
    if (a < 60) return sign + Math.round(a) + " s" + tail;
    if (a < 3600) return sign + Math.round(a / 60) + " min" + tail;
    if (a < 86400) return sign + (a / 3600).toFixed(1) + " h" + tail;
    return sign + (a / 86400).toFixed(1) + " d" + tail;
  }
  function render() {
    const now = Date.now() / 1000;
    document.querySelectorAll("time[data-ts]").forEach(function (el) {
      const ts = parseFloat(el.dataset.ts);
      if (!isFinite(ts)) return;
      const d = new Date(ts * 1000);
      el.title = d.toISOString();
      el.setAttribute("datetime", d.toISOString());
      el.textContent = el.dataset.mode === "abs"
        ? d.toLocaleString()
        : el.dataset.mode === "both"
          ? d.toLocaleString() + " (" + rel(now - ts) + ")"
          : rel(now - ts);
    });
  }
  render();
  setInterval(render, 15000);
})();
