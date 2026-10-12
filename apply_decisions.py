#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разове застосування рішень щодо вкладки «Довідник обладнання та цін».

Рішення — у JSON-файлі (DECISIONS_FILE, за замовчуванням decisions_2026_10_12.json):
  delete       — рядки-дублі: {row, name}. Видаляються, лише якщо в цьому рядку стоїть ТОЧНО ця назва;
                 якщо хоч один рядок не збігся — нічого не видаляється (захист від зсуву рядків).
  accept_price — {name}: прийняти ціну з прайсу (стара ціна очищається, наступне оновлення поставить нову).
  no_binding   — {name}: хибна прив'язка → у колонку J пишеться «-» (без прив'язки, ціна вручну).

DRY_RUN=1 — лише показати, що буде зроблено.
"""
import json
import os
import sys

import price_finder as pf
from gspread.utils import rowcol_to_a1 as a1


def main():
    dry = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")
    path = os.environ.get("DECISIONS_FILE", "decisions_2026_10_12.json")
    dec = json.load(open(path, encoding="utf-8"))
    url = (os.environ.get("SHEET_URL", "") or pf.SHEET_URL).strip()

    creds = pf.get_credentials()
    wb, ws = pf.open_sheet(creds, url)
    values = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
    _, cols = pf.read_catalog(values)

    def name_at(row):
        r = values[row - 1] if 0 < row <= len(values) else []
        return str(r[cols["name"]]).strip() if len(r) > cols["name"] else ""

    print("═" * 62 + f"\n  Рішення з {path}" + ("  (ПРОБНИЙ ЗАПУСК)" if dry else "") + "\n" + "═" * 62)

    # 1. Перевірка рядків на видалення
    bad = [d for d in dec.get("delete", []) if name_at(d["row"]) != d["name"].strip()]
    del_rows = sorted({d["row"] for d in dec.get("delete", [])}, reverse=True)
    if bad:
        print(f"❌ {len(bad)} рядків не збіглися з назвою — видалення СКАСОВАНО повністю:")
        for d in bad:
            print(f"   р.{d['row']}: очікувалось «{d['name'][:60]}», у таблиці «{name_at(d['row'])[:60]}»")
        del_rows = []
    else:
        print(f"🗑 До видалення: {len(del_rows)} рядків (усі назви збіглися)")
        for d in sorted(dec["delete"], key=lambda x: x["row"]):
            print(f"   р.{d['row']}: {d['name'][:80]}")

    # 2. Пошук рядків для рішень по цінах (за назвою, серед рядків, що залишаються)
    def find(name):
        return [i for i in range(2, len(values) + 1)
                if name_at(i) == name.strip() and i not in del_rows]

    updates = []
    for kind, items in (("accept_price", dec.get("accept_price", [])), ("no_binding", dec.get("no_binding", []))):
        for it in items:
            rows = find(it["name"])
            if len(rows) != 1:
                print(f"⚠ {kind}: «{it['name'][:60]}» — знайдено рядків: {len(rows)} — пропущено")
                continue
            r = rows[0]
            if kind == "accept_price":
                updates.append({"range": a1(r, cols["price"] + 1), "values": [[""]]})
                print(f"✔ р.{r}: прийняти ціну з прайсу — стару ціну очищено ({it['name'][:60]})")
            else:
                updates.append({"range": a1(r, pf.MAPPING_COL + 1), "values": [["-"]]})
                updates.append({"range": a1(r, pf.SOURCE_COL + 1), "values": [[""]]})
                print(f"✔ р.{r}: без прив'язки (J = «-»), ціна лишається вручну ({it['name'][:60]})")

    if dry:
        print("\nDRY_RUN — у таблиці нічого не змінено.")
        return
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    if del_rows:
        wb.batch_update({"requests": [
            {"deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                           "startIndex": r - 1, "endIndex": r}}}
            for r in del_rows]})
    print(f"\n✅ Готово: видалено {len(del_rows)} рядків, змінено клітинок {len(updates)}.")
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
