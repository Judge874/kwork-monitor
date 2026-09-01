import asyncio
import json
import logging
import os
import html
import aiohttp
from aiohttp import web
from aiogram import Bot

# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    raise ValueError("Не заданы переменные окружения BOT_TOKEN или CHAT_ID!")

CHAT_ID = int(CHAT_ID)
KWORK_URL = "https://kwork.ru/projects"
SEEN_FILE = "seen_orders.json"
MAX_SEEN = 1000
CHECK_INTERVAL = 180  # Проверка каждые 180 секунд

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

# ============================================================
# ХРАНЕНИЕ ЗАКАЗОВ
# ============================================================

def load_seen_orders():
    if not os.path.exists(SEEN_FILE):
        return [], True
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [str(item) for item in data], False
    except (json.JSONDecodeError, OSError):
        return [], True

def save_seen_orders(seen_orders_list):
    trimmed = seen_orders_list[-MAX_SEEN:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False, indent=2)

# ============================================================
# ПАРСИНГ И ОТПРАВКА
# ============================================================

async def get_page(session):
    try:
        async with session.get(
            KWORK_URL,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=10, connect=5, sock_read=8)
        ) as response:
            if response.status != 200:
                return None
            raw = await response.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("cp1251", errors="replace")
    except Exception as e:
        logging.warning(f"Ошибка получения страницы: {e}")
    return None

def extract_orders(page):
    marker = '"wants":'
    position = page.find(marker)
    if position == -1:
        return []
    start = position + len(marker)
    while start < len(page) and page[start].isspace():
        start += 1
    if start >= len(page) or page[start] != "[":
        return []
    try:
        orders, _ = json.JSONDecoder().raw_decode(page[start:])
        return orders if isinstance(orders, list) else []
    except json.JSONDecodeError:
        return []

async def send_order(bot: Bot, order: dict):
    order_id = order.get("id")
    title = html.escape(str(order.get("name") or "Без названия"))
    description = html.escape(str(order.get("description") or "Без описания"))
    price = html.escape(str(order.get("priceLimit") or order.get("possiblePriceLimit") or "Не указан"))
    link = f"https://kwork.ru/projects/{order_id}"

    if len(description) > 1500:
        description = description[:1500] + "..."

    message = (
        "🔥 <b>НОВЫЙ ЗАКАЗ KWORK!</b>\n\n"
        f"📌 <b>Проект:</b>\n{title}\n\n"
        f"💰 <b>Бюджет:</b> {price} ₽\n\n"
        f"📝 <b>Описание:</b>\n{description}\n\n"
        f"🔗 <a href=\"{link}\">Открыть заказ на Kwork</a>"
    )

    try:
        await bot.send_message(
            chat_id=CHAT_ID, text=message, parse_mode="HTML", disable_web_page_preview=True
        )
        logging.info(f"📨 Новый заказ отправлен: {title}")
    except Exception:
        logging.exception("Ошибка отправки заказа в Telegram")

# ============================================================
# ФОНОВЫЙ ПАРСЕР
# ============================================================

async def monitor_task():
    seen_orders_list, first_check = load_seen_orders()
    seen_set = set(seen_orders_list)
    bot = Bot(token=BOT_TOKEN)
    connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)

    logging.info("🚀 Парсер Kwork запущен!")

    try:
        async with aiohttp.ClientSession(connector=connector, headers=HEADERS) as session:
            while True:
                try:
                    page = await get_page(session)
                    if page:
                        orders = extract_orders(page)
                        if orders:
                            if first_check:
                                for order in orders:
                                    order_id = str(order.get("id"))
                                    if order_id and order_id not in seen_set:
                                        seen_set.add(order_id)
                                        seen_orders_list.append(order_id)
                                save_seen_orders(seen_orders_list)
                                first_check = False
                            else:
                                new_orders = []
                                for order in orders:
                                    order_id = str(order.get("id"))
                                    if order_id and order_id not in seen_set:
                                        seen_set.add(order_id)
                                        seen_orders_list.append(order_id)
                                        new_orders.append(order)

                                if new_orders:
                                    logging.info(f"🔥 Найдено новых заказов: {len(new_orders)}")
                                    for order in reversed(new_orders):
                                        await send_order(bot, order)
                                    save_seen_orders(seen_orders_list)
                except Exception as e:
                    logging.error(f"Ошибка в цикле парсинга: {e}")

                await asyncio.sleep(CHECK_INTERVAL)
    finally:
        await bot.session.close()

# ============================================================
# МИНИМАЛЬНЫЙ ВЕБ-СЕРВЕР ДЛЯ RENDER FREE TIER
# ============================================================

async def handle_ping(request):
    return web.Response(text="Kwork Monitor is running!")

async def start_app():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    
    # Запускаем парсер как фоновую задачу asyncio
    asyncio.create_task(monitor_task())
    return app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    port = int(os.environ.get("PORT", 10000))
    web.run_app(start_app(), host="0.0.0.0", port=port)
