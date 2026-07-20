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
import os
import re
import time
 
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
    "Натисни кнопку — скрипт знайде актуальні ціни (прайс ETI + прайс CHINT + Serper) "
    "і запише їх у таблицю. Заповнюються лише порожні комірки."
)
 
# ── Відмітка останнього оновлення (читається з таблиці) ────────────
_last_update = ""
try:
    _last_update = price_finder.get_last_update()
except Exception:
    _last_update = ""
if _last_update:
    st.info(f"🕓 {_last_update}")
else:
    st.caption("🕓 Ще не оновлювалося")
 
 
class _LiveLogWriter:
    """Пише в реальному часі у видимий Streamlit-плейсхолдер замість буфера,
    який показувався тільки після завершення. Це дає видимість, на якому
    кроці скрипт зараз знаходиться, поки він ще виконується."""
 
    def __init__(self, placeholder):
        self.placeholder = placeholder
        self.lines = []
        self._last_render = 0.0
 
    def write(self, s):
        if not s:
            return
        self.lines.append(s)
        now = time.monotonic()
        # оновлюємо UI не частіше ніж раз на 0.3с — щоб не заспамити сокет
        if now - self._last_render > 0.3 or "\n" in s:
            self._last_render = now
            text = _ANSI.sub("", "".join(self.lines))
            self.placeholder.code(text[-4000:], language=None)
 
    def flush(self):
        pass
 
    def full_text(self):
        return _ANSI.sub("", "".join(self.lines))
 
 
if st.button("🔄 Оновити ціни", type="primary"):
    st.caption("Журнал виконання (оновлюється наживо):")
    log_placeholder = st.empty()
    writer = _LiveLogWriter(log_placeholder)
 
    ok, err = True, None
    with st.spinner("Оновлюю ціни… це може зайняти кілька хвилин."):
        try:
            with contextlib.redirect_stdout(writer):
                price_finder.run_update()
        except Exception as e:        # будь-яка помилка — показуємо, не валимо застосунок
            ok, err = False, e
 
    log = writer.full_text()
    # фінальний рендер повного логу (без обрізки)
    if log.strip():
        log_placeholder.code(log, language=None)
 
    if ok:
        _fresh = ""
        try:
            _fresh = price_finder.get_last_update()
        except Exception:
            _fresh = ""
        st.success("✅ Готово! Ціни оновлено в таблиці." + (f"  ({_fresh})" if _fresh else ""))
    else:
        st.error(f"❌ Помилка: {err}")
 
st.divider()
st.caption("Доступ до застосунку — лише для співробітників EScore.")