"""
Демо-режим: Битрикс в памяти и готовые справочники цеха.

python shop/server.py --demo поднимает сервер на отдельной базе
shop/data/demo.db (пересоздаётся при каждом запуске) — можно пикать бейджи,
закрывать заказы и смотреть отчёт без настоящего портала. Этот же
FakeBitrix используют тесты.
"""

from __future__ import annotations

import copy
import threading
from datetime import date, timedelta

import logic
from bitrix import Bitrix, BitrixError, BitrixOffline
from db import tx

CATEGORY = "1"
STAGES = [
    ("C1:NEW", "Новый заказ", "#a3a3a3", "process"),
    ("C1:CUT", "Распил", "#5b8def", "process"),
    ("C1:EDGE", "Кромка", "#9b6bd6", "process"),
    ("C1:DRILL", "Присадка", "#e08a3c", "process"),
    ("C1:PACK", "Упаковка", "#3fa58f", "process"),
    ("C1:OTK", "ОТК", "#c25c7a", "process"),
    ("C1:READY", "Готов к отгрузке", "#6b9b3a", "process"),
    ("C1:WON", "Отгружен", "#7bd500", "success"),
    ("C1:LOSE", "Отказ", "#ff5752", "failure"),
]
PRODUCTS = {
    101: ("Распил ЛДСП", "п.м."),
    102: ("Кромление 0,4 мм", "м"),
    103: ("Кромление 2 мм", "м"),
    104: ("Присадка", "отв."),
    105: ("Упаковка", "лист"),
    201: ("ЛДСП Egger W1000 ST9 16 мм", "лист"),
    301: ("Доставка", "услуга"),
}


class FakeBitrix(Bitrix):
    """Минимальный Битрикс24 в памяти: те методы, что зовут табло и сервер."""

    def __init__(self, today: date | None = None):  # noqa: super().__init__ не нужен — нет сети
        self.offline = False
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.timeline: list[dict] = []
        self.notifications: list[dict] = []
        self.fail_methods: set[str] = set()
        self.contacts = {"11": ("Иванов", "Иван"), "12": ("Петрова", "Анна"), "13": ("Ким", "Виктор"),
                         "14": ("Абдуллаев", "Рустам")}
        self.companies = {"21": "ООО «Интерьер Плюс»", "22": "Студия «Линия»", "23": "Мебель-Сити"}
        self.users = [{"ID": "1", "NAME": "Даниэль", "LAST_NAME": ""}, {"ID": "7", "NAME": "Алексей", "LAST_NAME": "Мастер"}]
        self.deals: dict[int, dict] = {}
        self.rows: dict[int, list[dict]] = {}
        self._seed(today or date.today())

    def _seed(self, today: date) -> None:
        plan = [
            # id, title, stage, ship offset, contact, company, products {id: qty}
            (1201, "Кухня угловая", "C1:CUT", 0, "11", None, {101: 64, 102: 38, 103: 22, 104: 180, 105: 6, 201: 9}),
            (1204, "Шкаф-купе", "C1:CUT", 1, None, "21", {101: 41, 103: 30, 104: 96, 105: 4, 201: 6}),
            (1207, "Распил 12 листов", "C1:CUT", 3, "12", None, {101: 88, 102: 60, 201: 12}),
            (1210, "Гардероб", "C1:CUT", 6, None, "22", {101: 52, 102: 40, 104: 120, 105: 5, 301: 1}),
            (1213, "Прихожая", "C1:EDGE", -1, "13", None, {101: 30, 102: 25, 103: 10, 104: 64, 105: 3}),
            (1216, "Тумба ТВ", "C1:EDGE", 2, None, "23", {101: 18, 103: 12, 104: 40, 105: 2}),
            (1219, "Распил 4 листа", "C1:EDGE", 4, "14", None, {101: 26, 102: 20}),
            (1222, "Детская", "C1:DRILL", 0, None, "21", {101: 44, 102: 30, 104: 140, 105: 5}),
            (1225, "Кухня прямая", "C1:DRILL", 5, "11", None, {101: 58, 102: 41, 103: 16, 104: 210, 105: 7}),
            (1228, "Стеллаж", "C1:PACK", 1, "12", None, {101: 22, 102: 18, 104: 48, 105: 2}),
            (1231, "Комод", "C1:PACK", 7, None, "22", {101: 20, 103: 14, 104: 52, 105: 2}),
            (1234, "Шкаф в спальню", "C1:OTK", 0, None, "23", {101: 47, 102: 33, 104: 110, 105: 4}),
            (1237, "Кухня", "C1:OTK", 3, "13", None, {101: 61, 102: 44, 103: 18, 104: 190, 105: 6}),
            (1240, "Гардеробная", "C1:READY", 1, "14", None, {101: 70, 102: 50, 104: 160, 105: 6}),
            (1243, "Новая кухня", "C1:NEW", 9, "11", None, {101: 60, 102: 40, 104: 200, 105: 6}),
        ]
        for did, title, stage, off, contact, company, prods in plan:
            self.deals[did] = {
                "ID": str(did), "TITLE": title, "STAGE_ID": stage, "CATEGORY_ID": CATEGORY, "CLOSED": "N",
                "CONTACT_ID": contact, "COMPANY_ID": company,
                "UF_CRM_SHIP_DATE": (today + timedelta(days=off)).isoformat() + "T03:00:00+03:00",
            }
            self.rows[did] = [
                {"PRODUCT_ID": pid, "PRODUCT_NAME": PRODUCTS[pid][0], "QUANTITY": qty, "MEASURE_NAME": PRODUCTS[pid][1]}
                for pid, qty in prods.items()
            ]

    # --- REST ---
    def call(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        with self.lock:
            self.calls.append(method)
            if self.offline:
                raise BitrixOffline("Нет связи с Битриксом (демо)")
            if method in self.fail_methods:
                raise BitrixError(f"Битрикс: метод {method} отклонён (демо)")
            handler = getattr(self, "_m_" + method.replace(".", "_"), None)
            if not handler:
                raise BitrixError(f"Битрикс: Method not found: {method}")
            return copy.deepcopy(handler(params))

    def batch(self, commands: dict) -> dict:
        return {key: self.call(method, params)["result"] for key, (method, params) in commands.items()}

    @staticmethod
    def _page(rows: list, params: dict) -> dict:
        start = int(params.get("start") or 0)
        out = {"result": rows[start:start + 50], "total": len(rows)}
        if start + 50 < len(rows):
            out["next"] = start + 50
        return out

    def _m_crm_category_list(self, p):
        return {"result": {"categories": [{"id": 0, "name": "Общая"}, {"id": 1, "name": "Производство"}]}, "total": 2}

    def _m_crm_status_list(self, p):
        ent = (p.get("filter") or {}).get("ENTITY_ID")
        if ent != f"DEAL_STAGE_{CATEGORY}":
            return {"result": [{"STATUS_ID": "NEW", "NAME": "Новая", "EXTRA": {"SEMANTICS": "process", "COLOR": "#39a8ef"}}]}
        return {"result": [{"STATUS_ID": sid, "NAME": n, "SORT": i * 10, "EXTRA": {"COLOR": c, "SEMANTICS": sem}}
                           for i, (sid, n, c, sem) in enumerate(STAGES)]}

    def _m_crm_deal_fields(self, p):
        return {"result": {
            "ID": {"type": "integer", "title": "ID"},
            "TITLE": {"type": "string", "title": "Название"},
            "BEGINDATE": {"type": "date", "title": "Дата начала"},
            "CLOSEDATE": {"type": "date", "title": "Дата завершения"},
            "UF_CRM_SHIP_DATE": {"type": "date", "title": "UF_CRM_SHIP_DATE", "formLabel": "Дата отгрузки"},
            "UF_CRM_ORDER_NO": {"type": "string", "title": "UF_CRM_ORDER_NO", "formLabel": "Номер заказа"},
        }}

    def _m_crm_deal_list(self, p):
        f = p.get("filter") or {}
        rows = [d for d in self.deals.values()
                if str(d["CATEGORY_ID"]) == str(f.get("CATEGORY_ID", d["CATEGORY_ID"]))
                and (f.get("CLOSED") is None or d["CLOSED"] == f["CLOSED"])]
        sel = p.get("select")
        if sel:
            rows = [{k: v for k, v in d.items() if k in sel} for d in rows]
        return self._page(rows, p)

    def _m_crm_contact_list(self, p):
        ids = [str(x) for x in (p.get("filter") or {}).get("ID", [])]
        return {"result": [{"ID": i, "LAST_NAME": self.contacts[i][0], "NAME": self.contacts[i][1]}
                           for i in ids if i in self.contacts]}

    def _m_crm_company_list(self, p):
        ids = [str(x) for x in (p.get("filter") or {}).get("ID", [])]
        return {"result": [{"ID": i, "TITLE": self.companies[i]} for i in ids if i in self.companies]}

    def _m_crm_deal_productrows_get(self, p):
        return {"result": self.rows.get(int(p["id"]), [])}

    def _m_crm_deal_update(self, p):
        d = self.deals.get(int(p["id"]))
        if not d:
            raise BitrixError("Битрикс: Not found")
        stage = (p.get("fields") or {}).get("STAGE_ID")
        if stage:
            if stage not in {s[0] for s in STAGES}:
                raise BitrixError(f"Битрикс: неверная стадия {stage}")
            d["STAGE_ID"] = stage
            d["CLOSED"] = "Y" if stage in ("C1:WON", "C1:LOSE") else "N"
        return {"result": True}

    def _m_crm_timeline_comment_add(self, p):
        self.timeline.append(p["fields"])
        return {"result": len(self.timeline)}

    def _m_im_notify_system_add(self, p):
        self.notifications.append(p)
        return {"result": len(self.notifications)}

    def _m_user_get(self, p):
        return self._page(self.users, p)


def seed(conn) -> None:
    """Справочники цеха для демо и тестов: участки, ставки, люди, товары."""
    with tx(conn):
        logic.set_settings(conn, {
            "webhook": "https://demo.bitrix24.ru/rest/1/demo/",
            "category_id": CATEGORY,
            "ship_field": "UF_CRM_SHIP_DATE",
            "number_tpl": "DG-{ID}",
            "final_stage_id": "C1:READY",
            "notify_user_id": "7",
            "board_stages": ["C1:CUT", "C1:EDGE", "C1:DRILL", "C1:PACK", "C1:OTK", "C1:READY"],
            "shadow_mode": "1",
            "defect_policy": "unpaid",
        })
        secs = [
            ("Распил", "C1:CUT", "regular", 0, [("Распил", "п.м.", 25, 0, [101])]),
            ("Кромка", "C1:EDGE", "regular", 0, [("Кромка 0,4 мм", "м", 12, 0, [102]), ("Кромка 2 мм", "м", 18, 0, [103])]),
            ("Присадка", "C1:DRILL", "regular", 1, [("Присадка", "отв.", 3, 0, [104])]),
            ("Упаковка", "C1:PACK", "regular", 0, [("Упаковка", "лист", 40, 0, [105])]),
            ("ОТК", "C1:OTK", "otk", 0, [("Приёмка заказа", "заказ", 150, 1, [])]),
        ]
        for i, (name, stage, kind, skip, ops) in enumerate(secs):
            sid = conn.execute(
                "INSERT INTO sections(name, sort, stage_id, kind, skip_if_empty) VALUES(?, ?, ?, ?, ?)",
                (name, (i + 1) * 10, stage, kind, skip),
            ).lastrowid
            for op_name, unit, rate, per_deal, pids in ops:
                oid = conn.execute(
                    "INSERT INTO operations(section_id, name, unit, rate, per_deal) VALUES(?, ?, ?, ?, ?)",
                    (sid, op_name, unit, rate, per_deal),
                ).lastrowid
                for pid in pids:
                    conn.execute("INSERT INTO product_map(product_key, product_name, operation_id) VALUES(?, ?, ?)",
                                 (f"id:{pid}", PRODUCTS[pid][0], oid))
        # Материал осознанно не учитываем; «Доставку» оставляем несопоставленной — для примера в админке.
        conn.execute("INSERT INTO product_map(product_key, product_name, operation_id) VALUES('id:201', ?, NULL)",
                     (PRODUCTS[201][0],))
        for i, (name, salary) in enumerate([("Солех", 30000), ("Иван", 30000), ("Рустам", 28000),
                                            ("Алишер", 28000), ("Дильшод", 32000)]):
            conn.execute("INSERT INTO workers(name, badge, salary) VALUES(?, ?, ?)", (name, f"W-{i + 1:04d}", salary))
