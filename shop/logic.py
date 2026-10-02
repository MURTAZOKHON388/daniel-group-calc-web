"""
Правила цеха: поиск по скану, объёмы из товаров сделки, маршрут по участкам,
сессии «Начал/Готово», сдельная оплата, брак и месячный отчёт.

Функции работают с открытым соединением SQLite и не ходят в сеть: всё, что
надо отправить в Битрикс, ложится в outbox (см. enqueue), а sync.py отправит.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from db import tx


def now() -> datetime:
    """Текущее время сервера. Тесты подменяют logic.now."""
    return datetime.now()


def now_s() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


class ShopError(Exception):
    """Ошибка, которую можно показать человеку как есть."""


# ================= НАСТРОЙКИ =================

DEFAULTS: dict[str, str] = {
    "webhook": "",
    "category_id": "0",
    "ship_field": "",
    "number_tpl": "DG-{ID}",
    "final_stage_id": "",           # куда сделка уходит после последнего участка
    "board_stages": "[]",           # колонки табло; пусто — все рабочие стадии
    "notify_user_id": "",           # кому в Битриксе приходит «Проблема»
    "remark_reasons": json.dumps(["Скол", "Ошибка в УП", "Брак плиты"], ensure_ascii=False),
    "problem_reasons": json.dumps(["Нет материала", "Ошибка в УП", "Станок неисправен", "Другое"], ensure_ascii=False),
    "defect_reasons": json.dumps(["Скол", "Не та кромка", "Ошибка присадки", "Царапины", "Другое"], ensure_ascii=False),
    "defect_policy": "unpaid",      # unpaid | deduct
    "shadow_mode": "1",
    "admin_pin": "",
    "stages_cache": "[]",           # стадии воронки из Битрикса
    "meta_cache": "{}",             # воронки, поля дат, пользователи — для админки
    "last_sync_at": "",
    "last_sync_ok_at": "",
    "last_sync_error": "",
}

JSON_KEYS = {"board_stages", "remark_reasons", "problem_reasons", "defect_reasons", "stages_cache", "meta_cache"}

POLICY_LABELS = {
    "unpaid": "переделка не оплачивается",
    "deduct": "вычет за участок + переделка не оплачивается",
    "paid": "не вина рабочего — переделка оплачивается",
}


def get_settings(conn: sqlite3.Connection) -> dict:
    out = dict(DEFAULTS)
    for r in conn.execute("SELECT key, value FROM settings"):
        out[r["key"]] = r["value"]
    for k in JSON_KEYS:
        try:
            out[k] = json.loads(out[k]) if isinstance(out[k], str) else out[k]
        except ValueError:
            out[k] = json.loads(DEFAULTS[k])
    return out


def setting(conn: sqlite3.Connection, key: str):
    return get_settings(conn)[key]


def set_settings(conn: sqlite3.Connection, values: dict) -> None:
    """Пишет внутри уже открытой транзакции или в автокоммите."""
    for k, v in values.items():
        if k not in DEFAULTS:
            raise ShopError(f"Неизвестная настройка: {k}")
        if k in JSON_KEYS and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, str(v)),
        )


# ================= СКАНЫ =================

# Сканер «печатает» как клавиатура. Если на планшете включена русская
# раскладка, DG-1234 приходит как ВП-1234 — возвращаем латиницу по клавишам.
_RU = "йцукенгшщзхъфывапролджэячсмитьбю" + "ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ"
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,." + "QWERTYUIOP[]ASDFGHJKL;'ZXCVBNM,."
_LAYOUT = str.maketrans(_RU, _EN)


def fix_layout(code: str) -> str:
    return code.translate(_LAYOUT)


def code_variants(code: str) -> list[str]:
    c = (code or "").strip()
    out = [c]
    fixed = fix_layout(c)
    if fixed != c:
        out.append(fixed)
    return [x for x in out if x]


def number_for(tpl: str, fields: dict) -> str:
    return re.sub(r"\{([A-Z0-9_]+)\}", lambda m: str(fields.get(m.group(1)) or ""), tpl or "DG-{ID}")


def tpl_fields(tpl: str) -> list[str]:
    return re.findall(r"\{([A-Z0-9_]+)\}", tpl or "")


def _id_from_code(code: str, tpl: str) -> int | None:
    m = re.search(r"/deal/details/(\d+)", code)
    if m:
        return int(m.group(1))
    if code.isdigit():
        return int(code)
    if "{ID}" in (tpl or ""):
        rx = re.escape(tpl).replace(r"\{ID\}", r"(?P<id>\d+)")
        rx = re.sub(r"\\\{[A-Z0-9_]+\\\}", ".*?", rx)
        m = re.fullmatch(rx, code, re.I)
        if m:
            return int(m.group("id"))
    return None


def find_worker(conn: sqlite3.Connection, code: str):
    for c in code_variants(code):
        r = conn.execute("SELECT * FROM workers WHERE upper(badge) = upper(?)", (c,)).fetchone()
        if r:
            return r
    return None


def find_deal(conn: sqlite3.Connection, code: str):
    tpl = setting(conn, "number_tpl")
    for c in code_variants(code):
        r = conn.execute(
            "SELECT * FROM deals WHERE upper(number) = upper(?) ORDER BY gone, id DESC LIMIT 1", (c,)
        ).fetchone()
        if r:
            return r
        deal_id = _id_from_code(c, tpl)
        if deal_id is not None:
            r = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
            if r:
                return r
    return None


def next_badge(conn: sqlite3.Connection) -> str:
    n = 1
    for r in conn.execute("SELECT badge FROM workers"):
        m = re.fullmatch(r"W-(\d+)", r["badge"], re.I)
        if m:
            n = max(n, int(m.group(1)) + 1)
    return f"W-{n:04d}"


# ================= ОБЪЁМЫ И МАРШРУТ =================

def product_key(product_id, name) -> str:
    pid = str(product_id or "").strip()
    if pid and pid != "0":
        return "id:" + pid
    return "name:" + " ".join(str(name or "").lower().split())


def deal_volumes(conn: sqlite3.Connection, deal_id: int) -> dict[int, float]:
    """Объём каждой операции по сделке: сумма количеств сопоставленных товаров × коэффициент."""
    out: dict[int, float] = {}
    for r in conn.execute(
        """SELECT m.operation_id, p.quantity * m.factor AS qty
           FROM deal_products p JOIN product_map m ON m.product_key = p.product_key
           WHERE p.deal_id = ? AND m.operation_id IS NOT NULL""",
        (deal_id,),
    ):
        out[r["operation_id"]] = round(out.get(r["operation_id"], 0) + r["qty"], 4)
    return out


def sections(conn: sqlite3.Connection, active_only: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM sections" + (" WHERE active = 1" if active_only else "") + " ORDER BY sort, id"
    return conn.execute(q).fetchall()


def section_ops(conn: sqlite3.Connection, section_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM operations WHERE section_id = ? AND active = 1 ORDER BY id", (section_id,)
    ).fetchall()


def get_section(conn: sqlite3.Connection, section_id) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
    if not r:
        raise ShopError("Участок не найден")
    return r


def get_deal(conn: sqlite3.Connection, deal_id) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
    if not r:
        raise ShopError("Сделка не найдена — возможно, ещё не подтянулась из Битрикса")
    return r


def section_applies(conn: sqlite3.Connection, section: sqlite3.Row, volumes: dict[int, float]) -> bool:
    """Участок нужен заказу, если не стоит «пропускать без объёмов» или в
    заказе есть объём хотя бы одной его операции (без присадки — без присадки)."""
    if not section["skip_if_empty"]:
        return True
    product_ops = [op for op in section_ops(conn, section["id"]) if not op["per_deal"]]
    if not product_ops:
        return True
    return any(volumes.get(op["id"], 0) > 0 for op in product_ops)


def route_after(conn: sqlite3.Connection, deal_id: int, section_id: int) -> str:
    """Стадия следующего нужного заказу участка или финальная стадия."""
    secs = sections(conn)
    vols = deal_volumes(conn, deal_id)
    ids = [s["id"] for s in secs]
    if section_id in ids:
        for s in secs[ids.index(section_id) + 1:]:
            if s["stage_id"] and section_applies(conn, s, vols):
                return s["stage_id"]
    return setting(conn, "final_stage_id") or ""


def stage_pos(conn: sqlite3.Connection, stage_id: str) -> int | None:
    order = [s["stage_id"] for s in sections(conn) if s["stage_id"]]
    final = setting(conn, "final_stage_id")
    if final:
        order.append(final)
    return order.index(stage_id) if stage_id in order else None


def stage_name(conn: sqlite3.Connection, stage_id: str) -> str:
    for s in setting(conn, "stages_cache"):
        if s.get("id") == stage_id:
            return s.get("name") or stage_id
    for s in sections(conn, active_only=False):
        if s["stage_id"] == stage_id:
            return s["name"]
    return stage_id


# ================= OUTBOX =================

def enqueue(conn: sqlite3.Connection, method: str, params: dict, deal_id: int | None = None, kind: str = "") -> None:
    conn.execute(
        "INSERT INTO outbox(method, params, deal_id, kind, created_at) VALUES(?, ?, ?, ?, ?)",
        (method, json.dumps(params, ensure_ascii=False), deal_id, kind, now_s()),
    )


def move_stage(conn: sqlite3.Connection, deal_id: int, stage_id: str) -> None:
    """Меняем стадию у себя сразу (очереди и табло видят это мгновенно),
    в Битрикс — через outbox. Пока запись висит, синк стадию не перетирает."""
    conn.execute("UPDATE deals SET stage_id = ? WHERE id = ?", (stage_id, deal_id))
    enqueue(conn, "crm.deal.update", {"id": deal_id, "fields": {"STAGE_ID": stage_id}}, deal_id, "stage")


def timeline(conn: sqlite3.Connection, deal_id: int, text: str) -> None:
    enqueue(
        conn,
        "crm.timeline.comment.add",
        {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": text}},
        deal_id,
        "comment",
    )


def notify(conn: sqlite3.Connection, text: str, deal_id: int | None = None) -> None:
    user = str(setting(conn, "notify_user_id") or "").strip()
    if user:
        enqueue(conn, "im.notify.system.add", {"USER_ID": user, "MESSAGE": text}, deal_id, "notify")


# ================= СЕССИИ =================

def fmt_qty(q: float) -> str:
    s = f"{q:,.2f}".replace(",", " ").replace(".", ",")
    return s.rstrip("0").rstrip(",") if "," in s else s


def fmt_rub(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


def open_session(conn: sqlite3.Connection, deal_id: int, section_id: int):
    return conn.execute(
        "SELECT * FROM sessions WHERE deal_id = ? AND section_id = ? AND finished_at IS NULL ORDER BY id LIMIT 1",
        (deal_id, section_id),
    ).fetchone()


def session_workers(conn: sqlite3.Connection, session_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT w.* FROM session_workers sw JOIN workers w ON w.id = sw.worker_id
           WHERE sw.session_id = ? ORDER BY w.name""",
        (session_id,),
    ).fetchall()


def open_defect(conn: sqlite3.Connection, deal_id: int, section_id: int):
    return conn.execute(
        "SELECT * FROM defects WHERE deal_id = ? AND section_id = ? AND resolved_at IS NULL ORDER BY id LIMIT 1",
        (deal_id, section_id),
    ).fetchone()


def start_session(conn: sqlite3.Connection, section_id: int, deal_id: int, worker_ids: list[int]) -> dict:
    """«Начал». Если на участке по заказу уже идёт работа — рабочие присоединяются к ней."""
    if not worker_ids:
        raise ShopError("Сначала пикните бейдж")
    with tx(conn):
        section = get_section(conn, section_id)
        deal = get_deal(conn, deal_id)
        for wid in worker_ids:
            w = conn.execute("SELECT * FROM workers WHERE id = ? AND active = 1", (wid,)).fetchone()
            if not w:
                raise ShopError("Бейдж не найден или сотрудник отключён")
        s = open_session(conn, deal_id, section_id)
        joined = bool(s)
        if not s:
            defect = open_defect(conn, deal_id, section_id)
            cur = conn.execute(
                "INSERT INTO sessions(deal_id, section_id, started_at, defect_id) VALUES(?, ?, ?, ?)",
                (deal_id, section_id, now_s(), defect["id"] if defect else None),
            )
            s = conn.execute("SELECT * FROM sessions WHERE id = ?", (cur.lastrowid,)).fetchone()
        for wid in worker_ids:
            conn.execute("INSERT OR IGNORE INTO session_workers(session_id, worker_id) VALUES(?, ?)", (s["id"], wid))
        conn.execute("UPDATE deals SET problem = '' WHERE id = ?", (deal_id,))
        names = [w["name"] for w in session_workers(conn, s["id"])]
    return {
        "session_id": s["id"],
        "joined": joined,
        "rework": bool(s["defect_id"]),
        "workers": names,
        "deal": deal["number"],
        "section": section["name"],
    }


def _already_paid(conn: sqlite3.Connection, deal_id: int, section_id: int, session_id: int) -> bool:
    return bool(conn.execute(
        """SELECT 1 FROM ledger WHERE kind = 'piece' AND deal_id = ? AND section_id = ?
           AND session_id != ? AND amount > 0 LIMIT 1""",
        (deal_id, section_id, session_id),
    ).fetchone())


def compute_pay(conn: sqlite3.Connection, session: sqlite3.Row, workers: list[sqlite3.Row]) -> tuple[list[dict], list[str]]:
    """Строки начисления: объём операции × ставка, поровну на всех участников."""
    section_id, deal_id = session["section_id"], session["deal_id"]
    vols = deal_volumes(conn, deal_id)
    n = len(workers)
    paid, note, warnings = True, "", []
    if session["defect_id"]:
        d = conn.execute("SELECT * FROM defects WHERE id = ?", (session["defect_id"],)).fetchone()
        if d and d["worker_fault"]:
            paid, note = False, "переделка по браку — не оплачивается"
        else:
            note = "переделка не по вине рабочего"
    elif _already_paid(conn, deal_id, section_id, session["id"]):
        paid, note = False, "повторно по этому заказу — не оплачивается"
    lines = []
    for op in section_ops(conn, section_id):
        qty = 1.0 if op["per_deal"] else vols.get(op["id"], 0.0)
        if qty <= 0:
            continue
        for w in workers:
            lines.append({
                "worker_id": w["id"],
                "operation_id": op["id"],
                "op_name": op["name"],
                "qty": qty,
                "unit": op["unit"],
                "rate": op["rate"],
                "share": 1 / n,
                "amount": round(qty * op["rate"] / n, 2) if paid else 0.0,
                "note": note,
            })
    if not lines and section_ops(conn, section_id):
        warnings.append("В заказе нет объёмов для этого участка — сумма 0 ₽. Сообщите Алексею: "
                        "возможно, товар сделки не сопоставлен с операцией.")
    return lines, warnings


def worker_totals(conn: sqlite3.Connection, worker_id: int) -> dict:
    today = now().strftime("%Y-%m-%d")
    month = today[:7]
    t = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE worker_id = ? AND substr(created_at, 1, 10) = ?",
        (worker_id, today),
    ).fetchone()[0]
    m = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE worker_id = ? AND month = ?", (worker_id, month)
    ).fetchone()[0]
    return {"today": round(t, 2), "month": round(m, 2)}


def finish_session(conn: sqlite3.Connection, session_id: int, result: str, reason: str = "", comment: str = "") -> dict:
    """«Готово», «Готово с замечанием» или «Проблема»."""
    if result not in ("done", "remark", "problem"):
        raise ShopError("Неизвестный результат")
    if result in ("remark", "problem") and not reason:
        raise ShopError("Выберите причину")
    with tx(conn):
        s = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not s:
            raise ShopError("Сессия не найдена")
        if s["finished_at"]:
            raise ShopError("Эта работа уже закрыта")
        section = get_section(conn, s["section_id"])
        deal = get_deal(conn, s["deal_id"])
        workers = session_workers(conn, s["id"])
        names = ", ".join(w["name"] for w in workers)
        ts = now_s()
        conn.execute(
            "UPDATE sessions SET finished_at = ?, result = ?, reason = ?, comment = ? WHERE id = ?",
            (ts, result, reason, comment, s["id"]),
        )

        if result == "problem":
            conn.execute("UPDATE deals SET problem = ? WHERE id = ?", (reason, deal["id"]))
            text = f"⚠ Проблема на участке «{section['name']}»: {reason}."
            if comment:
                text += f" {comment}."
            text += f" Отметил: {names}."
            timeline(conn, deal["id"], text)
            notify(conn, f"{deal['number']} ({deal['client'] or deal['title']}): {text}", deal["id"])
            return {"result": result, "deal": deal["number"], "earnings": [], "warnings": [], "next_stage": ""}

        lines, warnings = compute_pay(conn, s, workers)
        month = ts[:7]
        for ln in lines:
            conn.execute(
                """INSERT INTO ledger(month, worker_id, kind, session_id, deal_id, section_id, operation_id,
                                      qty, unit, rate, share, amount, note, created_at)
                   VALUES(?, ?, 'piece', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (month, ln["worker_id"], s["id"], deal["id"], section["id"], ln["operation_id"],
                 ln["qty"], ln["unit"], ln["rate"], ln["share"], ln["amount"], ln["note"], ts),
            )

        # Куда дальше: после переделки — туда, откуда вернули; иначе — на следующий участок.
        target = ""
        if s["defect_id"]:
            d = conn.execute("SELECT * FROM defects WHERE id = ?", (s["defect_id"],)).fetchone()
            conn.execute("UPDATE defects SET resolved_at = ? WHERE id = ?", (ts, s["defect_id"]))
            target = d["return_stage_id"] if d else ""
        if not target:
            target = route_after(conn, deal["id"], section["id"])
        moved = ""
        cur_pos = stage_pos(conn, deal["stage_id"])
        sec_pos = stage_pos(conn, section["stage_id"]) if section["stage_id"] else None
        # Двигаем только вперёд: если сделку уже передвинули дальше руками — не трогаем.
        forward = cur_pos is None or sec_pos is None or cur_pos <= sec_pos or s["defect_id"]
        if target and target != deal["stage_id"] and forward:
            move_stage(conn, deal["id"], target)
            moved = target

        vol_text = "; ".join(sorted({f"{ln['op_name']} {fmt_qty(ln['qty'])} {ln['unit']}" for ln in lines}))
        head = "готово" if result == "done" else f"готово с замечанием: {reason}"
        text = f"{section['name']} — {head}. {names}."
        if comment:
            text += f" {comment}."
        if vol_text:
            text += f" {vol_text}."
        if s["defect_id"]:
            text += " Переделка по браку."
        timeline(conn, deal["id"], text)

        earnings = []
        for w in workers:
            amount = round(sum(ln["amount"] for ln in lines if ln["worker_id"] == w["id"]), 2)
            earnings.append({"worker_id": w["id"], "name": w["name"], "amount": amount, **worker_totals(conn, w["id"])})
    return {
        "result": result,
        "deal": deal["number"],
        "earnings": earnings,
        "warnings": warnings,
        "next_stage": stage_name(conn, moved) if moved else "",
        "note": lines[0]["note"] if lines else "",
    }


def cancel_session(conn: sqlite3.Connection, session_id: int) -> None:
    with tx(conn):
        conn.execute(
            "UPDATE sessions SET finished_at = ?, result = 'cancelled' WHERE id = ? AND finished_at IS NULL",
            (now_s(), session_id),
        )


# ================= БРАК =================

def passed_sections(conn: sqlite3.Connection, deal_id: int) -> list[dict]:
    """Участки, которые уже сделали этот заказ, — из них ОТК выбирает виноватый."""
    rows = conn.execute(
        """SELECT s.id, s.name, MAX(x.finished_at) AS at FROM sessions x JOIN sections s ON s.id = x.section_id
           WHERE x.deal_id = ? AND x.result IN ('done', 'remark') AND s.kind != 'otk'
           GROUP BY s.id ORDER BY s.sort, s.id""",
        (deal_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def report_defect(conn: sqlite3.Connection, deal_id: int, section_id: int, reason: str,
                  worker_fault: bool = True, reported_by: str = "") -> dict:
    if not reason:
        raise ShopError("Выберите причину брака")
    with tx(conn):
        deal = get_deal(conn, deal_id)
        section = get_section(conn, section_id)
        sess = conn.execute(
            """SELECT * FROM sessions WHERE deal_id = ? AND section_id = ? AND result IN ('done', 'remark')
               ORDER BY finished_at DESC, id DESC LIMIT 1""",
            (deal_id, section_id),
        ).fetchone()
        workers = session_workers(conn, sess["id"]) if sess else []
        policy = setting(conn, "defect_policy") if worker_fault else "paid"
        ts = now_s()
        cur = conn.execute(
            """INSERT INTO defects(deal_id, section_id, session_id, reason, worker_fault, policy,
                                   return_stage_id, reported_at, reported_by)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (deal_id, section_id, sess["id"] if sess else None, reason, 1 if worker_fault else 0, policy,
             deal["stage_id"], ts, reported_by),
        )
        defect_id = cur.lastrowid
        for w in workers:
            conn.execute("INSERT OR IGNORE INTO defect_workers(defect_id, worker_id) VALUES(?, ?)", (defect_id, w["id"]))

        deducted = 0.0
        if worker_fault and policy == "deduct" and sess:
            for row in conn.execute(
                "SELECT * FROM ledger WHERE session_id = ? AND kind = 'piece' AND amount > 0", (sess["id"],)
            ).fetchall():
                conn.execute(
                    """INSERT INTO ledger(month, worker_id, kind, session_id, deal_id, section_id, operation_id,
                                          qty, unit, rate, share, amount, note, ref_id, created_at)
                       VALUES(?, ?, 'defect', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts[:7], row["worker_id"], sess["id"], deal_id, section_id, row["operation_id"], row["qty"],
                     row["unit"], row["rate"], row["share"], -row["amount"], f"брак: {reason}", row["id"], ts),
                )
                deducted += row["amount"]

        # ОТК мог начать приёмку («Начал») — брак её завершает: без оплаты и без
        # смены стадии, заказ уже возвращается на участок. Принимать заново после переделки.
        conn.execute(
            """UPDATE sessions SET finished_at = ?, result = 'defect', reason = ?
               WHERE deal_id = ? AND finished_at IS NULL
               AND section_id IN (SELECT id FROM sections WHERE kind = 'otk')""",
            (ts, reason, deal_id),
        )
        if section["stage_id"] and section["stage_id"] != deal["stage_id"]:
            move_stage(conn, deal_id, section["stage_id"])
        names = ", ".join(w["name"] for w in workers) or "нет отметок"
        fault = "вина рабочего" if worker_fault else "не вина рабочего"
        timeline(conn, deal_id, f"Брак при приёмке ОТК: участок «{section['name']}», {reason} ({fault}). "
                                f"Делали: {names}. Заказ возвращён на участок.")
    return {"defect_id": defect_id, "workers": [w["name"] for w in workers], "deducted": round(deducted, 2),
            "policy": POLICY_LABELS.get(policy, policy)}


# ================= ОЧЕРЕДЬ И ТАБЛО =================

def _deal_flags(conn: sqlite3.Connection) -> tuple[dict, set]:
    working: dict[int, list[str]] = {}
    for r in conn.execute(
        """SELECT x.deal_id, w.name FROM sessions x
           JOIN session_workers sw ON sw.session_id = x.id JOIN workers w ON w.id = sw.worker_id
           WHERE x.finished_at IS NULL ORDER BY w.name"""
    ):
        working.setdefault(r["deal_id"], []).append(r["name"])
    rework = {r[0] for r in conn.execute("SELECT DISTINCT deal_id FROM defects WHERE resolved_at IS NULL")}
    return working, rework


def deal_dict(r: sqlite3.Row, working: dict, rework: set) -> dict:
    return {
        "id": r["id"],
        "number": r["number"],
        "title": r["title"],
        "client": r["client"],
        "shipDate": r["ship_date"],
        "stageId": r["stage_id"],
        "working": working.get(r["id"], []),
        "problem": r["problem"],
        "rework": r["id"] in rework,
    }


def board(conn: sqlite3.Connection) -> dict:
    s = get_settings(conn)
    stages = s["stages_cache"]
    wanted = s["board_stages"]
    if wanted:
        stages = [x for x in stages if x.get("id") in wanted]
    else:
        stages = [x for x in stages if x.get("semantics") not in ("S", "F", "success", "failure")]
    working, rework = _deal_flags(conn)
    deals = [deal_dict(r, working, rework) for r in conn.execute("SELECT * FROM deals WHERE gone = 0")]
    return {
        "stages": [{"id": x["id"], "name": x["name"], "color": x.get("color", "")} for x in stages],
        "deals": deals,
        "syncedAt": s["last_sync_ok_at"],
    }


def queue(conn: sqlite3.Connection, section_id: int) -> list[dict]:
    section = get_section(conn, section_id)
    working, rework = _deal_flags(conn)
    rows = conn.execute(
        """SELECT * FROM deals WHERE gone = 0 AND stage_id = ? AND stage_id != ''
           ORDER BY ship_date IS NULL, ship_date, id""",
        (section["stage_id"],),
    ).fetchall()
    ops = section_ops(conn, section_id)
    out = []
    for r in rows:
        d = deal_dict(r, working, rework)
        d["volumes"] = section_volumes(conn, r["id"], ops)
        out.append(d)
    return out


def section_volumes(conn: sqlite3.Connection, deal_id: int, ops: list[sqlite3.Row]) -> list[str]:
    """Объёмы заказа по операциям участка — для очереди и карточки на терминале."""
    vols = deal_volumes(conn, deal_id)
    return [f"{op['name']}: {fmt_qty(vols[op['id']])} {op['unit']}"
            for op in ops if not op["per_deal"] and vols.get(op["id"])]


# ================= ОТЧЁТ =================

def check_month(month: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise ShopError("Неверный месяц")
    return month


def month_closed(conn: sqlite3.Connection, month: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM months WHERE month = ?", (month,)).fetchone())


def _check_open(conn: sqlite3.Connection, month: str) -> None:
    if month_closed(conn, month):
        raise ShopError(f"Месяц {month} закрыт — правки запрещены")


def effective_amount(conn: sqlite3.Connection, ledger_id: int) -> float:
    r = conn.execute(
        "SELECT amount + COALESCE((SELECT SUM(amount) FROM ledger WHERE ref_id = l.id AND kind = 'adjust'), 0) "
        "FROM ledger l WHERE id = ?",
        (ledger_id,),
    ).fetchone()
    if not r:
        raise ShopError("Строка не найдена")
    return round(r[0], 2)


def adjust_line(conn: sqlite3.Connection, ledger_id: int, new_amount: float, note: str) -> None:
    """Правка спорной строки: исходная не меняется, добавляется разница с причиной."""
    if not note.strip():
        raise ShopError("Укажите причину правки")
    with tx(conn):
        row = conn.execute("SELECT * FROM ledger WHERE id = ?", (ledger_id,)).fetchone()
        if not row or row["kind"] == "adjust":
            raise ShopError("Эту строку нельзя править")
        _check_open(conn, row["month"])
        diff = round(float(new_amount) - effective_amount(conn, ledger_id), 2)
        if diff == 0:
            return
        conn.execute(
            """INSERT INTO ledger(month, worker_id, kind, session_id, deal_id, section_id, operation_id,
                                  amount, note, ref_id, created_at)
               VALUES(?, ?, 'adjust', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["month"], row["worker_id"], row["session_id"], row["deal_id"], row["section_id"],
             row["operation_id"], diff, note.strip(), row["id"], now_s()),
        )


def manual_line(conn: sqlite3.Connection, month: str, worker_id: int, amount: float, note: str) -> None:
    """Ручное начисление или удержание (наладка, уборка, премия)."""
    if not note.strip():
        raise ShopError("Укажите, за что")
    check_month(month)
    with tx(conn):
        _check_open(conn, month)
        if not conn.execute("SELECT 1 FROM workers WHERE id = ?", (worker_id,)).fetchone():
            raise ShopError("Сотрудник не найден")
        conn.execute(
            "INSERT INTO ledger(month, worker_id, kind, amount, note, created_at) VALUES(?, ?, 'manual', ?, ?, ?)",
            (month, worker_id, round(float(amount), 2), note.strip(), now_s()),
        )


def close_month(conn: sqlite3.Connection, month: str) -> None:
    """Оклады и теневой режим фиксируются: выключат режим позже — этот месяц останется теневым."""
    if check_month(month) >= now().strftime("%Y-%m"):
        raise ShopError("Закрыть можно только прошедший месяц")
    with tx(conn):
        _check_open(conn, month)
        salaries = {str(r["id"]): r["salary"] for r in conn.execute("SELECT id, salary FROM workers")}
        conn.execute("INSERT INTO months(month, closed_at, salaries, shadow) VALUES(?, ?, ?, ?)",
                     (month, now_s(), json.dumps(salaries), 1 if setting(conn, "shadow_mode") == "1" else 0))


def reopen_month(conn: sqlite3.Connection, month: str) -> None:
    check_month(month)
    with tx(conn):
        conn.execute("DELETE FROM months WHERE month = ?", (month,))


def report_months(conn: sqlite3.Connection) -> list[dict]:
    """Месяцы, за которые есть строки или брак, закрытые и текущий — новые сверху."""
    closed = {r[0] for r in conn.execute("SELECT month FROM months")}
    found = closed | {now().strftime("%Y-%m")}
    found |= {r[0] for r in conn.execute("SELECT DISTINCT month FROM ledger")}
    found |= {r[0] for r in conn.execute("SELECT DISTINCT substr(reported_at, 1, 7) FROM defects")}
    return [{"month": m, "closed": m in closed} for m in sorted(found, reverse=True)]


def month_report(conn: sqlite3.Connection, month: str) -> dict:
    check_month(month)
    closed = conn.execute("SELECT * FROM months WHERE month = ?", (month,)).fetchone()
    snap = json.loads(closed["salaries"]) if closed else None

    lines = []
    for r in conn.execute(
        """SELECT l.*, w.name AS worker, d.number AS deal_number, d.client AS client,
                  s.name AS section, o.name AS operation
           FROM ledger l JOIN workers w ON w.id = l.worker_id
           LEFT JOIN deals d ON d.id = l.deal_id
           LEFT JOIN sections s ON s.id = l.section_id
           LEFT JOIN operations o ON o.id = l.operation_id
           WHERE l.month = ? ORDER BY l.created_at, l.id""",
        (month,),
    ):
        lines.append(dict(r))

    partners: dict[int, list[str]] = {}
    for r in conn.execute(
        """SELECT sw.session_id, w.name FROM session_workers sw JOIN workers w ON w.id = sw.worker_id
           WHERE sw.session_id IN (SELECT DISTINCT session_id FROM ledger WHERE month = ? AND session_id IS NOT NULL)""",
        (month,),
    ):
        partners.setdefault(r["session_id"], []).append(r["name"])
    adjusted: dict[int, float] = {}
    for ln in lines:
        if ln["kind"] == "adjust" and ln["ref_id"]:
            adjusted[ln["ref_id"]] = round(adjusted.get(ln["ref_id"], 0) + ln["amount"], 2)
    for ln in lines:
        ln["partners"] = [n for n in partners.get(ln["session_id"], []) if n != ln["worker"]]
        ln["adjusted"] = adjusted.get(ln["id"], 0)

    defects = [dict(r) for r in conn.execute(
        """SELECT f.*, d.number AS deal_number, s.name AS section,
                  (SELECT group_concat(w.name, ', ') FROM defect_workers dw JOIN workers w ON w.id = dw.worker_id
                   WHERE dw.defect_id = f.id) AS workers
           FROM defects f LEFT JOIN deals d ON d.id = f.deal_id LEFT JOIN sections s ON s.id = f.section_id
           WHERE substr(f.reported_at, 1, 7) = ? ORDER BY f.reported_at""",
        (month,),
    )]
    # Брак по вине рабочего: человек × участок. Имена в defects[].workers склеены
    # через запятую — для статистики не резать их, а брать отсюда.
    matrix = [dict(r) for r in conn.execute(
        """SELECT dw.worker_id, w.name AS worker, f.section_id, s.name AS section, COUNT(*) AS count
           FROM defect_workers dw JOIN defects f ON f.id = dw.defect_id
           JOIN sections s ON s.id = f.section_id JOIN workers w ON w.id = dw.worker_id
           WHERE f.worker_fault = 1 AND substr(f.reported_at, 1, 7) = ?
           GROUP BY dw.worker_id, f.section_id ORDER BY s.sort, s.id, w.name""",
        (month,),
    )]
    defect_count: dict[int, int] = {}
    for r in conn.execute(
        """SELECT dw.worker_id, COUNT(*) AS n FROM defect_workers dw JOIN defects f ON f.id = dw.defect_id
           WHERE substr(f.reported_at, 1, 7) = ? AND f.worker_fault = 1 GROUP BY dw.worker_id""",
        (month,),
    ):
        defect_count[r["worker_id"]] = r["n"]

    people = []
    ids_with_rows = {ln["worker_id"] for ln in lines}
    for w in conn.execute("SELECT * FROM workers ORDER BY name"):
        if not w["active"] and w["id"] not in ids_with_rows:
            continue
        mine = [ln for ln in lines if ln["worker_id"] == w["id"]]
        salary = snap.get(str(w["id"]), 0) if snap is not None else w["salary"]
        piece = sum(ln["amount"] for ln in mine if ln["kind"] == "piece")
        defect = sum(ln["amount"] for ln in mine if ln["kind"] == "defect")
        adjust = sum(ln["amount"] for ln in mine if ln["kind"] in ("adjust", "manual"))
        deals = {ln["deal_id"] for ln in mine if ln["kind"] == "piece" and ln["deal_id"]}
        people.append({
            "worker_id": w["id"],
            "name": w["name"],
            "salary": round(salary, 2),
            "piece": round(piece, 2),
            "defect": round(defect, 2),
            "adjust": round(adjust, 2),
            "total": round(salary + piece + defect + adjust, 2),
            "deals": len(deals),
            "defects": defect_count.get(w["id"], 0),
        })

    by_section: dict[str, int] = {}
    for d in defects:
        by_section[d["section"] or "?"] = by_section.get(d["section"] or "?", 0) + 1

    return {
        "month": month,
        "closed": bool(closed),
        "closed_at": closed["closed_at"] if closed else "",
        # Закрытый месяц — каким был при закрытии, открытый — по текущей настройке.
        "shadow": bool(closed["shadow"]) if closed else setting(conn, "shadow_mode") == "1",
        "people": people,
        "lines": lines,
        "defects": defects,
        "defects_by_section": by_section,
        "defect_matrix": matrix,
    }
