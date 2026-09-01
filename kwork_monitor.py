import asyncio
import json
import logging
import os
import html
import aiohttp
from aiogram import Bot


# ============================================================
# НАСТРОЙКИ
# ============================================================

# Токен и chat_id берём из переменных окружения (GitHub Secrets),
# никогда не хардкодим в коде!
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])

KWORK_URL = "https://kwork.ru/projects"

# Файл, в котором храним id уже виденных заказов между запусками
SEEN_FILE = "seen_orders.json"

# Сколько последних id храним (чтобы файл не рос бесконечно)
MAX_SEEN = 1000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
}

bot = Bot(token=BOT_TOKEN)


# ============================================================
# ХРАНЕНИЕ УЖЕ ВИДЕННЫХ ЗАКАЗОВ (файл вместо памяти процесса)
# ============================================================

def load_seen_orders():
    if not os.path.exists(SEEN_FILE):
        return set(), True  # (пусто, это первый запуск)

    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data), False
    except (json.JSONDecodeError, OSError):
        logging.warning("Не удалось прочитать seen_orders.json, считаем первым запуском")
        return set(), True


def save_seen_orders(seen_orders):
    # Оставляем только последние MAX_SEEN, чтобы файл не рос вечно
    trimmed = list(seen_orders)[-MAX_SEEN:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(trimmed, f, ensure_ascii=False)


# ============================================================
# ПОЛУЧЕНИЕ ПЕРВОЙ СТРАНИЦЫ KWORK
# ============================================================

async def get_page(session):
    try:
        async with session.get(
            KWORK_URL,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=10, connect=5, sock_read=8)
        ) as response:

            if response.status != 200:
                logging.error(f"Kwork вернул HTTP {response.status}")
                return None

            raw = await response.read()

            try:
                page = raw.decode("utf-8")
            except UnicodeDecodeError:
                page = raw.decode("cp1251", errors="replace")

            logging.info(f"Kwork: получено {len(page)} символов")
            return page

    except asyncio.TimeoutError:
        logging.warning("⏱ Kwork не ответил вовремя. Пропускаем эту проверку.")
    except aiohttp.ClientError as e:
        logging.warning(f"Ошибка соединения с Kwork: {e}")
    except Exception:
        logging.exception("Неизвестная ошибка Kwork")

    return None


# ============================================================
# ИЗВЛЕЧЕНИЕ МАССИВА wants
# ============================================================

def extract_orders(page):
    marker = '"wants":'
    position = page.find(marker)

    if position == -1:
        logging.error('В HTML не найден "wants"')
        return []

    start = position + len(marker)

    while start < len(page) and page[start].isspace():
        start += 1

    if start >= len(page) or page[start] != "[":
        logging.error('После "wants" нет JSON-массива')
        return []

    decoder = json.JSONDecoder()

    try:
        orders, _ = decoder.raw_decode(page[start:])
    except json.JSONDecodeError as e:
        logging.error(f"Ошибка разбора JSON: {e}")
        return []

    if not isinstance(orders, list):
        logging.error("wants не является списком")
        return []

    return orders


# ============================================================
# ПОЛУЧЕНИЕ ЗАКАЗОВ
# ============================================================

async def fetch_orders(session):
    page = await get_page(session)
    if page is None:
        return None

    orders = extract_orders(page)
    logging.info(f"На первой странице: {len(orders)} заказов")
    return orders


# ============================================================
# ОТПРАВКА НОВОГО ЗАКАЗА
# ============================================================

async def send_order(order):
    order_id = order.get("id")
    title = order.get("name") or "Без названия"
    description = order.get("description") or "Без описания"
    price = order.get("priceLimit") or order.get("possiblePriceLimit") or "Не указан"

    link = f"https://kwork.ru/projects/{order_id}"

    title = html.escape(str(title))
    description = html.escape(str(description))
    price = html.escape(str(price))

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
            chat_id=CHAT_ID,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        logging.info(f"📨 Новый заказ отправлен: {title}")
    except Exception:
        logging.exception("Ошибка отправки заказа в Telegram")


# ============================================================
# ОДНОРАЗОВАЯ ПРОВЕРКА (адаптировано под GitHub Actions)
# ============================================================

async def check_kwork_once():
    seen_orders, first_check = load_seen_orders()

    connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)

    async with aiohttp.ClientSession(connector=connector, headers=HEADERS) as session:
        orders = await fetch_orders(session)

        if orders is None:
            logging.warning("Kwork не ответил, состояние не меняем")
            return

        if not orders:
            logging.warning("Kwork вернул 0 заказов")
            return

        if first_check:
            for order in orders:
                order_id = order.get("id")
                if order_id:
                    seen_orders.add(str(order_id))

            logging.info(f"Первый запуск: запомнено {len(seen_orders)} заказов, уведомления не шлём")
            save_seen_orders(seen_orders)
            return

        new_orders = []
        for order in orders:
            order_id = order.get("id")
            if not order_id:
                continue

            order_id = str(order_id)
            if order_id not in seen_orders:
                seen_orders.add(order_id)
                new_orders.append(order)

        if not new_orders:
            logging.info("Новых заказов нет")
        else:
            logging.info(f"🔥 НАЙДЕНО НОВЫХ ЗАКАЗОВ: {len(new_orders)}")
            for order in reversed(new_orders):
                await send_order(order)

        save_seen_orders(seen_orders)


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )
    asyncio.run(check_kwork_once())
