"""
Клиент REST Битрикс24 по входящему вебхуку — только stdlib.

Ошибки делятся на два вида:
- BitrixOffline — нет связи или портал перегружен: повторим позже, очередь
  не трогаем;
- BitrixError — Битрикс ответил ошибкой по существу (нет прав, неверная
  стадия): повтор не поможет, запись в outbox помечается как неудачная.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

# Ошибки, при которых стоит просто подождать и повторить.
TRANSIENT_ERRORS = {"QUERY_LIMIT_EXCEEDED", "INTERNAL_SERVER_ERROR", "OVERLOAD_LIMIT"}


class BitrixError(Exception):
    pass


class BitrixOffline(BitrixError):
    pass


def normalize_webhook(url: str) -> str:
    """Из вставленного адреса (можно с методом на конце) оставляет
    https://портал/rest/<user>/<ключ>/ или пустую строку."""
    m = re.match(r"^(https?://[^/]+/rest/\d+/[^/?#]+)", (url or "").strip(), re.I)
    return m.group(1) + "/" if m else ""


def mask_webhook(url: str) -> str:
    base = normalize_webhook(url)
    if not base:
        return ""
    head, _, _ = base.rstrip("/").rpartition("/")
    return head + "/••••••/"


def build_query(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """PHP-подобная сериализация вложенных параметров: filter[ID][0]=5.
    Нужна для команд внутри batch, которые передаются строкой."""
    out: list[tuple[str, str]] = []
    items = params.items() if isinstance(params, dict) else enumerate(params)
    for k, v in items:
        key = f"{prefix}[{k}]" if prefix else str(k)
        if isinstance(v, (dict, list, tuple)):
            out.extend(build_query(v if not isinstance(v, tuple) else list(v), key))
        elif v is None:
            out.append((key, ""))
        elif isinstance(v, bool):
            out.append((key, "Y" if v else "N"))
        else:
            out.append((key, str(v)))
    return out


class Bitrix:
    def __init__(self, webhook: str, timeout: float = 20):
        self.base = normalize_webhook(webhook)
        if not self.base:
            raise BitrixError("Не задан или неверный адрес вебхука")
        self.timeout = timeout

    def call(self, method: str, params: dict | None = None) -> dict:
        data = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base + method + ".json",
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            payload = _json_or_none(body)
            if payload and payload.get("error"):
                raise _error_from(payload) from None
            if e.code >= 500 or e.code == 429:
                raise BitrixOffline(f"Битрикс недоступен: HTTP {e.code}") from None
            raise BitrixError(f"Битрикс: HTTP {e.code}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            raise BitrixOffline(f"Нет связи с Битриксом: {getattr(e, 'reason', e)}") from None
        payload = _json_or_none(body)
        if payload is None:
            raise BitrixOffline("Битрикс вернул не JSON")
        if payload.get("error"):
            raise _error_from(payload)
        return payload

    def list_all(self, method: str, params: dict | None = None, pick=None, limit: int = 5000) -> list:
        """Списочные методы отдают по 50 записей, дальше — через start."""
        out: list = []
        start = 0
        while True:
            payload = self.call(method, {**(params or {}), "start": start})
            rows = pick(payload.get("result")) if pick else payload.get("result")
            out.extend(rows or [])
            nxt = payload.get("next")
            if nxt is None or len(out) >= limit:
                return out
            start = nxt

    def batch(self, commands: dict[str, tuple[str, dict]]) -> dict:
        """До 50 вызовов за один запрос. Возвращает {ключ: result}.
        Ошибка любой команды — исключение (частичный результат нам не нужен)."""
        cmd = {
            key: method + "?" + urllib.parse.urlencode(build_query(params or {}))
            for key, (method, params) in commands.items()
        }
        payload = self.call("batch", {"halt": 1, "cmd": cmd})
        result = payload.get("result") or {}
        errors = result.get("result_error") or {}
        if errors:
            key, err = next(iter(errors.items()))
            if isinstance(err, dict):
                raise _error_from(err)
            raise BitrixError(f"Битрикс: ошибка в команде {key}: {err}")
        res = result.get("result") or {}
        if isinstance(res, list):  # пустой результат PHP отдаёт как []
            res = {}
        return res


def _json_or_none(body: bytes):
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _error_from(payload: dict) -> BitrixError:
    code = str(payload.get("error") or "")
    text = payload.get("error_description") or code
    cls = BitrixOffline if code in TRANSIENT_ERRORS else BitrixError
    return cls(f"Битрикс: {text}")
