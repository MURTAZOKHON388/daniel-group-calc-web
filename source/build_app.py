"""
Собирает webapp/index.html из app_template.html, встраивая прайсы из
assets/*.csv как JSON прямо в страницу.

Единый источник цен: те же CSV, что использует build_excel.py. Правишь
прайс — перезапускаешь этот скрипт — переопубликовываешь артефакт. Цифры
не дублируются в коде страницы вручную.

Использование:
    python source/build_app.py
"""

import base64
import csv
import json
import mimetypes
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
TEMPLATE = HERE / "app_template.html"
OUTPUT = HERE.parent / "docs" / "index.html"

PLACEHOLDER_LDSP = "/*__LDSP_DATA__*/[]"
PLACEHOLDER_KROMKA = "/*__KROMKA_DATA__*/[]"
PLACEHOLDER_MISC = "/*__MISC_DATA__*/[]"
PLACEHOLDER_LOGO = "__LOGO_DATA_URI__"


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def num(value: str) -> float:
    return float(str(value).replace(",", "."))


def load_ldsp() -> list[dict]:
    rows = read_csv(ASSETS / "price_ldsp.csv")
    return [
        {
            "decor": r["decor"],
            "st": r["st"],
            "name": r["name"],
            "group": int(num(r["price_group"])),
            "priceM2": num(r["price_m2"]),
            "priceSheet": num(r["price_sheet"]),
        }
        for r in rows
    ]


def load_kromka() -> list[dict]:
    rows = read_csv(ASSETS / "price_kromka.csv")
    return [
        {
            "name": r["name"],
            "thickness": num(r["thickness_mm"]),
            "width": num(r["width_mm"]),
            "priceSolid": num(r["price_solid"]),
            "priceWood": num(r["price_wood"]),
            "lot": num(r["lot_multiple_m"]),
        }
        for r in rows
    ]


def load_misc() -> list[dict]:
    rows = read_csv(ASSETS / "price_list.csv")
    return [
        {
            "category": r["category"],
            "item": r["item"],
            "unit": r["unit"],
            "price": num(r["price"]),
        }
        for r in rows
    ]


def load_logo_data_uri() -> str:
    logo = ASSETS / "logo_daniel_group.png"
    if not logo.exists():
        return ""
    mime = mimetypes.guess_type(str(logo))[0] or "image/png"
    b64 = base64.b64encode(logo.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def main() -> None:
    html = TEMPLATE.read_text(encoding="utf-8")

    ldsp = load_ldsp()
    kromka = load_kromka()
    misc = load_misc()

    for placeholder in (PLACEHOLDER_LDSP, PLACEHOLDER_KROMKA, PLACEHOLDER_MISC, PLACEHOLDER_LOGO):
        if placeholder not in html:
            raise ValueError(f"В шаблоне не найден плейсхолдер: {placeholder}")

    html = html.replace(PLACEHOLDER_LDSP, json.dumps(ldsp, ensure_ascii=False))
    html = html.replace(PLACEHOLDER_KROMKA, json.dumps(kromka, ensure_ascii=False))
    html = html.replace(PLACEHOLDER_MISC, json.dumps(misc, ensure_ascii=False))
    html = html.replace(PLACEHOLDER_LOGO, load_logo_data_uri())

    OUTPUT.write_text(html, encoding="utf-8")
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"Готово: {OUTPUT} ({size_kb:.0f} КБ)")
    print(f"  декоров ЛДСП: {len(ldsp)}")
    print(f"  типов кромки: {len(kromka)}")
    print(f"  прочих позиций: {len(misc)}")


if __name__ == "__main__":
    main()
