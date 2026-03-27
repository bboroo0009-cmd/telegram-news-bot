import os
import sys
import re
import json
import time
import hashlib
import asyncio
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
    x.strip().lstrip("@")
    for x in os.getenv("SOURCE_CHANNELS", "").split(",")
    if x.strip()
]

if not SOURCE_CHANNELS:
    raise ValueError("SOURCE_CHANNELS хоосон байна. Railway Variables эсвэл .env файлаа шалга.")

oa = OpenAI(api_key=OPENAI_API_KEY)

user_client = TelegramClient(StringSession(USER_STRING_SESSION), API_ID, API_HASH)
bot_client = TelegramClient("bot_session", API_ID, API_HASH)

SEEN_FILE = "seen_news.json"
SEEN_TTL_SECONDS = 60 * 60 * 8  # 8 цаг

MAJOR_SOURCES = {
    "wublockchainenglish": "Wu Blockchain",
    "financialjuice": "FinancialJuice",
    "marketsalpha": "MarketsAlpha",
    "reuters": "Reuters",
    "bloomberg": "Bloomberg",
    "coindesk": "CoinDesk",
    "cointelegraph": "Cointelegraph",
    "fxhedgers": "FXHedgers",
    "axios": "Axios",
}

CATEGORY_LABELS = {
    "geopolitics": "🌍 Геополитик",
    "financial_markets": "📈 Санхүүгийн зах зээл",
    "crypto": "🪙 Крипто",
    "commodities": "🛢 Газрын тос, алт",
    "economy": "📊 Эдийн засгийн чухал мэдээ",
}


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return {}
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_seen(data):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


seen_cache = load_seen()


def cleanup_seen():
    now = int(time.time())
    expired = [k for k, v in seen_cache.items() if now - v > SEEN_TTL_SECONDS]
    for k in expired:
        seen_cache.pop(k, None)
    if expired:
        save_seen(seen_cache)


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

    stripped = text.strip()
    lowered = stripped.lower()

    urgent_terms = [
        "iran", "israel", "russia", "ukraine", "china", "taiwan",
        "military", "missile", "airstrike", "troops", "bombing",
        "ground forces", "final blow", "breaking", "urgent",
        "axios", "pentagon", "white house", "tehran", "retaliation",
        "attack", "strike"
    ]

    if any(term in lowered for term in urgent_terms):
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
        "final blow", "strike package", "retaliation", "attack plan",
        "defense ministry", "white house", "pentagon", "axios",
        "ceasefire", "drone strike", "naval", "army", "defense",

        "stocks", "stock market", "s&p 500", "sp500", "nasdaq", "dow",
        "bond", "treasury", "yield", "yields", "equities", "futures",
        "wall street", "fed", "federal reserve", "rate cut", "rate hike",
        "shares", "index", "investors",

        "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
        "solana", "binance", "blockchain", "token", "stablecoin", "altcoin",
        "etf inflow", "on-chain", "wallet", "exchange",

        "oil", "crude", "brent", "wti", "gold", "silver", "commodity",
        "commodities", "natural gas", "opec", "barrel", "bullion",

        "inflation", "cpi", "ppi", "gdp", "recession", "economy", "economic",
        "unemployment", "payrolls", "jobs report", "nonfarm payrolls",
        "central bank", "interest rate", "consumer spending", "retail sales", "macro",
    ]

    return any(k in t for k in keywords)


def is_urgent_geopolitics(text: str) -> bool:
    t = text.lower()

    urgent_patterns = [
        "iran", "israel", "russia", "ukraine",
        "military options", "ground forces", "bombing campaign",
        "final blow", "missile", "airstrike", "troops",
        "attack", "retaliation", "white house", "pentagon",
        "tehran", "middle east", "axios", "drone strike"
    ]

    hits = sum(1 for p in urgent_patterns if p in t)
    return hits >= 2


def normalize_for_hash(text: str) -> str:
    text = text.lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9\s$%.,:-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_text_hash(text: str) -> str:
    normalized = normalize_for_hash(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_duplicate(text: str) -> bool:
    cleanup_seen()
    h = get_text_hash(text)
    now = int(time.time())

    if h in seen_cache:
        return True

    seen_cache[h] = now
    save_seen(seen_cache)
    return False


def detect_source(event, text: str) -> str | None:
    channel_name = ""

    try:
        if getattr(event, "chat", None):
            if getattr(event.chat, "username", None):
                channel_name = event.chat.username.lower()
            elif getattr(event.chat, "title", None):
                channel_name = event.chat.title.lower()
    except Exception:
        pass

    combined = f"{channel_name} {text.lower()}"

    for key, label in MAJOR_SOURCES.items():
        if key in combined:
            return label

    return None


def get_priority_score(text: str, source_label: str | None = None) -> int:
    t = text.lower()
    score = 0.0

    if source_label:
        if source_label in ["FinancialJuice", "MarketsAlpha", "Bloomberg", "Reuters", "FXHedgers", "Axios"]:
            score += 3
        elif source_label in ["Wu Blockchain", "CoinDesk", "Cointelegraph"]:
            score += 2

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
        "final blow", "retaliation", "pentagon", "white house", "axios"
    ]

    medium_terms = [
        "stocks", "nasdaq", "dow", "sp500", "s&p 500",
        "treasury", "bond", "yields", "futures",
        "crypto", "bitcoin", "ethereum", "binance", "solana",
        "commodities", "natural gas",
        "economy", "central bank", "consumer spending", "retail sales",
        "etf inflow", "etf inflows", "attack", "strike"
    ]

    weak_terms = [
        "market", "markets", "investors", "shares",
        "exchange", "token", "blockchain"
    ]

    for term in strong_terms:
        if term in t:
            score += 3

    for term in medium_terms:
        if term in t:
            score += 1

    for term in weak_terms:
        if term in t:
            score += 0.5

    if "%" in t:
        score += 1

    if "$" in t:
        score += 1

    if re.search(r"\b\d+bp\b", t):
        score += 1

    if re.search(r"\b\d+\.\d+%\b", t) or re.search(r"\b\d+%\b", t):
        score += 1

    return int(score)


def get_priority_label(score: int) -> str:
    if score >= 8:
        return "🚨 BREAKING"
    if score >= 6:
        return "⚡ HIGH PRIORITY"
    if score >= 4:
        return "📌 IMPORTANT"
    return ""


def ai_process_news(text: str):
    cleaned = clean_text(text)

    prompt = f"""
Classify this news into one of:
geopolitics, financial_markets, crypto, commodities, economy.
If not relevant, return IGNORE.

If relevant, return exactly:

CATEGORY: <label>
TITLE: <short Mongolian headline>
BODY: <2-3 short Mongolian sentences>

Rules:
- Natural Mongolian
- Short and clear
- No links
- No promo
- Keep important names/tickers
- No extra text

News:
{cleaned}
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
    body_match = re.search(r"BODY:\s*(.+)", output, re.DOTALL)

    if not category_match or not title_match or not body_match:
        return None

    category = category_match.group(1).strip().lower()
    title = title_match.group(1).strip()
    body = body_match.group(1).strip()

    if category not in CATEGORY_LABELS:
        return None

    if len(title) < 4 or len(body) < 20:
        return None

    return category, title, body


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

        if not keyword_match(cleaned) and not is_urgent_geopolitics(cleaned):
            print("Skipped by keyword filter")
            return

        if is_duplicate(cleaned):
            print("Skipped duplicate")
            return

        source_label = detect_source(event, cleaned)

        priority_score = get_priority_score(cleaned, source_label)
        priority_label = get_priority_label(priority_score)

        if priority_score < 3:
            print(f"Skipped low priority | score={priority_score}")
            return

        result = ai_process_news(cleaned)

        if not result:
            print("Skipped by AI")
            return

        category, title, body = result

        parts = []

        if priority_label:
            parts.append(priority_label)

        parts.append(CATEGORY_LABELS[category])
        parts.append(title)
        parts.append("")
        parts.append(body)

        final_post = "\n".join(parts).strip()

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