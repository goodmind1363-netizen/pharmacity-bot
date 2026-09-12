import os
import time
import json
import hashlib
import threading
import xml.etree.ElementTree as ET
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
# ساعت:دقیقه‌های چک WHO در روز (به وقت UTC)، جدا شده با کاما — پیش‌فرض ۳ بار در روز
WHO_CHECK_TIMES = [
    t.strip() for t in os.environ.get("WHO_CHECK_TIMES", "04:30,07:00,11:30,15:30").split(",") if t.strip()
]
# منابع خبری انگلیسی معتبر پزشکی/دارویی (به‌جای WHO)
ENGLISH_SOURCES = {
    "STAT": os.environ.get("STAT_RSS_URL", "https://www.statnews.com/category/pharma/feed/"),
    "FiercePharma": os.environ.get("FIERCEPHARMA_RSS_URL", "https://www.fiercepharma.com/rss/xml"),
    "Endpoints": os.environ.get("ENDPOINTS_RSS_URL", "https://endpts.com/feed/"),
}

# اطلاعات JSONBin.io برای ذخیره‌سازی دائمی «خبرهای دیده‌شده»
# (چون دیسک Render رایگان با هر ری‌استارت پاک می‌شود)
JSONBIN_API_KEY = os.environ.get("JSONBIN_API_KEY", "")
JSONBIN_BIN_ID = os.environ.get("JSONBIN_BIN_ID", "")

# اگر سرور داخل ایران است و به فیلترشکن نیاز دارد، آدرس پراکسی محلی را
# اینجا تنظیم کنید (مثلا یک سرویس V2Ray/Xray که روی خود سرور اجرا می‌شود)
PROXY_URL = os.environ.get("PROXY_URL", "")  # مثال: socks5h://127.0.0.1:1080
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# ---------- کمکی: خواندن/نوشتن پیام‌های قبلاً دیده‌شده (جلوگیری از تکرار) ----------
# این‌ها را روی JSONBin.io (سرویس رایگان ذخیره‌سازی JSON) نگه می‌داریم تا با
# ری‌استارت یا دیپلوی جدید Render پاک نشوند.
def load_seen():
    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID:
        print("[WARN] JSONBIN تنظیم نشده — حافظه ضدتکرار موقتی و ناپایدار خواهد بود.")
        return set()
    try:
        resp = requests.get(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}/latest",
            headers={"X-Master-Key": JSONBIN_API_KEY},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return set(data.get("record", {}).get("seen", []))
    except Exception as e:
        print(f"[WARN] خطا در خواندن حافظه ضدتکرار از JSONBin: {e}")
        return set()


def save_seen(seen):
    if not JSONBIN_API_KEY or not JSONBIN_BIN_ID:
        return
    try:
        # فقط 500 مورد آخر را نگه می‌داریم تا حجم داده بیش از حد بزرگ نشود
        trimmed = list(seen)[-500:]
        resp = requests.put(
            f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}",
            headers={
                "X-Master-Key": JSONBIN_API_KEY,
                "Content-Type": "application/json",
            },
            json={"seen": trimmed},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] خطا در ذخیره حافظه ضدتکرار در JSONBin: {e}")




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


# ---------- گرفتن آخرین مطالب از منابع خبری انگلیسی معتبر (از طریق RSS) ----------
def fetch_rss_latest(source_name, feed_url, limit=1):
    try:
        resp = requests.get(feed_url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0"
        }, proxies=PROXIES)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] خطا در دریافت RSS از {source_name}: {e}")
        return []

    articles = []
    try:
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")[:limit]
        for item in items:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.text.strip() if link_el is not None and link_el.text else ""
            raw_desc = desc_el.text if desc_el is not None and desc_el.text else ""
            # حذف تگ‌های HTML ساده از خلاصه RSS
            desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ").strip()

            if not title or not link:
                continue

            # پیدا کردن عکس (media:content یا enclosure رایج‌ترین‌ها هستند)
            photo_url = None
            media_ns = "{http://search.yahoo.com/mrss/}"
            media_content = item.find(f"{media_ns}content")
            if media_content is not None and media_content.get("url"):
                photo_url = media_content.get("url")
            if not photo_url:
                enclosure = item.find("enclosure")
                if enclosure is not None and enclosure.get("url"):
                    photo_url = enclosure.get("url")
            if not photo_url:
                img_match = BeautifulSoup(raw_desc, "html.parser").find("img")
                if img_match and img_match.get("src"):
                    photo_url = img_match["src"]

            # اگر هنوز عکسی پیدا نشد، مستقیم به صفحه خبر برو و og:image را بردار
            if not photo_url:
                try:
                    page_resp = requests.get(
                        link, timeout=15, headers={"User-Agent": "Mozilla/5.0"}, proxies=PROXIES
                    )
                    page_resp.raise_for_status()
                    page_soup = BeautifulSoup(page_resp.text, "html.parser")
                    og_image = page_soup.select_one("meta[property='og:image']")
                    if og_image and og_image.get("content"):
                        photo_url = og_image["content"]
                except Exception as e:
                    print(f"[WARN] خطا در گرفتن عکس از صفحه خبر ({source_name}): {e}")

            articles.append({
                "url": link,
                "title": title,
                "text": f"{title}\n\n{desc}",
                "photo_url": photo_url,
                "source": source_name,
            })
    except Exception as e:
        print(f"[WARN] خطا در تجزیه RSS از {source_name}: {e}")

    return articles


def fetch_english_sources_latest(limit_per_source=1):
    all_articles = []
    for name, feed_url in ENGLISH_SOURCES.items():
        all_articles.extend(fetch_rss_latest(name, feed_url, limit=limit_per_source))
    return all_articles


# ---------- بازنویسی خبر با هوش مصنوعی (از طریق API رسمی Google Gemini) ----------
def rewrite_news(raw_text, is_scientific=False):
    style_note = (
        "این یک خبر تخصصی داروسازی/بیوتکنولوژی از یک منبع معتبر بین‌المللی انگلیسی‌زبان "
        "(مثل STAT، Fierce Pharma یا Endpoints News) است. "
        "آن را کامل و دقیق به فارسی روان ترجمه کن (نه فقط خلاصه‌برداری سطحی)، طوری که خواننده "
        "فارسی‌زبان بدون نیاز به منبع اصلی، محتوای خبر را کامل و درست متوجه شود."
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
    caption_or_text = text if len(text) <= 1024 else text[:1021] + "..."

    if photo_url:
        # عکس را خودمان دانلود می‌کنیم و مستقیم آپلود می‌کنیم، چون بعضی
        # سایت‌ها اجازه دانلود مستقیم توسط سرور تلگرام را نمی‌دهند (Hotlink Protection)
        try:
            img_resp = requests.get(
                photo_url, timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
                proxies=PROXIES,
            )
            img_resp.raise_for_status()
            image_bytes = img_resp.content
        except Exception as e:
            print(f"[WARN] خطا در دانلود عکس ({photo_url}): {e}")
            return send_to_channel(text, photo_url=None)

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        data = {
            "chat_id": CHANNEL_USERNAME,
            "caption": caption_or_text,
            "parse_mode": "HTML",
        }
        files = {"photo": ("image.jpg", image_bytes)}
        try:
            resp = requests.post(url, data=data, files=files, timeout=30, proxies=PROXIES)
            if not resp.ok:
                print(f"[WARN] تلگرام خطا داد: HTTP {resp.status_code} - {resp.text[:500]}")
                print("[INFO] تلاش دوباره بدون عکس...")
                return send_to_channel(text, photo_url=None)
            result = resp.json()
            return result.get("ok", False)
        except Exception as e:
            print(f"[WARN] خطا در ارسال عکس به کانال: {e}")
            print("[INFO] تلاش دوباره بدون عکس...")
            return send_to_channel(text, photo_url=None)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_USERNAME,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, data=payload, timeout=15, proxies=PROXIES)
        if not resp.ok:
            print(f"[WARN] تلگرام خطا داد: HTTP {resp.status_code} - {resp.text[:500]}")
            return False
        result = resp.json()
        return result.get("ok", False)
    except Exception as e:
        print(f"[WARN] خطا در ارسال به کانال: {e}")
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
    last_who_check_key = None
    print(f"شروع به کار ربات. کانال‌های منبع: {SOURCE_CHANNELS}")

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

        # ۲) چک WHO (چند بار در روز، در ساعت:دقیقه‌های مشخص‌شده، با کمی انعطاف)
        now = time.gmtime()
        today_str = time.strftime("%Y-%m-%d", now)
        now_minutes = now.tm_hour * 60 + now.tm_min
        tolerance_minutes = max(CHECK_INTERVAL_SECONDS // 60, 15)

        matched_time = None
        for t in WHO_CHECK_TIMES:
            th, tm = map(int, t.split(":"))
            target_minutes = th * 60 + tm
            if abs(now_minutes - target_minutes) <= tolerance_minutes:
                matched_time = t
                break

        check_key = f"{today_str}-{matched_time}"
        if matched_time and last_who_check_key != check_key:
            english_articles = fetch_english_sources_latest(limit_per_source=1)
            for article in english_articles:
                h = post_hash(article["source"], article["text"])
                if h in seen:
                    continue
                print(f"[INFO] مطلب جدید از {article['source']} پیدا شد، در حال ترجمه و بازنویسی...")
                rewritten = rewrite_news(article["text"], is_scientific=True)
                if not rewritten:
                    continue
                final_text = (
                    f"{rewritten}\n\n"
                    f"━━━━━━━━━━\n"
                    f"🌍 منبع: {article['url']}\n"
                    f"🔗 {CHANNEL_USERNAME if CHANNEL_USERNAME.startswith('@') else '@' + CHANNEL_USERNAME}"
                )
                ok = send_to_channel(final_text, photo_url=article.get("photo_url"))
                if ok:
                    print(f"[INFO] مطلب {article['source']} با موفقیت پست شد.")
                    new_seen.add(h)
                time.sleep(3)
            last_who_check_key = check_key

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
