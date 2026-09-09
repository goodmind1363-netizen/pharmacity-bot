import os
import time
import json
import hashlib
import threading
import requests
from bs4 import BeautifulSoup
from flask import Flask

# ---------- تنظیمات (از Environment Variables خوانده می‌شود) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME")  # مثلا: @Pharma_City_News
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

SOURCE_CHANNELS = [
    c.strip() for c in os.environ.get(
        "SOURCE_CHANNELS", "Fansalaran,phanair"
    ).split(",") if c.strip()
]

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "900"))  # 15 دقیقه
WHO_CHECK_HOUR = int(os.environ.get("WHO_CHECK_HOUR", "9"))  # ساعت چک روزانه WHO (به وقت سرور، معمولا UTC)
SEEN_FILE = "seen_posts.json"
WHO_NEWS_URL = "https://www.who.int/news"

# اگر سرور داخل ایران است و به فیلترشکن نیاز دارد، آدرس پراکسی محلی را
# اینجا تنظیم کنید (مثلا یک سرویس V2Ray/Xray که روی خود سرور اجرا می‌شود)
PROXY_URL = os.environ.get("PROXY_URL", "")  # مثال: socks5h://127.0.0.1:1080
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# ---------- کمکی: خواندن/نوشتن پیام‌های قبلاً دیده‌شده (جلوگیری از تکرار) ----------
def load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)


def post_hash(channel, text):
    raw = f"{channel}:{text[:200]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- گرفتن آخرین پیام‌های یک کانال از نسخه وب عمومی تلگرام ----------
def fetch_channel_posts(channel_username, limit=5):
    url = f"https://t.me/s/{channel_username}"
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0"
        }, proxies=PROXIES)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] خطا در دریافت کانال {channel_username}: {e}")
        return []

# ---------- گرفتن آخرین پیام‌های یک کانال از نسخه وب عمومی تلگرام (متن + عکس) ----------
def fetch_channel_posts(channel_username, limit=5):
    url = f"https://t.me/s/{channel_username}"
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0"
        }, proxies=PROXIES)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] خطا در دریافت کانال {channel_username}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    message_blocks = soup.select("div.tgme_widget_message")
    posts = []
    for block in message_blocks[-limit:]:
        text_div = block.select_one("div.tgme_widget_message_text")
        text = text_div.get_text(separator="\n").strip() if text_div else ""
        if not text:
            continue  # پیام‌های بدون متن (فقط عکس بدون توضیح) را رد می‌کنیم

        photo_url = None
        photo_div = block.select_one("a.tgme_widget_message_photo_wrap")
        if photo_div and photo_div.get("style"):
            style = photo_div["style"]
            match_start = style.find("url('")
            if match_start != -1:
                match_start += len("url('")
                match_end = style.find("')", match_start)
                if match_end != -1:
                    photo_url = style[match_start:match_end]

        posts.append({"text": text, "photo_url": photo_url})
    return posts


# ---------- گرفتن آخرین مطالب علمی از سایت WHO (۱ تا ۲ مطلب) ----------
def fetch_who_latest(limit=2):
    try:
        resp = requests.get(WHO_NEWS_URL, timeout=15, headers={
            "User-Agent": "Mozilla/5.0"
        })
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] خطا در دریافت WHO: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    # لینک‌های خبر در صفحه who.int/news معمولا داخل تگ‌های <a> با href شامل /news/item هستند
    link_tags = soup.select("a[href*='/news/item']")[:limit]
    articles = []

    for link_tag in link_tags:
        href = link_tag.get("href", "")
        title = link_tag.get_text(strip=True)
        if not href or not title:
            continue
        full_url = href if href.startswith("http") else f"https://www.who.int{href}"

        photo_url = None
        try:
            article_resp = requests.get(full_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            article_resp.raise_for_status()
            article_soup = BeautifulSoup(article_resp.text, "html.parser")
            paragraphs = article_soup.select("p")
            body = "\n".join(p.get_text(strip=True) for p in paragraphs[:6] if p.get_text(strip=True))

            og_image = article_soup.select_one("meta[property='og:image']")
            if og_image and og_image.get("content"):
                photo_url = og_image["content"]
        except Exception as e:
            print(f"[WARN] خطا در دریافت متن کامل خبر WHO: {e}")
            body = ""

        articles.append({
            "url": full_url,
            "title": title,
            "text": f"{title}\n\n{body}",
            "photo_url": photo_url,
        })

    return articles


# ---------- بازنویسی خبر با هوش مصنوعی (از طریق API رسمی Google Gemini) ----------
def rewrite_news(raw_text, is_scientific=False):
    style_note = (
        "این یک مطلب علمی/پزشکی از سازمان جهانی بهداشت (WHO) است."
        if is_scientific else
        "این یک خبر داروسازی/سلامت است."
    )
    prompt = (
        f"{style_note}\n"
        "متن زیر را به فارسی روان، با لحن خبری حرفه‌ای بازنویسی کن. خروجی باید این فرمت را داشته باشد:\n"
        "1) یک تیتر کوتاه و جذاب در خط اول، با یک ایموجی مناسب موضوع در ابتدای تیتر\n"
        "2) یک خط خالی\n"
        "3) بدنه خبر در ۲ تا ۴ پاراگراف کوتاه، بدون تکرار، خلاصه اما کامل\n"
        "4) یک خط خالی\n"
        "5) یک جمع‌بندی/نتیجه‌گیری کوتاه با پیشوند «🔎 نتیجه‌گیری:»\n"
        "نکته مهم: هیچ نام کانال، یوزرنیم (مثل @something)، لینک تلگرام، یا عبارت "
        "«Forwarded from» یا مشابه آن را از متن اصلی در خروجی نیاور — فقط خودِ محتوای خبر را بازنویسی کن.\n"
        "فقط خروجی نهایی را بنویس، بدون هیچ مقدمه یا توضیح اضافه:\n\n" + raw_text
    )
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
            proxies=PROXIES,
        )
        if not resp.ok:
            print(f"[WARN] خطا در بازنویسی با AI: HTTP {resp.status_code} - {resp.text[:500]}")
            return None
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"[WARN] خطا در بازنویسی با AI: {e}")
        return None


# ---------- ارسال پیام به کانال خودمان (با عکس اختیاری) ----------
def send_to_channel(text, photo_url=None):
    if photo_url:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        # کپشن عکس در تلگرام حداکثر 1024 کاراکتر است
        caption = text if len(text) <= 1024 else text[:1021] + "..."
        payload = {
            "chat_id": CHANNEL_USERNAME,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "HTML",
        }
    else:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHANNEL_USERNAME,
            "text": text,
            "parse_mode": "HTML",
        }

    try:
        resp = requests.post(url, data=payload, timeout=15, proxies=PROXIES)
        resp.raise_for_status()
        result = resp.json()
        if not result.get("ok"):
            print(f"[WARN] تلگرام خطا داد: {result}")
        return result.get("ok", False)
    except Exception as e:
        print(f"[WARN] خطا در ارسال به کانال: {e}")
        # اگر ارسال عکس شکست خورد (مثلا لینک عکس نامعتبر)، به‌صورت متن ساده امتحان کن
        if photo_url:
            print("[INFO] تلاش دوباره بدون عکس...")
            return send_to_channel(text, photo_url=None)
        return False


# ---------- بررسی مدل‌های مجاز برای این کلید (فقط برای عیب‌یابی) ----------
def print_available_models():
    try:
        resp = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        names = [m.get("name", "") for m in data.get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        print(f"[INFO] مدل‌های مجاز برای این کلید: {names}")
    except Exception as e:
        print(f"[WARN] خطا در گرفتن لیست مدل‌ها: {e}")


# ---------- حلقه اصلی ----------
def main_loop():
    seen = load_seen()
    last_who_check_date = None
    print(f"شروع به کار ربات. کانال‌های منبع: {SOURCE_CHANNELS}")
    print_available_models()

    while True:
        new_seen = set(seen)

        # ۱) چک کانال‌های تلگرام (هر بار)
        for channel in SOURCE_CHANNELS:
            posts = fetch_channel_posts(channel)
            for post in posts:
                text = post["text"]
                photo_url = post["photo_url"]
                h = post_hash(channel, text)
                if h in seen:
                    continue

                print(f"[INFO] خبر جدید از {channel} پیدا شد، در حال بازنویسی...")
                rewritten = rewrite_news(text, is_scientific=False)
                if not rewritten:
                    continue

                final_text = (
                    f"{rewritten}\n\n"
                    f"━━━━━━━━━━\n"
                    f"🔗 {CHANNEL_USERNAME if CHANNEL_USERNAME.startswith('@') else '@' + CHANNEL_USERNAME}"
                )
                ok = send_to_channel(final_text, photo_url=photo_url)
                if ok:
                    print(f"[INFO] خبر با موفقیت در کانال پست شد.")
                    new_seen.add(h)
                time.sleep(3)

        # ۲) چک روزانه WHO (فقط یک بار در روز، در ساعت مشخص‌شده)
        now = time.gmtime()
        today_str = time.strftime("%Y-%m-%d", now)
        if now.tm_hour == WHO_CHECK_HOUR and last_who_check_date != today_str:
            who_articles = fetch_who_latest(limit=2)
            for who_article in who_articles:
                h = post_hash("who.int", who_article["text"])
                if h in seen:
                    continue
                print("[INFO] مطلب جدید WHO پیدا شد، در حال بازنویسی...")
                rewritten = rewrite_news(who_article["text"], is_scientific=True)
                if not rewritten:
                    continue
                final_text = (
                    f"{rewritten}\n\n"
                    f"━━━━━━━━━━\n"
                    f"🌍 منبع: WHO\n"
                    f"🔗 {CHANNEL_USERNAME if CHANNEL_USERNAME.startswith('@') else '@' + CHANNEL_USERNAME}"
                )
                ok = send_to_channel(final_text, photo_url=who_article.get("photo_url"))
                if ok:
                    print("[INFO] مطلب WHO با موفقیت پست شد.")
                    new_seen.add(h)
                time.sleep(3)
            last_who_check_date = today_str

        seen = new_seen
        save_seen(seen)
        print(f"[INFO] چک بعدی در {CHECK_INTERVAL_SECONDS} ثانیه دیگر...")
        time.sleep(CHECK_INTERVAL_SECONDS)


# ---------- یک وب‌سرور خیلی ساده، فقط برای اینکه Render این را «سرویس وب» بشناسد ----------
app = Flask(__name__)


@app.route("/")
def health_check():
    return "PharmaCity bot is running."


if __name__ == "__main__":
    missing = [
        name for name, val in [
            ("BOT_TOKEN", BOT_TOKEN),
            ("CHANNEL_USERNAME", CHANNEL_USERNAME),
            ("GEMINI_API_KEY", GEMINI_API_KEY),
        ] if not val
    ]
    if missing:
        print(f"[ERROR] این متغیرها تنظیم نشده‌اند: {missing}")
    else:
        # حلقه اصلی ربات را در یک ترد جدا اجرا می‌کنیم تا وب‌سرور هم‌زمان کار کند
        bot_thread = threading.Thread(target=main_loop, daemon=True)
        bot_thread.start()

    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
