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
                # Файл з такою назвою (а в назві — дата прайсу) вже завантажено → не дублюємо історію
                cur.execute("""select id from escore.import_runs
                               where supplier_id = %s and file_name = %s and status = 'ok'
                               order by id limit 1""", (sup_ids[key], book.file_name))
                prev = cur.fetchone()
                if prev:
                    print(f"⏭ База: {book.file_name} вже завантажено (запуск #{prev[0]}) — пропущено")
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


# ══════════════════════════════════════════════════════════
# ЕТАП 3: довідник і прив'язки (дзеркало вкладки «Довідник», повне оновлення)
# ══════════════════════════════════════════════════════════

def _method(how: str) -> str:
    h = (how or "").lower()
    if "колонка e" in h:
        return "article_e"
    if "з назви" in h:
        return "article_in_name"
    if "збіг назви" in h:
        return "exact_name"
    return "manual_j"


def save_reference(values, results, review, url: Optional[str] = None) -> bool:
    """values — уся вкладка «Довідник» (як із get_all_values); results/review — з price_finder.plan().

    Поки «Довідник» — головне джерело, база щоразу перезаписує довідник і прив'язки з нього.
    Підтверджена прив'язка = ціну з прайсу застосовано; кандидат = різка зміна ціни або підказка зі схожістю.
    """
    url = url or os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return False
    try:
        import psycopg
    except ImportError:
        return False

    def cell(row, j):
        return str(row[j]).strip() if j < len(row) and row[j] is not None else ""

    refs, maps = [], []
    for r in results:
        cr = r.row
        raw = values[cr.row - 1] if cr.row - 1 < len(values) else []
        sub, sub2 = cell(raw, 5) or None, cell(raw, 6) or None
        confirmed = r.item is not None and r.new_price is not None
        manual = None if confirmed else cr.price
        refs.append((cr.row, cr.group or "—", sub, sub2, cr.name, manual))
        note_1c = "1С" if cr.source.startswith("Зіставлено з 1С") else None
        if r.item is not None:
            maps.append((cr.row, r.item.supplier, r.item.code or None, r.item.name, _method(r.how),
                         "confirmed" if confirmed else "candidate", confirmed, None,
                         note_1c if confirmed else r.problem))
    for r, cands in review:
        for it, score in cands:
            maps.append((r.row.row, it.supplier, it.code or None, it.name, "fuzzy",
                         "candidate", False, score, None))

    try:
        with psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
            cur.execute("delete from escore.item_mappings")
            cur.execute("delete from escore.reference_items")
            cur.executemany(
                """insert into escore.reference_items
                     (sheet_row, grp, subgroup, subgroup2, name, manual_price)
                   values (%s, %s, %s, %s, %s, %s)""", refs)
            cur.execute("""create temp table _maps (
                             sheet_row int, sup text, article_norm text, name text, method text,
                             status text, is_primary boolean, similarity numeric, note text
                           ) on commit drop""")
            with cur.copy("copy _maps from stdin") as cp:
                for m in maps:
                    cp.write_row(m)
            cur.execute(
                """insert into escore.item_mappings
                     (reference_item_id, supplier_item_id, method, status, is_primary, similarity,
                      confirmed_at, note)
                   select distinct on (ri.id, si.id)
                          ri.id, si.id, m.method, m.status, m.is_primary, m.similarity,
                          case when m.status = 'confirmed' then now() end, m.note
                   from _maps m
                   join escore.reference_items ri on ri.sheet_row = m.sheet_row
                   join escore.suppliers s on s.code = m.sup
                   join escore.supplier_items si on si.supplier_id = s.id
                    and (si.article_norm = m.article_norm
                         or (m.article_norm is null and si.article_norm is null and si.name = m.name))
                   order by ri.id, si.id, m.is_primary desc""")
            linked = cur.rowcount
            conn.commit()
        n_conf = sum(1 for m in maps if m[6])
        print(f"🗄 База: довідник — {len(refs)} позицій, прив'язок {linked} "
              f"(підтверджених {n_conf}, решта — кандидати)")
        return True
    except Exception as ex:
        print(f"⚠ Запис довідника в базу не вдався: {ex}")
        return False


# ══════════════════════════════════════════════════════════
# ЕТАП 4: звірка цін «база ↔ таблиця»
# ══════════════════════════════════════════════════════════

def reconcile(expected: Dict[int, Optional[float]], url: Optional[str] = None, show: int = 20) -> Optional[int]:
    """expected — {рядок довідника: ціна, яку скрипт залишив у таблиці}.
    Порівнює з escore.v_reference_prices (ціна, яку база рахує сама за прайсами й pricing_rules).
    Повертає кількість розбіжностей або None, якщо база недоступна."""
    url = url or os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return None
    try:
        import psycopg
        with psycopg.connect(url, connect_timeout=20) as conn, conn.cursor() as cur:
            cur.execute("select sheet_row, price, price_source, name, coalesce(source_note, '') "
                        "from escore.v_reference_prices")
            db = {r[0]: r for r in cur.fetchall()}
    except Exception as ex:
        print(f"⚠ Звірка з базою не вдалася: {ex}")
        return None

    diffs = []
    for row, sheet_price in expected.items():
        rec = db.get(row)
        db_price = float(rec[1]) if rec and rec[1] is not None else None
        if sheet_price is None and db_price is None:
            continue
        if sheet_price is None or db_price is None or abs(sheet_price - db_price) > 0.01:
            diffs.append((row, sheet_price, db_price, rec[2] if rec else "немає в базі",
                          (rec[3] if rec else "")[:50], rec[4] if rec else ""))
    missing = [r for r in db if r not in expected]

    if not diffs and not missing:
        print(f"🟰 Звірка база ↔ таблиця: {len(expected)} позицій, розбіжностей немає")
        return 0
    print(f"⚠ Звірка база ↔ таблиця: розбіжностей {len(diffs)}, зайвих рядків у базі {len(missing)}")
    for row, s, d, src, name, note in diffs[:show]:
        print(f"   р.{row}: таблиця {s} | база {d} ({src}) — {name} {('· ' + note) if note else ''}")
    if len(diffs) > show:
        print(f"   … і ще {len(diffs) - show}")
    return len(diffs) + len(missing)
