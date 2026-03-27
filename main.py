import os
import sys
import re
import json
import time
import hashlib
import asyncio
from difflib import SequenceMatcher
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from openai import OpenAI

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

load_dotenv()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} хоосон байна. Railway Variables эсвэл .env файлаа шалга.")
    return value.strip()


API_ID = int(require_env("API_ID"))
API_HASH = require_env("API_HASH")
BOT_TOKEN = require_env("BOT_TOKEN")
OPENAI_API_KEY = require_env("OPENAI_API_KEY")
TARGET_CHANNEL = require_env("TARGET_CHANNEL")
USER_STRING_SESSION = require_env("USER_STRING_SESSION")

SOURCE_CHANNELS = [
    x.strip().lstrip("@").lower()
    for x in os.getenv("SOURCE_CHANNELS", "").split(",")
    if x.strip()
]

if not SOURCE_CHANNELS:
    raise ValueError("SOURCE_CHANNELS хоосон байна. Railway Variables эсвэл .env файлаа шалга.")

oa = OpenAI(api_key=OPENAI_API_KEY)

user_client = TelegramClient(StringSession(USER_STRING_SESSION), API_ID, API_HASH)
bot_client = TelegramClient("bot_session", API_ID, API_HASH)

SEEN_FILE = "seen_news.json"
SMART_DUP_FILE = "smart_seen_news.json"

SEEN_TTL_SECONDS = 60 * 60 * 8
SMART_DUP_TTL_SECONDS = 60 * 60 * 6

CATEGORY_LABELS = {
    "geopolitics": "🌍 Геополитик",
    "financial_markets": "📈 Санхүүгийн зах зээл",
    "crypto": "🪙 Крипто",
    "commodities": "🛢 Газрын тос, алт",
    "economy": "📊 Эдийн засаг",
}


def load_json_file(path: str):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json_file(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


seen_cache = load_json_file(SEEN_FILE)
smart_dup_cache = load_json_file(SMART_DUP_FILE)


def cleanup_cache(cache: dict, ttl: int, path: str):
    now = int(time.time())
    expired = [k for k, v in cache.items() if now - v.get("ts", 0) > ttl]
    for k in expired:
        cache.pop(k, None)
    if expired:
        save_json_file(path, cache)


def clean_text(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r"(?i)\bmt\s*b?\s*max\b", "", text)
    text = re.sub(r"(?i)\bvisit here\b", "", text)
    text = re.sub(r"(?i)\bcheck out\b", "", text)
    text = re.sub(r"(?i)\bread more\b", "", text)
    text = re.sub(r"(?i)\bsubscribe\b", "", text)
    text = re.sub(r"(?i)\bjoin now\b", "", text)
    text = re.sub(r"(?i)\bsponsored\b", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_junk(text: str) -> bool:
    if not text:
        return True

    stripped = text.strip().lower()

    urgent_terms = [
        "iran", "israel", "russia", "ukraine", "china", "taiwan",
        "military", "missile", "airstrike", "troops", "bombing",
        "ground forces", "final blow", "breaking", "urgent",
        "attack", "strike"
    ]

    if any(term in stripped for term in urgent_terms):
        return False

    if len(stripped) < 30:
        return True

    junk_patterns = [
        r"(?i)^live:?$",
        r"(?i)^more:?$",
        r"(?i)^follow:?$",
        r"(?i)promo",
        r"(?i)advertisement",
        r"(?i)giveaway",
        r"(?i)follow us",
    ]

    return any(re.search(p, stripped) for p in junk_patterns)


def keyword_match(text: str) -> bool:
    t = text.lower()
    keywords = [
        "war", "military", "missile", "iran", "israel", "russia", "ukraine",
        "china", "taiwan", "nato", "sanction", "conflict", "troops",
        "airstrike", "middle east", "tehran", "moscow", "beijing",
        "ground forces", "bombing campaign", "military options",
        "final blow", "retaliation", "attack", "white house", "pentagon",

        "stocks", "stock market", "s&p 500", "sp500", "nasdaq", "dow",
        "bond", "treasury", "yield", "equities", "futures",
        "fed", "federal reserve", "rate cut", "rate hike",

        "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
        "solana", "binance", "blockchain", "token", "stablecoin", "altcoin",
        "etf inflow", "wallet", "exchange",

        "oil", "crude", "brent", "wti", "gold", "silver", "commodity",
        "natural gas", "opec", "barrel", "bullion",

        "inflation", "cpi", "ppi", "gdp", "recession", "economy", "economic",
        "unemployment", "payrolls", "jobs report", "nonfarm payrolls",
        "central bank", "interest rate", "consumer spending", "retail sales",
    ]
    return any(k in t for k in keywords)


def normalize_for_hash(text: str) -> str:
    text = text.lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9\s$%.,:-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_text_hash(text: str) -> str:
    normalized = normalize_for_hash(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_exact_duplicate(text: str) -> bool:
    cleanup_cache(seen_cache, SEEN_TTL_SECONDS, SEEN_FILE)
    h = get_text_hash(text)
    now = int(time.time())

    if h in seen_cache:
        return True

    seen_cache[h] = {"ts": now}
    save_json_file(SEEN_FILE, seen_cache)
    return False


def text_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def get_semantic_key(text: str) -> str:
    t = normalize_for_hash(text)

    important = [
        "iran", "israel", "russia", "ukraine", "china", "taiwan",
        "fed", "fomc", "powell", "cpi", "ppi", "gdp",
        "bitcoin", "btc", "ethereum", "eth", "solana",
        "oil", "crude", "brent", "wti", "gold", "silver",
        "stocks", "nasdaq", "sp500", "s&p 500", "dow",
        "treasury", "yield", "opec", "etf", "sec",
        "missile", "airstrike", "troops", "attack", "ceasefire"
    ]

    hits = [w for w in important if w in t]
    words = re.findall(r"\b[a-z0-9$%.-]{3,}\b", t)
    short = " ".join((hits + words[:12])[:18])
    return short.strip()


def is_smart_duplicate(text: str) -> bool:
    cleanup_cache(smart_dup_cache, SMART_DUP_TTL_SECONDS, SMART_DUP_FILE)

    now = int(time.time())
    current_norm = normalize_for_hash(text)
    current_key = get_semantic_key(text)

    for _, item in smart_dup_cache.items():
        old_norm = item.get("norm", "")
        old_key = item.get("key", "")

        sim = text_similarity(current_norm[:600], old_norm[:600])

        same_key = (
            current_key
            and old_key
            and (
                current_key in old_key
                or old_key in current_key
                or text_similarity(current_key, old_key) > 0.82
            )
        )

        if sim > 0.88 or (same_key and sim > 0.72):
            return True

    smart_dup_cache[get_text_hash(text)] = {
        "ts": now,
        "norm": current_norm[:1200],
        "key": current_key[:300],
    }
    save_json_file(SMART_DUP_FILE, smart_dup_cache)
    return False


def get_priority_score(text: str) -> int:
    t = text.lower()
    score = 0.0

    strong_terms = [
        "breaking", "urgent",
        "fed", "fomc", "powell",
        "cpi", "ppi", "inflation",
        "interest rate", "rate cut", "rate hike",
        "nonfarm payrolls", "jobs report", "gdp", "recession",
        "iran", "israel", "russia", "ukraine", "china", "taiwan",
        "missile", "airstrike", "military", "war", "sanctions",
        "opec", "oil", "crude", "gold",
        "bitcoin etf", "ethereum etf", "sec",
        "ground forces", "bombing campaign", "military options",
        "final blow", "retaliation", "pentagon", "white house"
    ]

    medium_terms = [
        "stocks", "nasdaq", "dow", "sp500", "s&p 500",
        "treasury", "bond", "yields", "futures",
        "crypto", "bitcoin", "ethereum", "binance", "solana",
        "commodities", "natural gas",
        "economy", "central bank", "consumer spending", "retail sales",
        "etf inflow", "attack", "strike"
    ]

    for term in strong_terms:
        if term in t:
            score += 3

    for term in medium_terms:
        if term in t:
            score += 1

    if "%" in t:
        score += 1
    if "$" in t:
        score += 1
    if re.search(r"\b\d+bp\b", t):
        score += 1
    if re.search(r"\b\d+(\.\d+)?%\b", t):
        score += 1

    return int(score)


def get_priority_label(score: int, text: str) -> str:
    t = text.lower()
    ultra_terms = [
        "breaking", "urgent", "missile", "airstrike", "ground forces",
        "bombing campaign", "fed", "fomc", "cpi", "ppi", "nonfarm payrolls"
    ]

    ultra_hit = any(term in t for term in ultra_terms)

    if score >= 8 or ultra_hit:
        return "🚨 BREAKING"
    if score >= 5:
        return "⚡ HIGH PRIORITY"
    return "📰 UPDATE"


def ai_process_news(text: str, priority_label: str):
    cleaned = clean_text(text)

    prompt = f"""
You are formatting a premium Mongolian Telegram market news post.

Return exactly in this structure:

CATEGORY: <geopolitics|financial_markets|crypto|commodities|economy>
TITLE: <very short strong Mongolian headline>
SUMMARY: <2 short Mongolian sentences explaining the news clearly>
IMPACT: <1-2 short Mongolian sentences about market impact, trader angle, what may react>
WHY: <1 short Mongolian sentence on why this matters now>

Rules:
- Natural Mongolian
- No source mention
- No links
- No promo
- No hashtags
- No markdown
- Keep tickers / instruments if relevant
- Market impact must mention likely reaction in assets like BTC, gold, oil, dollar, stocks when relevant
- Keep it concise and sharp
- If irrelevant, return IGNORE only

News:
{cleaned}

Priority:
{priority_label}
""".strip()

    response = oa.responses.create(
        model="gpt-5-mini",
        input=prompt
    )

    output = response.output_text.strip()

    if output.upper() == "IGNORE":
        return None

    category_match = re.search(r"CATEGORY:\s*(.+)", output)
    title_match = re.search(r"TITLE:\s*(.+)", output)
    summary_match = re.search(r"SUMMARY:\s*(.+)", output)
    impact_match = re.search(r"IMPACT:\s*(.+)", output)
    why_match = re.search(r"WHY:\s*(.+)", output)

    if not all([category_match, title_match, summary_match, impact_match, why_match]):
        return None

    category = category_match.group(1).strip().lower()
    title = title_match.group(1).strip()
    summary = summary_match.group(1).strip()
    impact = impact_match.group(1).strip()
    why = why_match.group(1).strip()

    if category not in CATEGORY_LABELS:
        return None

    if len(title) < 4 or len(summary) < 15 or len(impact) < 10:
        return None

    return category, title, summary, impact, why


def format_post(priority_label: str, category: str, title: str, summary: str, impact: str, why: str) -> str:
    parts = [
        f"{priority_label} | {CATEGORY_LABELS[category]}",
        "",
        title,
        "",
        "🧠 Товч утга:",
        summary,
        "",
        "📈 Зах зээлд нөлөө:",
        impact,
        "",
        "✅ Яагаад чухал вэ:",
        why,
    ]
    return "\n".join(parts).strip()


@user_client.on(events.NewMessage(chats=SOURCE_CHANNELS))
async def handler(event):
    text = event.message.message

    if not text:
        print("Skipped empty message")
        return

    try:
        print("New message detected")

        cleaned = clean_text(text)

        if is_junk(cleaned):
            print("Skipped junk/too short")
            return

        if not keyword_match(cleaned):
            print("Skipped by keyword filter")
            return

        if is_exact_duplicate(cleaned):
            print("Skipped exact duplicate")
            return

        if is_smart_duplicate(cleaned):
            print("Skipped smart duplicate")
            return

        priority_score = get_priority_score(cleaned)
        if priority_score < 2:
            print(f"Skipped low priority | score={priority_score}")
            return

        priority_label = get_priority_label(priority_score, cleaned)

        result = ai_process_news(cleaned, priority_label)
        if not result:
            print("Skipped by AI")
            return

        category, title, summary, impact, why = result

        final_post = format_post(
            priority_label=priority_label,
            category=category,
            title=title,
            summary=summary,
            impact=impact,
            why=why,
        )

        await bot_client.send_message(
            TARGET_CHANNEL,
            final_post,
            link_preview=False
        )

        print(f"Posted successfully | score={priority_score} | label={priority_label}")

    except Exception as e:
        print("Error:", str(e))


async def main():
    await user_client.connect()
    await bot_client.start(bot_token=BOT_TOKEN)

    if not await user_client.is_user_authorized():
        raise RuntimeError(
            "USER_STRING_SESSION ажиллахгүй байна. StringSession-ээ дахин үүсгээд Railway Variables дээр шинэчил."
        )

    print("Bot started...")
    print("Listening:", SOURCE_CHANNELS)
    print("Forwarding to:", TARGET_CHANNEL)

    await user_client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())