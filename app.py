import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import uuid
from html import escape
from pathlib import Path
from threading import Lock

import genanki
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
from google import genai
from google.genai import types
from gtts import gTTS
from gtts.lang import tts_langs

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024


@app.errorhandler(413)
def request_entity_too_large(_error):
    return jsonify({"error": "Request is too large."}), 413


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


# =========================================================
# CONFIGURATION
# =========================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

def env_int(name, default, minimum=0):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger_temp = logging.getLogger("ai_flashcards.config")
        logger_temp.warning("Invalid integer for %s=%r; using %s.", name, raw, default)
        value = default
    return max(minimum, value)

PORT = env_int("PORT", 5000, minimum=1)
GEMINI_TIMEOUT_MS = env_int("GEMINI_TIMEOUT_MS", 60000, minimum=1000)
TRUST_PROXY_COUNT = int(os.getenv("TRUST_PROXY_COUNT", "0").strip() or "0")
if TRUST_PROXY_COUNT < 0:
    TRUST_PROXY_COUNT = 0

if TRUST_PROXY_COUNT:
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=TRUST_PROXY_COUNT,
        x_proto=TRUST_PROXY_COUNT,
        x_host=TRUST_PROXY_COUNT,
        x_port=TRUST_PROXY_COUNT,
        x_prefix=TRUST_PROXY_COUNT,
    )

MAX_WORD_LENGTH = env_int("MAX_WORD_LENGTH", 120, minimum=1)
MAX_TOPIC_LENGTH = env_int("MAX_TOPIC_LENGTH", 500, minimum=1)
MAX_LANGUAGE_LENGTH = 40
MAX_CARD_COUNT = env_int("MAX_CARD_COUNT", 10, minimum=1)
GENERATION_RATE_LIMIT = env_int("GENERATION_RATE_LIMIT", 10, minimum=1)
GENERATION_RATE_WINDOW = env_int("GENERATION_RATE_WINDOW", 60, minimum=1)
TTS_RATE_LIMIT = env_int("TTS_RATE_LIMIT", 20, minimum=1)
TTS_RATE_WINDOW = env_int("TTS_RATE_WINDOW", 60, minimum=1)
DOWNLOAD_RATE_LIMIT = env_int("DOWNLOAD_RATE_LIMIT", 10, minimum=1)
DOWNLOAD_RATE_WINDOW = env_int("DOWNLOAD_RATE_WINDOW", 60, minimum=1)
_rate_windows = {
    "generate": GENERATION_RATE_WINDOW,
    "tts": TTS_RATE_WINDOW,
    "download": DOWNLOAD_RATE_WINDOW,
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai_flashcards")

_gemini_client = None
_tts_languages_cache = None
# Rate limiting is stored in SQLite so all Gunicorn workers on the same
# host share one limiter instead of keeping separate in-memory buckets.
# For multiple app instances/containers, point RATE_LIMIT_DB_PATH at a
# shared writable SQLite location or use a shared external rate-limit store.
if os.environ.get("VERCEL"):
    RATE_LIMIT_DB_PATH = "/tmp/rate_limit.sqlite3"
else:
    RATE_LIMIT_DB_PATH = os.getenv(
        "RATE_LIMIT_DB_PATH",
        str(Path(app.instance_path) / "rate_limit.sqlite3"),
    ).strip()

_rate_lock = Lock()

def _init_rate_limit_db():
    db_path = Path(RATE_LIMIT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bucket_name TEXT NOT NULL,
                client_id TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup "
            "ON rate_limit_events(bucket_name, client_id, created_at)"
        )


_init_rate_limit_db()

CARD_FIELDS = (
    "word",
    "meaning",
    "part_of_speech",
    "article",
    "gender",
    "plural",
    "target_sentence",
    "english_sentence",
    "cefr_level",
)

TTS_LANGUAGE_CANDIDATES = {
    "english": ("en",),
    "spanish": ("es",),
    "french": ("fr",),
    "german": ("de",),
    "italian": ("it",),
    "portuguese": ("pt", "pt-PT", "pt-BR"),
    "russian": ("ru",),
    "japanese": ("ja",),
    "korean": ("ko",),
    "chinese": ("zh-CN", "zh-TW", "zh"),
    "arabic": ("ar",),
    "hindi": ("hi",),
    "turkish": ("tr",),
    "dutch": ("nl",),
    "polish": ("pl",),
    "ukrainian": ("uk",),
    "swedish": ("sv",),
    "greek": ("el",),
    "indonesian": ("id",),
    "vietnamese": ("vi",),
}

SUPPORTED_LANGUAGES = tuple(
    name.title() for name in TTS_LANGUAGE_CANDIDATES.keys()
)


def normalize_supported_language(value):
    normalized = str(value or "").strip().casefold()
    for language in SUPPORTED_LANGUAGES:
        if language.casefold() == normalized:
            return language
    return None

# =========================================================
# HELPERS
# =========================================================


def json_data():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def client_identifier():
    # ProxyFix is opt-in via TRUST_PROXY_COUNT, so REMOTE_ADDR is safe to
    # use here without blindly trusting client-supplied forwarded headers.
    return request.remote_addr or "unknown"


def rate_limit(bucket_name, limit, window_seconds):
    """Apply a sliding-window limit shared by all workers on this host."""
    now = time.time()
    cutoff = now - window_seconds
    client_id = client_identifier()

    with _rate_lock:
        try:
            with sqlite3.connect(RATE_LIMIT_DB_PATH, timeout=10) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("BEGIN IMMEDIATE")

                # Remove expired records globally. This keeps the database
                # bounded instead of leaking one row per request forever.
                conn.execute(
                    "DELETE FROM rate_limit_events WHERE created_at <= ?",
                    (now - max(_rate_windows.values()),),
                )

                count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM rate_limit_events
                    WHERE bucket_name = ?
                      AND client_id = ?
                      AND created_at > ?
                    """,
                    (bucket_name, client_id, cutoff),
                ).fetchone()[0]

                if count >= limit:
                    oldest = conn.execute(
                        """
                        SELECT created_at
                        FROM rate_limit_events
                        WHERE bucket_name = ?
                          AND client_id = ?
                          AND created_at > ?
                        ORDER BY created_at ASC
                        LIMIT 1
                        """,
                        (bucket_name, client_id, cutoff),
                    ).fetchone()
                    retry_after = max(1, int(window_seconds - (now - oldest[0]))) if oldest else 1
                    conn.rollback()
                    return False, retry_after

                conn.execute(
                    "INSERT INTO rate_limit_events(bucket_name, client_id, created_at) VALUES (?, ?, ?)",
                    (bucket_name, client_id, now),
                )
                conn.commit()
                return True, 0
        except sqlite3.Error:
            logger.exception("Rate-limit storage failure")
            # Fail closed: if the limiter cannot safely record a request, do
            # not allow an unbounded request path through production.
            return False, 5


def rate_limit_response(retry_after):
    response = jsonify({
        "error": "Too many requests. Please wait a moment and try again."
    })
    response.status_code = 429
    response.headers["Retry-After"] = str(retry_after)
    return response


def validate_text(value, name, max_length):
    value = str(value or "").strip()
    if len(value) > max_length:
        raise ValueError(f"{name} is too long. Maximum length is {max_length} characters.")
    return value


def normalize_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def normalize_anki_word(value):
    cleaned = re.sub(r"<[^>]*>", "", str(value or ""))
    cleaned = html.unescape(cleaned).replace("\xa0", " ")
    return normalize_text(cleaned)


def safe_filename(value, fallback="audio", max_length=50):
    cleaned = re.sub(r"[^a-zA-Z0-9äöüÄÖÜß_-]+", "_", str(value or ""))
    cleaned = cleaned.strip("_")
    return cleaned[:max_length] or fallback

# =========================================================
# GEMINI
# =========================================================


def get_gemini_client():
    global _gemini_client
    if _gemini_client is not None:
        return _gemini_client
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not configured. Add it to your environment or .env file."
        )
    _gemini_client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )
    return _gemini_client


FLASHCARD_SCHEMA = {
    "type": "object",
    "properties": {
        "word": {"type": "string"},
        "meaning": {"type": "string"},
        "part_of_speech": {"type": "string"},
        "article": {"type": "string"},
        "gender": {"type": "string"},
        "plural": {"type": "string"},
        "target_sentence": {"type": "string"},
        "english_sentence": {"type": "string"},
        "cefr_level": {"type": "string"},
        "higher_vocab": {
            "type": "object",
            "properties": {
                "level": {"type": "string"},
                "word": {"type": "string"},
                "meaning": {"type": "string"},
                "target_sentence": {"type": "string"},
                "english_sentence": {"type": "string"},
            },
            "required": [
                "level",
                "word",
                "meaning",
                "target_sentence",
                "english_sentence",
            ],
        },
    },
    "required": list(CARD_FIELDS) + ["higher_vocab"],
}

FLASHCARDS_SCHEMA = {
    "type": "object",
    "properties": {
        "flashcards": {
            "type": "array",
            "items": FLASHCARD_SCHEMA,
        }
    },
    "required": ["flashcards"],
}


def gemini_generate(prompt, schema):
    client = get_gemini_client()
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
    except Exception as exc:
        logger.exception("Gemini request failed")
        raise RuntimeError("The AI service could not complete the request.") from exc

    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("The AI returned an empty response.")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error("Gemini returned invalid JSON: %s", text[:500])
        raise RuntimeError("The AI returned invalid structured data.") from exc


def normalize_cefr_level(value):
    text = str(value or "").strip().upper()
    match = re.search(r"\b(A1|A2|B1|B2|C1|C2)\b", text)
    return match.group(1) if match else ""


def empty_higher_vocab():
    return {
        "level": "",
        "word": "",
        "meaning": "",
        "target_sentence": "",
        "english_sentence": "",
    }


def validate_card(card, language=None):
    if not isinstance(card, dict):
        return False

    if any(not isinstance(card.get(field, ""), str) for field in CARD_FIELDS):
        return False

    required_main_fields = (
        "word",
        "meaning",
        "part_of_speech",
        "target_sentence",
        "english_sentence",
        "cefr_level",
    )
    if any(not card.get(field, "").strip() for field in required_main_fields):
        return False

    if normalize_cefr_level(card.get("cefr_level")) not in {"A1", "A2", "B1", "B2", "C1", "C2"}:
        return False

    higher_vocab = card.get("higher_vocab")
    if not isinstance(higher_vocab, dict):
        return False

    higher_fields = (
        "level",
        "word",
        "meaning",
        "target_sentence",
        "english_sentence",
    )
    if any(not isinstance(higher_vocab.get(field, ""), str) for field in higher_fields):
        return False

    return True


def sanitize_card(card, language):
    sanitized = dict(card)
    sanitized["cefr_level"] = normalize_cefr_level(sanitized.get("cefr_level"))

    raw_higher = sanitized.get("higher_vocab") or {}
    higher_vocab = {
        field: str(raw_higher.get(field, "")).strip()
        for field in (
            "level",
            "word",
            "meaning",
            "target_sentence",
            "english_sentence",
        )
    }

    if str(language or "").strip().casefold() != "german":
        sanitized["higher_vocab"] = empty_higher_vocab()
        return sanitized

    expected_level = {"A1": "B1", "A2": "B2"}.get(sanitized["cefr_level"])
    higher_vocab["level"] = normalize_cefr_level(higher_vocab["level"])
    complete = all(
        higher_vocab[field]
        for field in ("word", "meaning", "target_sentence", "english_sentence")
    )

    if expected_level and higher_vocab["level"] == expected_level and complete:
        sanitized["higher_vocab"] = higher_vocab
    else:
        sanitized["higher_vocab"] = empty_higher_vocab()

    return sanitized



def generate_word_card(word, language):
    source_word_json = json.dumps(word, ensure_ascii=False)

    prompt = f"""
You are an expert language teacher creating a vocabulary flashcard.

Create exactly ONE vocabulary flashcard from the learner's English input word.

English input word: {source_word_json}
Target language: {language}

IMPORTANT LANGUAGE DIRECTION:
- The learner enters the word in English.
- Translate that English word into the selected target language.
- The `word` field must contain the target-language vocabulary item.
- The `meaning` field must contain the English meaning of the target-language word.
- Do not put the target-language translation into the `meaning` field.

CEFR LEVEL:
- Determine the approximate CEFR level of the MAIN target-language vocabulary item.
- Put exactly one of: A1, A2, B1, B2, C1, C2 in `cefr_level`.

HIGHER VOCABULARY:
This feature is active ONLY for German.

If the target language is German:
- If the main word is A1, create one useful B1-level higher vocabulary item.
- If the main word is A2, create one useful B2-level higher vocabulary item.
- If the main word is B1, B2, C1, or C2, leave every `higher_vocab` field empty.

For a German higher vocabulary item:
- `level` must be B1 or B2 as appropriate.
- `word` must be a genuine useful higher-level German vocabulary item.
- `meaning` must be its English meaning.
- `target_sentence` must naturally use the higher-level German word.
- `english_sentence` must accurately translate that sentence.

If the target language is NOT German:
- Leave every `higher_vocab` field empty.

Do NOT create a higher vocabulary item just to fill the field.
Do not use an obscure or unnatural synonym.
Prefer a useful vocabulary upgrade that a learner could realistically encounter.

Example:
English input word: "cook"
Target language: German

Main card:
word: "kocht"
meaning: "to cook"
cefr_level: "A1"

Higher vocabulary:
level: "B1"
word: "zubereiten"
meaning: "to prepare"
target_sentence: "Ich bereite das Essen zu."
english_sentence: "I prepare the food."

Rules:
- Use the correct part of speech.
- Fill article/gender/plural only when applicable; otherwise use an empty string.
- target_sentence must be natural in the target language.
- english_sentence must accurately translate target_sentence.
- Every required field must follow the schema.
- Return only the required JSON object.
"""

    card = gemini_generate(prompt, FLASHCARD_SCHEMA)

    if not isinstance(card, dict):
        raise ValueError("The AI returned an invalid flashcard object.")

    if not validate_card(card, language):
        raise ValueError("The AI returned an invalid flashcard structure.")

    return sanitize_card(card, language)


# =========================================================
# TTS
# =========================================================


def get_tts_languages():
    global _tts_languages_cache
    if _tts_languages_cache is not None:
        return _tts_languages_cache
    try:
        _tts_languages_cache = tts_langs()
    except Exception:
        logger.exception("Could not load gTTS languages")
        _tts_languages_cache = {}
    return _tts_languages_cache


def get_language_code(language):
    normalized = str(language or "").strip().lower()
    available = get_tts_languages()
    available_lower = {code.lower(): code for code in available}

    for candidate in TTS_LANGUAGE_CANDIDATES.get(normalized, ()):
        if candidate.lower() in available_lower:
            return available_lower[candidate.lower()]

    for code, label in available.items():
        label_normalized = str(label).strip().lower()
        if label_normalized == normalized:
            return code

    return None


def create_pronunciation_file(text, language, kind="audio", slow=False):
    code = get_language_code(language)
    if not code:
        raise RuntimeError(f"Pronunciation is not available for {language}.")
    filename = (
        f"ai_flashcard_{kind}_{safe_filename(text)}_"
        f"{uuid.uuid4().hex[:10]}.mp3"
    )
    path = os.path.join(tempfile.gettempdir(), filename)
    try:
        gTTS(text=text, lang=code, slow=slow).save(path)
        return path, filename
    except Exception as exc:
        if os.path.exists(path):
            os.remove(path)
        raise RuntimeError("Pronunciation generation failed.") from exc

# =========================================================
# ANKI HTML
# =========================================================


def card_values(card):
    return {field: str(card.get(field, "")).strip() for field in CARD_FIELDS}


def should_show_higher_vocab(card, language):
    higher_vocab = card.get("higher_vocab") or {}
    main_level = normalize_cefr_level(card.get("cefr_level"))
    higher_level = normalize_cefr_level(higher_vocab.get("level"))
    is_german = str(language or "").strip().casefold() == "german"

    return bool(
        is_german
        and str(higher_vocab.get("word") or "").strip()
        and str(higher_vocab.get("meaning") or "").strip()
        and str(higher_vocab.get("target_sentence") or "").strip()
        and str(higher_vocab.get("english_sentence") or "").strip()
        and (
            (main_level == "A1" and higher_level == "B1")
            or (main_level == "A2" and higher_level == "B2")
        )
    )


def build_anki_back(
    card,
    language,
    word_audio=None,
    sentence_audio=None,
    higher_word_audio=None,
    higher_sentence_audio=None,
):
    values = card_values(card)

    audio_html = ""
    if sentence_audio:
       audio_html += (
        "<br><br><b>🔊 Sentence Pronunciation:</b><br>"
        f"[sound:{escape(sentence_audio)}]"
    )

    higher_vocab = card.get("higher_vocab") or {}
    higher_vocab_html = ""

    if should_show_higher_vocab(card, language):
        higher_audio_html = ""
        if higher_word_audio:
            higher_audio_html += (
                "<br><br><b>🔊 Word Pronunciation:</b><br>"
                f"[sound:{escape(higher_word_audio)}]"
            )
        if higher_sentence_audio:
            higher_audio_html += (
                "<br><br><b>🔊 Sentence Pronunciation:</b><br>"
                f"[sound:{escape(higher_sentence_audio)}]"
            )

        higher_vocab_html = f"""
<details style="margin-top:24px;text-align:left;">
    <summary style="cursor:pointer;font-weight:bold;">high vocab</summary>
    <div style="margin-top:16px;">
        <div style="font-size:24px;font-weight:bold;">
            {escape(str(higher_vocab.get('word', '')).strip())}
        </div>
        <div style="margin-top:8px;">
            <b>Level:</b> {escape(normalize_cefr_level(higher_vocab.get('level')))}
        </div>
        <div style="margin-top:8px;">
            <b>Meaning:</b> {escape(str(higher_vocab.get('meaning', '')).strip())}
        </div>
        <div style="margin-top:14px;">
            <b>German sentence:</b><br>
            {escape(str(higher_vocab.get('target_sentence', '')).strip())}
        </div>
        <div style="margin-top:14px;">
            <b>English sentence:</b><br>
            {escape(str(higher_vocab.get('english_sentence', '')).strip())}
        </div>
        {higher_audio_html}
    </div>
</details>
"""

    return f"""
<div>
    <div style="display:grid;grid-template-columns:1fr auto 1fr;align-items:start;gap:18px;margin-bottom:20px;">
        <div style="text-align:left;min-width:0;">
            {higher_vocab_html}
        </div>

        <div style="font-size:28px;font-weight:bold;text-align:center;">
            {escape(values['word'])}
            {f'<span style="display:inline-block;margin-left:4em;font-size:14px;font-weight:bold;vertical-align:middle;">🔊 Word Pronunciation:</span>' if word_audio else ""}
        </div>

        <div></div>
    </div>

    <b>Meaning:</b> {escape(values['meaning'])}
    <br><br>

    <b>Part of speech:</b> {escape(values['part_of_speech'])}
    <br><br>

    <b>Article:</b> {escape(values['article'])}
    <br>

    <b>Gender:</b> {escape(values['gender'])}
    <br>

    <b>Plural:</b> {escape(values['plural'])}
    <br><br>

    <b>{escape(language)} sentence:</b>
    <br>
    {escape(values['target_sentence'])}
    <br><br>

    <b>English sentence:</b>
    <br>
    {escape(values['english_sentence'])}

    {audio_html}
</div>
"""


# =========================================================
# ROUTES
# =========================================================

@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.get("/")
def home():
    return render_template("index.html")


@app.post("/generate")
def generate():
    allowed, retry_after = rate_limit(
        "generate", GENERATION_RATE_LIMIT, GENERATION_RATE_WINDOW
    )
    if not allowed:
        return rate_limit_response(retry_after)

    data = json_data()
    try:
        word = validate_text(data.get("word"), "Word", MAX_WORD_LENGTH)
        topic = validate_text(data.get("topic"), "Topic", MAX_TOPIC_LENGTH)
        language = validate_text(data.get("language"), "Language", MAX_LANGUAGE_LENGTH)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not language:
        return jsonify({"error": "Please select a language."}), 400
    language = normalize_supported_language(language)
    if not language:
        return jsonify({"error": "That language is not supported."}), 400
    if not word and not topic:
        return jsonify({"error": "Please enter a word or topic."}), 400

    try:
        if word:
            card = generate_word_card(word, language)
            return jsonify({
                "flashcards": [card],
            })

        raw_count = data.get("card_count", 1)
        try:
            card_count = int(raw_count)
        except (TypeError, ValueError):
            card_count = 1
        card_count = max(1, min(card_count, MAX_CARD_COUNT))

        prompt = f"""
You are an expert language teacher.
Create exactly {card_count} useful vocabulary flashcards about this topic: {json.dumps(topic, ensure_ascii=False)}
Target language: {language}

Requirements:
- Every main field must be a string.
- `word` must contain the vocabulary item in the selected target language.
- `meaning` must contain its English meaning, not the target-language word.
- The exact target-language `word` (including its grammatical form) must be the vocabulary item actually used in `target_sentence`.
- `english_sentence` must accurately translate `target_sentence`.
- Determine the approximate CEFR level of each main target-language vocabulary item.
- Put exactly one of A1, A2, B1, B2, C1, or C2 into `cefr_level`.
- For German only: A1 -> one useful B1 higher vocabulary item; A2 -> one useful B2 higher vocabulary item; B1/B2/C1/C2 -> leave all `higher_vocab` fields empty.
- For non-German languages, leave all `higher_vocab` fields empty.
- `higher_vocab` must contain: level, word, meaning, target_sentence, english_sentence.
- Article/gender/plural should be filled only when applicable.
- target_sentence must be a natural target-language example sentence.
- english_sentence must accurately translate target_sentence.
- Do not invent a higher-level word merely to fill the field.
- Return only the required structured JSON object.
"""
        generated = gemini_generate(prompt, FLASHCARDS_SCHEMA)
        cards = generated.get("flashcards") if isinstance(generated, dict) else None
        if not isinstance(cards, list) or len(cards) != card_count:
            raise ValueError(
                f"The AI returned an unexpected number of flashcards; expected {card_count}."
            )

        sanitized_cards = []
        for index, card in enumerate(cards, start=1):
            if not validate_card(card, language):
                raise ValueError(f"The AI returned an invalid flashcard at position {index}.")
            sanitized_cards.append(sanitize_card(card, language))

        return jsonify({
            "flashcards": sanitized_cards,
        })

    except ValueError as exc:
        logger.warning("Generation validation failure: %s", exc)
        return jsonify({"error": str(exc)}), 422
    except RuntimeError as exc:
        logger.error("Generation service failure: %s", exc)
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logger.exception("Unexpected generation failure")
        return jsonify({"error": "Could not generate the flashcard right now."}), 500


@app.post("/pronunciation-audio")
def pronunciation_audio():
    allowed, retry_after = rate_limit(
        "tts", TTS_RATE_LIMIT, TTS_RATE_WINDOW
    )
    if not allowed:
        return rate_limit_response(retry_after)

    data = json_data()
    try:
        text = validate_text(data.get("text", data.get("word")), "Text", MAX_WORD_LENGTH + 240)
        language = validate_text(data.get("language"), "Language", MAX_LANGUAGE_LENGTH)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not text:
        return jsonify({"error": "No pronunciation text was provided."}), 400
    if not language:
        return jsonify({"error": "No language was selected."}), 400
    language = normalize_supported_language(language)
    if not language:
        return jsonify({"error": "That language is not supported."}), 400

    path = None
    try:
        path, filename = create_pronunciation_file(text, language, kind="browser", slow=True)
        with open(path, "rb") as audio_file:
            encoded = base64.b64encode(audio_file.read()).decode("ascii")
        return jsonify({
            "success": True,
            "filename": filename,
            "data": encoded,
        })
    except RuntimeError as exc:
        logger.warning("TTS failure: %s", exc)
        return jsonify({"error": str(exc)}), 502
    except Exception:
        logger.exception("Unexpected TTS failure")
        return jsonify({"error": "Could not generate pronunciation right now."}), 500
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


@app.post("/download-flashcard")
def download_flashcard():
    allowed, retry_after = rate_limit(
        "download", DOWNLOAD_RATE_LIMIT, DOWNLOAD_RATE_WINDOW
    )
    if not allowed:
        return rate_limit_response(retry_after)

    data = json_data()
    card = data.get("card")
    try:
        language = validate_text(data.get("language"), "Language", MAX_LANGUAGE_LENGTH)
        requested_deck = validate_text(data.get("deck"), "Deck", 120)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not language:
        return jsonify({"error": "Please select a language."}), 400
    language = normalize_supported_language(language)
    if not language:
        return jsonify({"error": "That language is not supported."}), 400
    if not validate_card(card, language):
        return jsonify({"error": "The flashcard data is invalid."}), 400
    card = sanitize_card(card, language)

    word_audio_file = None
    sentence_audio_file = None
    higher_word_audio_file = None
    higher_sentence_audio_file = None
    temp_apkg = None

    try:
        model = genanki.Model(
            1607392319,
            "AI Flashcards Basic Model",
            fields=[{"name": "Front"}, {"name": "Back"}],
            templates=[{
                "name": "Card 1",
                "qfmt": "{{Front}}",
                "afmt": "{{FrontSide}}<hr id='answer'>{{Back}}",
            }],
            css="""
.card {
    font-family: Arial, sans-serif;
    font-size: 20px;
    text-align: center;
    color: #ffffff;
    background-color: #2f2f2f;
    padding: 20px;
}
.card b { font-weight: bold; }
.card hr { border: 0; border-top: 1px solid #555555; margin: 20px 0; }
""",
        )

        note = genanki.Note(
            model=model,
            fields=[escape(card["word"]), ""],
        )

        # Use the user's selected deck when supplied; fall back to a deterministic
        # generated deck for the manual-import option.
        deck_name = requested_deck or f"AI Flashcards - {language}"
        deck_id = (
            int(hashlib.md5(deck_name.encode("utf-8")).hexdigest(), 16)
            % 9000000000
        ) + 1000000000
        deck = genanki.Deck(deck_id, deck_name)
        deck.add_note(note)
        package = genanki.Package(deck)

        try:
            word_audio_file, word_filename = create_pronunciation_file(
                card["word"], language, kind="word", slow=False
            )
            package.media_files.append(word_audio_file)
        except Exception as exc:
            word_filename = None
            logger.warning("Word audio skipped during APKG creation: %s", exc)

        try:
            sentence_audio_file, sentence_filename = create_pronunciation_file(
                card["target_sentence"], language, kind="sentence", slow=False
            )
            package.media_files.append(sentence_audio_file)
        except Exception as exc:
            sentence_filename = None
            logger.warning("Sentence audio skipped during APKG creation: %s", exc)

        higher_word_audio_file = None
        higher_sentence_audio_file = None
        higher_word_filename = None
        higher_sentence_filename = None

        if should_show_higher_vocab(card, language):
            higher_vocab = card["higher_vocab"]

            try:
                higher_word_audio_file, higher_word_filename = create_pronunciation_file(
                    higher_vocab["word"], language, kind="higher_word", slow=False
                )
                package.media_files.append(higher_word_audio_file)
            except Exception as exc:
                logger.warning("Higher vocab word audio skipped during APKG creation: %s", exc)

            try:
                higher_sentence_audio_file, higher_sentence_filename = create_pronunciation_file(
                    higher_vocab["target_sentence"], language, kind="higher_sentence", slow=False
                )
                package.media_files.append(higher_sentence_audio_file)
            except Exception as exc:
                logger.warning("Higher vocab sentence audio skipped during APKG creation: %s", exc)

        final_back = build_anki_back(
            card,
            language,
            word_audio=word_filename,
            sentence_audio=sentence_filename,
            higher_word_audio=higher_word_filename,
            higher_sentence_audio=higher_sentence_filename,
        )
        note.fields[1] = final_back

        temp_apkg = os.path.join(
            tempfile.gettempdir(),
            f"ai_flashcard_{uuid.uuid4().hex}.apkg",
        )
        package.write_to_file(temp_apkg)

        package_bytes = io.BytesIO(Path(temp_apkg).read_bytes())
        package_bytes.seek(0)
        filename = f"AI_Flashcard_{safe_filename(card['word'], 'flashcard', 40)}.apkg"

        return send_file(
            package_bytes,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=filename,
        )

    except Exception:
        logger.exception("APKG creation failed")
        return jsonify({"error": "Could not create the Anki package right now."}), 500
    finally:
        for path in (
            temp_apkg,
            word_audio_file,
            sentence_audio_file,
            higher_word_audio_file,
            higher_sentence_audio_file,
        ):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=FLASK_DEBUG)
