/* Общие помощники экранов цехового сервера. */
function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function fmt(n) {
  return (n === null || n === undefined || isNaN(n)) ? "—" : Number(n).toLocaleString("ru-RU", { maximumFractionDigits: 0 });
}
function fmt2(n) {
  return (n === null || n === undefined || isNaN(n)) ? "—" : Number(n).toLocaleString("ru-RU", { maximumFractionDigits: 2 });
}
function rub(n) { return fmt(n) + " ₽"; }

const PIN_KEY = "dg_shop_pin";
function ssGet(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
function ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) { /* без памяти — спросим снова */ } }

/* Запрос к API. body === undefined — GET, иначе POST с JSON.
   Справочники и отчёт закрыты PIN: при 401 спрашиваем и повторяем. */
async function api(path, body, raw) {
  const headers = {};
  const pin = ssGet(PIN_KEY);
  if (pin) headers["X-Pin"] = pin;
  const opts = { headers, cache: "no-store" };
  if (body !== undefined) {
    opts.method = "POST";
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try { r = await fetch(path, opts); } catch (e) { throw new Error("Нет связи с сервером цеха"); }
  if (r.status === 401) {
    const j = await r.json().catch(() => ({}));
    if (j.pin) {
      const entered = prompt(pin ? "Неверный PIN. Введите PIN справочников:" : "Введите PIN справочников:");
      if (entered === null) throw new Error("Нужен PIN");
      ssSet(PIN_KEY, entered);
      return api(path, body, raw);
    }
  }
  if (raw && r.ok) return r;
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "Ошибка " + r.status);
  return j;
}

let toastTimer = null;
function toast(msg, kind) {
  let el = $("toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "toast";
    el.setAttribute("role", "status");
    document.body.appendChild(el);
  }
  el.className = "toast" + (kind === "err" ? " err" : "");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === "err" ? 6000 : 2500);
}

/* "2026-10-02 14:05:00" → "02.10 14:05" */
function shortTs(ts) {
  if (!ts) return "—";
  const m = String(ts).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
  return m ? `${m[3]}.${m[2]} ${m[4]}:${m[5]}` : ts;
}
function shortDate(d) {
  if (!d) return "—";
  const m = String(d).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[3]}.${m[2]}.${m[1]}` : d;
}
