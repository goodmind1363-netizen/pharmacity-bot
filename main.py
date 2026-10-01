import os
import time
import json
import hashlib
import re
import threading
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import requests
from bs4 import BeautifulSoup
from flask import Flask
from PIL import Image, ImageDraw
from io import BytesIO

# ---------- تنظیمات (از Environment Variables خوانده می‌شود) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME")  # مثلا: @Pharma_City_News
CHANNEL_DISPLAY_NAME = os.environ.get("CHANNEL_DISPLAY_NAME", "شهر دارو")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SOURCE_CHANNELS = [
    c.strip() for c in os.environ.get(
        "SOURCE_CHANNELS", "Universe_HM,phanair"
    ).split(",") if c.strip()
]
# اگر مقدار قدیمی Fansalaran هنوز در Environment Variables رندر مانده باشد، خودکار جایگزین می‌شود.
SOURCE_CHANNELS = [
    "Universe_HM" if c.lstrip("@").lower() == "fansalaran" else c
    for c in SOURCE_CHANNELS
]

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "900"))  # 15 دقیقه

# ساعت:دقیقه‌های چک منابع انگلیسی در روز (به وقت UTC)
WHO_CHECK_TIMES = [
    t.strip() for t in os.environ.get("WHO_CHECK_TIMES", "05:00,07:20,11:10,14:20,17:00").split(",") if t.strip()
]

# زمان بررسی منابع فیزیوتراپی/توانبخشی (UTC)
# دو نوبت در روز تا کانال بیش از حد شلوغ نشود.
PHYSIO_CHECK_TIMES = [
    t.strip() for t in os.environ.get("PHYSIO_CHECK_TIMES", "09:30,16:30").split(",") if t.strip()
]

# RSSهای معتبر مجلات تخصصی فیزیوتراپی و توانبخشی
PHYSIO_RSS_SOURCES = {
    "Physiotherapy Journal": "https://rss.sciencedirect.com/publication/science/00319406",
    "Archives of Physical Medicine and Rehabilitation": "https://rss.sciencedirect.com/publication/science/00039993",
    "PM&R": "https://onlinelibrary.wiley.com/feed/19341563/most-recent",
}

# منابع خبری انگلیسی معتبر پزشکی/دارویی
ENGLISH_SOURCES = {
    "STAT": os.environ.get("STAT_RSS_URL", "https://www.statnews.com/category/pharma/feed/"),
    "Endpoints": os.environ.get("ENDPOINTS_RSS_URL", "https://endpts.com/feed/"),
    "NatureMedicine": os.environ.get("NATURE_MEDICINE_RSS_URL", "https://www.nature.com/nm.rss"),
    "NatureBiotechnology": os.environ.get("NATURE_BIOTECHNOLOGY_RSS_URL", "https://www.nature.com/nbt.rss"),
    "PharmaTimes": os.environ.get("PHARMATIMES_RSS_URL", "https://www.pharmatimes.com/rss/news_rss.rss"),
}

# اطلاعات JSONBin.io برای ذخیره‌سازی دائمی «خبرهای دیده‌شده»
JSONBIN_API_KEY = os.environ.get("JSONBIN_API_KEY", "")
JSONBIN_BIN_ID = os.environ.get("JSONBIN_BIN_ID", "")

# اگر سرور نیاز به فیلترشکن دارد، آدرس پراکسی محلی را اینجا تنظیم کنید
PROXY_URL = os.environ.get("PROXY_URL", "")
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# امضای پایانی که به همه پست‌ها اضافه می‌شود
SIGNATURE_LINE = "📌 شهر دارو، منبع اطلاع‌رسانی دنیای دارو"

SEEN_FILE = "seen_posts.json"

# سقف حجم عکس (بایت) برای جلوگیری از مصرف زیاد رم روی پلن رایگان
MAX_IMAGE_BYTES = 4 * 1024 * 1024  # 4 مگابایت
BRAND_LOGO_PATH = os.environ.get("BRAND_LOGO_PATH", "pharmacity_logo.jpg")
BRAND_IMAGE_WIDTH = 1200
BRAND_IMAGE_HEIGHT = 675

# حداکثر تعداد پست در هر چک هر کانال (برای جلوگیری از فشار ناگهانی حافظه بعد از یک وقفه طولانی)
MAX_POSTS_PER_CHECK = 3


# ---------- کمکی: خواندن/نوشتن پیام‌های قبلاً دیده‌شده (جلوگیری از تکرار) ----------
# این‌ها را روی JSONBin.io نگه می‌داریم تا با ری‌استارت یا دیپلوی جدید Render پاک نشوند.
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


def post_hash(source, text):
    raw = f"{source}:{text[:200]}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- گرفتن آخرین پیام‌های یک کانال از نسخه وب عمومی تلگرام (متن + عکس) ----------
def normalize_channel_username(value):
    value = (value or "").strip()
    value = re.sub(r"^https?://t\.me/(?:s/)?", "", value, flags=re.I)
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    return value.lstrip("@").strip()


def fetch_channel_posts(channel_username, limit=MAX_POSTS_PER_CHECK):
    channel_username = normalize_channel_username(channel_username)
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
            continue

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
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }, proxies=PROXIES)
        resp.raise_for_status()
        return parse_rss_xml(source_name, resp.content, limit)
    except Exception as e:
        print(f"[WARN] خطا در دریافت مستقیم RSS از {source_name}: {e} — تلاش با سرویس واسط...")
        return fetch_rss_via_proxy(source_name, feed_url, limit)


def fetch_rss_via_proxy(source_name, feed_url, limit=1):
    try:
        proxy_url = f"https://api.rss2json.com/v1/api.json?rss_url={feed_url}&count={limit}"
        resp = requests.get(proxy_url, timeout=20, proxies=PROXIES)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            print(f"[WARN] سرویس واسط هم نتوانست RSS {source_name} را بگیرد: {data}")
            return []

        articles = []
        for item in data.get("items", [])[:limit]:
            title = (item.get("title") or "").strip()
            link = (item.get("link") or "").strip()
            raw_desc = item.get("description") or ""
            desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ").strip()
            if not title or not link:
                continue
            photo_url = item.get("enclosure", {}).get("link") or item.get("thumbnail") or None
            articles.append({
                "url": link,
                "title": title,
                "text": f"{title}\n\n{desc}",
                "photo_url": photo_url,
                "source": source_name,
            })
        return articles
    except Exception as e:
        print(f"[WARN] خطا در سرویس واسط RSS برای {source_name}: {e}")
        return []


def parse_rss_xml(source_name, content, limit=1):
    articles = []
    try:
        root = ET.fromstring(content)
        items = root.findall(".//item")[:limit]
        for item in items:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.text.strip() if link_el is not None and link_el.text else ""
            raw_desc = desc_el.text if desc_el is not None and desc_el.text else ""
            desc = BeautifulSoup(raw_desc, "html.parser").get_text(separator=" ").strip()

            if not title or not link:
                continue

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
        articles = fetch_rss_latest(name, feed_url, limit=limit_per_source)
        if articles:
            titles = [a["title"][:50] for a in articles]
            print(f"[INFO] منبع {name} چک شد — {len(articles)} مطلب پیدا شد: {titles}")
        else:
            print(f"[INFO] منبع {name} چک شد — هیچ مطلبی برنگشت (احتمالا خطا یا فید خالی)")
        all_articles.extend(articles)
    return all_articles


# ---------- بازنویسی خبر با هوش مصنوعی (از طریق API رسمی Google Gemini) ----------
# ---------- هشتگ‌گذاری هوشمند ----------
HASHTAG_RULES = [
    (['فیزیوتراپی','physiotherapy','rehabilitation','توانبخشی'], ['#فیزیوتراپی','#توانبخشی','#Physiotherapy','#Rehabilitation']),
    (['stroke','سکته'], ['#سکته_مغزی','#توانبخشی','#Stroke']),
    (['pain','درد','low back','کمر','spine','ستون فقرات'], ['#درد','#کمردرد','#ستون_فقرات','#Pain']),
    (['knee','زانو','osteoarthritis','آرتروز'], ['#زانو','#آرتروز','#Osteoarthritis']),
    (['cancer','سرطان','oncology','انکولوژی'], ['#سرطان','#انکولوژی','#Oncology']),
    (['fda','approval','تاییدیه'], ['#FDA','#دارو','#مجوز_دارویی']),
    (['clinical trial','کارآزمایی','trial','مطالعه بالینی'], ['#کارآزمایی_بالینی','#ClinicalTrial','#پژوهش']),
    (['biotech','بیوتکنولوژی','gene therapy','ژن درمانی'], ['#بیوتکنولوژی','#ژن_درمانی','#Biotech']),
    (['drug','دارو','pharma','داروسازی'], ['#دارو','#داروسازی','#Pharma']),
    (['stroke','heart','cardiovascular','قلب','قلبی'], ['#قلب','#بیماری_قلبی','#Cardiovascular']),
    (['multiple sclerosis','ms','ام اس'], ['#MS','#ام_اس','#توانبخشی']),
    (['sports injury','آسیب ورزشی','sports medicine'], ['#آسیب_ورزشی','#پزشکی_ورزشی','#SportsMedicine']),
]

def generate_smart_hashtags(text, is_physio=False):
    low = (text or '').lower()
    tags = []
    if is_physio:
        tags.extend(['#فیزیوتراپی_جهان'])
    for keywords, rule_tags in HASHTAG_RULES:
        if any(k.lower() in low for k in keywords):
            for tag in rule_tags:
                if tag not in tags:
                    tags.append(tag)
    # تگ‌های پایه، محدود و غیرتکراری
    base = ['#سلامت', '#MedicalNews'] if is_physio else ['#دارو', '#سلامت']
    for tag in base:
        if tag not in tags:
            tags.append(tag)
    return ' '.join(tags[:8])


def rewrite_news(raw_text, is_scientific=False):
    style_note = (
        "این یک خبر تخصصی داروسازی/بیوتکنولوژی از یک منبع معتبر بین‌المللی انگلیسی‌زبان "
        "(مثل STAT، Nature Medicine، Nature Biotechnology، Endpoints یا PharmaTimes) است. "
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
        "نیازی به جمع‌بندی یا نتیجه‌گیری در پایان نیست — فقط تیتر و بدنه خبر کافی است.\n"
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


# ---------- ساخت تصویر برندشده برای همه خبرها ----------
def make_branded_image(image_bytes=None):
    """یک قاب ثابت و قابل تشخیص برای همه خبرهای کانال می‌سازد."""
    try:
        if image_bytes:
            src = Image.open(BytesIO(image_bytes)).convert("RGB")
            # قاب 16:9؛ تصویر خبر داخل قاب قرار می‌گیرد.
            src.thumbnail((1140, 615), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (BRAND_IMAGE_WIDTH, BRAND_IMAGE_HEIGHT), "white")
            x = (BRAND_IMAGE_WIDTH - src.width) // 2
            y = 30 + (615 - src.height) // 2
            canvas.paste(src, (x, y))
        else:
            canvas = Image.new("RGB", (BRAND_IMAGE_WIDTH, BRAND_IMAGE_HEIGHT), (245, 250, 250))

        draw = ImageDraw.Draw(canvas)
        # قاب اصلی سرمه‌ای + نوار سبز برند
        draw.rectangle((0, 0, BRAND_IMAGE_WIDTH-1, BRAND_IMAGE_HEIGHT-1), outline=(8, 55, 110), width=18)
        draw.rectangle((18, 18, BRAND_IMAGE_WIDTH-19, BRAND_IMAGE_HEIGHT-19), outline=(39, 180, 125), width=7)

        # نوار پایین با نام انگلیسی برند
        draw.rectangle((18, BRAND_IMAGE_HEIGHT-58, BRAND_IMAGE_WIDTH-19, BRAND_IMAGE_HEIGHT-19), fill=(8, 55, 110))
        try:
            draw.text((BRAND_IMAGE_WIDTH//2, BRAND_IMAGE_HEIGHT-39), "PHARMA CITY NEWS", fill="white", anchor="mm")
        except Exception:
            pass

        # لوگوی کانال در گوشه بالا-چپ
        logo_path = Path(BRAND_LOGO_PATH)
        if logo_path.exists():
            logo = Image.open(logo_path).convert("RGB")
            logo.thumbnail((145, 145), Image.Resampling.LANCZOS)
            badge = Image.new("RGB", (165, 165), "white")
            lx = (165 - logo.width)//2
            ly = (165 - logo.height)//2
            badge.paste(logo, (lx, ly))
            # قاب ظریف دور لوگو
            badge_draw = ImageDraw.Draw(badge)
            badge_draw.rectangle((1,1,163,163), outline=(39,180,125), width=4)
            canvas.paste(badge, (38, 38))

        out = BytesIO()
        canvas.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()
    except Exception as e:
        print(f"[WARN] خطا در ساخت تصویر برندشده: {e}")
        return image_bytes


# ---------- ارسال پیام به کانال خودمان (با عکس اختیاری) ----------
def send_to_channel(text, photo_url=None):
    if photo_url:
        try:
            img_resp = requests.get(
                photo_url, timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
                proxies=PROXIES,
                stream=True,
            )
            img_resp.raise_for_status()
            chunks = []
            total = 0
            for chunk in img_resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ValueError(f"عکس بیش از حد مجاز بزرگ است ({total} بایت)")
                chunks.append(chunk)
            image_bytes = b"".join(chunks)
            img_resp.close()
            image_bytes = make_branded_image(image_bytes)
        except Exception as e:
            print(f"[WARN] خطا در دانلود عکس ({photo_url}): {e}")
            return send_to_channel(text, photo_url=None)

        send_caption_with_photo = len(text) <= 1024
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        data = {
            "chat_id": CHANNEL_USERNAME,
            "parse_mode": "HTML",
        }
        if send_caption_with_photo:
            data["caption"] = text
        files = {"photo": ("image.jpg", image_bytes)}
        try:
            resp = requests.post(url, data=data, files=files, timeout=30, proxies=PROXIES)
            if not resp.ok:
                print(f"[WARN] تلگرام خطا داد: HTTP {resp.status_code} - {resp.text[:500]}")
                print("[INFO] تلاش دوباره بدون عکس...")
                return send_to_channel(text, photo_url=None)
            result = resp.json()
            print(f"[TELEGRAM] sendPhoto HTTP={resp.status_code} ok={result.get('ok')} message_id={result.get('result', {}).get('message_id') if isinstance(result.get('result'), dict) else None} chat_id={result.get('result', {}).get('chat', {}).get('id') if isinstance(result.get('result'), dict) else None} caption_with_photo={send_caption_with_photo}")
            if not result.get("ok"):
                print(f"[TELEGRAM] sendPhoto ERROR={result.get('description')}")
                return False
            if not send_caption_with_photo:
                print("[TELEGRAM] caption was over 1024 chars; sending text as a separate message.")
                return send_to_channel(text, photo_url=None)
            return True
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
        print(f"[TELEGRAM] sendMessage HTTP={resp.status_code} ok={result.get('ok')} message_id={result.get('result', {}).get('message_id') if isinstance(result.get('result'), dict) else None} chat_id={result.get('result', {}).get('chat', {}).get('id') if isinstance(result.get('result'), dict) else None}")
        if not result.get("ok"):
            print(f"[TELEGRAM] sendMessage ERROR={result.get('description')}")
        return result.get("ok", False)
    except Exception as e:
        print(f"[WARN] خطا در ارسال به کانال: {e}")
        return False


def fetch_physiotherapy_sources_latest(limit_per_source=1):
    all_articles = []
    for name, feed_url in PHYSIO_RSS_SOURCES.items():
        articles = fetch_rss_latest(name, feed_url, limit=limit_per_source)
        if articles:
            titles = [a["title"][:60] for a in articles]
            print(f"[PHYSIO] منبع {name} چک شد — {len(articles)} مطلب: {titles}")
        else:
            print(f"[PHYSIO] منبع {name} چک شد — مطلبی دریافت نشد")
        all_articles.extend(articles)
    return all_articles


def publish_physiotherapy_articles(seen):
    new_seen = set(seen)
    articles = fetch_physiotherapy_sources_latest(limit_per_source=1)
    for article in articles:
        h = post_hash("physio:" + article["source"], article["text"])
        if h in seen:
            continue

        print(f"[PHYSIO] مطلب جدید از {article['source']}، در حال بازنویسی...")
        rewritten = rewrite_news(article["text"], is_scientific=True)
        if not rewritten:
            continue

        final_text = (
            f"🦴 <b>#فیزیوتراپی_جهان</b>\n\n"
            f"{rewritten}\n\n"
            f"━━━━━━━━━━\n"
            f"🔬 منبع علمی: {article['source']}\n"
            f"🔗 {article['url']}\n"
            f"{generate_smart_hashtags(article['text'], is_physio=True)}\n"
            f"{SIGNATURE_LINE}\n"
            f"🔗 {CHANNEL_USERNAME if CHANNEL_USERNAME.startswith('@') else '@' + CHANNEL_USERNAME}"
        )
        ok = send_to_channel(final_text, photo_url=article.get("photo_url"))
        if ok:
            print(f"[PHYSIO] مطلب {article['source']} با موفقیت پست شد.")
            new_seen.add(h)
        time.sleep(3)
    return new_seen


# ---------- حلقه اصلی ----------
def main_loop():
    global worker_last_cycle, worker_last_error

    seen = load_seen()
    last_who_check_key = None
    last_physio_check_key = None
    print(f"شروع به کار ربات. کانال‌های منبع: {SOURCE_CHANNELS}")
    print(f"[TELEGRAM] destination CHANNEL_USERNAME={CHANNEL_USERNAME}")
    print(f"[INFO] زمان‌بندی فیزیوتراپی (UTC): {PHYSIO_CHECK_TIMES}")

    while True:
        try:
            seen, last_who_check_key, last_physio_check_key = run_one_cycle(
                seen, last_who_check_key, last_physio_check_key
            )
            worker_last_cycle = datetime.now(timezone.utc).isoformat()
            worker_last_error = None
        except Exception as e:
            import traceback
            worker_last_error = str(e)
            worker_last_cycle = datetime.now(timezone.utc).isoformat()
            print(f"[ERROR] خطای پیش‌بینی‌نشده در چرخه اصلی: {e}")
            traceback.print_exc()

        print(f"[INFO] چک بعدی در {CHECK_INTERVAL_SECONDS} ثانیه دیگر...")
        time.sleep(CHECK_INTERVAL_SECONDS)


def run_one_cycle(seen, last_who_check_key, last_physio_check_key=None):
        new_seen = set(seen)

        # ۱) چک کانال‌های تلگرام فارسی
        for channel in SOURCE_CHANNELS:
            try:
                print(f"[INFO] در حال بررسی کانال تلگرام: {channel}")
                posts = fetch_channel_posts(channel)
                print(f"[INFO] {channel}: {len(posts)} پست دریافت شد.")
            except Exception as e:
                print(f"[ERROR] خطا در پردازش منبع {channel}: {e}")
                continue

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
                    f"{generate_smart_hashtags(text)}\n"
                    f"━━━━━━━━━━\n"
                    f"{SIGNATURE_LINE}\n"
                    f"🔗 {CHANNEL_USERNAME if CHANNEL_USERNAME.startswith('@') else '@' + CHANNEL_USERNAME}"
                )
                ok = send_to_channel(final_text, photo_url=photo_url)
                if ok:
                    print("[INFO] خبر با موفقیت در کانال پست شد.")
                    new_seen.add(h)
                time.sleep(3)

        # زمان فعلی UTC را یک بار محاسبه می‌کنیم تا همه زمان‌بندی‌ها از یک مبنا استفاده کنند.
        now = time.gmtime()
        today_str = time.strftime("%Y-%m-%d", now)
        now_minutes = now.tm_hour * 60 + now.tm_min
        tolerance_minutes = max(CHECK_INTERVAL_SECONDS // 60, 15)

        # ۲) چک منابع فیزیوتراپی/توانبخشی
        physio_matched_time = None
        for t in PHYSIO_CHECK_TIMES:
            th, tm = map(int, t.split(":"))
            target_minutes = th * 60 + tm
            if abs(now_minutes - target_minutes) <= tolerance_minutes:
                physio_matched_time = t
                break

        physio_check_key = f"{today_str}-{physio_matched_time}"
        if physio_matched_time and last_physio_check_key != physio_check_key:
            new_seen = publish_physiotherapy_articles(new_seen)
            last_physio_check_key = physio_check_key

        # ۳) چک منابع انگلیسی (چند بار در روز، در ساعت‌های مشخص‌شده)

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
                    f"📰 به گزارش {CHANNEL_DISPLAY_NAME} و به نقل از {article['source']}:\n\n"
                    f"{rewritten}\n\n"
                    f"{generate_smart_hashtags(article['text'])}\n"
                    f"━━━━━━━━━━\n"
                    f"🌍 منبع: {article['url']}\n"
                    f"{SIGNATURE_LINE}\n"
                    f"🔗 {CHANNEL_USERNAME if CHANNEL_USERNAME.startswith('@') else '@' + CHANNEL_USERNAME}"
                )
                ok = send_to_channel(final_text, photo_url=article.get("photo_url"))
                if ok:
                    print(f"[INFO] مطلب {article['source']} با موفقیت پست شد.")
                    new_seen.add(h)
                time.sleep(3)
            last_who_check_key = check_key

        save_seen(new_seen)
        return new_seen, last_who_check_key, last_physio_check_key


# ---------- وب‌سرور + Watchdog ----------
app = Flask(__name__)

worker_thread = None
worker_lock = threading.Lock()
worker_started_at = None
worker_last_cycle = None
worker_last_error = None


def worker_is_alive():
    return worker_thread is not None and worker_thread.is_alive()


def start_worker_if_needed():
    global worker_thread, worker_started_at

    with worker_lock:
        if worker_is_alive():
            return False

        worker_started_at = datetime.now(timezone.utc).isoformat()
        print("[WATCHDOG] News worker is not running — starting/restarting it...")

        worker_thread = threading.Thread(
            target=main_loop,
            name="news-worker",
            daemon=True
        )
        worker_thread.start()
        return True


@app.route("/")
def health_check():
    # Cron-job.org calls this endpoint every minute.
    # If the worker has stopped, the request restarts it.
    restarted = start_worker_if_needed()

    return {
        "service": "PharmaCity bot",
        "status": "running" if worker_is_alive() else "starting",
        "worker_alive": worker_is_alive(),
        "worker_restarted": restarted,
        "time_utc": datetime.now(timezone.utc).isoformat()
    }, 200


@app.route("/health")
def health():
    return {
        "service": "PharmaCity bot",
        "worker_alive": worker_is_alive(),
        "worker_started_at": worker_started_at,
        "worker_last_cycle": worker_last_cycle,
        "worker_last_error": worker_last_error,
        "time_utc": datetime.now(timezone.utc).isoformat()
    }, 200


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
        start_worker_if_needed()

    port = int(os.environ.get("PORT", "10000"))
    print(f"[INFO] PharmaCity web service starting on port {port}")
    app.run(host="0.0.0.0", port=port)
