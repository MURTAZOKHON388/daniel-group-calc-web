"""
База цехового сервера — один файл SQLite (по умолчанию shop/data/shop.db).

Битрикс остаётся главным для заказов: таблицы deals и deal_products — только
кэш, который синк перезаписывает раз в минуту. Своё у цеха — люди, участки,
ставки, сессии работы, начисления (ledger), брак и очередь отправки в Битрикс
(outbox). Их и надо бэкапить.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
  id      INTEGER PRIMARY KEY,
  name    TEXT NOT NULL,
  badge   TEXT NOT NULL UNIQUE,          -- код на бейдже, его отдаёт сканер
  salary  REAL NOT NULL DEFAULT 0,       -- оклад за месяц, ₽
  active  INTEGER NOT NULL DEFAULT 1,
  is_master INTEGER NOT NULL DEFAULT 0   -- начальник производства: разрешает начать заказ без очереди
);

-- Участки в порядке прохождения заказа. stage_id — стадия сделки в Битриксе,
-- на которой заказ стоит в очереди этого участка.
CREATE TABLE IF NOT EXISTS sections (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL,
  sort           INTEGER NOT NULL DEFAULT 0,
  stage_id       TEXT NOT NULL DEFAULT '',
  kind           TEXT NOT NULL DEFAULT 'regular',   -- regular | otk
  skip_if_empty  INTEGER NOT NULL DEFAULT 0,        -- пропускать, если в заказе нет объёмов участка
  active         INTEGER NOT NULL DEFAULT 1
);

-- Операции участка и ставка рабочему за единицу. per_deal = 1 — объём не из
-- товаров сделки, а «1 заказ» (например, приёмка ОТК).
CREATE TABLE IF NOT EXISTS operations (
  id          INTEGER PRIMARY KEY,
  section_id  INTEGER NOT NULL REFERENCES sections(id),
  name        TEXT NOT NULL,
  unit        TEXT NOT NULL DEFAULT 'шт',
  rate        REAL NOT NULL DEFAULT 0,
  per_deal    INTEGER NOT NULL DEFAULT 0,
  active      INTEGER NOT NULL DEFAULT 1
);

-- Какой товар сделки — объём какой операции. product_key: "id:<PRODUCT_ID>"
-- для товара из каталога или "name:<название в нижнем регистре>" для строки
-- без товара. operation_id = NULL — товар осознанно не учитывается.
CREATE TABLE IF NOT EXISTS product_map (
  product_key   TEXT PRIMARY KEY,
  product_name  TEXT NOT NULL DEFAULT '',
  operation_id  INTEGER REFERENCES operations(id),
  factor        REAL NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS deals (
  id           INTEGER PRIMARY KEY,
  number       TEXT NOT NULL DEFAULT '',
  title        TEXT NOT NULL DEFAULT '',
  client       TEXT NOT NULL DEFAULT '',
  ship_date    TEXT,                           -- YYYY-MM-DD
  stage_id     TEXT NOT NULL DEFAULT '',
  category_id  TEXT NOT NULL DEFAULT '',
  synced_at    TEXT,
  gone         INTEGER NOT NULL DEFAULT 0,      -- закрыта или ушла из воронки
  problem      TEXT NOT NULL DEFAULT ''         -- причина «Проблемы» до следующего захода
);

CREATE TABLE IF NOT EXISTS deal_products (
  deal_id       INTEGER NOT NULL,
  product_key   TEXT NOT NULL,
  product_name  TEXT NOT NULL DEFAULT '',
  quantity      REAL NOT NULL DEFAULT 0,
  measure       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS deal_products_deal ON deal_products(deal_id);

-- Сессия — один заход на участок по заказу: «Начал» … «Готово».
CREATE TABLE IF NOT EXISTS sessions (
  id           INTEGER PRIMARY KEY,
  deal_id      INTEGER NOT NULL,
  section_id   INTEGER NOT NULL,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  result       TEXT,            -- done | remark | problem | cancelled | defect (ОТК нашёл брак)
  reason       TEXT NOT NULL DEFAULT '',
  comment      TEXT NOT NULL DEFAULT '',
  defect_id    INTEGER,         -- переделка по этому браку
  approved_by  INTEGER          -- кто разрешил начать без очереди (workers.id начальника)
);
CREATE INDEX IF NOT EXISTS sessions_deal ON sessions(deal_id, section_id);

CREATE TABLE IF NOT EXISTS session_workers (
  session_id  INTEGER NOT NULL,
  worker_id   INTEGER NOT NULL,
  PRIMARY KEY (session_id, worker_id)
);

-- Все деньги — строками: начисление за операцию (piece), вычет за брак
-- (defect), правка Алексея (adjust), ручное начисление (manual). Ставка и
-- объём сохраняются на момент начисления, правка ставок назад не действует.
CREATE TABLE IF NOT EXISTS ledger (
  id            INTEGER PRIMARY KEY,
  month         TEXT NOT NULL,           -- YYYY-MM
  worker_id     INTEGER NOT NULL,
  kind          TEXT NOT NULL,
  session_id    INTEGER,
  deal_id       INTEGER,
  section_id    INTEGER,
  operation_id  INTEGER,
  qty           REAL NOT NULL DEFAULT 0,
  unit          TEXT NOT NULL DEFAULT '',
  rate          REAL NOT NULL DEFAULT 0,
  share         REAL NOT NULL DEFAULT 1, -- доля при работе вдвоём
  amount        REAL NOT NULL,
  note          TEXT NOT NULL DEFAULT '',
  ref_id        INTEGER,                 -- исходная строка для правки или сторно
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_month ON ledger(month, worker_id);

CREATE TABLE IF NOT EXISTS defects (
  id                INTEGER PRIMARY KEY,
  deal_id           INTEGER NOT NULL,
  section_id        INTEGER NOT NULL,     -- чей брак
  session_id        INTEGER,              -- сессия, в которой участок делал заказ
  reason            TEXT NOT NULL DEFAULT '',
  worker_fault      INTEGER NOT NULL DEFAULT 1,
  policy            TEXT NOT NULL DEFAULT '',
  return_stage_id   TEXT NOT NULL DEFAULT '',  -- куда вернуть заказ после переделки
  reported_at       TEXT NOT NULL,
  reported_by       TEXT NOT NULL DEFAULT '',
  resolved_at       TEXT
);

CREATE TABLE IF NOT EXISTS defect_workers (
  defect_id  INTEGER NOT NULL,
  worker_id  INTEGER NOT NULL,
  PRIMARY KEY (defect_id, worker_id)
);

CREATE TABLE IF NOT EXISTS months (
  month      TEXT PRIMARY KEY,
  closed_at  TEXT NOT NULL,
  salaries   TEXT NOT NULL DEFAULT '{}'   -- оклады на момент закрытия, JSON {worker_id: ₽}
);

-- Всё, что пишется в Битрикс, сначала ложится сюда и уходит, когда есть связь.
CREATE TABLE IF NOT EXISTS outbox (
  id          INTEGER PRIMARY KEY,
  method      TEXT NOT NULL,
  params      TEXT NOT NULL,           -- JSON
  deal_id     INTEGER,
  kind        TEXT NOT NULL DEFAULT '', -- stage — смена стадии сделки
  created_at  TEXT NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'pending',  -- pending | done | failed
  done_at     TEXT
);
CREATE INDEX IF NOT EXISTS outbox_status ON outbox(status, id);
"""

DEFAULT_DB = Path(__file__).resolve().parent / "data" / "shop.db"


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: транзакции открываем сами через tx() — так синк и
    # терминалы не перетирают друг другу стадии сделок.
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


# Колонки, появившиеся после первых установок: в старой базе их добавляем на месте.
MIGRATIONS = [
    ("workers", "is_master", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "approved_by", "INTEGER"),
]


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


@contextmanager
def tx(conn: sqlite3.Connection):
    """Пишущая транзакция: BEGIN IMMEDIATE сразу берёт блокировку записи."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
