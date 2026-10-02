"""
Демо терминала для телефона: один HTML без сервера и Битрикса, на примерных данных.

Берёт настоящий shop/web/terminal.html и подменяет сервер цеха кодом в браузере
(mock.js повторяет правила logic.py на демо-данных demo.py). Нужен, чтобы
показать терминал и проверить камеру с телефона по обычной ссылке.

    python shop/phone-demo/build.py                    # → shop/phone-demo/index.html
    python shop/phone-demo/build.py --artifact out.html # то же без <html>/<head> — для страницы в Claude

После правок терминала пересоберите и закоммитьте index.html.
"""

from __future__ import annotations

import argparse
import base64
import re
from pathlib import Path

D = Path(__file__).resolve().parent
SHOP = D.parent
ROOT = SHOP.parent


def build() -> str:
    term = (SHOP / "web/terminal.html").read_text(encoding="utf-8")
    css_common = (SHOP / "web/common.css").read_text(encoding="utf-8")
    js_common = (SHOP / "web/common.js").read_text(encoding="utf-8")
    js_qr = (SHOP / "web/vendor/jsQR.min.js").read_text(encoding="utf-8")
    logo = "data:image/png;base64," + base64.b64encode((ROOT / "source/assets/logo_daniel_group.png").read_bytes()).decode()

    style = re.search(r"<style>(.*?)</style>", term, re.S).group(1)
    body = re.search(r"<body>\n(.*?)<script src=\"/static/vendor/jsQR.min.js\"></script>", term, re.S).group(1)
    script = re.search(r"<script src=\"/static/common.js\"></script>\n<script>(.*?)</script>\n</body>", term, re.S).group(1)

    patches = [
        # Участок — в адресе после #: своя ссылка у каждого планшета (#raspil, #kromka …).
        ('  if (new URLSearchParams(location.search).get("section") !== String(id)) setUrl("/terminal?section=" + id);\n',
         '  if (location.hash.slice(1) !== demoSlug(id)) setUrl("#" + demoSlug(id));\n'),
        ('  if (!confirm("Сменить участок этого планшета?")) { keepFocus(); return; }\n', ""),
        ('  setUrl("/terminal");\n', '  setUrl(location.pathname + location.search);\n'),
        ('const fromUrl = new URLSearchParams(location.search).get("section");',
         'const fromUrl = String({ raspil: 1, kromka: 2, prisadka: 3, upakovka: 4, otk: 5 }[location.hash.slice(1)] || "");'),
        ("const saved = fromUrl || lsGet(SEC_KEY);", 'const saved = fromUrl || lsGet(SEC_KEY) || "1";'),
        ('  if (e.key === "Escape" && !$("sheet").hidden) { closeSheet(); return; }',
         '  if (e.key === "Escape" && !$("sheet").hidden) { closeSheet(); return; }\n  if (!$("demoSheet").hidden) return;'),
        (': "Камера работает только на защищённом адресе (https). Как настроить планшет — README, раздел «Камера».");',
         ': "Здесь камера недоступна — откройте демо по ссылке в Chrome или Safari. Без камеры — «Тест-сканер» вверху.");'),
        ('const CAM_DENIED = "Нет доступа к камере. Разрешите камеру для этой страницы в настройках браузера.";',
         'const CAM_DENIED = "Нет доступа к камере. Внутри Claude камера запрещена — откройте демо по ссылке в Chrome или '
         'Safari. Если вы уже там — разрешите камеру для страницы в настройках браузера.";'),
    ]
    for old, new in patches:
        assert script.count(old) == 1, f"терминал изменился, правка демо не нашла: {old[:60]}"
        script = script.replace(old, new)
    body = body.replace("/static/logo.png", logo)
    assert "/static/" not in body

    part = lambda name: (D / name).read_text(encoding="utf-8")  # noqa: E731
    return f"""<title>Терминал участка DANIEL GROUP</title>
<style>
{css_common}
</style>
<style>{style}</style>
<style>
{part("glue.css")}
</style>
{part("glue.html")}
{body}
<script>
{part("guard.js")}
</script>
<script>
{js_qr}
</script>
<script>
{part("mock.js")}
</script>
<script>
{js_common}
</script>
<script>{script}</script>
<script>
{part("glue.js")}
</script>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--artifact", help="ещё записать вариант без <html>/<head> сюда")
    args = ap.parse_args()
    page = build()
    head = ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')
    (D / "index.html").write_text(head + page + "</body>\n</html>\n", encoding="utf-8")
    print(f"index.html: {len(page) // 1024} КБ")
    if args.artifact:
        Path(args.artifact).write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
