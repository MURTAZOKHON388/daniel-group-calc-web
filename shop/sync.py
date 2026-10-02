"""
Синхронизация с Битриксом в фоне: сначала отправить накопленное (outbox),
потом забрать сделки воронки с товарами. Раз в минуту и сразу после
отметки на терминале (wake), чтобы стадия в Битриксе менялась без задержки.

Нет интернета — цех продолжает работать на кэше, outbox копится и уйдёт,
когда связь вернётся.
"""

from __future__ import annotations

import json
import threading
import time
import traceback

import db
import logic
from bitrix import Bitrix, BitrixError, BitrixOffline

MAX_ATTEMPTS = 5  # после стольких ошибок по существу запись помечается failed


def stage_entity(category_id: str) -> str:
    return "DEAL_STAGE" if str(category_id) == "0" else f"DEAL_STAGE_{category_id}"


def parse_date(value) -> str | None:
    """Дата из Битрикса: '2026-10-02T03:00:00+03:00' или '02.10.2026' → '2026-10-02'."""
    if not value:
        return None
    s = str(value)
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    if len(s) >= 10 and s[2] == "." and s[5] == ".":
        return f"{s[6:10]}-{s[3:5]}-{s[0:2]}"
    return None


def _chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def flush_outbox(conn, client) -> dict:
    """Отправляет очередь по порядку. Нет связи — останавливаемся до следующего раза."""
    sent = failed = 0
    rows = conn.execute("SELECT * FROM outbox WHERE status = 'pending' ORDER BY id").fetchall()
    for r in rows:
        try:
            client.call(r["method"], json.loads(r["params"]))
        except BitrixOffline as e:
            conn.execute("UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?", (str(e), r["id"]))
            return {"sent": sent, "failed": failed, "offline": True, "error": str(e)}
        except BitrixError as e:
            status = "failed" if r["attempts"] + 1 >= MAX_ATTEMPTS else "pending"
            failed += status == "failed"
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ?, status = ? WHERE id = ?",
                (str(e), status, r["id"]),
            )
            continue
        conn.execute("UPDATE outbox SET status = 'done', done_at = ?, last_error = '' WHERE id = ?",
                     (logic.now_s(), r["id"]))
        sent += 1
    return {"sent": sent, "failed": failed, "offline": False, "error": ""}


def fetch_meta(client) -> dict:
    """Воронки, поля-даты и пользователи — для выпадающих списков в админке."""
    cats = client.list_all("crm.category.list", {"entityTypeId": 2}, pick=lambda r: (r or {}).get("categories"))
    categories = [{"id": str(c["id"]), "name": c["name"]} for c in cats]
    if not any(c["id"] == "0" for c in categories):
        categories.insert(0, {"id": "0", "name": "Общая"})
    fields = client.call("crm.deal.fields").get("result") or {}
    date_fields = [
        {"code": code, "label": f.get("formLabel") or f.get("listLabel") or f.get("title") or code}
        for code, f in fields.items() if f.get("type") in ("date", "datetime")
    ]
    other_fields = [
        {"code": code, "label": f.get("formLabel") or f.get("listLabel") or f.get("title") or code}
        for code, f in fields.items() if f.get("type") in ("string", "integer") and code.startswith("UF_")
    ]
    users = []
    try:
        for u in client.list_all("user.get", {"ACTIVE": True}, limit=500):
            name = " ".join(x for x in (u.get("LAST_NAME"), u.get("NAME")) if x) or u.get("EMAIL") or str(u.get("ID"))
            users.append({"id": str(u["ID"]), "name": name})
    except BitrixOffline:
        raise
    except BitrixError:
        pass  # у вебхука нет права user — тогда ID вводят руками
    stages_by_cat = {}
    for c in categories:
        stages_by_cat[c["id"]] = fetch_stages(client, c["id"])
    return {"categories": categories, "date_fields": date_fields, "other_fields": other_fields,
            "users": users, "stages": stages_by_cat}


def fetch_stages(client, category_id: str) -> list[dict]:
    rows = client.list_all("crm.status.list", {"order": {"SORT": "ASC"}, "filter": {"ENTITY_ID": stage_entity(category_id)}})
    out = []
    for r in rows:
        extra = r.get("EXTRA") or {}
        out.append({
            "id": r["STATUS_ID"],
            "name": r["NAME"],
            "color": extra.get("COLOR") or r.get("COLOR") or "",
            "semantics": extra.get("SEMANTICS") or r.get("SEMANTICS") or "",
        })
    return out


def pull(conn, client) -> int:
    """Забирает открытые сделки воронки, клиентов и товары. Возвращает число сделок."""
    s = logic.get_settings(conn)
    cat = str(s["category_id"])
    stages = fetch_stages(client, cat)

    select = ["ID", "TITLE", "STAGE_ID", "CONTACT_ID", "COMPANY_ID", "CATEGORY_ID"]
    if s["ship_field"]:
        select.append(s["ship_field"])
    for f in logic.tpl_fields(s["number_tpl"]):
        if f not in select:
            select.append(f)
    deals = client.list_all("crm.deal.list", {
        "order": {"ID": "ASC"},
        "filter": {"CATEGORY_ID": cat, "CLOSED": "N"},
        "select": select,
    })

    contacts = sorted({str(d.get("CONTACT_ID")) for d in deals if d.get("CONTACT_ID") not in (None, "", "0", 0)})
    companies = sorted({str(d.get("COMPANY_ID")) for d in deals if d.get("COMPANY_ID") not in (None, "", "0", 0)})
    names = {"c": {}, "co": {}}
    for ids in _chunks(contacts, 50):
        for c in client.call("crm.contact.list", {"filter": {"ID": ids}, "select": ["ID", "NAME", "LAST_NAME"]}).get("result") or []:
            names["c"][str(c["ID"])] = " ".join(x for x in (c.get("LAST_NAME"), c.get("NAME")) if x)
    for ids in _chunks(companies, 50):
        for c in client.call("crm.company.list", {"filter": {"ID": ids}, "select": ["ID", "TITLE"]}).get("result") or []:
            names["co"][str(c["ID"])] = c.get("TITLE") or ""

    products: dict[int, list] = {}
    for part in _chunks([int(d["ID"]) for d in deals], 50):
        res = client.batch({f"d{i}": ("crm.deal.productrows.get", {"id": i}) for i in part})
        for i in part:
            products[i] = res.get(f"d{i}") or []

    ts = logic.now_s()
    with db.tx(conn):
        logic.set_settings(conn, {"stages_cache": stages})
        # Читаем внутри транзакции: терминал не успеет сдвинуть стадию между чтением и записью.
        pending = {r[0] for r in conn.execute(
            "SELECT DISTINCT deal_id FROM outbox WHERE status = 'pending' AND kind = 'stage' AND deal_id IS NOT NULL")}
        seen = []
        for d in deals:
            did = int(d["ID"])
            seen.append(did)
            client_name = names["co"].get(str(d.get("COMPANY_ID"))) or names["c"].get(str(d.get("CONTACT_ID"))) or ""
            number = logic.number_for(s["number_tpl"], d)
            ship = parse_date(d.get(s["ship_field"])) if s["ship_field"] else None
            exists = conn.execute("SELECT 1 FROM deals WHERE id = ?", (did,)).fetchone()
            if exists:
                conn.execute(
                    f"""UPDATE deals SET number = ?, title = ?, client = ?, ship_date = ?, category_id = ?,
                        synced_at = ?, gone = 0 {'' if did in pending else ', stage_id = ?'} WHERE id = ?""",
                    (number, d.get("TITLE") or "", client_name, ship, cat, ts)
                    + (() if did in pending else (d.get("STAGE_ID") or "",)) + (did,),
                )
            else:
                conn.execute(
                    """INSERT INTO deals(id, number, title, client, ship_date, stage_id, category_id, synced_at)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                    (did, number, d.get("TITLE") or "", client_name, ship, d.get("STAGE_ID") or "", cat, ts),
                )
            conn.execute("DELETE FROM deal_products WHERE deal_id = ?", (did,))
            for p in products.get(did, []):
                conn.execute(
                    "INSERT INTO deal_products(deal_id, product_key, product_name, quantity, measure) VALUES(?, ?, ?, ?, ?)",
                    (did, logic.product_key(p.get("PRODUCT_ID"), p.get("PRODUCT_NAME")), p.get("PRODUCT_NAME") or "",
                     float(p.get("QUANTITY") or 0), p.get("MEASURE_NAME") or ""),
                )
        # Сделки, которых больше нет в выдаче (закрыли, перенесли в другую воронку).
        if seen:
            marks = ",".join("?" * len(seen))
            conn.execute(f"UPDATE deals SET gone = 1 WHERE gone = 0 AND id NOT IN ({marks})", seen)
        else:
            conn.execute("UPDATE deals SET gone = 1 WHERE gone = 0")
    return len(deals)


def make_client(settings: dict):
    return Bitrix(settings["webhook"])


class Syncer(threading.Thread):
    """Фоновый поток. make_client(settings) → клиент Битрикса (в демо — поддельный)."""

    def __init__(self, db_path, make_client=make_client, interval: float = 60):
        super().__init__(daemon=True, name="bitrix-sync")
        self.db_path = db_path
        self.make_client = make_client
        self.interval = interval
        self._wake = threading.Event()
        self._force_pull = False
        self._stopping = False
        self._last_pull = 0.0
        self._busy = threading.Lock()

    def wake(self, pull: bool = False) -> None:
        if pull:
            self._force_pull = True
        self._wake.set()

    def sync_now(self) -> dict:
        """Синхронизация прямо сейчас (кнопка в справочниках), с забором сделок."""
        self._force_pull = True
        return self.run_once()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    def run(self) -> None:
        while not self._stopping:
            self.run_once()
            self._wake.wait(timeout=max(1.0, self.interval - (time.monotonic() - self._last_pull)))
            self._wake.clear()

    def run_once(self) -> dict:
        with self._busy:
            conn = db.connect(self.db_path)
            try:
                return self._run(conn)
            finally:
                conn.close()

    def _run(self, conn) -> dict:
        s = logic.get_settings(conn)
        if not s["webhook"]:
            logic.set_settings(conn, {"last_sync_error": "Битрикс не подключён — укажите вебхук в справочниках"})
            return {"ok": False}
        status = {"ok": True}
        try:
            client = self.make_client(s)
            out = flush_outbox(conn, client)
            status.update(out)
            due = self._force_pull or time.monotonic() - self._last_pull >= self.interval - 0.5
            if not out["offline"] and due:
                self._force_pull = False
                self._last_pull = time.monotonic()
                status["deals"] = pull(conn, client)
                flush_outbox(conn, client)  # то, что успели отметить, пока шёл pull
            elif out["offline"]:
                self._last_pull = time.monotonic()
            err = out["error"]
            vals = {"last_sync_at": logic.now_s(), "last_sync_error": err}
            if not err:
                vals["last_sync_ok_at"] = logic.now_s()
            logic.set_settings(conn, vals)
        except BitrixError as e:
            self._last_pull = time.monotonic()
            logic.set_settings(conn, {"last_sync_at": logic.now_s(), "last_sync_error": str(e)})
            status = {"ok": False, "error": str(e)}
        except Exception as e:  # синк не должен ронять сервер
            traceback.print_exc()
            self._last_pull = time.monotonic()
            logic.set_settings(conn, {"last_sync_at": logic.now_s(), "last_sync_error": f"Сбой синхронизации: {e}"})
            status = {"ok": False, "error": str(e)}
        return status
