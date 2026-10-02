"""
Тесты цеховой логики на поддельном Битриксе.

    python -m unittest discover shop/tests
"""

from __future__ import annotations

import io
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
import demo  # noqa: E402
import logic  # noqa: E402
import server  # noqa: E402
import sync  # noqa: E402
import xlsx  # noqa: E402
from bitrix import build_query, mask_webhook, normalize_webhook  # noqa: E402

TODAY = date(2026, 10, 2)


class Clock:
    def __init__(self, dt: datetime):
        self.dt = dt

    def __call__(self):
        return self.dt


class ShopCase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(datetime(2026, 10, 2, 10, 0, 0))
        self._real_now = logic.now
        logic.now = self.clock
        self.conn = db.connect(":memory:")
        db.init(self.conn)
        demo.seed(self.conn)
        self.bx = demo.FakeBitrix(today=TODAY)
        sync.pull(self.conn, self.bx)

    def tearDown(self):
        logic.now = self._real_now
        self.conn.close()

    # помощники
    def sec(self, name):
        return self.conn.execute("SELECT * FROM sections WHERE name = ?", (name,)).fetchone()

    def worker(self, name):
        return self.conn.execute("SELECT * FROM workers WHERE name = ?", (name,)).fetchone()

    def deal(self, did):
        return self.conn.execute("SELECT * FROM deals WHERE id = ?", (did,)).fetchone()

    def work(self, deal_id, section, names, result="done", reason=""):
        sid = logic.start_session(self.conn, self.sec(section)["id"], deal_id, [self.worker(n)["id"] for n in names])["session_id"]
        return logic.finish_session(self.conn, sid, result, reason)

    def ledger_sum(self, name=None, kind=None):
        q, args = "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE 1 = 1", []
        if name:
            q += " AND worker_id = ?"
            args.append(self.worker(name)["id"])
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        return round(self.conn.execute(q, args).fetchone()[0], 2)


class TestScan(ShopCase):
    def test_find_deal_variants(self):
        for code in ("DG-1201", "dg-1201", "ВП-1201", "1201", "https://x.bitrix24.ru/crm/deal/details/1201/"):
            self.assertEqual(logic.find_deal(self.conn, code)["id"], 1201, code)
        self.assertIsNone(logic.find_deal(self.conn, "DG-9999"))

    def test_find_worker_russian_layout(self):
        self.assertEqual(logic.find_worker(self.conn, "W-0001")["name"], "Солех")
        self.assertEqual(logic.find_worker(self.conn, "Ц-0001")["name"], "Солех")  # русская раскладка
        self.assertEqual(logic.next_badge(self.conn), "W-0006")

    def test_product_key(self):
        self.assertEqual(logic.product_key("101", "x"), "id:101")
        self.assertEqual(logic.product_key("0", "  Распил   ЛДСП "), "name:распил лдсп")


class TestSync(ShopCase):
    def test_pull_fills_cache(self):
        d = self.deal(1204)
        self.assertEqual(d["number"], "DG-1204")
        self.assertEqual(d["client"], "ООО «Интерьер Плюс»")
        self.assertEqual(d["ship_date"], "2026-10-03")
        self.assertEqual(logic.deal_volumes(self.conn, 1201)[1], 64)  # распил, п.м.

    def test_pending_stage_survives_pull_and_offline(self):
        self.bx.offline = True
        self.work(1201, "Распил", ["Солех"])
        self.assertEqual(self.deal(1201)["stage_id"], "C1:EDGE")
        out = sync.flush_outbox(self.conn, self.bx)
        self.assertTrue(out["offline"])
        self.bx.offline = False
        # Битрикс ещё не знает о смене стадии, но синк не должен откатить её у нас.
        sync.pull(self.conn, self.bx)
        self.assertEqual(self.deal(1201)["stage_id"], "C1:EDGE")
        out = sync.flush_outbox(self.conn, self.bx)
        self.assertFalse(out["offline"])
        self.assertEqual(self.bx.deals[1201]["STAGE_ID"], "C1:EDGE")
        self.assertTrue(any("Распил — готово" in t["COMMENT"] for t in self.bx.timeline))
        pending = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]
        self.assertEqual(pending, 0)

    def test_permanent_error_marks_failed(self):
        self.bx.fail_methods.add("crm.timeline.comment.add")
        self.work(1201, "Распил", ["Солех"])
        for _ in range(sync.MAX_ATTEMPTS):
            sync.flush_outbox(self.conn, self.bx)
        rows = self.conn.execute("SELECT method, status FROM outbox ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("crm.deal.update", "done"), ("crm.timeline.comment.add", "failed")])

    def test_closed_deal_gone(self):
        self.bx.deals[1240]["CLOSED"] = "Y"
        sync.pull(self.conn, self.bx)
        self.assertEqual(self.deal(1240)["gone"], 1)
        self.assertNotIn(1240, [d["id"] for d in logic.board(self.conn)["deals"]])

    def test_syncer_run_once(self):
        path = ":memory:"
        self.assertTrue(path)  # Syncer открывает свою базу — проверяем на временном файле
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.db"
            c = db.connect(p)
            db.init(c)
            demo.seed(c)
            c.close()
            s = sync.Syncer(p, lambda settings: self.bx, interval=60)
            res = s.sync_now()
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["deals"], 15)
            c = db.connect(p)
            self.assertTrue(logic.setting(c, "last_sync_ok_at"))
            c.close()

    def test_meta(self):
        meta = sync.fetch_meta(self.bx)
        self.assertIn({"code": "UF_CRM_SHIP_DATE", "label": "Дата отгрузки"}, meta["date_fields"])
        self.assertEqual([c["id"] for c in meta["categories"]], ["0", "1"])
        self.assertEqual(meta["users"][1]["name"], "Мастер Алексей")


class TestWork(ShopCase):
    def test_done_pays_and_moves(self):
        res = self.work(1201, "Распил", ["Солех"])
        self.assertEqual(res["earnings"][0]["amount"], 64 * 25)
        self.assertEqual(res["earnings"][0]["today"], 1600)
        self.assertEqual(res["next_stage"], "Кромка")
        self.assertEqual(self.deal(1201)["stage_id"], "C1:EDGE")

    def test_two_workers_split(self):
        res = self.work(1213, "Кромка", ["Солех", "Иван"])  # 25 м × 12 + 10 м × 18 = 480
        self.assertEqual([e["amount"] for e in res["earnings"]], [240, 240])
        self.assertEqual(self.ledger_sum("Иван"), 240)

    def test_join_open_session(self):
        a = logic.start_session(self.conn, self.sec("Кромка")["id"], 1213, [self.worker("Солех")["id"]])
        b = logic.start_session(self.conn, self.sec("Кромка")["id"], 1213, [self.worker("Иван")["id"]])
        self.assertEqual(a["session_id"], b["session_id"])
        self.assertTrue(b["joined"])
        self.assertEqual(b["workers"], ["Иван", "Солех"])

    def test_skip_section_without_volume(self):
        # 1219: распил и кромка, без присадки и упаковки → после кромки сразу на упаковку? Упаковка
        # не пропускается (skip_if_empty = 0), присадка пропускается.
        res = self.work(1219, "Кромка", ["Рустам"])
        self.assertEqual(self.deal(1219)["stage_id"], "C1:PACK")
        self.assertEqual(res["next_stage"], "Упаковка")

    def test_last_section_to_final_stage(self):
        res = self.work(1234, "ОТК", ["Дильшод"])
        self.assertEqual(self.deal(1234)["stage_id"], "C1:READY")
        self.assertEqual(res["earnings"][0]["amount"], 150)  # приёмка — за заказ

    def test_rate_snapshot(self):
        self.work(1201, "Распил", ["Солех"])
        self.conn.execute("UPDATE operations SET rate = 100 WHERE name = 'Распил'")
        self.assertEqual(self.ledger_sum("Солех"), 1600)

    def test_repeat_not_paid(self):
        self.work(1201, "Распил", ["Солех"])
        res = self.work(1201, "Распил", ["Иван"])
        self.assertEqual(res["earnings"][0]["amount"], 0)
        self.assertIn("повторно", res["note"])

    def test_no_backward_move(self):
        # Сделку уже передвинули руками на упаковку, а распил только отметился.
        self.conn.execute("UPDATE deals SET stage_id = 'C1:PACK' WHERE id = 1201")
        self.work(1201, "Распил", ["Солех"])
        self.assertEqual(self.deal(1201)["stage_id"], "C1:PACK")

    def test_problem(self):
        res = self.work(1201, "Распил", ["Солех"], "problem", "Нет материала")
        self.assertEqual(res["earnings"], [])
        self.assertEqual(self.deal(1201)["stage_id"], "C1:CUT")
        self.assertEqual(self.deal(1201)["problem"], "Нет материала")
        sync.flush_outbox(self.conn, self.bx)
        self.assertEqual(self.bx.notifications[0]["USER_ID"], "7")
        self.assertIn("Нет материала", self.bx.notifications[0]["MESSAGE"])
        # Следующий заход снимает пометку.
        logic.start_session(self.conn, self.sec("Распил")["id"], 1201, [self.worker("Солех")["id"]])
        self.assertEqual(self.deal(1201)["problem"], "")

    def test_remark_needs_reason(self):
        sid = logic.start_session(self.conn, self.sec("Распил")["id"], 1201, [self.worker("Солех")["id"]])["session_id"]
        with self.assertRaises(logic.ShopError):
            logic.finish_session(self.conn, sid, "remark", "")
        res = logic.finish_session(self.conn, sid, "remark", "Скол")
        self.assertEqual(res["earnings"][0]["amount"], 1600)

    def test_double_finish_rejected(self):
        sid = logic.start_session(self.conn, self.sec("Распил")["id"], 1201, [self.worker("Солех")["id"]])["session_id"]
        logic.finish_session(self.conn, sid, "done")
        with self.assertRaises(logic.ShopError):
            logic.finish_session(self.conn, sid, "done")

    def test_unmapped_volume_warns(self):
        self.conn.execute("DELETE FROM product_map WHERE product_key = 'id:101'")
        res = self.work(1201, "Распил", ["Солех"])
        self.assertEqual(res["earnings"][0]["amount"], 0)
        self.assertTrue(res["warnings"])

    def test_board_flags(self):
        logic.start_session(self.conn, self.sec("Распил")["id"], 1201, [self.worker("Солех")["id"]])
        b = {d["id"]: d for d in logic.board(self.conn)["deals"]}
        self.assertEqual(b[1201]["working"], ["Солех"])
        self.assertEqual(len(logic.board(self.conn)["stages"]), 6)


class TestDefects(ShopCase):
    def _flow(self):
        # 1234 стоит на ОТК; пусть до этого упаковку сделал Алишер.
        self.conn.execute("UPDATE deals SET stage_id = 'C1:PACK' WHERE id = 1234")
        self.work(1234, "Упаковка", ["Алишер"])  # 4 листа × 40 = 160
        self.assertEqual(self.deal(1234)["stage_id"], "C1:OTK")

    def test_unpaid_policy(self):
        self._flow()
        res = logic.report_defect(self.conn, 1234, self.sec("Упаковка")["id"], "Царапины", True)
        self.assertEqual(res["workers"], ["Алишер"])
        self.assertEqual(self.deal(1234)["stage_id"], "C1:PACK")
        self.assertEqual(self.ledger_sum("Алишер"), 160)
        self.assertTrue(logic.board(self.conn)["deals"][[d["id"] for d in logic.board(self.conn)["deals"]].index(1234)]["rework"])
        r = self.work(1234, "Упаковка", ["Алишер"])
        self.assertEqual(r["earnings"][0]["amount"], 0)
        self.assertEqual(self.deal(1234)["stage_id"], "C1:OTK")  # после переделки — обратно на ОТК
        self.assertIsNotNone(self.conn.execute("SELECT resolved_at FROM defects").fetchone()[0])

    def test_deduct_policy(self):
        self.conn.execute("UPDATE settings SET value = 'deduct' WHERE key = 'defect_policy'")
        self._flow()
        res = logic.report_defect(self.conn, 1234, self.sec("Упаковка")["id"], "Царапины", True)
        self.assertEqual(res["deducted"], 160)
        self.assertEqual(self.ledger_sum("Алишер"), 0)
        self.work(1234, "Упаковка", ["Алишер"])
        self.assertEqual(self.ledger_sum("Алишер"), 0)

    def test_not_worker_fault_paid(self):
        self._flow()
        logic.report_defect(self.conn, 1234, self.sec("Упаковка")["id"], "Брак плиты", False)
        self.work(1234, "Упаковка", ["Иван"])
        self.assertEqual(self.ledger_sum("Иван"), 160)
        rep = logic.month_report(self.conn, "2026-10")
        alisher = next(p for p in rep["people"] if p["name"] == "Алишер")
        self.assertEqual(alisher["defects"], 0)  # не его вина — в статистику не идёт

    def test_passed_sections(self):
        self._flow()
        self.assertEqual([s["name"] for s in logic.passed_sections(self.conn, 1234)], ["Упаковка"])

    def test_otk_defect_closes_otk_session(self):
        self._flow()
        otk = self.sec("ОТК")["id"]
        logic.start_session(self.conn, otk, 1234, [self.worker("Дильшод")["id"]])  # ОТК начал приёмку
        logic.report_defect(self.conn, 1234, self.sec("Упаковка")["id"], "Царапины", True)
        self.assertIsNone(logic.open_session(self.conn, 1234, otk))
        row = self.conn.execute("SELECT result, reason FROM sessions WHERE deal_id = 1234 AND section_id = ?",
                                (otk,)).fetchone()
        self.assertEqual(tuple(row), ("defect", "Царапины"))
        self.assertEqual(self.deal(1234)["stage_id"], "C1:PACK")  # брак не двигает сделку дальше ОТК
        self.assertEqual(self.ledger_sum("Дильшод"), 0)
        self.assertNotIn(1234, logic._deal_flags(self.conn)[0])  # на табло больше не «в работе»

        self.work(1234, "Упаковка", ["Алишер"])  # переделка → обратно на ОТК
        self.assertEqual(self.deal(1234)["stage_id"], "C1:OTK")
        res = self.work(1234, "ОТК", ["Дильшод"])
        self.assertEqual((res["earnings"][0]["amount"], res["note"]), (150, ""))
        self.assertEqual(self.ledger_sum("Дильшод"), 150)  # приёмка оплачена один раз
        self.assertEqual(self.deal(1234)["stage_id"], "C1:READY")
        self.assertEqual([s["name"] for s in logic.passed_sections(self.conn, 1234)], ["Упаковка"])


class TestReport(ShopCase):
    def test_month_report_adjust_close(self):
        self.work(1201, "Распил", ["Солех"])             # 1600
        self.work(1213, "Кромка", ["Солех", "Иван"])     # по 240
        rep = logic.month_report(self.conn, "2026-10")
        soleh = next(p for p in rep["people"] if p["name"] == "Солех")
        self.assertEqual((soleh["salary"], soleh["piece"], soleh["total"], soleh["deals"]), (30000, 1840, 31840, 2))
        line = next(ln for ln in rep["lines"] if ln["worker"] == "Солех" and ln["operation"] == "Кромка 0,4 мм")
        self.assertEqual(line["partners"], ["Иван"])
        self.assertEqual(line["amount"], 150)  # 25 м × 12 ₽ пополам

        logic.adjust_line(self.conn, line["id"], 300, "переделывал торцы")
        logic.adjust_line(self.conn, line["id"], 280, "уточнил")  # вторая правка считает от текущей суммы
        self.assertEqual(logic.effective_amount(self.conn, line["id"]), 280)
        logic.manual_line(self.conn, "2026-10", self.worker("Солех")["id"], 500, "наладка станка")
        soleh = next(p for p in logic.month_report(self.conn, "2026-10")["people"] if p["name"] == "Солех")
        self.assertEqual(soleh["total"], 30000 + 1840 + (280 - 150) + 500)

        with self.assertRaises(logic.ShopError):
            logic.close_month(self.conn, "2026-10")  # текущий месяц закрыть нельзя
        self.clock.dt = datetime(2026, 11, 3, 9, 0)
        logic.close_month(self.conn, "2026-10")
        with self.assertRaises(logic.ShopError):
            logic.adjust_line(self.conn, line["id"], 1, "поздно")
        # Оклад после закрытия меняют — в закрытом месяце остаётся старый.
        self.conn.execute("UPDATE workers SET salary = 99999")
        soleh = next(p for p in logic.month_report(self.conn, "2026-10")["people"] if p["name"] == "Солех")
        self.assertEqual(soleh["salary"], 30000)
        logic.reopen_month(self.conn, "2026-10")
        logic.adjust_line(self.conn, line["id"], 240, "вернул")

    def test_defect_matrix(self):
        self.conn.execute("UPDATE deals SET stage_id = 'C1:PACK' WHERE id IN (1234, 1237)")
        self.work(1234, "Упаковка", ["Алишер"])
        self.work(1237, "Упаковка", ["Иван"])
        pack = self.sec("Упаковка")["id"]
        logic.report_defect(self.conn, 1234, pack, "Царапины", True)
        logic.report_defect(self.conn, 1237, pack, "Брак плиты", False)  # не вина — в матрицу не идёт
        rep = logic.month_report(self.conn, "2026-10")
        self.assertEqual(rep["defect_matrix"], [{"worker_id": self.worker("Алишер")["id"], "worker": "Алишер",
                                                 "section_id": pack, "section": "Упаковка", "count": 1}])
        self.assertEqual(len(rep["defects"]), 2)
        self.assertEqual(logic.month_report(self.conn, "2026-09")["defect_matrix"], [])

    def test_shadow_snapshot(self):
        self.work(1201, "Распил", ["Солех"])
        self.assertTrue(logic.month_report(self.conn, "2026-10")["shadow"])
        self.clock.dt = datetime(2026, 11, 2, 9, 0)
        logic.close_month(self.conn, "2026-10")  # закрыли в теневом режиме
        logic.set_settings(self.conn, {"shadow_mode": "0"})  # с ноября платят по системе
        self.assertTrue(logic.month_report(self.conn, "2026-10")["shadow"])
        self.assertFalse(logic.month_report(self.conn, "2026-11")["shadow"])
        self.assertNotIn("ТЕНЕВОЙ", zipfile.ZipFile(io.BytesIO(server.report_xlsx(
            logic.month_report(self.conn, "2026-11")))).read("xl/worksheets/sheet1.xml").decode())
        logic.reopen_month(self.conn, "2026-10")  # открытый месяц — по текущей настройке
        self.assertFalse(logic.month_report(self.conn, "2026-10")["shadow"])

    def test_report_months(self):
        self.assertEqual(logic.report_months(self.conn), [{"month": "2026-10", "closed": False}])
        demo.seed_history(self.conn)
        logic.close_month(self.conn, "2026-09")
        self.assertEqual(logic.report_months(self.conn), [{"month": "2026-10", "closed": False},
                                                          {"month": "2026-09", "closed": True}])
        for bad in ("", "2026-13", "сентябрь"):
            with self.assertRaises(logic.ShopError):
                logic.close_month(self.conn, bad)

    def test_seed_history(self):
        month = demo.seed_history(self.conn)
        self.assertEqual(month, "2026-09")
        sync.pull(self.conn, self.bx)  # синк не трогает закрытые заказы, которых нет в Битриксе
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM deals WHERE gone = 1 AND stage_id = 'C1:WON'")
                         .fetchone()[0], 5)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        rep = logic.month_report(self.conn, month)
        pieces = [ln for ln in rep["lines"] if ln["kind"] == "piece"]
        for ln in pieces:
            expected = 0 if ln["note"] else round(ln["qty"] * ln["rate"] * ln["share"], 2)
            self.assertEqual(ln["amount"], expected, ln)
        rework = [ln for ln in pieces if ln["note"]]
        self.assertEqual([(ln["worker"], ln["deal_number"], ln["amount"]) for ln in rework], [("Алишер", "DG-1104", 0)])
        self.assertEqual([(ln["worker"], ln["amount"]) for ln in rep["lines"] if ln["kind"] == "manual"], [("Иван", 1200)])
        self.assertEqual([(m["worker"], m["section"], m["count"]) for m in rep["defect_matrix"]], [("Алишер", "Присадка", 1)])
        self.assertIsNotNone(rep["defects"][0]["resolved_at"])
        people = {p["name"]: p for p in rep["people"]}
        self.assertEqual(people["Дильшод"]["piece"], 5 * 150)  # приёмку DG-1104 закрыл брак — оплачена один раз
        self.assertEqual(people["Иван"]["total"], 30000 + people["Иван"]["piece"] + 1200)
        logic.close_month(self.conn, month)
        self.assertTrue(logic.month_report(self.conn, month)["closed"])

    def test_xlsx(self):
        self.work(1201, "Распил", ["Солех"])
        data = server.report_xlsx(logic.month_report(self.conn, "2026-10"))
        z = zipfile.ZipFile(io.BytesIO(data))
        for name in z.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                minidom.parseString(z.read(name))
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("Солех", sheet)
        self.assertIn("ТЕНЕВОЙ РЕЖИМ", sheet)

    def test_xlsx_escapes(self):
        data = xlsx.write_xlsx([{"name": "A<&>", "rows": [["<b>&", 1.5, None, True]]}])
        z = zipfile.ZipFile(io.BytesIO(data))
        minidom.parseString(z.read("xl/workbook.xml"))
        minidom.parseString(z.read("xl/worksheets/sheet1.xml"))


class TestMigration(unittest.TestCase):
    def test_months_shadow_column(self):
        # База, созданная до снимка теневого режима: в months нет колонки shadow.
        conn = db.connect(":memory:")
        conn.execute("""CREATE TABLE months (month TEXT PRIMARY KEY, closed_at TEXT NOT NULL,
                        salaries TEXT NOT NULL DEFAULT '{}')""")
        conn.execute("""INSERT INTO months VALUES('2026-09', '2026-10-01 10:00:00', '{"1": 30000}')""")
        db.init(conn)
        db.init(conn)  # повторный запуск сервера — без ошибок
        cols = [r[1] for r in conn.execute("PRAGMA table_info(months)")]
        self.assertIn("shadow", cols)
        row = conn.execute("SELECT * FROM months").fetchone()
        self.assertEqual((row["month"], row["closed_at"], row["salaries"], row["shadow"]),
                         ("2026-09", "2026-10-01 10:00:00", '{"1": 30000}', 0))
        self.assertFalse(logic.month_report(conn, "2026-09")["shadow"])
        conn.close()


class TestBitrixHelpers(unittest.TestCase):
    def test_webhook(self):
        self.assertEqual(normalize_webhook(" https://a.bitrix24.ru/rest/1/abc123/profile.json "),
                         "https://a.bitrix24.ru/rest/1/abc123/")
        self.assertEqual(normalize_webhook("https://a.bitrix24.ru/crm/"), "")
        self.assertEqual(mask_webhook("https://a.bitrix24.ru/rest/1/abc123/"), "https://a.bitrix24.ru/rest/1/••••••/")

    def test_build_query(self):
        self.assertEqual(build_query({"id": 5, "filter": {"ID": [1, 2]}}),
                         [("id", "5"), ("filter[ID][0]", "1"), ("filter[ID][1]", "2")])


class TestHttp(unittest.TestCase):
    """Сервер целиком: поднимаем на свободном порту с поддельным Битриксом."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "http.db"
        conn = db.connect(cls.db_path)
        db.init(conn)
        demo.seed(conn)
        cls.bx = demo.FakeBitrix()
        sync.pull(conn, cls.bx)
        conn.close()
        cls.syncer = sync.Syncer(cls.db_path, lambda s: cls.bx, interval=3600)
        app = server.App(cls.db_path, cls.syncer, cls.bx)
        cls.srv = server.make_server(app, "127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def req(self, path, body=None, pin=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET",
                                   headers={"Content-Type": "application/json", **({"X-Pin": pin} if pin else {})})
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_flow(self):
        code, info = self.req("/api/info")
        self.assertEqual(code, 200)
        cut = next(s for s in info["sections"] if s["name"] == "Распил")["id"]
        code, w = self.req("/api/scan", {"section_id": cut, "code": "W-0002"})
        self.assertEqual(w["type"], "worker")
        code, d = self.req("/api/scan", {"section_id": cut, "code": "DG-1207"})
        self.assertEqual((d["type"], d["open_session"]), ("deal", None))
        code, st = self.req("/api/start", {"section_id": cut, "deal_id": 1207, "worker_ids": [w["worker"]["id"]]})
        self.assertEqual(code, 200, st)
        code, d = self.req("/api/scan", {"section_id": cut, "code": "1207"})
        self.assertEqual(d["open_session"]["workers"], ["Иван"])
        code, fin = self.req("/api/finish", {"session_id": st["session_id"], "result": "done"})
        self.assertEqual(fin["earnings"][0]["amount"], 88 * 25)
        code, term = self.req(f"/api/terminal/{cut}")
        self.assertNotIn(1207, [q["id"] for q in term["queue"]])
        code, board = self.req("/api/board")
        self.assertIn("C1:EDGE", [d["stageId"] for d in board["deals"] if d["id"] == 1207])
        code, err = self.req("/api/finish", {"session_id": st["session_id"], "result": "done"})
        self.assertEqual(code, 400)
        self.assertIn("закрыта", err["error"])

    def test_terminal_api(self):
        code, info = self.req("/api/info")
        sec = {s["name"]: s["id"] for s in info["sections"]}
        code, term = self.req(f"/api/terminal/{sec['Распил']}")
        self.assertEqual(code, 200)
        self.assertEqual((term["section"]["stage_id"], term["section"]["kind"]), ("C1:CUT", "regular"))
        self.assertRegex(term["today"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(next(s for s in term["sections"] if s["name"] == "ОТК")["kind"], "otk")
        code, d = self.req("/api/scan", {"section_id": sec["Распил"], "code": "ВП-1201"})  # русская раскладка
        self.assertEqual((d["deal"]["id"], d["deal"]["volumes"]), (1201, ["Распил: 64 п.м."]))
        code, d = self.req("/api/scan", {"section_id": sec["Распил"], "code": "НЕТ-ТАКОГО"})
        self.assertEqual(d["type"], "unknown")
        code, err = self.req("/api/terminal/9999")
        self.assertEqual(code, 400)

    def test_otk_defect_http(self):
        code, info = self.req("/api/info")
        sec = {s["name"]: s["id"] for s in info["sections"]}
        code, w = self.req("/api/scan", {"section_id": sec["ОТК"], "code": "W-0005"})
        code, st = self.req("/api/start", {"section_id": sec["ОТК"], "deal_id": 1237, "worker_ids": [w["worker"]["id"]]})
        self.assertEqual(code, 200, st)
        code, res = self.req("/api/defect", {"deal_id": 1237, "section_id": sec["Упаковка"], "reason": "Царапины",
                                             "worker_fault": False, "reported_by": "Дильшод"})
        self.assertEqual(code, 200, res)
        self.assertIn("оплачивается", res["policy"])
        code, term = self.req(f"/api/terminal/{sec['ОТК']}")
        self.assertNotIn(1237, [o["deal_id"] for o in term["open"]])
        code, term = self.req(f"/api/terminal/{sec['Упаковка']}")
        d = next(q for q in term["queue"] if q["id"] == 1237)
        self.assertTrue(d["rework"])
        code, err = self.req("/api/finish", {"session_id": st["session_id"], "result": "done"})
        self.assertEqual(code, 400)  # приёмку закрыл брак — «Готово» по ней уже не пройдёт

    def test_pin(self):
        code, _ = self.req("/api/admin/settings", {"admin_pin": "4321"})
        self.assertEqual(code, 200)
        code, res = self.req("/api/admin/state")
        self.assertEqual((code, res.get("pin")), (401, True))
        code, res = self.req("/api/admin/state", pin="4321")
        self.assertEqual(code, 200)
        self.assertNotIn("admin_pin", res["settings"])
        self.assertNotIn("demo/", json.dumps(res))  # ключ вебхука наружу не отдаётся
        code, _ = self.req("/api/admin/settings", {"admin_pin": ""}, pin="4321")
        self.assertEqual(code, 200)

    def test_pages(self):
        for path in ("/", "/terminal", "/admin", "/report", "/print", "/static/common.css", "/static/common.js", "/static/logo.png",
                     "/static/vendor/qrcode.js", "/static/vendor/JsBarcode.code128.min.js"):
            code, _ = self.req(path)
            self.assertEqual(code, 200, path)
        code, html = self.req("/tablo")
        self.assertIn(b'/*__SERVER_API__*/"/api/board"', html)
        code, _ = self.req("/static/../server.py")
        self.assertEqual(code, 404)

    def test_report_api(self):
        code, info = self.req("/api/info")
        edge = next(s for s in info["sections"] if s["name"] == "Кромка")["id"]
        code, w = self.req("/api/scan", {"section_id": edge, "code": "W-0004"})
        code, st = self.req("/api/start", {"section_id": edge, "deal_id": 1216, "worker_ids": [w["worker"]["id"]]})
        self.assertEqual(code, 200, st)
        code, fin = self.req("/api/finish", {"session_id": st["session_id"], "result": "done"})
        self.assertEqual(fin["earnings"][0]["amount"], 12 * 18)  # Кромка 2 мм

        code, rep = self.req("/api/report")
        self.assertEqual(code, 200, rep)
        for key in ("month", "closed", "closed_at", "shadow", "people", "lines", "defects", "defects_by_section",
                    "defect_matrix", "workers", "today"):
            self.assertIn(key, rep)
        self.assertEqual(rep["month"], rep["today"][:7])
        self.assertEqual((rep["closed"], rep["shadow"]), (False, True))
        line = next(ln for ln in rep["lines"] if ln["deal_id"] == 1216 and ln["worker"] == "Алишер")
        for key in ("id", "kind", "deal_number", "client", "section", "operation", "qty", "unit", "rate", "share",
                    "amount", "note", "partners", "adjusted", "created_at"):
            self.assertIn(key, line)
        before = next(p for p in rep["people"] if p["name"] == "Алишер")

        code, err = self.req("/api/report/adjust", {"ledger_id": line["id"], "amount": 300, "note": " "})
        self.assertEqual(code, 400)
        self.assertIn("причину", err["error"])
        code, res = self.req("/api/report/adjust", {"ledger_id": line["id"], "amount": "300", "note": "торцы"})
        self.assertEqual(code, 200, res)
        code, res = self.req("/api/report/manual", {"month": rep["month"], "worker_id": w["worker"]["id"],
                                                    "amount": 500, "note": "наладка станка"})
        self.assertEqual(code, 200, res)
        code, rep = self.req("/api/report?month=" + rep["month"])
        line = next(ln for ln in rep["lines"] if ln["id"] == line["id"])
        self.assertEqual(line["adjusted"], 300 - 216)
        adj = next(ln for ln in rep["lines"] if ln["kind"] == "adjust" and ln["ref_id"] == line["id"])
        self.assertEqual((adj["amount"], adj["note"]), (84, "торцы"))
        after = next(p for p in rep["people"] if p["name"] == "Алишер")
        self.assertEqual(after["total"] - before["total"], 84 + 500)

        code, months = self.req("/api/report/months")
        self.assertEqual(code, 200)
        self.assertEqual(months[0], {"month": rep["month"], "closed": False})
        code, err = self.req("/api/report/close", {"month": rep["month"]})
        self.assertEqual(code, 400)  # текущий месяц не закрыть
        code, err = self.req("/api/report?month=2026-13")
        self.assertEqual(code, 400)

    def test_report_close_reopen(self):
        code, _ = self.req("/api/report/close", {"month": "2020-01"})
        self.assertEqual(code, 200)
        code, months = self.req("/api/report/months")
        self.assertIn({"month": "2020-01", "closed": True}, months)
        code, rep = self.req("/api/report?month=2020-01")
        self.assertTrue(rep["closed"])
        self.assertTrue(rep["closed_at"])
        body = {"month": "2020-01", "worker_id": rep["workers"][0]["id"], "amount": 100, "note": "премия"}
        code, err = self.req("/api/report/manual", body)
        self.assertEqual(code, 400)
        self.assertIn("закрыт", err["error"])
        code, _ = self.req("/api/report/reopen", {"month": "2020-01"})
        self.assertEqual(code, 200)
        code, _ = self.req("/api/report/manual", body)
        self.assertEqual(code, 200)

    def test_report_pin(self):
        code, _ = self.req("/api/admin/settings", {"admin_pin": "2468"})
        self.assertEqual(code, 200)
        try:
            for path in ("/api/report", "/api/report/months", "/api/report/xlsx"):
                code, res = self.req(path)
                self.assertEqual((code, res.get("pin")), (401, True), path)
            code, _ = self.req("/api/report/close", {"month": "2020-02"})
            self.assertEqual(code, 401)
            code, res = self.req("/api/report/months", pin="2468")
            self.assertEqual(code, 200)
        finally:
            code, _ = self.req("/api/admin/settings", {"admin_pin": ""}, pin="2468")
            self.assertEqual(code, 200)

    def test_xlsx_download(self):
        code, data = self.req("/api/report/xlsx?month=2026-10")
        self.assertEqual(code, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))


if __name__ == "__main__":
    unittest.main()
