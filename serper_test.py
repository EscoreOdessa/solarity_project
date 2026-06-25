#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Тест Serper: порівняння звичайного пошуку (organic) vs Google Shopping
на двох реальних позиціях зі спецификації.

ЯК ЗАПУСТИТИ:
  1. Зареєструйся на https://serper.dev (без картки) → скопіюй API-ключ
  2. У терміналі, у цій папці, виконай (підстав свій ключ):
        SERPER_API_KEY=твій_ключ python3 serper_test.py
  3. Подивись на вивід і вибери, звідки брати ціну.
"""

import json
import os
import re
import urllib.request

API_KEY = os.environ.get("SERPER_API_KEY", "").strip()

# Дві тестові позиції: проста + з брендом і кодом (перевірка «неточних» назв)
ITEMS = [
    "Кабель ВВГнг 4х95",
    "Лоток перфорований 100х50х3000 DKC (35262)",
]

PRICE_RE = re.compile(r"\d[\d\s ]*(?:[.,]\d{1,2})?\s*(?:грн|₴|UAH)", re.I)


def call(endpoint: str, query: str) -> dict:
    """Один запит до Serper (search або shopping), ринок — Україна."""
    req = urllib.request.Request(
        f"https://google.serper.dev/{endpoint}",
        data=json.dumps({"q": query, "gl": "ua", "hl": "uk"}).encode(),
        headers={"X-API-KEY": API_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def show(query: str):
    print("\n" + "=" * 70)
    print(f"ПОЗИЦІЯ: {query}")
    print("=" * 70)

    # --- 1. Звичайний пошук (organic) ---
    print("\n--- SEARCH (органіка): ціну треба виловити зі сніпета ---")
    data = call("search", query)
    for o in data.get("organic", [])[:5]:
        snip = o.get("snippet", "")
        prices = PRICE_RE.findall(snip)
        flag = f"  💰 {prices}" if prices else "  (ціни в сніпеті немає)"
        print(f"• {o.get('title','')[:60]}\n  {o.get('link','')[:70]}{flag}")

    # --- 2. Google Shopping ---
    print("\n--- SHOPPING: ціна приходить окремим полем 'price' ---")
    data = call("shopping", query)
    rows = data.get("shopping", [])
    if not rows:
        print("  (Shopping нічого не повернув для цієї позиції)")
    for s in rows[:8]:
        print(f"• {s.get('price','—'):>12}  | {s.get('source','')[:22]:22} | {s.get('title','')[:45]}")


if __name__ == "__main__":
    if not API_KEY:
        print("❌ Немає ключа. Запусти так:")
        print("   SERPER_API_KEY=твій_ключ python3 serper_test.py")
        raise SystemExit(1)
    for it in ITEMS:
        try:
            show(it)
        except Exception as ex:
            print(f"\n⚠ Помилка по «{it}»: {ex}")
    print("\nГотово. Порівняй: де ціни чистіші й їх більше — звідти й братимемо.")
