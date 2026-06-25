#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EScore — Streamlit-застосунок «Оновлення цін».

Будь-який користувач (зокрема на Windows) відкриває посилання у браузері й
натискає кнопку — скрипт знаходить ціни і записує їх у Google-таблицю.
Встановлювати Python не потрібно: застосунок працює у хмарі Streamlit.

Секрети (посилання на таблицю, ключ Serper, ключ сервісного акаунта) беруться
зі сховища Streamlit Cloud (Settings → Secrets) і прокидаються у змінні
оточення, які читає price_finder.
"""
import contextlib
import io
import os
import re

import streamlit as st

# ── Секрети зі сховища Streamlit → у змінні оточення ───────────────
# (price_finder читає SHEET_URL, SERPER_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON з env)
try:
    _secrets = dict(st.secrets)
except Exception:
    _secrets = {}
for _key in ("SHEET_URL", "SERPER_API_KEY", "GOOGLE_SERVICE_ACCOUNT_JSON"):
    if _key in _secrets:
        os.environ[_key] = str(_secrets[_key])

# Імпорт ПІСЛЯ встановлення env, щоб price_finder одразу побачив секрети
import price_finder

_ANSI = re.compile(r"\x1b\[[0-9;]*m")   # для очищення кольорових кодів у журналі

st.set_page_config(page_title="EScore — Оновлення цін", page_icon="⚡")

st.title("⚡ EScore — Оновлення цін СЕС")
st.caption(
    "Натисни кнопку — скрипт знайде актуальні ціни (прайс ETI + Serper) "
    "і запише їх у таблицю. Заповнюються лише порожні комірки."
)

if st.button("🔄 Оновити ціни", type="primary"):
    buf = io.StringIO()
    ok, err = True, None
    with st.spinner("Оновлюю ціни… це може зайняти кілька хвилин."):
        try:
            with contextlib.redirect_stdout(buf):
                price_finder.run_update()
        except Exception as e:        # будь-яка помилка — показуємо, не валимо застосунок
            ok, err = False, e

    log = _ANSI.sub("", buf.getvalue())
    if ok:
        st.success("✅ Готово! Ціни оновлено в таблиці.")
    else:
        st.error(f"❌ Помилка: {err}")
    if log.strip():
        st.text_area("Журнал виконання", log, height=420)

st.divider()
st.caption("Доступ до застосунку — лише для співробітників EScore.")
