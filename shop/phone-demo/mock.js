/* Демо: сервер цеха в браузере. Перехватывает fetch("/api/…") и отвечает так же, как
   shop/server.py + logic.py на демо-данных (shop/demo.py). Состояние — в памяти страницы. */
const DEMO = (() => {
  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const stamp = (d) => `${ymd(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  const nowS = () => stamp(new Date());
  const today = () => ymd(new Date());
  const inDays = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return ymd(d); };

  class ShopError extends Error {}

  const STAGES = { "C1:NEW": "Новый заказ", "C1:CUT": "Распил", "C1:EDGE": "Кромка", "C1:DRILL": "Присадка",
                   "C1:PACK": "Упаковка", "C1:OTK": "ОТК", "C1:READY": "Готов к отгрузке" };
  const FINAL = "C1:READY";
  const SECTIONS = [
    { id: 1, name: "Распил", stage: "C1:CUT", kind: "regular", skip: false },
    { id: 2, name: "Кромка", stage: "C1:EDGE", kind: "regular", skip: false },
    { id: 3, name: "Присадка", stage: "C1:DRILL", kind: "regular", skip: true },
    { id: 4, name: "Упаковка", stage: "C1:PACK", kind: "regular", skip: false },
    { id: 5, name: "ОТК", stage: "C1:OTK", kind: "otk", skip: false },
  ];
  const OPS = [
    { id: 1, sec: 1, name: "Распил", unit: "п.м.", rate: 25, product: 101 },
    { id: 2, sec: 2, name: "Кромка 0,4 мм", unit: "м", rate: 12, product: 102 },
    { id: 3, sec: 2, name: "Кромка 2 мм", unit: "м", rate: 18, product: 103 },
    { id: 4, sec: 3, name: "Присадка", unit: "отв.", rate: 3, product: 104 },
    { id: 5, sec: 4, name: "Упаковка", unit: "лист", rate: 40, product: 105 },
    { id: 6, sec: 5, name: "Приёмка заказа", unit: "заказ", rate: 150, perDeal: true },
  ];
  const WORKERS = [
    { id: 1, name: "Солех", badge: "W-0001", daily: 2140 },
    { id: 2, name: "Иван", badge: "W-0002", daily: 1890 },
    { id: 3, name: "Рустам", badge: "W-0003", daily: 1620 },
    { id: 4, name: "Алишер", badge: "W-0004", daily: 1750 },
    { id: 5, name: "Дильшод", badge: "W-0005", daily: 1310 },
    { id: 6, name: "Алексей", badge: "W-0006", daily: 0, master: true },  // начальник производства
  ];
  const PLAN = [
    [1201, "Кухня угловая", "C1:CUT", 0, "Иванов Иван", { 101: 64, 102: 38, 103: 22, 104: 180, 105: 6 }],
    [1204, "Шкаф-купе", "C1:CUT", 1, "ООО «Интерьер Плюс»", { 101: 41, 103: 30, 104: 96, 105: 4 }],
    [1207, "Распил 12 листов", "C1:CUT", 3, "Петрова Анна", { 101: 88, 102: 60 }],
    [1210, "Гардероб", "C1:CUT", 6, "Студия «Линия»", { 101: 52, 102: 40, 104: 120, 105: 5 }],
    [1213, "Прихожая", "C1:EDGE", -1, "Ким Виктор", { 101: 30, 102: 25, 103: 10, 104: 64, 105: 3 }],
    [1216, "Тумба ТВ", "C1:EDGE", 2, "Мебель-Сити", { 101: 18, 103: 12, 104: 40, 105: 2 }],
    [1219, "Распил 4 листа", "C1:EDGE", 4, "Абдуллаев Рустам", { 101: 26, 102: 20 }],
    [1222, "Детская", "C1:DRILL", 0, "ООО «Интерьер Плюс»", { 101: 44, 102: 30, 104: 140, 105: 5 }],
    [1225, "Кухня прямая", "C1:DRILL", 5, "Иванов Иван", { 101: 58, 102: 41, 103: 16, 104: 210, 105: 7 }],
    [1228, "Стеллаж", "C1:PACK", 1, "Петрова Анна", { 101: 22, 102: 18, 104: 48, 105: 2 }],
    [1231, "Комод", "C1:PACK", 7, "Студия «Линия»", { 101: 20, 103: 14, 104: 52, 105: 2 }],
    [1234, "Шкаф в спальню", "C1:OTK", 0, "Мебель-Сити", { 101: 47, 102: 33, 104: 110, 105: 4 }],
    [1237, "Кухня", "C1:OTK", 3, "Ким Виктор", { 101: 61, 102: 44, 103: 18, 104: 190, 105: 6 }],
    [1240, "Гардеробная", "C1:READY", 1, "Абдуллаев Рустам", { 101: 70, 102: 50, 104: 160, 105: 6 }],
  ];

  let S;
  function seed() {
    S = { deals: {}, sessions: [], ledger: [], defects: [], notifications: [], nextId: 1 };
    for (const [id, title, stage, off, client, products] of PLAN) {
      S.deals[id] = { id, number: "DG-" + id, title, client, ship: inDays(off), stage, problem: "", products };
    }
    // Прошлые дни месяца — чтобы «за месяц» выглядело как в середине месяца.
    const d = new Date(), past = d.getDate() - 1;
    if (past > 0) {
      const first = `${ymd(d).slice(0, 8)}01 18:00:00`;
      for (const w of WORKERS.filter((x) => x.daily)) S.ledger.push({ worker: w.id, amount: Math.round(w.daily * past / 10) * 10, at: first, kind: "piece" });
    }
    // Утро в цеху: Рустам уже на кромке, у присадки проблема со станком.
    const started = new Date(Math.min(Date.now() - 25 * 60000, new Date().setHours(8, 40, 0, 0)));
    S.sessions.push({ id: S.nextId++, deal: 1216, sec: 2, started: stamp(started), finished: null, result: null, defect: null, workers: [3] });
    S.deals[1225].problem = "Станок неисправен";
  }
  seed();

  const err = (m) => { throw new ShopError(m); };
  const sec = (id) => SECTIONS.find((s) => s.id === Number(id)) || err("Участок не найден");
  const deal = (id) => S.deals[id] || err("Сделка не найдена — возможно, ещё не подтянулась из Битрикса");
  const worker = (id) => WORKERS.find((w) => w.id === Number(id));
  const opsOf = (secId) => OPS.filter((o) => o.sec === secId);
  const fmtQty = (q) => Number(q).toLocaleString("ru-RU", { maximumFractionDigits: 2 }).replace(/\s/g, " ");
  const namesOf = (ids) => ids.map((i) => worker(i).name).sort((a, b) => a.localeCompare(b, "ru"));

  const RU = "йцукенгшщзхъфывапролджэячсмитьбюЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ";
  const EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.QWERTYUIOP[]ASDFGHJKL;'ZXCVBNM,.";
  const fixLayout = (c) => [...c].map((ch) => { const i = RU.indexOf(ch); return i < 0 ? ch : EN[i]; }).join("");
  const variants = (code) => { const c = (code || "").trim(), f = fixLayout(c); return f !== c ? [c, f] : [c]; };

  function findWorker(code) {
    for (const c of variants(code)) {
      const w = WORKERS.find((x) => x.badge.toUpperCase() === c.toUpperCase());
      if (w) return w;
    }
    return null;
  }
  function findDeal(code) {
    for (const c of variants(code)) {
      const byNum = Object.values(S.deals).find((d) => d.number.toUpperCase() === c.toUpperCase());
      if (byNum) return byNum;
      const m = c.match(/\/deal\/details\/(\d+)/) || c.match(/^(\d+)$/) || c.match(/^DG-(\d+)$/i);
      if (m && S.deals[m[1]]) return S.deals[m[1]];
    }
    return null;
  }

  function volumes(d) {
    const out = {};
    for (const op of OPS) if (!op.perDeal && d.products[op.product]) out[op.id] = d.products[op.product];
    return out;
  }
  const secVolumes = (d, secId) => {
    const v = volumes(d);
    return opsOf(secId).filter((op) => !op.perDeal && v[op.id]).map((op) => `${op.name}: ${fmtQty(v[op.id])} ${op.unit}`);
  };
  const applies = (s, v) => !s.skip || opsOf(s.id).filter((o) => !o.perDeal).some((o) => (v[o.id] || 0) > 0);
  function routeAfter(d, secId) {
    const v = volumes(d), i = SECTIONS.findIndex((s) => s.id === secId);
    for (const s of SECTIONS.slice(i + 1)) if (applies(s, v)) return s.stage;
    return FINAL;
  }
  const stagePos = (st) => { const i = [...SECTIONS.map((s) => s.stage), FINAL].indexOf(st); return i < 0 ? null : i; };

  const openSession = (dealId, secId) => S.sessions.find((x) => x.deal === dealId && x.sec === secId && !x.finished);
  const openDefect = (dealId, secId) => S.defects.find((x) => x.deal === dealId && x.sec === secId && !x.resolved);
  function flags() {
    const working = {};
    for (const x of S.sessions.filter((x) => !x.finished)) (working[x.deal] = working[x.deal] || []).push(...namesOf(x.workers));
    for (const k in working) working[k].sort((a, b) => a.localeCompare(b, "ru"));
    return { working, rework: new Set(S.defects.filter((x) => !x.resolved).map((x) => x.deal)) };
  }
  const dealDict = (d, f) => ({ id: d.id, number: d.number, title: d.title, client: d.client, shipDate: d.ship, stageId: d.stage,
                                working: f.working[d.id] || [], problem: d.problem, rework: f.rework.has(d.id) });

  function totals(wid) {
    const t = today(), m = t.slice(0, 7);
    const mine = S.ledger.filter((l) => l.worker === wid);
    const sum = (rows) => Math.round(rows.reduce((a, l) => a + l.amount, 0) * 100) / 100;
    return { today: sum(mine.filter((l) => l.at.slice(0, 10) === t)), month: sum(mine.filter((l) => l.at.slice(0, 7) === m)) };
  }

  function terminal(secId) {
    const s = sec(secId), f = flags();
    const queue = Object.values(S.deals).filter((d) => d.stage === s.stage)
      .sort((a, b) => (a.ship < b.ship ? -1 : a.ship > b.ship ? 1 : a.id - b.id))
      .map((d) => ({ ...dealDict(d, f), volumes: secVolumes(d, s.id) }));
    const open = S.sessions.filter((x) => x.sec === s.id && !x.finished).map((x) => {
      const d = S.deals[x.deal];
      return { session_id: x.id, deal_id: d.id, number: d.number, client: d.client, title: d.title, started_at: x.started,
               rework: !!x.defect, workers: namesOf(x.workers) };
    });
    return {
      section: { id: s.id, name: s.name, kind: s.kind, stage_id: s.stage },
      sections: SECTIONS.map((x) => ({ id: x.id, name: x.name, kind: x.kind })),
      today: today(), queue, open,
      reasons: { remark: ["Скол", "Ошибка в УП", "Брак плиты"], problem: ["Нет материала", "Ошибка в УП", "Станок неисправен", "Другое"],
                 defect: ["Скол", "Не та кромка", "Ошибка присадки", "Царапины", "Другое"] },
      shadow: true,
      sync: { at: nowS(), ok_at: nowS(), error: "", pending: 0 },
    };
  }

  function scan(secId, code) {
    code = String(code || "").trim();
    if (!code) err("Пустой код");
    const w = findWorker(code);
    if (w) return { type: "worker", worker: { id: w.id, name: w.name, master: !!w.master }, ...totals(w.id) };
    const d = findDeal(code);
    if (!d) return { type: "unknown", code };
    const f = flags(), s = openSession(d.id, Number(secId));
    const passed = SECTIONS.filter((x) => x.kind !== "otk").map((x) => {
      const done = S.sessions.filter((y) => y.deal === d.id && y.sec === x.id && ["done", "remark"].includes(y.result));
      return done.length ? { id: x.id, name: x.name, at: done.map((y) => y.finished).sort().pop() } : null;
    }).filter(Boolean);
    return {
      type: "deal",
      deal: { ...dealDict(d, f), stageName: STAGES[d.stage] || d.stage, gone: false, volumes: secVolumes(d, Number(secId)) },
      open_session: s ? { session_id: s.id, started_at: s.started, rework: !!s.defect, workers: namesOf(s.workers), worker_ids: s.workers.slice() } : null,
      rework_here: !!openDefect(d.id, Number(secId)),
      in_queue: d.stage === sec(secId).stage,
      passed,
    };
  }

  function start(secId, dealId, ids, approvedBy) {
    if (!ids || !ids.length) err("Сначала пикните бейдж");
    const s = sec(secId), d = deal(dealId);
    for (const id of ids) if (!worker(id)) err("Бейдж не найден или сотрудник отключён");
    let x = openSession(d.id, s.id);
    const joined = !!x;
    let master = null;
    if (!x && d.stage !== s.stage) {
      master = approvedBy && worker(approvedBy) && worker(approvedBy).master ? worker(approvedBy) : null;
      if (!master) err(`Заказ ${d.number} на этапе «${STAGES[d.stage]}», а не в очереди участка. Начать без очереди можно только с бейджем начальника производства.`);
    }
    if (!x) {
      const df = openDefect(d.id, s.id);
      x = { id: S.nextId++, deal: d.id, sec: s.id, started: nowS(), finished: null, result: null, defect: df ? df.id : null, workers: [] };
      S.sessions.push(x);
    }
    for (const id of ids) if (!x.workers.includes(Number(id))) x.workers.push(Number(id));
    d.problem = "";
    return { session_id: x.id, joined, rework: !!x.defect, workers: namesOf(x.workers), deal: d.number, section: s.name,
             approved_by: master ? master.name : "" };
  }

  function finish(sid, result, reason) {
    if (!["done", "remark", "problem"].includes(result)) err("Неизвестный результат");
    if (result !== "done" && !reason) err("Выберите причину");
    const x = S.sessions.find((y) => y.id === Number(sid)) || err("Сессия не найдена");
    if (x.finished) err("Эта работа уже закрыта");
    const s = sec(x.sec), d = deal(x.deal), at = nowS();
    Object.assign(x, { finished: at, result, reason });
    if (result === "problem") {
      d.problem = reason;
      S.notifications.push(`${d.number}: проблема на участке «${s.name}»: ${reason}`);
      return { result, deal: d.number, earnings: [], warnings: [], next_stage: "", shadow: true };
    }
    const v = volumes(d), n = x.workers.length;
    let paid = true, note = "";
    const df = x.defect ? S.defects.find((y) => y.id === x.defect) : null;
    if (df) { if (df.fault) { paid = false; note = "переделка по браку — не оплачивается"; } else note = "переделка не по вине рабочего"; }
    else if (S.ledger.some((l) => l.kind === "piece" && l.deal === d.id && l.sec === s.id && l.session !== x.id && l.amount > 0)) {
      paid = false; note = "повторно по этому заказу — не оплачивается";
    }
    const lines = [];
    for (const op of opsOf(s.id)) {
      const qty = op.perDeal ? 1 : v[op.id] || 0;
      if (qty <= 0) continue;
      for (const wid of x.workers) lines.push({ worker: wid, amount: paid ? Math.round(qty * op.rate / n * 100) / 100 : 0 });
    }
    for (const ln of lines) S.ledger.push({ ...ln, kind: "piece", deal: d.id, sec: s.id, session: x.id, at });
    let target = "";
    if (df) { df.resolved = at; target = df.returnStage; }
    if (!target) target = routeAfter(d, s.id);
    const cur = stagePos(d.stage), mine = stagePos(s.stage);
    const forward = cur === null || mine === null || cur <= mine || !!df;
    let moved = "";
    if (target && target !== d.stage && forward) { d.stage = target; moved = target; }
    const earnings = x.workers.map((wid) => ({
      worker_id: wid, name: worker(wid).name,
      amount: Math.round(lines.filter((l) => l.worker === wid).reduce((a, l) => a + l.amount, 0) * 100) / 100, ...totals(wid),
    })).sort((a, b) => a.name.localeCompare(b.name, "ru"));
    const warnings = !lines.length && opsOf(s.id).length
      ? ["В заказе нет объёмов для этого участка — сумма 0 ₽. Сообщите Алексею: возможно, товар сделки не сопоставлен с операцией."] : [];
    return { result, deal: d.number, earnings, warnings, next_stage: moved ? STAGES[moved] : "", note: lines.length ? note : "", shadow: true };
  }

  function defect(dealId, secId, reason, fault) {
    if (!reason) err("Выберите причину брака");
    const d = deal(dealId), s = sec(secId);
    const last = S.sessions.filter((y) => y.deal === d.id && y.sec === s.id && ["done", "remark"].includes(y.result))
      .sort((a, b) => (a.finished < b.finished ? 1 : -1))[0];
    const policy = fault ? "unpaid" : "paid";
    S.defects.push({ id: S.nextId++, deal: d.id, sec: s.id, fault, returnStage: d.stage, resolved: null });
    const at = nowS();
    for (const y of S.sessions) {
      if (y.deal === d.id && !y.finished && sec(y.sec).kind === "otk") Object.assign(y, { finished: at, result: "defect", reason });
    }
    if (s.stage !== d.stage) d.stage = s.stage;
    return { defect_id: S.nextId - 1, workers: last ? namesOf(last.workers) : [], deducted: 0,
             policy: { unpaid: "переделка не оплачивается", paid: "не вина рабочего — переделка оплачивается" }[policy] };
  }

  function route(method, path, body) {
    let m;
    if (method === "GET" && path === "/api/info") {
      return { sections: SECTIONS.map((s) => ({ id: s.id, name: s.name, kind: s.kind })), addresses: [], connected: true, demo: true };
    }
    if (method === "GET" && (m = path.match(/^\/api\/terminal\/(\d+)$/))) return terminal(Number(m[1]));
    if (method === "POST" && path === "/api/scan") return scan(body.section_id, body.code);
    if (method === "POST" && path === "/api/start") return start(body.section_id, body.deal_id, (body.worker_ids || []).map(Number), body.approved_by);
    if (method === "POST" && path === "/api/finish") return finish(body.session_id, body.result, String(body.reason || "").trim());
    if (method === "POST" && path === "/api/defect") return defect(body.deal_id, body.section_id, String(body.reason || "").trim(), body.worker_fault !== false);
    return null;
  }

  const realFetch = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = async (input, opts = {}) => {
    const path = String(input).split("?")[0];
    if (!path.startsWith("/api/")) return realFetch(input, opts);
    const method = (opts.method || "GET").toUpperCase();
    let body = {};
    try { body = opts.body ? JSON.parse(opts.body) : {}; } catch (e) { body = {}; }
    await new Promise((r) => setTimeout(r, 120));  // как по Wi-Fi до сервера цеха
    let status = 200, data;
    try {
      data = route(method, path, body);
      if (data === null) { status = 404; data = { error: "Не найдено" }; }
    } catch (e) {
      if (!(e instanceof ShopError)) console.error(e);
      status = e instanceof ShopError ? 400 : 500;
      data = { error: e instanceof ShopError ? e.message : "Внутренняя ошибка: " + e.message };
    }
    return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
  };

  const deals = () => Object.values(S.deals).sort((a, b) => a.id - b.id)
    .map((d) => ({ number: d.number, client: d.client, stage: STAGES[d.stage] || d.stage }));
  return { reset: seed, workers: WORKERS, sections: SECTIONS, deals, notifications: () => S.notifications };
})();
