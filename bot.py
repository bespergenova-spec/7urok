"""Telegram-бот: время, погода в Астане и курс биткоина на любое сообщение.

«привет» -> вместо погоды текст из privet.txt; «что нового» -> последняя новость @zakonkz.
Только стандартная библиотека. Погода: Open-Meteo, биткоин: CoinGecko (без ключей).
Токен читается из .env (BOT_TOKEN=...).
"""
import html
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# задачи из «Планера задач» (личные данные, исключены из git)
TASKS_FILE = os.environ.get("BOT_TASKS_FILE") or os.path.join(BASE_DIR, "tasks.json")
SYNC_PORT = int(os.environ.get("BOT_SYNC_PORT", "8765"))
MAX_TASKS = 15

# Астана
LAT, LON = 51.1801, 71.4460
# UTC+5, без перехода на летнее время (фиксированный пояс не требует пакета tzdata на Windows)
ASTANA_TZ = timezone(timedelta(hours=5))
NEWS_URL = "https://t.me/s/zakonkz"

WEATHER_CODES = {
    0: "ясно", 1: "преимущественно ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозевый туман",
    51: "лёгкая морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "сильная ледяная морось",
    61: "небольшой дождь", 63: "дождь", 65: "сильный дождь",
    66: "ледяной дождь", 67: "сильный ледяной дождь",
    71: "небольшой снег", 73: "снег", 75: "сильный снег", 77: "снежные зёрна",
    80: "небольшой ливень", 81: "ливень", 82: "сильный ливень",
    85: "небольшой снегопад", 86: "сильный снегопад",
    95: "гроза", 96: "гроза с градом", 99: "сильная гроза с градом",
}


def load_env(path):
    env = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def http_json(url, data=None, timeout=60):
    body = None
    headers = {}
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def get_weather_text():
    params = urllib.parse.urlencode({
        "latitude": LAT,
        "longitude": LON,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "pressure_msl,wind_speed_10m,wind_direction_10m,weather_code",
        "wind_speed_unit": "ms",
        "timezone": "Asia/Almaty",
    })
    cur = http_json(f"https://api.open-meteo.com/v1/forecast?{params}", timeout=15)["current"]
    desc = WEATHER_CODES.get(cur["weather_code"], "—")
    pressure_mmhg = round(cur["pressure_msl"] * 0.750062)
    return (
        "Погода в Астане сейчас:\n"
        f"{desc.capitalize()}\n"
        f"Температура: {cur['temperature_2m']:+.1f}°C "
        f"(ощущается как {cur['apparent_temperature']:+.1f}°C)\n"
        f"Ветер: {cur['wind_speed_10m']} м/с\n"
        f"Влажность: {cur['relative_humidity_2m']}%\n"
        f"Давление: {pressure_mmhg} мм рт. ст."
    )


def get_bitcoin_text():
    params = urllib.parse.urlencode({
        "ids": "bitcoin",
        "vs_currencies": "usd,kzt",
        "include_24hr_change": "true",
    })
    btc = http_json(f"https://api.coingecko.com/api/v3/simple/price?{params}", timeout=15)["bitcoin"]
    return (
        "Курс биткоина:\n"
        f"${btc['usd']:,.0f} ({btc['usd_24h_change']:+.2f}% за 24 ч)\n"
        f"{btc['kzt']:,.0f} ₸"
    ).replace(",", " ")


def get_time_text():
    now = datetime.now(ASTANA_TZ)
    return f"Время в Астане: {now:%H:%M:%S}, {now:%d.%m.%Y}"


def get_latest_news_text():
    """Последняя новость из публичного Telegram-канала @zakonkz (веб-превью t.me/s/...)."""
    req = urllib.request.Request(NEWS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        page = resp.read().decode("utf-8", errors="replace")

    # посты идут от старых к новым, берём последний, у которого есть текст
    for block in reversed(page.split("tgme_widget_message_wrap")[1:]):
        post = re.search(r'data-post="([^"]+)"', block)
        body = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                         block, re.S)
        if not (post and body):
            continue
        text = re.sub(r"<br\s*/?>", "\n", body.group(1))
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
        text = re.sub(r"[ \t ]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            return f"Последняя новость Zakon.kz:\n\n{text[:3500]}\n\nhttps://t.me/{post.group(1)}"
    raise RuntimeError("в канале не найдено постов с текстом")


def get_greeting_text():
    """Текст для ответа на «привет» берётся из privet.txt рядом с ботом."""
    try:
        with open(os.path.join(BASE_DIR, "privet.txt"), encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        text = ""
    return text or "Привет!"


class PlannerSyncHandler(BaseHTTPRequestHandler):
    """Принимает список задач от «Планера задач» (страница в Edge) и кладёт в tasks.json."""

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _reply(self, code):
        self.send_response(code)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self):  # noqa: N802
        self._reply(204)

    def do_POST(self):  # noqa: N802
        # страница планера открыта как file:// -> Origin "null"; чужие сайты отсекаем
        if self.path != "/tasks" or self.headers.get("Origin") not in (None, "null"):
            return self._reply(403)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 2_000_000:
                return self._reply(413)
            tasks = json.loads(self.rfile.read(length).decode("utf-8"))["tasks"]
            if not isinstance(tasks, list):
                return self._reply(400)
            tmp = TASKS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"updated": datetime.now(ASTANA_TZ).isoformat(), "tasks": tasks},
                          f, ensure_ascii=False)
            os.replace(tmp, TASKS_FILE)
            self._reply(204)
        except (ValueError, KeyError, OSError):
            self._reply(400)

    def log_message(self, *args):  # без шума в консоли
        pass


def start_planner_sync():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", SYNC_PORT), PlannerSyncHandler)
    except OSError as e:
        print(f"Синхронизация с планером недоступна (порт {SYNC_PORT}): {e}", flush=True)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Синхронизация с планером: http://127.0.0.1:{SYNC_PORT}/tasks", flush=True)


CAT_NAMES = {"work": "Работа", "health": "Здоровье", "family": "Семья",
             "friends": "Друзья", "home": "Дом"}


def get_tasks_text():
    """Открытые задачи из «Планера задач» (данные приходят через start_planner_sync)."""
    try:
        with open(TASKS_FILE, encoding="utf-8") as f:
            tasks = json.load(f)["tasks"]
    except (OSError, ValueError, KeyError):
        return "Задачи: данных пока нет — откройте «Планер задач», он передаст список."

    now = datetime.now(ASTANA_TZ)
    today, hm = f"{now:%Y-%m-%d}", f"{now:%H:%M}"
    open_tasks = [t for t in tasks if not t.get("done")]
    if not open_tasks:
        return "Задачи: открытых нет."
    open_tasks.sort(key=lambda t: (t.get("due") or "9999", t.get("time") or "99:99"))

    lines = [f"Открытые задачи ({len(open_tasks)}):"]
    for t in open_tasks[:MAX_TASKS]:
        due, tm = t.get("due") or "", t.get("time") or ""
        late = bool(due) and (due < today or (due == today and tm and tm < hm))
        when = f"{due[8:10]}.{due[5:7]}.{due[:4]}" + (f" {tm}" if tm else "") if due else "без срока"
        cat = CAT_NAMES.get(t.get("cat"), "")
        lines.append(f"{'⚠️' if late else '•'} {t.get('title', '')} [{cat}] — {when}"
                     + (" (просрочено)" if late else ""))
    if len(open_tasks) > MAX_TASKS:
        lines.append(f"…и ещё {len(open_tasks) - MAX_TASKS}")
    return "\n".join(lines)


def build_reply(user_text="", is_owner=False):
    # на «что нового» отвечаем только последней новостью
    if "что нового" in user_text.lower():
        try:
            return get_latest_news_text()
        except Exception as e:  # noqa: BLE001
            print(f"Ошибка новостей: {e}", flush=True)
            return "Не удалось получить новости, попробуйте позже."

    parts = [get_time_text()]
    # на «привет» вместо погоды отвечаем фразой из privet.txt
    first = (get_greeting_text, "Привет!") if "привет" in user_text.lower() \
        else (get_weather_text, "Не удалось получить погоду.")
    for fn, err in (first, (get_bitcoin_text, "Не удалось получить курс биткоина.")):
        try:
            parts.append(fn())
        except Exception as e:  # noqa: BLE001
            print(f"Ошибка {fn.__name__}: {e}", flush=True)
            parts.append(err)
    # личные задачи видит только владелец бота
    if is_owner:
        parts.append(get_tasks_text())
    return "\n\n".join(parts)


def main():
    env = load_env(os.path.join(BASE_DIR, ".env"))
    token = env["BOT_TOKEN"]
    owner_id = env.get("OWNER_ID", "")  # chat id владельца; без него задачи никому не показываются
    api = f"https://api.telegram.org/bot{token}"
    start_planner_sync()
    if not owner_id:
        print("OWNER_ID не задан: задачи из планера никому не показываются. "
              "Напишите боту /id и добавьте OWNER_ID=... в .env", flush=True)

    me = http_json(f"{api}/getMe")["result"]
    print(f"Бот запущен: @{me['username']}", flush=True)

    # long polling не работает при установленном вебхуке;
    # drop_pending_updates сбрасывает сообщения, пришедшие, пока бот был офлайн
    http_json(f"{api}/deleteWebhook", {"drop_pending_updates": True})

    # на случай, если очередь не сбросилась: offset=-1 возвращает только последнее
    # обновление, следующий запрос с offset=last+1 подтверждает и удаляет все старые
    offset = None
    last = http_json(f"{api}/getUpdates", {"offset": -1, "timeout": 0})["result"]
    if last:
        offset = last[-1]["update_id"] + 1
    print("Старые сообщения пропущены", flush=True)

    while True:
        try:
            payload = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                payload["offset"] = offset
            updates = http_json(f"{api}/getUpdates", payload, timeout=45)["result"]
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"Ошибка getUpdates: {e}", flush=True)
            time.sleep(5)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message")
            if not msg:
                continue
            chat_id = msg["chat"]["id"]
            user_text = msg.get("text") or ""
            sender = msg.get("from", {})
            print(f"Сообщение: chat_id={chat_id}, тип={msg['chat']['type']}, "
                  f"имя={sender.get('first_name', '')}, @{sender.get('username', '-')}", flush=True)
            if user_text.strip().lower().split("@")[0] == "/id":
                # подсказка, чтобы узнать свой id и записать его в .env как OWNER_ID
                text = f"Ваш chat id: {chat_id}"
            else:
                is_owner = bool(owner_id) and msg["chat"]["type"] == "private" \
                    and str(chat_id) == owner_id
                text = build_reply(user_text, is_owner)
            try:
                http_json(f"{api}/sendMessage", {"chat_id": chat_id, "text": text})
            except Exception as e:  # noqa: BLE001
                print(f"Ошибка sendMessage: {e}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
