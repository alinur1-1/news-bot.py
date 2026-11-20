"""Telegram-уведомления о фриланс-заказах.

Установка: python -m pip install aiohttp
Запуск: python freelance_bot.py
Переменные окружения:
  BOT_TOKEN — токен Telegram-бота, задаётся в настройках хостинга.
  ADMIN_ID — числовой Telegram ID получателя. Сначала нажмите Start у бота.
  RSS_FEEDS — необязательный JSON-объект {"Площадка": "https://.../feed.xml"}.
  В RSS_FEEDS также поддерживается официальный JSON-поиск Himalayas.
  POLL_SECONDS — интервал проверки в секундах, по умолчанию 120 (минимум 30).
  DB_PATH — путь к SQLite, по умолчанию freelance.sqlite3.
  IMAP_HOST, IMAP_USER, IMAP_PASSWORD — необязательное подключение к почте (TLS).
  IMAP_FOLDER — папка только для уведомлений о заказах (например FreelanceAlerts).
  Создайте правила почты, которые складывают туда нужные письма площадок.
  EMAIL_BOOTSTRAP — необязательно: 1, чтобы также отправить последние 100 писем.
  PORT — порт HTTP-проверки состояния для хостинга, по умолчанию 8080.
  RENDER_EXTERNAL_URL — публичный URL Render для самопинга каждые 14 минут.
Запускайте один экземпляр и храните SQLite на постоянном диске.
"""

import asyncio
import html
import imaplib
import json
import logging
import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import closing
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web

DEFAULT_FEEDS = {
    "Freelancehunt": "https://freelancehunt.com/projects.rss",
    "Jobicy": "https://jobicy.com/jobs/feed?type=contract,freelance",
    "Himalayas": "https://himalayas.app/jobs/api/search?country=KZ&employment_type=Contractor&sort=recent",
}
TELEGRAM_CHANNELS = (
    "free_Design1",
    "webfrl",
    "digital_partners_chat",
    "vacancies_in_english",
    "Freelanceroff",
    "freelance_jobs_online",
    "freelancersworldwide",
    "cashhuntersKZ",
    "jobkz_1",
    "remote_kazakhstan",
    "smm_almaty1",
    "smm_astana1",
    "jumystapjobs",
    "digitaltender",
    "digitaltalks",
    "itfreelancers",
    "outsource456",
    "devkg",
    "freelancetaverna",
    "GetClient",
    "GetFrilans",
    "zakaz_design",
    "motionhunter",
    "editors_video",
    "editorspro",
    "neiro_ai_vacancy",
    "neiro_ai_chat",
    "vacancysmm",
    "textodromo",
    "translationsTranslators",
    "freelancebay",
    "remotejobs4u",
    "zakazy_it",
    "designerworkchat",
    "designwork_vacansii",
    "design_freevacancies",
    "vakansii_dlya_dizaynera",
    "chikchirikbird",
    "takoyfreelance",
    "board_axolotl",
    "diworkru",
    "freelanceeboom",
    "work_editor",
    "tut_zakaz",
    "freelancevpsmm",
    "Frilans_Birzha",
    "rabota_is_doma_vakansii",
    "zaichenkoteam",
)
SOURCE_MIN_INTERVALS = {"jobicy.com": 3600, "himalayas.app": 86400, "t.me": 300}
EMAIL_PLATFORMS = {
    "kwork.ru": ("Kwork", "https://kwork.ru/projects"),
    "weblancer.net": ("Weblancer", "https://www.weblancer.net/"),
    "upwork.com": ("Upwork", "https://www.upwork.com/nx/find-work/"),
    "fiverr.com": ("Fiverr", "https://www.fiverr.com/inbox"),
    "freelancehunt.com": ("Freelancehunt", "https://freelancehunt.com/projects"),
}
LOG = logging.getLogger("freelance_bot")

HIRING = re.compile(
    r"\b(?:ищем|ищу|нуж(?:ен|на|ны|но)|требу(?:ется|ются)|"
    r"ваканси[яию]|нанимаем|открыта\s+позиция|looking\s+for|"
    r"hiring|wanted|job\s+opening|seeking|we\s+need)\b|"
    r"#(?:заказ|проект|вакансия)\b",
    re.IGNORECASE,
)
WORK_AREAS = re.compile(
    r"\b(?:сайт\w*|лендинг\w*|в[еэ]б\w*|верст\w*|разработ\w*|"
    r"программист\w*|приложени\w*|телеграм[- ]бот\w*|бот\w*|"
    r"автоматизаци\w*|интеграци\w*|дизайн\w*|график\w*|"
    r"иллюстратор\w*|моуш[еэ]н\w*|монтаж\w*|видеомонтаж\w*|"
    r"видео\w*|анимаци\w*|ретуш\w*|моделировани\w*|"
    r"python|java|javascript|typescript|react|wordpress|tilda|figma|"
    r"front[- ]?end|back[- ]?end|full[- ]?stack|web\w*|developer\w*|"
    r"design\w*|video|editor\w*|motion|3[дd]|ui|ux|1[сc]|"
    r"smm|смм|нейросет\w*|ai[- ](?:video|designer|developer))\b",
    re.IGNORECASE,
)
SELF_PROMOTION = re.compile(
    r"\b(?:ищу\s+(?:работу|заказы|клиентов|подработку|проект(?:ы|ов)?)|"
    r"предлагаю\s+(?:услуги|свои\s+услуги)|возьму\s+(?:заказ|проект)|"
    r"открыт[аы]?\s+к\s+(?:работе|проектам)|"
    r"подписывайтесь\s+на\s+(?:наш|мой)\s+канал|"
    r"looking\s+for\s+(?:work|clients|projects)|available\s+for\s+(?:work|hire))\b|"
    r"\[\s*for\s+hire\s*\]|#(?:ищуработу|резюме|самореклама)\b",
    re.IGNORECASE,
)


def is_telegram_order(text):
    return bool(HIRING.search(text) and WORK_AREAS.search(text) and not SELF_PROMOTION.search(text))


def plain_text(value):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def parse_feed(data, base_url):
    root = ET.fromstring(data)
    kind = root.tag.rsplit("}", 1)[-1].lower()
    if kind not in {"rss", "rdf", "feed"}:
        raise ValueError("ответ не является RSS или Atom")
    if kind == "rss" and root.find("channel") is None:
        raise ValueError("в RSS отсутствует channel")
    entries = root.findall(".//{*}item") if kind != "feed" else root.findall("{*}entry")
    orders = {}
    for entry in entries:
        title = plain_text(entry.findtext("{*}title", ""))
        description = plain_text(
            entry.findtext("{*}description") or entry.findtext("{*}summary") or ""
        )
        for element in entry.findall("{*}link"):
            if element.get("rel", "alternate") != "alternate":
                continue
            link = urljoin(base_url, (element.get("href") or element.text or "").strip())
            parts = urlsplit(link)
            if title and parts.scheme in {"http", "https"} and parts.netloc and link != base_url:
                link = parts._replace(fragment="").geturl()
                orders.setdefault(link, (title, description))
                break
    if entries and not orders:
        raise ValueError("в объявлениях отсутствуют заголовки или прямые ссылки")
    return orders


def parse_alert(raw):
    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = parseaddr(str(message.get("From", "")))[1]
    host = sender.rpartition("@")[2].lower()
    for domain, (source, home) in EMAIL_PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            break
    else:
        return None

    subject = str(message.get("Subject", "")).strip() or "Уведомление"
    body_part = message.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part else ""
    html_part = message.get_body(preferencelist=("html",))
    link_body = body + ("\n" + html_part.get_content() if html_part else "")
    link = home
    for candidate in re.findall(r"https://[^\s<>\x22\x27]+", html.unescape(link_body)):
        try:
            parts = urlsplit(candidate.rstrip(").,;"))
            candidate_host = parts.hostname or ""
        except ValueError:
            continue
        if candidate_host not in {domain, "www." + domain, "support." + domain}:
            continue
        if re.search(r"/(?:projects?/|jobs/[~\w-]|inbox/|hire/)", parts.path):
            link = parts._replace(query="", fragment="").geturl()
            break

    message_id = str(message.get("Message-ID", "")).encode() or raw
    key = sha256(message_id).hexdigest()
    description = plain_text(body)
    return source, f"{link}#email-{key}", subject, description


def read_alerts(host, user, password, folder, after_uid, previous_epoch, bootstrap):
    with imaplib.IMAP4_SSL(host, timeout=20) as mailbox:
        mailbox.login(user, password)
        status, _ = mailbox.select('"' + folder.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"') + '"', readonly=True)
        if status != "OK":
            raise ValueError("Папка IMAP недоступна")
        _, epoch_values = mailbox.response("UIDVALIDITY")
        epoch = epoch_values[0].decode()
        after_uid = after_uid if epoch == previous_epoch else None
        if after_uid is None:
            criterion = "ALL"
        else:
            criterion = f"UID {after_uid + 1}:*"
        status, data = mailbox.uid("SEARCH", None, criterion)
        if status != "OK":
            raise ValueError("Поиск в IMAP недоступен")
        uids = sorted(int(uid) for uid in data[0].split())
        if after_uid is None and not bootstrap:
            return epoch, max(uids, default=0), []
        if after_uid is None:
            uids = uids[-100:]
        else:
            uids = [uid for uid in uids if uid > after_uid][:100]
        orders = []
        for uid in uids:
            status, payload = mailbox.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK":
                raise ValueError("Письмо IMAP недоступно")
            raw = next((item[1] for item in payload if isinstance(item, tuple)), None)
            if raw is None:
                continue
            alert = parse_alert(raw)
            if alert:
                orders.append(alert)
        return epoch, max(uids, default=after_uid or 0), orders


def initialize_db(db):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
        db.execute("""CREATE TABLE IF NOT EXISTS mail_cursors (
            account TEXT PRIMARY KEY, epoch TEXT NOT NULL, uid INTEGER NOT NULL
        )""")
        db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                seq INTEGER PRIMARY KEY,
                url TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                delivered INTEGER NOT NULL
            )
        """)


def store_orders(db, source, feed_url, orders):
    with db:
        first_check = db.execute(
            "INSERT OR IGNORE INTO sources VALUES (?)", (feed_url,)
        ).rowcount
        db.executemany(
            """INSERT OR IGNORE INTO orders (url, source, title, description, delivered)
               VALUES (?, ?, ?, ?, ?)""",
            [(link, source, title, description, first_check)
             for link, (title, description) in reversed(list(orders.items()))],
        )
    return bool(first_check)


class TelegramPreview(HTMLParser):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel.lower()
        self.posts = {}
        self.post = None
        self.text_depth = 0
        self.text = []

    def handle_starttag(self, tag, attrs):
        if self.text_depth:
            if tag == "div":
                self.text_depth += 1
            if tag in {"br", "p", "div"}:
                self.text.append(" ")
            return
        attrs = dict(attrs)
        if tag != "div":
            return
        if "data-post" in attrs:
            handle, _, message_id = (attrs["data-post"] or "").partition("/")
            self.post = message_id if handle.lower() == self.channel and message_id.isdecimal() else None
        if self.post and "tgme_widget_message_text" in attrs.get("class", "").split():
            self.text_depth = 1
            self.text = []

    def handle_data(self, data):
        if self.text_depth:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag != "div" or not self.text_depth:
            return
        self.text_depth -= 1
        if self.text_depth == 0:
            text = " ".join("".join(self.text).split())
            if text:
                link = f"https://t.me/{self.channel}/{self.post}"
                self.posts[link] = (text[:120], text if len(text) > 120 else "")


async def fetch_telegram_preview(session, url):
    channel = urlsplit(url).path.rsplit("/", 1)[-1]
    async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=8)) as response:
        response.raise_for_status()
        if response.status != 200:
            raise ValueError("нет публичной веб-ленты Telegram")
        body = await response.text()
    preview = TelegramPreview(channel)
    preview.feed(body)
    preview.close()
    if "tgme_channel_history" not in body and not preview.posts:
        raise ValueError("нет публичной веб-ленты Telegram")
    # The existing queue expects newest-first source results.
    return dict(sorted(preview.posts.items(),
                       key=lambda item: int(item[0].rsplit("/", 1)[-1]), reverse=True))


async def fetch_feed(session, url, telegram_slots=None):

    parts = urlsplit(url)
    if parts.hostname == "t.me" and parts.path.startswith("/s/"):
        if telegram_slots is None:
            return await fetch_telegram_preview(session, url)
        async with telegram_slots:
            return await fetch_telegram_preview(session, url)
    if parts.hostname == "himalayas.app" and parts.path == "/jobs/api/search":
        async with session.get(url) as response:
            response.raise_for_status()
            payload = await response.json()
        orders = {}
        for job in payload["jobs"]:
            title = plain_text(job.get("title") or "")
            link = job.get("applicationLink") or ""
            if title and urlsplit(link).scheme == "https" and urlsplit(link).hostname:
                description = plain_text(job.get("excerpt") or "")
                orders[link] = (title, f"Источник: https://himalayas.app\n{description}")
        return orders
    async with session.get(url, headers={"Accept": "application/rss+xml, application/atom+xml, */*"}) as response:
        response.raise_for_status()
        return parse_feed(await response.read(), str(response.url))


async def poll_mail(db, settings):
    host, user, password, folder = settings
    account = json.dumps([host, user, folder])
    cursor = db.execute(
        "SELECT epoch, uid FROM mail_cursors WHERE account = ?", (account,)
    ).fetchone()
    epoch, uid = cursor or (None, None)
    try:
        epoch, uid, orders = await asyncio.to_thread(
            read_alerts, host, user, password, folder, uid, epoch,
            os.getenv("EMAIL_BOOTSTRAP") == "1",
        )
    except (imaplib.IMAP4.error, OSError, ValueError) as error:
        LOG.warning("Почтовые уведомления недоступны (%s).", type(error).__name__)
        return
    with db:
        db.executemany(
            """INSERT OR IGNORE INTO orders (source, url, title, description, delivered)
               VALUES (?, ?, ?, ?, 0)""", orders,
        )
        db.execute(
            """INSERT INTO mail_cursors VALUES (?, ?, ?)
               ON CONFLICT(account) DO UPDATE SET epoch=excluded.epoch, uid=excluded.uid""",
            (account, epoch, uid),
        )


async def send_order(session, token, admin_id, order):
    _, link, source, title, description = order
    is_alert = "#email-" in link
    if is_alert:
        link = link.split("#email-", 1)[0]
    button = "Открыть площадку" if is_alert and link == next(
        (home for name, home in EMAIL_PLATFORMS.values() if name == source), None
    ) else "Открыть заказ"
    message = f"{source[:100]}\n{title[:400]}"
    if description:
        message += f"\n\n{description[:700]}"
    async with session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": admin_id,
            "text": message,
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": [[{"text": button, "url": link}]]},
        },
    ) as response:
        result = await response.json()
        if not result.get("ok"):
            delay = result.get("parameters", {}).get("retry_after")
            LOG.warning("Telegram отклонил отправку (HTTP %s).", response.status)
            return False, delay
        return True, None


async def poll_sources(session, db, feeds, token, admin_id, interval, mail=None):
    retry_at = 0
    next_poll = {}
    telegram_slots = asyncio.Semaphore(4)
    clock = asyncio.get_running_loop().time
    while True:
        now = clock()
        due = [(source, url) for source, url in feeds.items() if now >= next_poll.get(url, 0)]
        for _, url in due:
            host = (urlsplit(url).hostname or "").removeprefix("www.")
            next_poll[url] = now + max(interval, SOURCE_MIN_INTERVALS.get(host, 0))
        results = await asyncio.gather(*(fetch_feed(session, url, telegram_slots) for _, url in due),
                                       return_exceptions=True)
        for (source, url), result in zip(due, results):
            if isinstance(result, Exception):
                LOG.warning("%s: лента недоступна или некорректна (%s).",
                            source, type(result).__name__)
                continue
            initial = store_orders(db, source, url, result)
            LOG.info("%s: %s объявлений%s.", source, len(result),
                     " запомнено при первом запуске" if initial else " в ленте")
        if mail:
            await poll_mail(db, mail)
        if clock() >= retry_at:
            pending = db.execute(
                """SELECT seq, url, source, title, description FROM orders
                   WHERE delivered = 0 ORDER BY seq"""
            ).fetchall()
            for order in pending:
                _, _, source, title, description = order
                if source.startswith("Telegram @") and not is_telegram_order(description or title):
                    with db:
                        db.execute("UPDATE orders SET delivered = 1 WHERE seq = ?", (order[0],))
                    continue
                try:
                    delivered, delay = await send_order(session, token, admin_id, order)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                    LOG.warning("Ошибка связи с Telegram (%s).", type(error).__name__)
                    break
                if not delivered:
                    retry_at = clock() + (delay or interval)
                    break
                with db:
                    db.execute("UPDATE orders SET delivered = 1 WHERE seq = ?", (order[0],))
                await asyncio.sleep(1)
        await asyncio.sleep(interval)


async def handle_ping(_request):
    return web.Response(text="OK")


async def keepalive(session, public_url):
    url = public_url.rstrip("/") + "/"
    while True:
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                await response.read()
                LOG.info("Самопинг Render: HTTP %s.", response.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            LOG.warning("Самопинг Render не удался (%s).", type(error).__name__)
        await asyncio.sleep(14 * 60)


async def main():
    token = os.environ.get("BOT_TOKEN")
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if not token or admin_id <= 0:
        raise SystemExit("Задайте BOT_TOKEN и ADMIN_ID в окружении.")
    feeds = json.loads(os.environ["RSS_FEEDS"]) if "RSS_FEEDS" in os.environ else DEFAULT_FEEDS
    if not isinstance(feeds, dict) or not feeds or not all(
        isinstance(url, str) and urlsplit(url).scheme in {"http", "https"}
        and urlsplit(url).netloc for url in feeds.values()
    ):
        raise SystemExit("RSS_FEEDS должен содержать названия площадок и HTTP(S)-адреса RSS/Atom.")
    feeds = {**feeds, **{f"Telegram @{name}": f"https://t.me/s/{name}" for name in TELEGRAM_CHANNELS}}
    interval = max(30, int(os.environ.get("POLL_SECONDS", "120")))
    mail = tuple(os.getenv(key) for key in
                 ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD", "IMAP_FOLDER"))
    if any(mail) and not all(mail):
        raise SystemExit("Для почты задайте все четыре переменные IMAP.")
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        port = int(os.environ.get("PORT", "8080"))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        with closing(sqlite3.connect(os.environ.get("DB_PATH", "freelance.sqlite3"))) as db:
            initialize_db(db)
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                public_url = os.getenv("RENDER_EXTERNAL_URL")
                ping_task = asyncio.create_task(keepalive(session, public_url)) if public_url else None
                try:
                    await poll_sources(session, db, feeds, token, admin_id, interval, mail if all(mail) else None)
                finally:
                    if ping_task:
                        ping_task.cancel()
                        await asyncio.gather(ping_task, return_exceptions=True)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
