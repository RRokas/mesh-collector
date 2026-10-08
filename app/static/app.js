// Live dashboard: every REFRESH_MS, re-fetch this page and swap in the
// contents of each [data-live] element (by id) when it changed. In-place
// swaps keep scroll position, chart state and focus, unlike a full reload.
window.MESH_REFRESH_MS = 1000;

// <time data-ts="unix seconds"> -> local time and/or relative age.
function meshRenderTimes(root) {
  const now = Date.now() / 1000;
  (root || document).querySelectorAll("time[data-ts]").forEach(function (el) {
    const ts = parseFloat(el.dataset.ts);
    if (!isFinite(ts)) return;
    const d = new Date(ts * 1000);
    el.title = d.toISOString();
    el.setAttribute("datetime", d.toISOString());
    el.textContent = el.dataset.mode === "abs"
      ? d.toLocaleString()
      : el.dataset.mode === "both"
        ? d.toLocaleString() + " (" + meshRel(now - ts) + ")"
        : meshRel(now - ts);
  });
}

function meshRel(sec) {
  const a = Math.abs(sec), pre = sec < 0 ? "in " : "", post = sec < 0 ? "" : " ago";
  if (a < 60) return pre + Math.round(a) + " s" + post;
  if (a < 3600) return pre + Math.round(a / 60) + " min" + post;
  if (a < 86400) return pre + (a / 3600).toFixed(1) + " h" + post;
  return pre + (a / 86400).toFixed(1) + " d" + post;
}

(function () {
  meshRenderTimes();
  const lastHtml = {};  // id -> last server HTML, so unchanged regions aren't touched
  document.querySelectorAll("[data-live][id]").forEach(function (el) { lastHtml[el.id] = null; });
  let busy = false;

  async function tick() {
    meshRenderTimes();  // keep "3 s ago" ticking even if nothing changed
    if (busy || document.hidden || !Object.keys(lastHtml).length) return;
    busy = true;
    try {
      const r = await fetch(location.href, { cache: "no-store", credentials: "same-origin" });
      if (!r.ok) return;
      const doc = new DOMParser().parseFromString(await r.text(), "text/html");
      Object.keys(lastHtml).forEach(function (id) {
        const fresh = doc.getElementById(id), el = document.getElementById(id);
        if (!fresh || !el || fresh.innerHTML === lastHtml[id]) return;
        lastHtml[id] = fresh.innerHTML;
        el.innerHTML = fresh.innerHTML;
        meshRenderTimes(el);
      });
    } catch (e) {
      // offline or redeploying: try again next tick
    } finally {
      busy = false;
    }
  }

  setInterval(tick, window.MESH_REFRESH_MS);
})();
