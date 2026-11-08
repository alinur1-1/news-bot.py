import asyncio
import os
import random
import sqlite3
import re
import feedparser
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
)
from google import genai

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", ""))
CHANNEL_ID = os.getenv("CHANNEL_ID", "@ActualAiNewsNow")
GEMINI_KEY = os.getenv("GEMINI_KEY", "")

APP_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
client = genai.Client(api_key=GEMINI_KEY)

bot_state = {
    "batch_size": 5,
    "auto_mode": True
}

RSS_FEEDS = [
    "https://openai.com/news/rss.xml",
    "https://deepmind.google/blog/rss.xml",
    "https://huggingface.co/blog/feed.xml",
    "https://blogs.nvidia.com/feed/",
    "https://news.google.com/rss/search?q=Anthropic+Claude+AI+when:2d&hl=en-US&gl=US&ceid=US:en",
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day",
    "https://www.reddit.com/r/singularity/top/.rss?t=day",
    "https://habr.com/ru/rss/hubs/artificial_intelligence/articles/?fl=ru",
    "https://habr.com/ru/rss/hubs/python/articles/?fl=ru",
    "https://hnrss.org/newest?points=100",
    "https://github.blog/feed/",
    "https://spectrum.ieee.org/feeds/topic/robotics.rss",
    "https://news.mit.edu/rss/topic/robotics",
    "https://www.sciencedaily.com/rss/computers_math/artificial_intelligence.xml",
    "https://news.google.com/rss/search?q=(humanoid+robot+OR+robotics+sports+OR+Boston+Dynamics+OR+Unitree)+when:3d&hl=en-US&gl=US&ceid=US:en",
    "https://www.datacenterdynamics.com/en/rss/",
    "https://news.google.com/rss/search?q=(AI+data+center+energy+OR+AI+electricity+water+consumption+OR+nuclear+AI)+when:3d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=(AI+trading+OR+algorithmic+trading+AI+OR+quant+hedge+fund+AI)+when:3d&hl=en-US&gl=US&ceid=US:en",
    "https://cointelegraph.com/rss/tag/artificial-intelligence",
    "https://rsshub.app/telegram/channel/ai_newz",
    "https://rsshub.app/telegram/channel/gonzo_ML"
]

def init_db():
    with sqlite3.connect("seen_news.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS seen (link TEXT PRIMARY KEY, title TEXT)")
        conn.commit()

def is_seen(link: str, title: str) -> bool:
    with sqlite3.connect("seen_news.db") as conn:
        cur = conn.execute("SELECT 1 FROM seen WHERE link = ? OR title = ?", (link, title))
        return cur.fetchone() is not None

def mark_seen(link: str, title: str):
    with sqlite3.connect("seen_news.db") as conn:
        conn.execute("INSERT OR IGNORE INTO seen (link, title) VALUES (?, ?)", (link, title))
        conn.commit()

def get_seen_count() -> int:
    with sqlite3.connect("seen_news.db") as conn:
        cur = conn.execute("SELECT COUNT(*) FROM seen")
        return cur.fetchone()[0]

def clean_html(raw_html: str) -> str:
    clean = re.sub(r"<.*?>", " ", raw_html or "")
    return re.sub(r"\s+", " ", clean).strip()[:1500]

# Поиск картинки в RSS-элементе
def extract_image(entry) -> str:
    # 1. Ищем в media_content
    if hasattr(entry, "media_content"):
        for media in entry.media_content:
            if "url" in media:
                return media["url"]
    # 2. Ищем в enclosures
    if hasattr(entry, "enclosures"):
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("image/"):
                return enc.get("href")
    # 3. Ищем тег img внутри summary/content
    content_html = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
    img_match = re.search(r'<img[^>]+src="([^">]+)"', content_html)
    if img_match:
        return img_match.group(1)
    return ""

# Генерация поста с зашитой ссылкой-невидимкой и жестким русским языком
async def summarize_news(title: str, summary: str, link: str) -> str:
    clean_desc = clean_html(summary)
    prompt = (
        f"Ты — практикующий разработчик и автор крупного Telegram-канала про технологии, код, железо, роботов и ИИ для СНГ аудитории.\n"
        f"ПЕРЕВЕДИ ЭТУ НОВОСТЬ НА РУССКИЙ ЯЗЫК И НАПИШИ ПОСТ:\n"
        f"Заголовок: {title}\n"
        f"Детали: {clean_desc}\n\n"
        f"СТРОГИЕ ПРАВИЛА:\n"
        f"1. ЯЗЫК: Весь текст должен быть ИСКЛЮЧИТЕЛЬНО на русском языке. Никаких английских предложений!\n"
        f"2. СТИЛЬ: 3–5 плотных абзацев (900–1400 символов). Без воды, без штампов вроде 'В современном мире' или 'Революционный прорыв'. Техническая конкретика, цифры, факты.\n"
        f"3. ССЫЛКА В ТЕКСТЕ: В самом конце поста органично вшей ссылку на источник в любое подходящее слово или фразу (например: '...прочитать об этом подробнее можно [в источнике]({link})' или '...исходный код доступен [на сайте проекта]({link})'). НЕ выноси ссылку отдельной строкой в самый низ, она должна быть частью последнего предложения!\n"
        f"4. Поставь 1–2 эмодзи в начале."
    )

    for model_name in ["gemini-3.8-flash", "gemini-2.5-flash"]:
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=model_name,
                contents=prompt
            )
            if response and response.text:
                res_text = response.text.strip()
                # Страховка: если модель забыла вставить ссылку markdown, добавим ее принудительно
                if link not in res_text:
                    res_text += f"\n\nПодробнее читайте [в источнике]({link})."
                return res_text
        except Exception as e:
            print(f"Ошибка Gemini ({model_name}): {e}")

    # Запасной вариант через Pollinations AI (если Gemini отклонил)
    try:
        async with aiohttp.ClientSession() as session:
            payload = {
                "messages": [
                    {"role": "system", "content": "Ты русскоязычный IT-автор. Пиши статьи строго на русском языке."},
                    {"role": "user", "content": prompt}
                ],
                "model": "openai"
            }
            async with session.post("https://text.pollinations.ai/", json=payload, timeout=45) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if text and len(text) > 100:
                        if link not in text:
                            text += f"\n\nПодробнее читайте [в источнике]({link})."
                        return text.strip()
    except Exception as e:
        print(f"Ошибка резервного ИИ: {e}")

    return f"⚡ {title}\n\n{clean_desc[:400]}...\n\nИсточник: [ссылка]({link})"

def get_main_menu() -> ReplyKeyboardMarkup:
    auto_status = "🟢 Авто: ВКЛ" if bot_state["auto_mode"] else "🔴 Авто: ВЫКЛ"
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🔥 Дай 3 статьи"),
                KeyboardButton(text="🔥 Дай 5 статей"),
                KeyboardButton(text="🔥 Дай 10 статей"),
            ],
            [
                KeyboardButton(text=auto_status),
                KeyboardButton(text="📊 Статистика базы"),
            ]
        ],
        resize_keyboard=True
    )

def get_draft_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ В канал", callback_data="pub"),
            InlineKeyboardButton(text="❌ Удалить", callback_data="del")
        ]
    ])

# Сбор и отправка новостей (теперь с картинкой сверху и текстом под ней)
async def fetch_and_send_news(limit: int, notify_empty: bool = False):
    feeds_copy = RSS_FEEDS.copy()
    random.shuffle(feeds_copy)

    sent_count = 0
    for url in feeds_copy:
        if sent_count >= limit:
            break
        try:
            feed = await asyncio.to_thread(feedparser.parse, url)
            for entry in feed.entries[:4]:
                link = entry.get("link", "")
                title = entry.get("title", "")
                if not link or not title:
                    continue
                if not is_seen(link, title):
                    mark_seen(link, title)
                    
                    post_text = await summarize_news(title, entry.get("summary", ""), link)
                    image_url = extract_image(entry)

                    if image_url:
                        # Отправляем с картинкой сверху
                        await bot.send_photo(
                            chat_id=ADMIN_ID,
                            photo=image_url,
                            caption=post_text,
                            reply_markup=get_draft_kb(),
                            parse_mode="Markdown"
                        )
                    else:
                        # Если картинки нет, то просто текст
                        await bot.send_message(
                            chat_id=ADMIN_ID,
                            text=post_text,
                            reply_markup=get_draft_kb(),
                            parse_mode="Markdown",
                            disable_web_page_preview=True
                        )

                    sent_count += 1
                    await asyncio.sleep(2)
                    if sent_count >= limit:
                        break
        except Exception as e:
            print(f"Ошибка чтения ленты {url}: {e}")

    if sent_count == 0 and notify_empty:
        await bot.send_message(ADMIN_ID, "Пока свежих необработанных статей нет — все текущие уже были показаны.")

@dp.message(Command("start", "menu", "test"))
async def cmd_start(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        f"⚙️ Панель управления новостным ботом\n\n"
        f"• Источников подключено: {len(RSS_FEEDS)}\n"
        f"• Уже обработано и в ЧС повторов: {get_seen_count()} статей\n"
        f"• Авто-поиск каждые 25 мин: {'Включен' if bot_state['auto_mode'] else 'Выключен'}\n\n"
        f"Выбери на кнопках внизу, сколько свежих статей выгрузить прямо сейчас:",
        reply_markup=get_main_menu()
    )

@dp.message(F.text.in_(["🔥 Дай 3 статьи", "🔥 Дай 5 статей", "🔥 Дай 10 статей"]))
async def handle_batch_request(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    count = int(message.text.split()[2])
    bot_state["batch_size"] = count
    status_msg = await message.answer(f"⏳ Собираю и пишу {count} свежих статей с картинками и переводом...")
    await fetch_and_send_news(limit=count, notify_empty=True)
    await status_msg.delete()

@dp.message(F.text.in_(["🟢 Авто: ВКЛ", "🔴 Авто: ВЫКЛ"]))
async def toggle_auto_mode(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    bot_state["auto_mode"] = not bot_state["auto_mode"]
    state_text = "включен (бот сам присылает новинки каждые 25 минут)" if bot_state["auto_mode"] else "поставлен на паузу"
    await message.answer(f"🔄 Автоматический режим {state_text}.", reply_markup=get_main_menu())

@dp.message(F.text == "📊 Статистика базы")
async def show_stats(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        f"📊 Статистика бота:\n"
        f"• В базе прочитанных статей: {get_seen_count()} шт.\n"
        f"• Активных RSS и TG-лент: {len(RSS_FEEDS)}\n"
        f"• Текущий лимит пачки: {bot_state['batch_size']} шт."
    )

@dp.callback_query(F.data == "pub")
async def publish_post(call: CallbackQuery):
    if call.message.photo:
        await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=call.message.photo[-1].file_id,
            caption=call.message.caption,
            parse_mode="Markdown"
        )
        await call.message.edit_caption(caption=f"{call.message.caption}\n\n✅ Опубликовано в {CHANNEL_ID}!", parse_mode="Markdown")
    else:
        await bot.send_message(
            chat_id=CHANNEL_ID,
            text=call.message.text,
            entities=call.message.entities,
            disable_web_page_preview=True
        )
        await call.message.edit_text(f"{call.message.text}\n\n✅ Опубликовано в {CHANNEL_ID}!")
    await call.answer("Пост улетел в канал!")

@dp.callback_query(F.data == "del")
async def delete_post(call: CallbackQuery):
    await call.message.delete()
    await call.answer("Черновик удален")

async def rss_loop():
    if get_seen_count() == 0:
        print("Первичная инициализация базы...")
        for url in RSS_FEEDS:
            try:
                feed = await asyncio.to_thread(feedparser.parse, url)
                for entry in feed.entries[2:]:
                    if entry.get("link") and entry.get("title"):
                        mark_seen(entry.link, entry.title)
            except Exception:
                pass
        print(f"База готова! Записано старых ссылок: {get_seen_count()}")

    while True:
        await asyncio.sleep(1500)
        if bot_state["auto_mode"]:
            await fetch_and_send_news(limit=bot_state["batch_size"], notify_empty=False)

async def self_ping():
    await asyncio.sleep(10)
    if not APP_URL:
        print("Бот запущен! Открой Telegram и напиши боту /start")
        return

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(APP_URL) as resp:
                    print(f"Self-ping status: {resp.status}")
            except Exception as e:
                print(f"Self-ping error: {e}")
            await asyncio.sleep(600)

async def handle_ping(request):
    return web.Response(text="Bot is alive!")

async def main():
    init_db()
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    asyncio.create_task(rss_loop())
    asyncio.create_task(self_pin := self_ping())
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
