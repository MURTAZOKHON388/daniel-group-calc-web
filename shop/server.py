"""
Цеховой сервер DANIEL GROUP: терминалы участков, табло, справочники и отчёт.

Работает в локальной сети цеха (мини-ПК или офисный компьютер), планшеты и
ТВ открывают его по Wi-Fi. Только стандартная библиотека Python 3.10+.

Запуск:
    python shop/server.py                    # порт 8080, база shop/data/shop.db
    python shop/server.py --port 8090 --db D:/cex/shop.db
    python shop/server.py --demo             # поддельный Битрикс и демо-данные

Экраны:
    /            список экранов и адреса для планшетов
    /terminal    терминал участка (?section=<id>)
    /tablo       ТВ-табло (то же, что docs/tablo.html, но данные с сервера)
    /admin       справочники: Битрикс, люди, участки и ставки, товары
    /report      сделка за месяц, правки, закрытие месяца, Excel
"""

from __future__ import annotations

import argparse
import hmac
import json
import re
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import db  # noqa: E402
import logic  # noqa: E402
import sync  # noqa: E402
import xlsx  # noqa: E402
from bitrix import Bitrix, BitrixError, mask_webhook, normalize_webhook  # noqa: E402

WEB = HERE / "web"
ROOT = HERE.parent
TABLO = ROOT / "docs" / "tablo.html"
LOGO = ROOT / "source" / "assets" / "logo_daniel_group.png"
TABLO_API_PLACEHOLDER = '/*__SERVER_API__*/""'

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
}

# Настройки, которые можно менять из справочников (вебхук — отдельным запросом с проверкой).
EDITABLE_SETTINGS = {
    "category_id", "ship_field", "number_tpl", "final_stage_id", "board_stages", "notify_user_id",
    "remark_reasons", "problem_reasons", "defect_reasons", "defect_policy", "shadow_mode", "admin_pin",
}

ROUTES: list[tuple[str, re.Pattern, str, bool]] = []


def route(method: str, pattern: str, auth: bool = False):
    def deco(fn):
        ROUTES.append((method, re.compile(pattern), fn.__name__, auth))
        return fn
    return deco


def lan_addresses() -> list[str]:
    """IP этого компьютера в локальной сети — их вводят на планшетах."""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # пакет не уходит, просто выбирается интерфейс
        out.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    return out or ["127.0.0.1"]


def _int(v, name="значение") -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise logic.ShopError(f"Неверное {name}") from None


def _num(v, name="число") -> float:
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        raise logic.ShopError(f"Неверное {name}") from None


class Handler(BaseHTTPRequestHandler):
    server_version = "DanielShop/1.0"
    app: "App"

    # ---------- транспорт ----------
    def log_message(self, fmt, *args):
        if self.command == "POST" or (len(args) > 1 and str(args[1])[:1] in "45"):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _file(self, path: Path, replace: tuple[str, str] | None = None):
        if not path.is_file():
            return self._json({"error": "Не найдено"}, 404)
        data = path.read_bytes()
        if replace:
            data = data.replace(replace[0].encode(), replace[1].encode())
        self._send(200, data, CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise logic.ShopError("Слишком большой запрос")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise logic.ShopError("Неверный JSON") from None
        return data if isinstance(data, dict) else {}

    def _auth_ok(self, conn) -> bool:
        pin = str(logic.setting(conn, "admin_pin") or "")
        if not pin:
            return True
        return hmac.compare_digest(self.headers.get("X-Pin", "").encode(), pin.encode())

    def _dispatch(self, method: str):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        conn = db.connect(self.app.db_path)
        try:
            for m, rx, name, auth in ROUTES:
                match = rx.fullmatch(path)
                if m != method or not match:
                    continue
                if auth and not self._auth_ok(conn):
                    return self._json({"error": "Нужен PIN", "pin": True}, 401)
                body = self._body() if method == "POST" else {}
                return getattr(self, name)(conn, q, body, *match.groups())
            return self._json({"error": "Не найдено"}, 404)
        except logic.ShopError as e:
            self._json({"error": str(e)}, 400)
        except BitrixError as e:
            self._json({"error": str(e)}, 502)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 — сервер цеха не должен падать от одной ошибки
            traceback.print_exc()
            self._json({"error": f"Внутренняя ошибка: {e}"}, 500)
        finally:
            conn.close()

    # ---------- страницы ----------
    @route("GET", r"/")
    def page_index(self, conn, q, body):
        self._file(WEB / "index.html")

    @route("GET", r"/(terminal|admin|report|print)")
    def page(self, conn, q, body, name):
        self._file(WEB / f"{name}.html")

    @route("GET", r"/tablo")
    def page_tablo(self, conn, q, body):
        self._file(TABLO, (TABLO_API_PLACEHOLDER, '/*__SERVER_API__*/"/api/board"'))

    @route("GET", r"/static/logo\.png")
    def static_logo(self, conn, q, body):
        self._file(LOGO)

    @route("GET", r"/static/([A-Za-z0-9_./-]+)")
    def static(self, conn, q, body, rel):
        p = (WEB / rel).resolve()
        if WEB.resolve() not in p.parents or ".." in rel:
            return self._json({"error": "Не найдено"}, 404)
        self._file(p)

    # ---------- терминал и табло ----------
    @route("GET", r"/api/board")
    def api_board(self, conn, q, body):
        self._json(logic.board(conn))

    @route("GET", r"/api/info")
    def api_info(self, conn, q, body):
        s = logic.get_settings(conn)
        self._json({
            "sections": [dict(id=r["id"], name=r["name"], kind=r["kind"]) for r in logic.sections(conn)],
            "addresses": [f"http://{ip}:{self.server.server_address[1]}" for ip in lan_addresses()],
            "connected": bool(s["webhook"]),
            "demo": self.app.demo,
        })

    def _sync_status(self, conn, s=None) -> dict:
        s = s or logic.get_settings(conn)
        pending = conn.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending'").fetchone()[0]
        return {"at": s["last_sync_at"], "ok_at": s["last_sync_ok_at"], "error": s["last_sync_error"], "pending": pending}

    @route("GET", r"/api/terminal/(\d+)")
    def api_terminal(self, conn, q, body, section_id):
        s = logic.get_settings(conn)
        section = logic.get_section(conn, int(section_id))
        opened = []
        for r in conn.execute(
            """SELECT x.*, d.number, d.client, d.title FROM sessions x JOIN deals d ON d.id = x.deal_id
               WHERE x.section_id = ? AND x.finished_at IS NULL ORDER BY x.started_at""",
            (section["id"],),
        ):
            opened.append({"session_id": r["id"], "deal_id": r["deal_id"], "number": r["number"], "client": r["client"],
                           "title": r["title"], "started_at": r["started_at"], "rework": bool(r["defect_id"]),
                           "workers": [w["name"] for w in logic.session_workers(conn, r["id"])]})
        self._json({
            "section": {"id": section["id"], "name": section["name"], "kind": section["kind"],
                        "stage_id": section["stage_id"]},
            "sections": [dict(id=r["id"], name=r["name"], kind=r["kind"]) for r in logic.sections(conn)],
            "today": logic.now().strftime("%Y-%m-%d"),  # дата сервера: часы планшета могут врать
            "queue": logic.queue(conn, section["id"]),
            "open": opened,
            "reasons": {"remark": s["remark_reasons"], "problem": s["problem_reasons"], "defect": s["defect_reasons"]},
            "shadow": s["shadow_mode"] == "1",
            "sync": self._sync_status(conn, s),
        })

    @route("POST", r"/api/scan")
    def api_scan(self, conn, q, body):
        section = logic.get_section(conn, _int(body.get("section_id"), "участок"))
        section_id = section["id"]
        code = str(body.get("code") or "").strip()
        if not code:
            raise logic.ShopError("Пустой код")
        w = logic.find_worker(conn, code)
        if w:
            if not w["active"]:
                raise logic.ShopError(f"{w['name']}: бейдж отключён")
            return self._json({"type": "worker", "worker": {"id": w["id"], "name": w["name"], "master": bool(w["is_master"])},
                               **logic.worker_totals(conn, w["id"])})
        d = logic.find_deal(conn, code)
        if not d:
            return self._json({"type": "unknown", "code": code})
        working, rework = logic._deal_flags(conn)
        s = logic.open_session(conn, d["id"], section_id)
        self._json({
            "type": "deal",
            "deal": logic.deal_dict(d, working, rework) | {
                "stageName": logic.stage_name(conn, d["stage_id"]),
                "gone": bool(d["gone"]),
                "volumes": logic.section_volumes(conn, d["id"], logic.section_ops(conn, section_id)),
            },
            "in_queue": logic.in_queue(section, d),  # нет — начать можно только с бейджем начальника
            "open_session": {"session_id": s["id"], "started_at": s["started_at"], "rework": bool(s["defect_id"]),
                             "workers": [x["name"] for x in logic.session_workers(conn, s["id"])],
                             "worker_ids": [x["id"] for x in logic.session_workers(conn, s["id"])]} if s else None,
            "rework_here": bool(logic.open_defect(conn, d["id"], section_id)),
            "passed": logic.passed_sections(conn, d["id"]),
        })

    @route("POST", r"/api/start")
    def api_start(self, conn, q, body):
        ids = [_int(x, "сотрудник") for x in body.get("worker_ids") or []]
        approved = _int(body["approved_by"], "начальник") if body.get("approved_by") else None
        res = logic.start_session(conn, _int(body.get("section_id"), "участок"), _int(body.get("deal_id"), "сделка"),
                                  ids, approved)
        if approved:
            self.app.syncer.wake()
        self._json(res)

    @route("POST", r"/api/finish")
    def api_finish(self, conn, q, body):
        res = logic.finish_session(conn, _int(body.get("session_id"), "сессия"), str(body.get("result") or ""),
                                   str(body.get("reason") or "").strip(), str(body.get("comment") or "").strip())
        res["shadow"] = logic.setting(conn, "shadow_mode") == "1"
        self.app.syncer.wake()
        self._json(res)

    @route("POST", r"/api/defect")
    def api_defect(self, conn, q, body):
        res = logic.report_defect(conn, _int(body.get("deal_id"), "сделка"), _int(body.get("section_id"), "участок"),
                                  str(body.get("reason") or "").strip(), bool(body.get("worker_fault", True)),
                                  str(body.get("reported_by") or "ОТК"))
        self.app.syncer.wake()
        self._json(res)

    # ---------- справочники ----------
    @route("GET", r"/api/admin/state", auth=True)
    def api_admin_state(self, conn, q, body):
        s = logic.get_settings(conn)
        products = []
        for r in conn.execute(
            """SELECT p.product_key, MAX(p.product_name) AS name, MAX(p.measure) AS measure,
                      COUNT(DISTINCT p.deal_id) AS deals, SUM(p.quantity) AS qty,
                      m.operation_id, m.factor, m.product_key IS NOT NULL AS mapped
               FROM deal_products p JOIN deals d ON d.id = p.deal_id AND d.gone = 0
               LEFT JOIN product_map m ON m.product_key = p.product_key
               GROUP BY p.product_key ORDER BY mapped, name"""
        ):
            products.append(dict(r))
        seen = {p["product_key"] for p in products}
        for r in conn.execute("SELECT * FROM product_map ORDER BY product_name"):
            if r["product_key"] not in seen:
                products.append({"product_key": r["product_key"], "name": r["product_name"], "measure": "", "deals": 0,
                                 "qty": 0, "operation_id": r["operation_id"], "factor": r["factor"], "mapped": 1})
        opened = [dict(r) | {"workers": [w["name"] for w in logic.session_workers(conn, r["id"])]} for r in conn.execute(
            """SELECT x.id, x.started_at, d.number, s.name AS section FROM sessions x
               JOIN deals d ON d.id = x.deal_id JOIN sections s ON s.id = x.section_id
               WHERE x.finished_at IS NULL ORDER BY x.started_at""")]
        failed = [dict(r) for r in conn.execute(
            "SELECT id, method, deal_id, created_at, attempts, last_error FROM outbox WHERE status = 'failed' ORDER BY id DESC LIMIT 30")]
        problems = [dict(r) for r in conn.execute(
            "SELECT id, number, client, problem FROM deals WHERE problem != '' AND gone = 0 ORDER BY id")]
        settings = {k: s[k] for k in EDITABLE_SETTINGS if k != "admin_pin"}
        settings["webhook_masked"] = mask_webhook(s["webhook"])
        settings["has_pin"] = bool(s["admin_pin"])
        self._json({
            "settings": settings,
            "meta": s["meta_cache"],
            "stages": s["stages_cache"],
            "workers": [dict(r) for r in conn.execute("SELECT * FROM workers ORDER BY active DESC, name")],
            "sections": [dict(r) for r in logic.sections(conn, active_only=False)],
            "operations": [dict(r) for r in conn.execute("SELECT * FROM operations ORDER BY section_id, id")],
            "products": products,
            "open_sessions": opened,
            "problems": problems,
            "sync": self._sync_status(conn, s) | {"failed": failed},
            "deals_count": conn.execute("SELECT COUNT(*) FROM deals WHERE gone = 0").fetchone()[0],
            "next_badge": logic.next_badge(conn),
            "demo": self.app.demo,
        })

    @route("POST", r"/api/admin/settings", auth=True)
    def api_admin_settings(self, conn, q, body):
        bad = set(body) - EDITABLE_SETTINGS
        if bad:
            raise logic.ShopError("Нельзя менять: " + ", ".join(sorted(bad)))
        if "defect_policy" in body and body["defect_policy"] not in ("unpaid", "deduct"):
            raise logic.ShopError("Неизвестная политика брака")
        for k in ("remark_reasons", "problem_reasons", "defect_reasons", "board_stages"):
            if k in body and not isinstance(body[k], list):
                raise logic.ShopError("Ожидается список")
        with db.tx(conn):
            logic.set_settings(conn, body)
        if {"category_id", "ship_field", "number_tpl"} & set(body):
            self.app.syncer.wake(pull=True)
        self._json({"ok": True})

    @route("POST", r"/api/admin/webhook", auth=True)
    def api_admin_webhook(self, conn, q, body):
        url = normalize_webhook(str(body.get("webhook") or ""))
        if not url:
            raise logic.ShopError("Адрес должен быть вида https://портал.bitrix24.ru/rest/1/ключ/")
        client = self.app.client_for(url)
        meta = sync.fetch_meta(client)
        with db.tx(conn):
            logic.set_settings(conn, {"webhook": url, "meta_cache": meta})
        self.app.syncer.wake(pull=True)
        self._json({"ok": True, "meta": meta, "webhook_masked": mask_webhook(url)})

    @route("POST", r"/api/admin/meta", auth=True)
    def api_admin_meta(self, conn, q, body):
        s = logic.get_settings(conn)
        if not s["webhook"]:
            raise logic.ShopError("Сначала укажите вебхук")
        meta = sync.fetch_meta(self.app.client_for(s["webhook"]))
        with db.tx(conn):
            logic.set_settings(conn, {"meta_cache": meta})
        self._json({"ok": True, "meta": meta})

    @route("POST", r"/api/admin/sync", auth=True)
    def api_admin_sync(self, conn, q, body):
        self._json(self.app.syncer.sync_now())

    @route("POST", r"/api/admin/worker", auth=True)
    def api_admin_worker(self, conn, q, body):
        name = str(body.get("name") or "").strip()
        if not name:
            raise logic.ShopError("Укажите имя")
        badge = str(body.get("badge") or "").strip() or logic.next_badge(conn)
        salary = _num(body.get("salary") or 0, "оклад")
        active = 1 if body.get("active", True) else 0
        master = 1 if body.get("is_master") else 0
        with db.tx(conn):
            clash = conn.execute("SELECT id FROM workers WHERE upper(badge) = upper(?)", (badge,)).fetchone()
            if clash and clash["id"] != body.get("id"):
                raise logic.ShopError(f"Бейдж {badge} уже занят")
            if body.get("id"):
                conn.execute("UPDATE workers SET name = ?, badge = ?, salary = ?, active = ?, is_master = ? WHERE id = ?",
                             (name, badge, salary, active, master, _int(body["id"])))
            else:
                conn.execute("INSERT INTO workers(name, badge, salary, active, is_master) VALUES(?, ?, ?, ?, ?)",
                             (name, badge, salary, active, master))
        self._json({"ok": True})

    @route("POST", r"/api/admin/section", auth=True)
    def api_admin_section(self, conn, q, body):
        name = str(body.get("name") or "").strip()
        if not name:
            raise logic.ShopError("Укажите название участка")
        vals = (name, _int(body.get("sort") or 0, "порядок"), str(body.get("stage_id") or ""),
                "otk" if body.get("kind") == "otk" else "regular", 1 if body.get("skip_if_empty") else 0,
                1 if body.get("active", True) else 0)
        with db.tx(conn):
            if body.get("id"):
                conn.execute("UPDATE sections SET name = ?, sort = ?, stage_id = ?, kind = ?, skip_if_empty = ?, active = ? "
                             "WHERE id = ?", vals + (_int(body["id"]),))
            else:
                conn.execute("INSERT INTO sections(name, sort, stage_id, kind, skip_if_empty, active) VALUES(?, ?, ?, ?, ?, ?)",
                             vals)
        self._json({"ok": True})

    @route("POST", r"/api/admin/operation", auth=True)
    def api_admin_operation(self, conn, q, body):
        name = str(body.get("name") or "").strip()
        if not name:
            raise logic.ShopError("Укажите название операции")
        section_id = _int(body.get("section_id"), "участок")
        logic.get_section(conn, section_id)
        vals = (section_id, name, str(body.get("unit") or "шт").strip(), _num(body.get("rate") or 0, "ставка"),
                1 if body.get("per_deal") else 0, 1 if body.get("active", True) else 0)
        with db.tx(conn):
            if body.get("id"):
                conn.execute("UPDATE operations SET section_id = ?, name = ?, unit = ?, rate = ?, per_deal = ?, active = ? "
                             "WHERE id = ?", vals + (_int(body["id"]),))
            else:
                conn.execute("INSERT INTO operations(section_id, name, unit, rate, per_deal, active) VALUES(?, ?, ?, ?, ?, ?)",
                             vals)
        self._json({"ok": True})

    @route("POST", r"/api/admin/product", auth=True)
    def api_admin_product(self, conn, q, body):
        key = str(body.get("product_key") or "")
        if not re.fullmatch(r"(id|name):.+", key):
            raise logic.ShopError("Неверный ключ товара")
        with db.tx(conn):
            if body.get("delete"):
                conn.execute("DELETE FROM product_map WHERE product_key = ?", (key,))
            else:
                op = body.get("operation_id")
                op = _int(op, "операция") if op not in (None, "", "null") else None
                conn.execute(
                    """INSERT INTO product_map(product_key, product_name, operation_id, factor) VALUES(?, ?, ?, ?)
                       ON CONFLICT(product_key) DO UPDATE SET product_name = excluded.product_name,
                       operation_id = excluded.operation_id, factor = excluded.factor""",
                    (key, str(body.get("product_name") or ""), op, _num(body.get("factor") or 1, "коэффициент")),
                )
        self._json({"ok": True})

    @route("POST", r"/api/admin/outbox", auth=True)
    def api_admin_outbox(self, conn, q, body):
        oid = _int(body.get("id"), "запись")
        with db.tx(conn):
            if body.get("action") == "retry":
                conn.execute("UPDATE outbox SET status = 'pending', attempts = 0 WHERE id = ? AND status = 'failed'", (oid,))
            elif body.get("action") == "drop":
                conn.execute("UPDATE outbox SET status = 'dropped' WHERE id = ? AND status = 'failed'", (oid,))
        self.app.syncer.wake()
        self._json({"ok": True})

    @route("POST", r"/api/admin/session/cancel", auth=True)
    def api_admin_session_cancel(self, conn, q, body):
        logic.cancel_session(conn, _int(body.get("session_id"), "сессия"))
        self._json({"ok": True})

    @route("POST", r"/api/admin/deal/clear_problem", auth=True)
    def api_admin_clear_problem(self, conn, q, body):
        with db.tx(conn):
            conn.execute("UPDATE deals SET problem = '' WHERE id = ?", (_int(body.get("deal_id"), "сделка"),))
        self._json({"ok": True})

    @route("GET", r"/api/admin/print", auth=True)
    def api_admin_print(self, conn, q, body):
        out = {"workers": [], "deals": []}
        if q.get("badges"):
            ids = [int(x) for x in q["badges"].split(",") if x.isdigit() and int(x) > 0]  # "all" — все
            rows = conn.execute("SELECT * FROM workers WHERE active = 1 ORDER BY name").fetchall()
            out["workers"] = [{"name": r["name"], "badge": r["badge"]} for r in rows if not ids or r["id"] in ids]
        if q.get("deals"):
            for x in q["deals"].split(","):
                d = logic.find_deal(conn, x.strip()) if x.strip() else None
                if d:
                    out["deals"].append({"id": d["id"], "number": d["number"], "client": d["client"], "title": d["title"],
                                         "shipDate": d["ship_date"]})
        self._json(out)

    @route("GET", r"/api/admin/deals", auth=True)
    def api_admin_deals(self, conn, q, body):
        rows = conn.execute(
            "SELECT id, number, client, title, ship_date, stage_id FROM deals WHERE gone = 0 "
            "ORDER BY ship_date IS NULL, ship_date, id").fetchall()
        self._json({"deals": [dict(r) | {"stage": logic.stage_name(conn, r["stage_id"])} for r in rows]})

    # ---------- отчёт ----------
    @route("GET", r"/api/report", auth=True)
    def api_report(self, conn, q, body):
        month = q.get("month") or logic.now().strftime("%Y-%m")
        rep = logic.month_report(conn, month)
        rep["workers"] = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
            "SELECT id, name FROM workers WHERE active = 1 ORDER BY name")]
        self._json(rep)

    @route("POST", r"/api/report/adjust", auth=True)
    def api_report_adjust(self, conn, q, body):
        logic.adjust_line(conn, _int(body.get("ledger_id"), "строка"), _num(body.get("amount"), "сумма"),
                          str(body.get("note") or ""))
        self._json({"ok": True})

    @route("POST", r"/api/report/manual", auth=True)
    def api_report_manual(self, conn, q, body):
        logic.manual_line(conn, str(body.get("month") or ""), _int(body.get("worker_id"), "сотрудник"),
                          _num(body.get("amount"), "сумма"), str(body.get("note") or ""))
        self._json({"ok": True})

    @route("POST", r"/api/report/close", auth=True)
    def api_report_close(self, conn, q, body):
        logic.close_month(conn, str(body.get("month") or ""))
        self._json({"ok": True})

    @route("POST", r"/api/report/reopen", auth=True)
    def api_report_reopen(self, conn, q, body):
        logic.reopen_month(conn, str(body.get("month") or ""))
        self._json({"ok": True})

    @route("GET", r"/api/report/xlsx", auth=True)
    def api_report_xlsx(self, conn, q, body):
        month = q.get("month") or logic.now().strftime("%Y-%m")
        data = report_xlsx(logic.month_report(conn, month))
        name = f"sdelka_{month}.xlsx"
        self._send(200, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                   {"Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}"})


KIND_LABELS = {"piece": "сделка", "defect": "вычет за брак", "adjust": "правка", "manual": "ручное"}


def report_xlsx(rep: dict) -> bytes:
    shadow = " — ТЕНЕВОЙ РЕЖИМ, не к выплате" if rep["shadow"] else ""
    summary = [["Сотрудник", "Оклад", "Сделка", "Вычеты за брак", "Правки и ручные", "Итого", "Заказов", "Браков"]]
    for p in rep["people"]:
        summary.append([p["name"], p["salary"], p["piece"], p["defect"], p["adjust"], p["total"], p["deals"], p["defects"]])
    summary.append([])
    summary.append([f"Месяц {rep['month']}" + (" (закрыт)" if rep["closed"] else " (открыт)") + shadow])
    lines = [["Дата", "Сотрудник", "Заказ", "Клиент", "Участок", "Операция", "Объём", "Ед.", "Ставка", "Доля",
              "Сумма", "Вид", "Напарники", "Примечание"]]
    for ln in rep["lines"]:
        lines.append([ln["created_at"], ln["worker"], ln["deal_number"] or "", ln["client"] or "", ln["section"] or "",
                      ln["operation"] or "", ln["qty"] or "", ln["unit"], ln["rate"] or "",
                      round(ln["share"], 3) if ln["kind"] == "piece" else "", ln["amount"],
                      KIND_LABELS.get(ln["kind"], ln["kind"]), ", ".join(ln["partners"]), ln["note"]])
    defects = [["Дата", "Заказ", "Участок", "Причина", "Вина рабочего", "Делали", "Политика", "Переделано"]]
    for d in rep["defects"]:
        defects.append([d["reported_at"], d["deal_number"] or "", d["section"] or "", d["reason"], bool(d["worker_fault"]),
                        d["workers"] or "", logic.POLICY_LABELS.get(d["policy"], d["policy"]), d["resolved_at"] or "нет"])
    return xlsx.write_xlsx([
        {"name": "Свод", "rows": summary, "widths": [22, 12, 12, 15, 16, 12, 9, 8], "money": {1, 2, 3, 4, 5}},
        {"name": "Строки", "rows": lines, "widths": [19, 16, 11, 22, 12, 18, 9, 7, 8, 7, 11, 13, 18, 34], "money": {10}},
        {"name": "Брак", "rows": defects, "widths": [19, 11, 12, 20, 13, 20, 36, 19]},
    ])


class App:
    def __init__(self, db_path, syncer: sync.Syncer, demo_client=None):
        self.db_path = db_path
        self.syncer = syncer
        self.demo_client = demo_client
        self.demo = demo_client is not None

    def client_for(self, webhook: str):
        return self.demo_client if self.demo else Bitrix(webhook)


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Цеховой сервер DANIEL GROUP")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--db", default=str(db.DEFAULT_DB))
    ap.add_argument("--interval", type=float, default=60, help="как часто забирать сделки из Битрикса, сек")
    ap.add_argument("--demo", action="store_true", help="поддельный Битрикс и демо-данные (база data/demo.db)")
    args = ap.parse_args(argv)

    demo_client = None
    db_path = args.db
    if args.demo:
        import demo
        db_path = str(HERE / "data" / "demo.db")
        for suffix in ("", "-wal", "-shm"):
            Path(db_path + suffix).unlink(missing_ok=True)
        demo_client = demo.FakeBitrix()
    conn = db.connect(db_path)
    db.init(conn)
    if demo_client:
        import demo
        demo.seed(conn)
        with db.tx(conn):
            logic.set_settings(conn, {"meta_cache": sync.fetch_meta(demo_client)})
    conn.close()

    make_client = (lambda s: demo_client) if demo_client else sync.make_client
    syncer = sync.Syncer(db_path, make_client, interval=args.interval)
    syncer.start()
    app = App(db_path, syncer, demo_client)
    srv = make_server(app, args.host, args.port)
    print("Цеховой сервер DANIEL GROUP" + (" — ДЕМО" if demo_client else ""))
    print(f"  база: {db_path}")
    for ip in lan_addresses():
        print(f"  открыть: http://{ip}:{args.port}/")
    print("  остановить: Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        syncer.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
