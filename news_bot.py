    return orders


def initialize_db(db):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS sources (url TEXT PRIMARY KEY)")
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


async def fetch_feed(session, url):
    async with session.get(url, headers={"Accept": "application/rss+xml, application/atom+xml, */*"}) as response:
        response.raise_for_status()
        return parse_feed(await response.read(), str(response.url))


async def send_order(session, token, admin_id, order):
    _, link, source, title, description = order
    message = f"{source[:100]}\n{title[:400]}"
    if description:
        message += f"\n\n{description[:700]}"
    async with session.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": admin_id,
            "text": message,
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": [[{"text": "Открыть заказ", "url": link}]]},
        },
    ) as response:
        result = await response.json()
        if not result.get("ok"):
            delay = result.get("parameters", {}).get("retry_after")
            LOG.warning("Telegram отклонил отправку (HTTP %s).", response.status)
            return False, delay
        return True, None


async def poll_sources(session, db, feeds, token, admin_id, interval):
    retry_at = 0
    clock = asyncio.get_running_loop().time
    while True:
        results = await asyncio.gather(*(fetch_feed(session, url) for url in feeds.values()),
                                       return_exceptions=True)
        for (source, url), result in zip(feeds.items(), results):
            if isinstance(result, Exception):
                LOG.warning("%s: лента недоступна или некорректна (%s).",
                            source, type(result).__name__)
                continue
            initial = store_orders(db, source, url, result)
            LOG.info("%s: %s объявлений%s.", source, len(result),
                     " запомнено при первом запуске" if initial else " в ленте")
        if clock() >= retry_at:
            pending = db.execute(
                """SELECT seq, url, source, title, description FROM orders
                   WHERE delivered = 0 ORDER BY seq"""
            ).fetchall()
            for order in pending:
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
    interval = max(30, int(os.environ.get("POLL_SECONDS", "120")))
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
                await poll_sources(session, db, feeds, token, admin_id, interval)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
