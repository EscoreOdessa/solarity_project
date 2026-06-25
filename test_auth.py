#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Перевірка доступу сервісного акаунта — ТІЛЬКИ ЧИТАННЯ, нічого не записує.
Підтверджує, що робот авторизувався і має доступ до таблиці та прайсу ETI.

Запуск:
    source .venv/bin/activate
    python3 test_auth.py
"""
import gspread

from price_finder import (
    get_credentials, SHEET_URL,
    ETIPriceList, SPEC_TAB_NAME,
)


def main():
    print("1) Авторизація...")
    creds = get_credentials()

    print("\n2) Відкриваю робочу таблицю...")
    gc = gspread.Client(auth=creds)
    wb = gc.open_by_url(SHEET_URL)
    titles = [w.title for w in wb.worksheets()]
    print(f"   ✅ Таблиця відкрита. Вкладки: {titles}")
    if any(SPEC_TAB_NAME.lower() in t.lower() for t in titles):
        print(f"   ✅ Знайдено вкладку «{SPEC_TAB_NAME}»")
    else:
        print(f"   ⚠ Вкладку «{SPEC_TAB_NAME}» не знайдено серед: {titles}")

    print("\n3) Перевіряю доступ до прайсу ETI на Диску...")
    eti = ETIPriceList(creds)
    if eti.load():
        print("   ✅ Прайс ETI прочитано роботом")
    else:
        print("   ⚠ ETI не прочитано — перевір, що файл розшарено на робота (Viewer)")

    print("\n✅ Готово: сервісний акаунт працює, нічого не записано.")


if __name__ == "__main__":
    main()
