# -*- coding: utf-8 -*-
"""
Запис прайсів у PostgreSQL (Supabase), етап 2.

Викликається з price_finder.py після читання прайсів. Працює паралельно з таблицею:
якщо DATABASE_URL не задано або база недоступна — лише попередження, таблиця оновлюється як завжди.

Кожен прочитаний файл → запис у escore.import_runs;
кожна позиція прайсу → escore.supplier_items (оновлюється назва/група) + escore.price_snapshots (історія).
"""
import os
import re
from datetime import date
from typing import Dict, Optional

# Ціна у прайсі вже з ПДВ? (для БДК — без ПДВ, множник ×1.2 у pricing_rules)
VAT_INCLUDED = {"eti": True, "chint": True, "bdk": False, "kablex": True}


def price_date_from_name(file_name: str) -> Optional[date]:
    """kablex_2026_10_09.xls / eti_15.06.2026.xlsx / ...2026_09_01.xlsx → дата прайсу."""
    m = re.search(r"(20\d{2})[._-](\d{2})[._-](\d{2})", file_name)
    if m:
        y, mo, d = m.groups()
    else:
        m = re.search(r"(\d{2})[._-](\d{2})[._-](20\d{2})", file_name)
        if not m:
            return None
        d, mo, y = m.groups()
    try:
        return date(int(y), int(mo), int(d))
    except ValueError:
        return None


def map_status(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "unknown"
    if s in ("так", "є", "в наявності", "yes"):
        return "in_stock"
    if s in ("ні", "no") or "під замов" in s:
        return "on_order"
    if s in ("n/a", "na") or "недоступ" in s:
        return "unavailable"
    if "new" in s or "нов" in s:
        return "new"
    if "виводиться" in s or "розпродаж" in s:
        return "discontinued"
    return "unknown"


def _items(book):
    """Унікальні позиції PriceBook: з артикулом (by_code) + без артикулу (by_name)."""
    seen = set()
    for it in list(book.by_code.values()) + list(book.by_name.values()):
        if id(it) not in seen:
            seen.add(id(it))
            yield it


def save_books(books: Dict[str, object], url: Optional[str] = None) -> bool:
    url = url or os.environ.get("DATABASE_URL", "").strip()
    if not url:
        print("ℹ DATABASE_URL не задано — запис у базу пропущено")
        return False
    try:
        import psycopg
    except ImportError:
        print("⚠ Немає пакета psycopg — запис у базу пропущено")
        return False

    try:
        with psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
            cur.execute("select code, id from escore.suppliers")
            sup_ids = dict(cur.fetchall())
            for key, book in books.items():
                if key not in sup_ids:
                    print(f"⚠ База: постачальника «{key}» немає в escore.suppliers — пропущено")
                    continue
                items = list(_items(book))
                rates = {it.currency: it.rate for it in items if it.currency != "UAH" and it.rate}
                cur.execute(
                    """insert into escore.import_runs
                         (supplier_id, drive_file_id, file_name, price_date, rates, rates_source, rows_parsed)
                       values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                    (sup_ids[key], getattr(book, "file_id", None), book.file_name,
                     price_date_from_name(book.file_name), psycopg.types.json.Jsonb(rates),
                     "НБУ / резерв" if rates else None, len(items)))
                run_id = cur.fetchone()[0]
                vat = VAT_INCLUDED.get(key, True)
                # Масове завантаження: COPY у тимчасову таблицю, далі два INSERT ... SELECT
                cur.execute("""create temp table _stage (
                                 article text, article_norm text, name text, group_path text,
                                 base_price numeric, currency text, status_raw text, status text
                               ) on commit drop""")
                with cur.copy("copy _stage from stdin") as cp:
                    for it in items:
                        # для Kablex status — наша примітка про знижку, а не статус прайсу
                        status_raw = it.status if key == "bdk" else None
                        cp.write_row((it.code_raw or None, it.code or None, it.name, it.group or None,
                                      it.base, it.currency or "UAH", status_raw, map_status(status_raw)))
                cur.execute(
                    """insert into escore.supplier_items
                         (supplier_id, article, article_norm, name, group_path, first_run_id, last_run_id)
                       select distinct on (coalesce(article_norm, 'name:' || lower(name)))
                              %(sup)s, article, article_norm, name, group_path, %(run)s, %(run)s
                       from _stage
                       order by coalesce(article_norm, 'name:' || lower(name))
                       on conflict (supplier_id, item_key) do update
                         set name = excluded.name, article = excluded.article,
                             group_path = coalesce(excluded.group_path, escore.supplier_items.group_path),
                             last_run_id = excluded.last_run_id""",
                    {"sup": sup_ids[key], "run": run_id})
                cur.execute(
                    """insert into escore.price_snapshots
                         (supplier_item_id, import_run_id, base_price, currency, vat_included, status_raw, status)
                       select si.id, %(run)s, s.base_price, s.currency, %(vat)s, s.status_raw, s.status
                       from _stage s
                       join escore.supplier_items si
                         on si.supplier_id = %(sup)s
                        and si.item_key = coalesce(s.article_norm, 'name:' || lower(s.name))
                       on conflict do nothing""",
                    {"sup": sup_ids[key], "run": run_id, "vat": vat})
                conn.commit()
                print(f"🗄 База: {book.file_name} — {len(items)} позицій (запуск #{run_id})")
        return True
    except Exception as ex:
        print(f"⚠ Запис у базу не вдався (таблицю оновлюємо як завжди): {ex}")
        return False
