/* Если на этом устройстве что-то сломалось — показать текст ошибки, а не пустой экран. */
(() => {
  function show(msg) {
    let el = document.getElementById("demoError");
    if (!el) {
      el = document.createElement("div");
      el.id = "demoError";
      el.setAttribute("role", "alert");
      el.style.cssText = "margin:16px;padding:14px 16px;border-radius:10px;border:1px solid var(--err,#b3261e);" +
        "background:var(--err-soft,#fbe4e1);color:var(--text,#1c1c1a);font:16px/1.4 system-ui,sans-serif";
      document.body.prepend(el);
    }
    el.textContent = "Страница не запустилась на этом устройстве. Пришлите, пожалуйста, скриншот: " + msg;
  }
  window.addEventListener("error", (e) => show(e.message || String(e.error)));
  window.addEventListener("unhandledrejection", (e) => show((e.reason && e.reason.message) || String(e.reason)));
})();
