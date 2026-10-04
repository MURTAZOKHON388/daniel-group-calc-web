/* Демо: у каждого участка своя ссылка (#raspil, #kromka …) и тест-сканер, если нет камеры. */
(() => {
  const SLUG = { 1: "raspil", 2: "kromka", 3: "prisadka", 4: "upakovka", 5: "otk" };
  const BY_SLUG = Object.fromEntries(Object.entries(SLUG).map(([id, s]) => [s, Number(id)]));
  window.demoSlug = (id) => SLUG[id] || "";
  window.addEventListener("hashchange", () => {
    const id = BY_SLUG[location.hash.slice(1)];
    if (id && id !== st.sectionId) openSection(id);
  });

  function renderCodes() {
    $("demoBadges").innerHTML = DEMO.workers.map((w) =>
      `<button type="button" class="demo-code ${w.master ? "master" : ""}" data-code="${esc(w.badge)}"><b>${esc(w.badge)}</b>
        <span>${esc(w.name)}${w.master ? " — начальник производства" : ""}</span></button>`).join("");
    $("demoDeals").innerHTML = DEMO.deals().map((d) =>
      `<button type="button" class="demo-code" data-code="${esc(d.number)}"><b>${esc(d.number)}</b>
        <span>этап: ${esc(d.stage)} · ${esc(d.client)}</span></button>`).join("");
  }
  const open = () => { renderCodes(); $("demoSheet").hidden = false; $("demoSheet").scrollTop = 0; };
  const close = () => { $("demoSheet").hidden = true; };
  $("demoScan").addEventListener("click", () => { if (!$("app").hidden) open(); });
  $("demoClose").addEventListener("click", close);
  $("demoSheet").addEventListener("click", (e) => {
    const b = e.target.closest("[data-code]");
    if (!b) return;
    close();
    submitScan(b.dataset.code);
    window.scrollTo({ top: 0 });
  });
  $("demoReset").addEventListener("click", () => { DEMO.reset(); if (st.sectionId) { goIdle(); refresh(); } });
})();
