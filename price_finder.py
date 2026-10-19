#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EScore Energy — Оновлення цін СЕС з прайсів постачальників
==========================================================
Версія 2 (жовтень 2026): ціни беруться ВИКЛЮЧНО з прайсів у папці на Google Диску.
Інтернет-пошук (Serper / браузер) вимкнено — попередня версія з ним є в історії git.

Алгоритм:
  1. У папці з прайсами (PRICES_FOLDER_ID) для кожного постачальника береться
     НАЙСВІЖІШИЙ файл (ETI, CHINT, БДК/DKC, Kablex, KOPOS — визначаються за назвою файлу).
  2. Кожен прайс розбирається у словник «артикул → ціна» з урахуванням знижок.
  3. Для кожної позиції вкладки «Довідник обладнання та цін» визначається прив'язка:
       • колонка J («Kablex — … (арт. MBN232)», «BDK/ДКС — … (арт. 35264)»,
         або коротко «Kablex: MBN232», «БДК: 35264», «ETI: 2163518», «CHINT: 131361»);
       • якщо J порожня — артикул у колонці E (постачальник — з колонки C);
       • інакше — артикул у назві (6–10 цифр) у прайсі ETI/CHINT або точний збіг назви.
  4. Знайдено → нова ціна в «Ціна», назва постачальника в C, артикул у E (як у прайсі),
     джерело (файл, артикул, розрахунок) — у K.
     Не знайдено / «за запитом» / різка зміна ціни → стара ціна ЛИШАЄТЬСЯ, позиція йде
     на вкладку «На перевірку» (там же — підказки-кандидати з прайсів).
  5. Підсвітка A:E перемальовується заново: червоний — не знайдено в прайсах,
     жовтий — знайдено, але ціну не застосовано (потрібна перевірка).
  6. У J1 пишеться відмітка часу оновлення.

Запуск: GitHub Actions (розклад раз на 14 днів + кнопка «Оновити ціни» у таблиці).
Змінні оточення:
  GOOGLE_SERVICE_ACCOUNT_JSON — ключ сервісного акаунта (обов'язково)
  SHEET_URL                   — посилання на таблицю
  DRY_RUN=1                   — нічого не записувати, лише показати зміни
  SCHEDULED=1                 — запуск за розкладом: пропустити, якщо з
                                останнього оновлення минуло < MIN_DAYS_BETWEEN днів
"""

import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ══════════════════════════════════════════════════════════
# НАЛАШТУВАННЯ
# ══════════════════════════════════════════════════════════

SHEET_URL = "https://docs.google.com/spreadsheets/d/1g_ICKAssywjzinoyrvTAl9fQ7WbJQHLqj1vasJtuqRU/edit"

# Папка на Google Диску, куди кладуться прайси постачальників
PRICES_FOLDER_ID = "1S2fDS6V1SZPDP4G6BpWLRBn9SVEf-H8M"

SPEC_TAB_NAME = "Довідник обладнання та цін"
REVIEW_TAB_NAME = "На перевірку"
TIMESTAMP_CELL = "J1"
TIMESTAMP_PREFIX = "Останнє оновлення цін: "
MAPPING_COL = 9          # J (0-based) — прив'язка позиції до прайсу
ARTICLE_COL = 4          # E (0-based) — артикул у прайсі (пише скрипт; можна вписати вручну)
ARTICLE_HEADER = "Артикул"
HIGHLIGHT_COLS = 5       # A:E — підсвітка рядків
COLOR_NOT_FOUND = {"red": 0.957, "green": 0.800, "blue": 0.800}   # #F4CCCC
COLOR_CHECK = {"red": 1.0, "green": 0.949, "blue": 0.800}         # #FFF2CC
SOURCE_COL = 10          # K (0-based) — джерело ціни (пише скрипт)
SOURCE_HEADER = "Джерело ціни (авто)"

JUMP_LIMIT = 0.5         # зміна > 50% від поточної ціни (ще не підтвердженої скриптом) → на перевірку
MIN_DAYS_BETWEEN = 13   # для запуску за розкладом: «раз на два тижні»

# ── Постачальники ─────────────────────────────────────────
# file_re — як розпізнати файл прайсу за назвою (нижній регістр)
# label   — що писати в колонку «Постачальник»
ETI_DISCOUNT = 0.25      # ETI: −25% від «Ціна в грн з ПДВ»
CHINT_DISCOUNT = 0.0     # CHINT: знижка вже врахована у файлі
BDK_DISCOUNT = 0.0       # БДК/DKC: знижка від рекомендованої ціни (поки 0%)
BDK_VAT = 1.2            # БДК: рекомендована ціна в прайсі БЕЗ ПДВ → ×1.2, як у самому файлі

# Kablex: знижка залежить від групи верхнього рівня у прайсі
KABLEX_GROUP_DISCOUNTS = [
    ("кабель", 0.37),
    ("hager", 0.30),
    ("schneider", 0.25),
    ("копос", 0.25),
    ("kopos", 0.25),
    ("реле напряжения", 0.25),   # ZUBR
    ("zubr", 0.25),
]
# KOPOS (Ресурс Електро): прайс у EUR без ПДВ за 1000 шт/м → за 1 шт/м × курс × 1.2.
# Курс — той, що постачальник вказав у файлі (ЗМІСТ!E13), за ним він і виставляє рахунки.
KOPOS_DISCOUNT = 0.0     # «Ваша знижка, %» у файлі порожня
KOPOS_VAT = 1.2

# Частина цін Kablex — у EUR/USD. Курс: НБУ на день оновлення; якщо НБУ недоступний — ці значення
FALLBACK_RATES = {"EUR": 50.5, "USD": 45.1}

SUPPLIERS = {
    "eti":    {"file_re": r"^eti",             "label": "ETI/ДС-Електро"},
    "chint":  {"file_re": r"chint",            "label": "CHINT"},
    "bdk":    {"file_re": r"^(bdk|бдк|dkc)",   "label": "Прайс БДК (DKC)"},
    "kablex": {"file_re": r"^(kablex|каблекс)", "label": "Kablex"},
    "kopos":  {"file_re": r"^(kopos|копос)",   "label": "KOPOS (Ресурс Електро)"},
}

# Як постачальник може бути записаний у колонці J / «Постачальник»
SUPPLIER_ALIASES = [
    ("kablex", r"kablex|каблекс"),
    ("kopos",  r"kopos|копос|ресурс"),
    ("bdk",    r"bdk|бдк|дкс|dkc"),
    ("chint",  r"chint|чінт|чинт"),
    ("eti",    r"\beti\b|ds-електро|дс-електро|етi|еті"),
]

# Статуси БДК, які позначаємо на перевірку (ціну все одно беремо)
BDK_WARN_STATUSES = ("виводиться", "розпродаж")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ══════════════════════════════════════════════════════════
# ДОПОМІЖНЕ
# ══════════════════════════════════════════════════════════

def norm_code(v) -> str:
    """Артикул → рядок для порівняння: '2163518.0' → '2163518', без пробілів, верхній регістр."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip().replace("\xa0", "").replace(" ", "")
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s.upper()


def raw_code(v) -> str:
    """Артикул «в чистому вигляді», як у прайсі: без '.0', дужок і зайвих пробілів."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip().strip("()[]").strip()
    # число, яке Google Sheets показав у форматі «131,361.00» / «131 361,00» → 131361
    m = re.fullmatch(r"(\d{1,3}(?:[,\s\xa0]\d{3})+)(?:[.,]0+)?", s)
    if m:
        s = re.sub(r"[,\s\xa0]", "", m.group(1))
    if re.fullmatch(r"\d+\.0+", s):
        s = s.split(".")[0]
    return s


def norm_name(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip()).casefold()


def to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("\xa0", "").replace(" ", "").replace("₴", "")
    s = s.replace("грн.", "").replace("грн", "").replace("$", "").replace("€", "")
    if "," in s and "." in s:
        # «2,146.00» (кома — розділювач тисяч) або «2.146,00» (крапка — тисячі)
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def fmt(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ")


def kyiv_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Kyiv"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=3)))


def detect_supplier(text: str) -> Optional[str]:
    t = str(text or "").lower()
    for key, rx in SUPPLIER_ALIASES:
        if re.search(rx, t):
            return key
    return None


@dataclass
class PriceItem:
    supplier: str
    code: str                       # нормалізований (для пошуку)
    name: str
    price: Optional[float]          # фінальна ціна (з урахуванням знижки); None = немає ціни
    base: Optional[float] = None    # ціна з прайсу до знижки
    discount: float = 0.0
    status: str = ""                # статус/примітка з прайсу
    group: str = ""
    currency: str = "UAH"           # валюта ціни в прайсі
    rate: float = 1.0               # курс, за яким переведено в грн
    code_raw: str = ""              # артикул як у прайсі (для колонки E)

    def __post_init__(self):
        if not self.code_raw:
            self.code_raw = self.code


@dataclass
class PriceBook:
    supplier: str
    file_name: str
    file_id: str = ""
    by_code: Dict[str, PriceItem] = field(default_factory=dict)
    by_name: Dict[str, PriceItem] = field(default_factory=dict)

    def add(self, it: PriceItem):
        if it.code and it.code not in self.by_code:
            self.by_code[it.code] = it
        nk = norm_name(it.name)
        if nk and nk not in self.by_name:
            self.by_name[nk] = it

    def __len__(self):
        return len(self.by_code) or len(self.by_name)


# ══════════════════════════════════════════════════════════
# ПАРСЕРИ ПРАЙСІВ
# ══════════════════════════════════════════════════════════

def _rows_xlsx(data: bytes, sheet_hint: str = ""):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = None
    for name in wb.sheetnames:
        if sheet_hint and sheet_hint.lower() in name.lower():
            ws = wb[name]
            break
    ws = ws or wb.worksheets[0]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _rows_xls(data: bytes):
    import xlrd
    sh = xlrd.open_workbook(file_contents=data).sheet_by_index(0)
    return [sh.row_values(i) for i in range(sh.nrows)]


def _rows_any(data: bytes, file_name: str, sheet_hint: str = ""):
    if file_name.lower().endswith(".xls"):
        return _rows_xls(data)
    return _rows_xlsx(data, sheet_hint)


def parse_eti(data: bytes, file_name: str) -> PriceBook:
    """ETI (ДС-Електро): вкладка Price_*, колонки «Код», «Повне найменування», «Ціна в грн. з ПДВ»."""
    rows = _rows_any(data, file_name, "price_")
    book = PriceBook("eti", file_name)
    hdr = c_code = c_name = c_price = None
    for i, r in enumerate(rows):
        low = [str(c).lower() if c is not None else "" for c in r]
        if any(c.strip() == "код" for c in low) and any("найменування" in c for c in low):
            hdr = i
            for j, c in enumerate(low):
                if c_code is None and c.strip() == "код":
                    c_code = j
                if c_name is None and "найменування" in c:
                    c_name = j
                if c_price is None and "ціна" in c and "грн" in c:
                    c_price = j
            break
    if hdr is None or c_name is None or c_price is None:
        raise ValueError("ETI: не знайдено рядок заголовків (Код / Повне найменування / Ціна в грн)")
    for r in rows[hdr + 1:]:
        name = r[c_name] if c_name < len(r) else None
        base = to_float(r[c_price]) if c_price < len(r) else None
        if not name or not base or base <= 0:
            continue
        raw = raw_code(r[c_code]) if c_code is not None else ""
        book.add(PriceItem("eti", norm_code(raw), str(name).strip(),
                           round(base * (1 - ETI_DISCOUNT), 2), base, ETI_DISCOUNT, code_raw=raw))
    return book


def parse_chint(data: bytes, file_name: str) -> PriceBook:
    """CHINT: вкладка «Прайс-Лист», «Артикул», «Найменування/Тип», ціна (пріоритет — «зі знижкою»)."""
    rows = _rows_any(data, file_name, "прайс-лист")
    book = PriceBook("chint", file_name)
    hdr = c_code = c_name = c_price = c_disc = None
    for i, r in enumerate(rows):
        low = [str(c).lower() if c is not None else "" for c in r]
        txt = " ".join(low)
        if "артикул" in txt and ("найменування" in txt or "тип" in txt) and "ціна" in txt:
            hdr = i
            for j, c in enumerate(low):
                if c_code is None and "артикул" in c:
                    c_code = j
                if c_name is None and ("найменування" in c or "тип" in c):
                    c_name = j
                if "ціна" in c and "знижк" in c:
                    c_disc = j
                elif c_price is None and "ціна" in c:
                    c_price = j
            break
    col = c_disc if c_disc is not None else c_price
    if hdr is None or c_name is None or col is None:
        raise ValueError("CHINT: не знайдено рядок заголовків (Артикул / Найменування / Ціна)")
    for r in rows[hdr + 1:]:
        name = r[c_name] if c_name < len(r) else None
        base = to_float(r[col]) if col < len(r) else None
        if not name or not base or base <= 0:
            continue
        raw = raw_code(r[c_code]) if c_code is not None else ""
        book.add(PriceItem("chint", norm_code(raw), str(name).strip(),
                           round(base * (1 - CHINT_DISCOUNT), 2), base, CHINT_DISCOUNT, code_raw=raw))
    return book


def parse_bdk(data: bytes, file_name: str) -> PriceBook:
    """БДК/DKC: вкладка «Прейскурант». Ціни у файлі рахуються формулами від комірки знижки,
    тому беремо «Рекомендована ціна без ПДВ» і рахуємо самі: × (1 − BDK_DISCOUNT) × BDK_VAT."""
    rows = _rows_any(data, file_name, "прейскурант")
    book = PriceBook("bdk", file_name)
    hdr = None
    cols = {}
    for i, r in enumerate(rows):
        low = [str(c).lower() if c is not None else "" for c in r]
        if any(c.strip() == "артикул" for c in low) and any(c.strip() == "опис" for c in low):
            hdr = i
            for j, c in enumerate(low):
                c = c.strip()
                if c == "артикул":
                    cols["code"] = j
                elif c == "опис":
                    cols["name"] = j
                elif c == "статус":
                    cols["status"] = j
                elif c == "примітка":
                    cols["note"] = j
                elif "рекомендована ціна" in c:
                    cols["rec"] = j
            break
    if hdr is None or not {"code", "name", "rec"} <= cols.keys():
        raise ValueError("БДК: не знайдено заголовки (Артикул / Опис / Рекомендована ціна)")

    def g(r, k):
        j = cols.get(k)
        return r[j] if j is not None and j < len(r) else None

    for r in rows[hdr + 1:]:
        art = raw_code(g(r, "code"))
        code = norm_code(art)
        name = g(r, "name")
        if not code or not name:
            continue
        raw = g(r, "rec")
        base = to_float(raw)
        status = " / ".join(str(x).strip() for x in (g(r, "status"), g(r, "note")) if x)
        if base is None and raw:
            status = (status + " / " if status else "") + str(raw).strip()   # «за запитом»
        price = round(base * (1 - BDK_DISCOUNT) * BDK_VAT, 2) if base and base > 0 else None
        book.add(PriceItem("bdk", code, str(name).strip(), price, base, BDK_DISCOUNT, status,
                           code_raw=art))
    return book


def _kablex_discount(group: str) -> Tuple[float, bool]:
    g = group.lower()
    for key, d in KABLEX_GROUP_DISCOUNTS:
        if key in g:
            return d, True
    return 0.0, False


_RATES: Dict[str, float] = {}
_RATES_SRC = ""


def get_rates() -> Tuple[Dict[str, float], str]:
    """Курс EUR/USD → грн: змінні RATE_EUR/RATE_USD, інакше офіційний курс НБУ на сьогодні,
    інакше запасні значення FALLBACK_RATES."""
    global _RATES, _RATES_SRC
    if _RATES:
        return _RATES, _RATES_SRC
    rates, src = dict(FALLBACK_RATES), "запасний курс зі скрипта"
    env = {k: to_float(os.environ.get(f"RATE_{k}")) for k in ("EUR", "USD")}
    if all(env.values()):
        rates, src = env, "курс із налаштувань запуску"
    else:
        try:
            import urllib.request
            for k in ("EUR", "USD"):
                u = f"https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode={k}&json"
                with urllib.request.urlopen(u, timeout=15) as r:
                    rates[k] = float(json.loads(r.read())[0]["rate"])
            src = "курс НБУ"
        except Exception as ex:
            print(f"⚠ Курс НБУ не отримано ({ex}) — беру запасний: {FALLBACK_RATES}")
    _RATES, _RATES_SRC = rates, src
    print(f"💱 {src}: EUR {rates['EUR']:.2f}, USD {rates['USD']:.2f}")
    return rates, src


def _currency_of(fmt_str: str) -> str:
    f = (fmt_str or "").upper()
    if "EUR" in f or "€" in f:
        return "EUR"
    if "USD" in f or "$" in f:
        return "USD"
    return "UAH"


def _rows_with_price_currency(data: bytes, file_name: str):
    """Рядки + валюта кожної комірки (Kablex зберігає валюту лише у форматі комірки: 0.00" EUR")."""
    if file_name.lower().endswith(".xls"):
        import xlrd
        wb = xlrd.open_workbook(file_contents=data, formatting_info=True)
        sh = wb.sheet_by_index(0)
        rows, curr = [], []
        for i in range(sh.nrows):
            rows.append(sh.row_values(i))
            cr = []
            for j in range(sh.ncols):
                xf = wb.xf_list[sh.cell_xf_index(i, j)]
                cr.append(_currency_of(wb.format_map[xf.format_key].format_str))
            curr.append(cr)
        return rows, curr
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)   # не read_only: потрібні формати
    ws = wb.worksheets[0]
    rows, curr = [], []
    for r in ws.iter_rows():
        rows.append([c.value for c in r])
        curr.append([_currency_of(c.number_format) for c in r])
    return rows, curr


def parse_kablex(data: bytes, file_name: str) -> PriceBook:
    """Kablex: дерево груп з відступами в колонці назви; знижка за групою верхнього рівня.
    Частина цін — у EUR/USD (видно лише з формату комірки) → переводимо в грн за курсом."""
    rows, curr = _rows_with_price_currency(data, file_name)
    book = PriceBook("kablex", file_name)
    hdr = c_name = c_code = c_price = None
    for i, r in enumerate(rows):
        low = [str(c).lower() if c is not None else "" for c in r]
        if any("номенклатура" in c for c in low) and any("артикул" in c for c in low):
            hdr = i
            for j, c in enumerate(low):
                if c_name is None and "номенклатура" in c and "артикул" not in c:
                    c_name = j
                if c_code is None and "артикул" in c:
                    c_code = j
            break
    if hdr is None or c_name is None or c_code is None:
        raise ValueError("Kablex: не знайдено заголовки (Номенклатура / Артикул)")
    # ціна — перша колонка праворуч від артикулу, де є числа
    c_price = c_code + 1

    top_group = ""
    for ri in range(hdr + 1, len(rows)):
        r = rows[ri]
        raw_name = str(r[c_name]) if c_name < len(r) and r[c_name] is not None else ""
        name = raw_name.strip()
        if not name:
            continue
        indent = len(raw_name) - len(raw_name.lstrip(" "))
        price = to_float(r[c_price]) if c_price < len(r) else None
        raw = raw_code(r[c_code]) if c_code < len(r) else ""
        code = norm_code(raw)
        if price is None and not code:
            # рядок-група; верхній рівень — відступ ≤ 4 пробіли (у розділі «Грн» — 4)
            if indent <= 4 and name.lower() != "грн":
                top_group = name
            continue
        if not price or price <= 0:
            continue
        cur = curr[ri][c_price] if c_price < len(curr[ri]) else "UAH"
        disc, known = _kablex_discount(top_group)
        status = "" if known else f"група «{top_group}» без налаштованої знижки"
        rate = 1.0
        if cur != "UAH":
            rate = get_rates()[0][cur]
        book.add(PriceItem("kablex", code, name, round(price * rate * (1 - disc), 2),
                           price, disc, status, top_group, cur, rate, raw))
    return book


def parse_kopos(data: bytes, file_name: str) -> PriceBook:
    """KOPOS (Ресурс Електро): вкладка «Загальний прайс» — «Артикул», «Номенклатура»,
    «грошова одиниця», «Ціни відпускні БЕЗ ПДВ, 1000 шт/м». Курс EUR — з вкладки «ЗМІСТ»
    (рядок «Поточний курс»), ціна = база / 1000 × курс × (1 − знижка) × ПДВ."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rate = None
    if "ЗМІСТ" in wb.sheetnames:
        for r in wb["ЗМІСТ"].iter_rows(values_only=True):
            vals = [c for c in r if c is not None]
            if any("курс" in str(c).lower() for c in vals):
                rate = next((float(c) for c in vals if isinstance(c, (int, float))), None)
                if rate:
                    break
    if not rate:
        rate = get_rates()[0]["EUR"]
        print(f"⚠ KOPOS: курс у файлі не знайдено — беру {rate:.2f}")
    ws = next((wb[n] for n in wb.sheetnames if "загальний" in n.lower()), None)
    if ws is None:
        raise ValueError("KOPOS: немає вкладки «Загальний прайс»")
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    book = PriceBook("kopos", file_name)
    hdr = None
    cols = {}
    for i, r in enumerate(rows):
        low = [str(c).strip().lower() if c is not None else "" for c in r]
        if "артикул" in low and "номенклатура" in low:
            hdr = i
            for j, c in enumerate(low):
                if c == "артикул":
                    cols["code"] = j
                elif c == "номенклатура":
                    cols["name"] = j
                elif "грошова" in c:
                    cols["cur"] = j
                elif "без пдв" in c and "1000" in c:
                    cols["base"] = j
                elif c.startswith("зміна ціни"):
                    cols["note"] = j
                elif c.startswith("одиниця"):
                    cols["unit"] = j
            break
    if hdr is None or not {"code", "name", "base"} <= cols.keys():
        raise ValueError("KOPOS: не знайдено заголовки (Артикул / Номенклатура / Ціни БЕЗ ПДВ, 1000 шт/м)")

    def g(r, k):
        j = cols.get(k)
        return r[j] if j is not None and j < len(r) else None

    cols["ean"] = cols["code"] - 1      # EAN — колонка ліворуч від артикулу
    data = [r for r in rows[hdr + 1:] if raw_code(g(r, "code")) and g(r, "name")]
    seen: Dict[str, int] = {}
    for r in data:
        a = norm_code(raw_code(g(r, "code")))
        seen[a] = seen.get(a, 0) + 1
    for r in data:
        art = raw_code(g(r, "code"))
        name = g(r, "name")
        # Один артикул буває двічі: чеське виробництво (EUR, EAN 859…) і українське (грн, EAN 482…).
        # Українську версію ведемо окремою позицією: «1416E_K50D_UA».
        if seen[norm_code(art)] > 1 and str(g(r, "ean") or "").startswith("482"):
            art = f"{art}_UA"
        per1000 = to_float(g(r, "base"))
        note = g(r, "note")
        status = str(note).strip() if isinstance(note, str) else ""
        cur = "EUR" if "eur" in str(g(r, "cur") or "eur").lower() else "UAH"
        r_ = rate if cur == "EUR" else 1.0
        base = round(per1000 / 1000, 6) if per1000 and per1000 > 0 else None
        price = round(base * r_ * (1 - KOPOS_DISCOUNT) * KOPOS_VAT, 2) if base else None
        if base is None and not status:
            status = "немає ціни"
        book.add(PriceItem("kopos", norm_code(art), str(name).strip(), price, base, KOPOS_DISCOUNT,
                           status, "", cur, r_, art))
    return book


PARSERS = {"eti": parse_eti, "chint": parse_chint, "bdk": parse_bdk, "kablex": parse_kablex,
           "kopos": parse_kopos}


def classify_file(file_name: str) -> Optional[str]:
    n = file_name.lower()
    for key, cfg in SUPPLIERS.items():
        if re.search(cfg["file_re"], n):
            return key
    return None


# ══════════════════════════════════════════════════════════
# ЛОГІКА ЗІСТАВЛЕННЯ (без Google — тестується локально)
# ══════════════════════════════════════════════════════════

@dataclass
class CatalogRow:
    row: int            # 1-based номер рядка у вкладці
    group: str
    name: str
    supplier: str
    price: Optional[float]
    mapping: str
    source: str = ""
    article: str = ""


@dataclass
class Result:
    row: CatalogRow
    item: Optional[PriceItem] = None
    how: str = ""
    problem: str = ""        # непорожнє → на перевірку
    new_price: Optional[float] = None


_ART_RE = re.compile(r"\(\s*арт\.?\s*([^)]+?)\s*\)\s*$", re.IGNORECASE)
_SHORT_RE = re.compile(r"^\s*([^:—\-]+?)\s*[:]\s*(\S.*?)\s*$")   # артикул може містити пробіли (KOPOS: KU 68-45_KA)


def parse_mapping(text: str) -> Tuple[Optional[str], Optional[str]]:
    """'Kablex — Авт.выкл… (арт. MBN232)' → ('kablex','MBN232'); 'БДК: 35264' → ('bdk','35264')."""
    t = str(text or "").strip()
    if not t:
        return None, None
    m = _ART_RE.search(t)
    if m:
        return detect_supplier(t.split("—")[0]), norm_code(m.group(1))
    m = _SHORT_RE.match(t)
    if m:
        return detect_supplier(m.group(1)), norm_code(m.group(2))
    return detect_supplier(t), None


def match_row(cr: CatalogRow, books: Dict[str, PriceBook]) -> Result:
    res = Result(cr)
    if cr.mapping.strip() == "-":
        return res          # «-» у колонці J: свідомо без прив'язки, ціна вручну, не на перевірку
    sup, code = parse_mapping(cr.mapping)

    if sup or code:
        if not sup:
            res.problem = "У колонці J не вказано постачальника"
            return res
        book = books.get(sup)
        if not book:
            res.problem = f"Прайс «{SUPPLIERS[sup]['label']}» не знайдено в папці"
            return res
        it = book.by_code.get(code) if code else None
        if not it:
            res.problem = f"Артикул {code or '—'} відсутній у прайсі {book.file_name}"
            return res
        res.item, res.how = it, f"арт. {it.code}"
    if not res.item and not (sup or code) and str(cr.article).strip():
        # артикул у колонці E; постачальник — з колонки C (або шукаємо в усіх прайсах)
        ecode = norm_code(raw_code(cr.article))
        sup_col = detect_supplier(cr.supplier)
        for s in ([sup_col] if sup_col else list(books)):
            book = books.get(s)
            if book and ecode in book.by_code:
                res.item, res.how = book.by_code[ecode], f"арт. {ecode} (колонка E)"
                break
    if not res.item and not (sup or code):
        # без прив'язки: ETI/CHINT — артикул у назві або точний збіг назви
        sup_col = detect_supplier(cr.supplier)
        order = [sup_col] if sup_col in ("eti", "chint") else ([] if sup_col else ["eti", "chint"])
        for s in order:
            book = books.get(s)
            if not book:
                continue
            for c in re.findall(r"(?<!\d)(\d{6,10})(?!\d)", cr.name):
                it = book.by_code.get(c) or book.by_code.get(c.lstrip("0"))
                if it:
                    res.item, res.how = it, f"арт. {c} з назви"
                    break
            if not res.item:
                it = book.by_name.get(norm_name(cr.name))
                if it:
                    res.item, res.how = it, "точний збіг назви"
            if res.item:
                break
        if not res.item:
            res.problem = "Немає прив'язки до прайсу"
            return res

    it = res.item
    if it.price is None:
        res.problem = f"У прайсі немає ціни ({it.status or 'порожньо'}) — лишено стару"
        return res
    # Захист від хибних прив'язок / упаковка-vs-штука: різка зміна ціни, якої скрипт ще не підтверджував
    if (cr.price and not cr.source.strip()
            and abs(it.price - cr.price) / cr.price > JUMP_LIMIT):
        res.problem = (f"Різка зміна ціни: {fmt(cr.price)} → {fmt(it.price)} "
                       f"({SUPPLIERS[it.supplier]['label']}, арт. {it.code}). Перевірте прив'язку/одиниці; "
                       f"щоб прийняти — очистіть стару ціну")
        return res
    res.new_price = it.price
    if it.supplier == "bdk" and any(s in it.status.lower() for s in BDK_WARN_STATUSES):
        res.problem = f"Статус у прайсі: {it.status}"
    elif it.supplier == "kablex" and it.status:
        res.problem = it.status
    return res


def source_note(res: Result, today: str) -> str:
    it = res.item
    disc = f" −{round(it.discount * 100)}%" if it.discount else ""
    vat = " ×1.2 ПДВ" if it.supplier in ("bdk", "kopos") else ""
    cur = f" {it.currency}×{it.rate:.2f}" if it.currency != "UAH" else ""
    calc = f"{fmt(it.base)}{cur}{disc}{vat} = {fmt(it.price)}" if it.base else fmt(it.price)
    book_file = res.item and res.item.supplier
    extra = f" | {it.status}" if it.status else ""
    return f"{today} | {SUPPLIERS[book_file]['label']} | {res.how} | {calc}{extra}"


def suggest(cr: CatalogRow, books: Dict[str, PriceBook], limit: int = 2) -> List[Tuple[PriceItem, float]]:
    """Кандидати для позицій без прив'язки — лише підказка на вкладці «На перевірку»."""
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        return []
    q = norm_name(cr.name)
    q_models = _model_tokens(q)
    if not q_models:
        return []                       # без моделі/розміру в назві підказка буде випадковою
    out = []
    for book in books.values():
        names = list(book.by_name.keys())
        for nk, score, _ in process.extract(q, names, scorer=fuzz.WRatio, limit=25, score_cutoff=70):
            shared = q_models & _model_tokens(nk)
            ts = fuzz.token_sort_ratio(q, nk)
            if shared and ts >= 60:
                out.append((book.by_name[nk], ts))
    out.sort(key=lambda x: -x[1])
    return [(it, min(s, 100)) for it, s in out[:limit]]


def _model_tokens(s: str) -> set:
    """«Модельні» токени: містять цифру і мають ≥3 символи (MBN232, 60x40, 2x4, 35264)."""
    toks = re.findall(r"[0-9a-zа-яіїє][0-9a-zа-яіїє.,/x×*\-]+", s.lower())
    out = set()
    for t in toks:
        t = t.strip(".,-/").replace("×", "x").replace("*", "x").replace("х", "x").replace(",", ".")
        if len(t) >= 3 and re.search(r"\d", t):
            out.add(t)
    return out


def plan(rows: List[CatalogRow], books: Dict[str, PriceBook], with_suggestions=True):
    results = [match_row(cr, books) for cr in rows]
    review = []
    for r in results:
        if r.problem:
            cands = suggest(r.row, books) if (with_suggestions and not r.item) else []
            review.append((r, cands))
    return results, review


# ══════════════════════════════════════════════════════════
# GOOGLE: АВТОРИЗАЦІЯ, ДИСК, ТАБЛИЦЯ
# ══════════════════════════════════════════════════════════

def get_credentials():
    from google.oauth2 import service_account
    sa_env = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if sa_env:
        print("✅ Авторизація: сервісний акаунт (секрет оточення)")
        return service_account.Credentials.from_service_account_info(json.loads(sa_env), scopes=SCOPES)
    p = Path("service_account.json")
    if p.exists():
        print("✅ Авторизація: service_account.json")
        return service_account.Credentials.from_service_account_file(str(p), scopes=SCOPES)
    sys.exit("❌ Немає ключа сервісного акаунта (GOOGLE_SERVICE_ACCOUNT_JSON або service_account.json)")


def load_books_from_drive(creds) -> Dict[str, PriceBook]:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    drive = build("drive", "v3", credentials=creds)
    files = drive.files().list(
        q=f"'{PRICES_FOLDER_ID}' in parents and trashed=false",
        orderBy="modifiedTime desc", pageSize=100,
        fields="files(id,name,mimeType,modifiedTime)",
    ).execute().get("files", [])
    if not files:
        print("⚠ Папка з прайсами порожня або недоступна сервісному акаунту")

    # Найсвіжіший прайс = найпізніша ДАТА В НАЗВІ файлу; modifiedTime — лише якщо дати немає
    # (відкрили/поправили старий прайс на Диску → modifiedTime оновився, але прайс від цього не новіший)
    from db_sync import price_date_from_name
    newest: Dict[str, dict] = {}
    for f in files:                       # відсортовано від найновішого за modifiedTime
        key = classify_file(f["name"])
        if not key:
            print(f"   (пропущено — невідомий постачальник: {f['name']})")
            continue
        d = price_date_from_name(f["name"])
        prev = newest.get(key)
        if prev is None:
            newest[key] = f
        else:
            pd = price_date_from_name(prev["name"])
            if d and (pd is None or d > pd):
                newest[key] = f
        if not d:
            print(f"   ⚠ У назві «{f['name']}» немає дати РРРР_ММ_ДД — порядок за датою зміни файлу")

    books = {}
    for key, f in newest.items():
        try:
            if f["mimeType"] == "application/vnd.google-apps.spreadsheet":
                req = drive.files().export_media(
                    fileId=f["id"],
                    mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                fname = f["name"] + ".xlsx"
            else:
                req = drive.files().get_media(fileId=f["id"])
                fname = f["name"]
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(buf, req, chunksize=20 * 1024 * 1024)
            done = False
            while not done:
                _, done = dl.next_chunk()
            book = PARSERS[key](buf.getvalue(), fname)
            book.file_name = f["name"]
            book.file_id = f["id"]
            books[key] = book
            print(f"📋 {SUPPLIERS[key]['label']}: {f['name']} — {len(book)} позицій")
        except Exception as ex:
            print(f"⚠ Не вдалося прочитати {f['name']}: {ex}")
    for key in SUPPLIERS:
        if key not in books:
            print(f"⚠ Прайс «{SUPPLIERS[key]['label']}» у папці не знайдено — ці позиції не оновлюються")
    return books


def open_sheet(creds, url: str):
    import gspread
    gc = gspread.Client(auth=creds)
    wb = gc.open_by_url(url)
    ws = next((s for s in wb.worksheets() if SPEC_TAB_NAME.lower() in s.title.lower()), None)
    if ws is None:
        sys.exit(f"❌ Вкладку «{SPEC_TAB_NAME}» не знайдено")
    return wb, ws


def read_catalog(values: List[List]) -> Tuple[List[CatalogRow], Dict[str, int]]:
    """values — усі значення вкладки (рядки списками). Перший рядок — заголовки."""
    head = [str(c).strip().lower() for c in values[0]]

    def col(*keys, default=None):
        for k in keys:
            for j, h in enumerate(head):
                if h.startswith(k):
                    return j
        return default

    cols = {
        "group": col("група", default=0),
        "name": col("найменування", default=1),
        "supplier": col("постачальник", default=2),
        "price": col("ціна", default=3),
    }
    rows = []
    for i, r in enumerate(values[1:], start=2):
        def c(j):
            return r[j] if j is not None and j < len(r) else None
        name = str(c(cols["name"]) or "").strip()
        if not name:
            continue
        rows.append(CatalogRow(i, str(c(cols["group"]) or ""), name, str(c(cols["supplier"]) or ""),
                               to_float(c(cols["price"])), str(c(MAPPING_COL) or ""),
                               str(c(SOURCE_COL) or ""), str(c(ARTICLE_COL) or "")))
    return rows, cols


def last_update_age_days(stamp_text: str) -> Optional[float]:
    m = re.search(r"(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2})", stamp_text or "")
    if not m:
        return None
    then = datetime.strptime(m.group(1), "%d.%m.%Y %H:%M").replace(tzinfo=kyiv_now().tzinfo)
    return (kyiv_now() - then).total_seconds() / 86400


RENAMES_FILE = "renames_1c.csv"   # разове перейменування позицій за зіставленням з 1С


def apply_renames(ws, values, dry: bool) -> bool:
    """Застосовує renames_1c.csv (old → new, артикул, постачальник).
    Рядок змінюється лише якщо в колонці B стоїть РІВНО старе найменування,
    тож повторний запуск нічого не зіпсує. Повертає True, якщо щось змінено."""
    import csv
    import gspread
    p = Path(__file__).with_name(RENAMES_FILE)
    if not p.exists():
        return False
    by_old = {}
    for r in csv.DictReader(p.open(encoding="utf-8")):
        by_old.setdefault(" ".join(r["old"].split()), r)
    a1 = gspread.utils.rowcol_to_a1
    upd, upd_raw = [], []
    stamp = f"Зіставлено з 1С {kyiv_now().strftime('%d.%m.%Y')}"
    for i, row in enumerate(values[1:], start=2):
        name = " ".join(str(row[1] if len(row) > 1 else "").split())
        r = by_old.get(name)
        if not r:
            continue
        upd.append({"range": a1(i, 2), "values": [[r["new"]]]})
        if r["supplier"]:
            upd.append({"range": a1(i, 3), "values": [[r["supplier"]]]})
        if r["article"]:
            upd_raw.append({"range": a1(i, ARTICLE_COL + 1), "values": [[r["article"]]]})
        if r.get("clear_j"):   # підтверджений артикул важливіший за стару прив'язку в J
            upd.append({"range": a1(i, MAPPING_COL + 1), "values": [[""]]})
        k = str(row[SOURCE_COL]) if len(row) > SOURCE_COL else ""
        if r.get("stamp") and not k.strip():   # підтверджено → без захисту від «різкої зміни»
            upd.append({"range": a1(i, SOURCE_COL + 1), "values": [[stamp]]})
        print(f"  ↻ р.{i}: «{name[:50]}» → «{r['new'][:50]}» | арт. {r['article'] or '—'}")
    if not upd:
        return False
    print(f"Перейменовано за 1С: {sum(1 for u in upd if u['range'].startswith('B'))} позицій")
    if dry:
        return False
    ws.batch_update(upd, value_input_option="USER_ENTERED")
    if upd_raw:
        ws.batch_update(upd_raw, value_input_option="RAW")
    return True


def write_review(wb, review, today: str):
    import gspread
    try:
        ws = wb.worksheet(REVIEW_TAB_NAME)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = wb.add_worksheet(REVIEW_TAB_NAME, rows=max(100, len(review) + 10), cols=10)
    data = [[f"Оновлення {today}: позиції, де ціну НЕ змінено або потрібна увага. "
             f"Щоб прив'язати позицію — впишіть у колонку J довідника, напр. «Kablex: MBN232»."],
            ["Рядок", "Група", "Найменування", "Поточна ціна", "Причина",
             "Кандидат: постачальник", "Кандидат: артикул", "Кандидат: назва", "Кандидат: ціна", "Схожість %"]]
    for r, cands in review:
        base = [r.row.row, r.row.group, r.row.name, r.row.price if r.row.price is not None else "", r.problem]
        if cands:
            for k, (it, score) in enumerate(cands):
                data.append((base if k == 0 else ["", "", "", "", ""]) +
                            [SUPPLIERS[it.supplier]["label"], it.code, it.name,
                             it.price if it.price is not None else "", round(score)])
        else:
            data.append(base + ["", "", "", "", ""])
    if ws.row_count < len(data):
        ws.add_rows(len(data) - ws.row_count)
    ws.update(values=data, range_name="A1", value_input_option="USER_ENTERED")


def set_article_text_format(wb, ws, last_row: int):
    """Колонка E (артикул) — текстовий формат, щоб артикули не перетворювались на числа."""
    rng = {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": max(last_row, ws.row_count),
           "startColumnIndex": ARTICLE_COL, "endColumnIndex": ARTICLE_COL + 1}
    wb.batch_update({"requests": [{"repeatCell": {
        "range": rng,
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}, "horizontalAlignment": "LEFT"}},
        "fields": "userEnteredFormat.numberFormat,userEnteredFormat.horizontalAlignment"}}]})


def highlight(wb, ws, rows: List[CatalogRow], review):
    """Знімає всю стару заливку в рядках довідника (крім заголовка) і фарбує A:E заново:
    червоний — не знайдено в прайсах, жовтий — знайдено, але ціну не застосовано."""
    if not rows:
        return
    last = max(cr.row for cr in rows)

    def rng(r0, r1, c1=HIGHLIGHT_COLS):   # r0, r1 — 1-based включно
        return {"sheetId": ws.id, "startRowIndex": r0 - 1, "endRowIndex": r1,
                "startColumnIndex": 0, "endColumnIndex": c1}

    # усі старі кольори в рядках довідника (усі колонки) знімаємо — лишається лише нова підсвітка
    reqs = [{"repeatCell": {"range": rng(2, max(last, ws.row_count), ws.col_count),
                            "cell": {"userEnteredFormat": {}},
                            "fields": "userEnteredFormat.backgroundColor"}}]
    for r, _ in review:
        color = COLOR_CHECK if r.item else COLOR_NOT_FOUND
        reqs.append({"repeatCell": {"range": rng(r.row.row, r.row.row),
                                    "cell": {"userEnteredFormat": {"backgroundColor": color}},
                                    "fields": "userEnteredFormat.backgroundColor"}})
    for i in range(0, len(reqs), 400):
        wb.batch_update({"requests": reqs[i:i + 400]})
    n_red = sum(1 for r, _ in review if not r.item)
    print(f"🎨 Підсвічено: червоним {n_red} (не знайдено), жовтим {len(review) - n_red} (перевірити)")


# ══════════════════════════════════════════════════════════
# ГОЛОВНИЙ ЗАПУСК
# ══════════════════════════════════════════════════════════

def run_update(sheet_url: str = "") -> None:
    import gspread
    url = (sheet_url or os.environ.get("SHEET_URL", "") or SHEET_URL).strip()
    dry = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "yes")
    print("═" * 62 + "\n  EScore Energy — оновлення цін з прайсів\n" + "═" * 62)

    creds = get_credentials()
    wb, ws = open_sheet(creds, url)
    values = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")   # ціни — числами
    if apply_renames(ws, values, dry):
        values = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")

    if os.environ.get("SCHEDULED", "").strip() in ("1", "true", "yes"):
        stamp = ws.acell(TIMESTAMP_CELL).value or ""
        age = last_update_age_days(stamp)
        if age is not None and age < MIN_DAYS_BETWEEN:
            print(f"⏭ Останнє оновлення {age:.1f} дн. тому (< {MIN_DAYS_BETWEEN}) — пропускаю запуск за розкладом")
            return

    books = load_books_from_drive(creds)
    if not books:
        sys.exit("❌ Жодного прайсу не прочитано — таблицю не змінено")
    if not dry:
        import db_sync
        db_sync.save_books(books)   # PostgreSQL паралельно з таблицею; помилка бази не зупиняє оновлення

    rows, cols = read_catalog(values)
    results, review = plan(rows, books)
    if not dry:
        db_sync.save_reference(values, results, review)   # етап 3: довідник і прив'язки → база
    today = kyiv_now().strftime("%d.%m.%Y")

    # етап 5: ціну для довідника рахує база (прайс + escore.pricing_rules).
    # PRICE_SOURCE=code (змінна репозиторію) — повернутися до розрахунку в коді.
    db_prices = None
    if not dry and os.environ.get("PRICE_SOURCE", "").strip().lower() != "code":
        db_prices = db_sync.load_reference_prices()
        print("🗄 Ціни для довідника — з бази" if db_prices is not None
              else "⚠ База недоступна — ціни рахує код, як раніше")
    code_vs_db = []

    updates = []
    art_updates = []          # артикули пишемо як ТЕКСТ (RAW), інакше 131361 стає числом «131,361.00»
    changed = same = 0
    a1 = gspread.utils.rowcol_to_a1
    for r in results:
        if r.new_price is None:
            continue
        cr = r.row
        label = SUPPLIERS[r.item.supplier]["label"]
        note = source_note(r, today)
        rec = db_prices.get(cr.row) if db_prices is not None else None
        if rec and rec[1] == "price_list" and rec[0] is not None:
            if abs(rec[0] - r.new_price) > 0.011:     # 1 коп. — різниця округлення, не показуємо
                code_vs_db.append((cr.row, cr.name, r.new_price, rec[0]))
            r.new_price, note = rec[0], rec[2]
        if cr.price is None or abs(cr.price - r.new_price) > 0.005:
            changed += 1
            print(f"  ✏ р.{cr.row}: {cr.name[:55]} — {cr.price} → {r.new_price}")
        else:
            same += 1
        updates.append({"range": a1(cr.row, cols["price"] + 1), "values": [[r.new_price]]})
        updates.append({"range": a1(cr.row, cols["supplier"] + 1), "values": [[label]]})
        updates.append({"range": a1(cr.row, SOURCE_COL + 1), "values": [[note]]})
        art_updates.append({"range": a1(cr.row, ARTICLE_COL + 1), "values": [[str(r.item.code_raw)]]})

    print(f"\nЗнайдено в прайсах: {changed + same} (змінено ціну: {changed}, без змін: {same})")
    if code_vs_db:
        print(f"ℹ Код і база рахують ціну по-різному: {len(code_vs_db)} позицій (у таблицю — ціна з бази). "
              f"Перевірте знижки в escore.pricing_rules:")
        for row, name, c, d in code_vs_db[:10]:
            print(f"   р.{row}: код {c} | база {d} — {name[:55]}")
    if not dry:
        # етап 4: ціна, що лишиться в таблиці після запису, проти ціни, яку рахує база
        final = {cr.row: cr.price for cr in rows}
        final.update({r.row.row: r.new_price for r in results if r.new_price is not None})
        db_sync.reconcile(final)
    print(f"На перевірку: {len(review)} (стара ціна лишається)")

    if dry:
        print("\nDRY_RUN — у таблицю нічого не записано.")
        return

    updates.append({"range": a1(1, SOURCE_COL + 1), "values": [[SOURCE_HEADER]]})
    updates.append({"range": a1(1, ARTICLE_COL + 1), "values": [[ARTICLE_HEADER]]})
    stamp = kyiv_now().strftime("%d.%m.%Y %H:%M")
    updates.append({"range": TIMESTAMP_CELL, "values": [[TIMESTAMP_PREFIX + stamp]]})
    set_article_text_format(wb, ws, max((cr.row for cr in rows), default=2))
    for i in range(0, len(updates), 500):
        ws.batch_update(updates[i:i + 500], value_input_option="USER_ENTERED")
    for i in range(0, len(art_updates), 500):
        ws.batch_update(art_updates[i:i + 500], value_input_option="RAW")
    write_review(wb, review, today)
    highlight(wb, ws, rows, review)
    print(f"✅ Готово. Відмітка оновлення: {stamp}")


def main():
    run_update()


if __name__ == "__main__":
    main()
