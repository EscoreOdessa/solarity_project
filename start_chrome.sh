#!/bin/bash
# ─────────────────────────────────────────────────────────
# Окремий ПОСТІЙНИЙ профіль Chrome з debug-портом 9222.
# Твій основний Chrome НЕ чіпаємо і закривати не треба.
# Один раз увійдеш у Google у цьому вікні — і капча зникне.
# ─────────────────────────────────────────────────────────

PORT=9222
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_DATA="$HOME/chrome-escore-debug"   # постійний (НЕ /tmp), щоб логін зберігся

# Перевіряємо саме 127.0.0.1 (як до нього стукає price_finder.py)
if curl -s "http://127.0.0.1:$PORT/json/version" > /dev/null 2>&1; then
    echo "✅ Chrome вже готовий на порту $PORT — запускай: python3 price_finder.py"
    exit 0
fi

echo "🚀 Запускаю окремий Chrome на порту $PORT (основний не чіпаю)..."
"$CHROME" \
    --remote-debugging-port=$PORT \
    --remote-debugging-address=127.0.0.1 \
    --user-data-dir="$USER_DATA" \
    --no-first-run \
    --no-default-browser-check \
    > /dev/null 2>&1 &

echo -n "   Чекаю запуску"
for i in {1..30}; do
    sleep 1
    echo -n "."
    if curl -s "http://127.0.0.1:$PORT/json/version" > /dev/null 2>&1; then
        echo ""
        echo "✅ Готово."
        echo "   ПЕРШИЙ РАЗ: у вікні, що відкрилось, зайди на google.com і увійди у свій Google-акаунт."
        echo "   Потім запускай:  python3 price_finder.py"
        exit 0
    fi
done
echo ""
echo "❌ Не піднявся на порту $PORT. Перевір шлях CHROME у скрипті."
exit 1
