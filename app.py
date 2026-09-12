import base64
import hashlib
import html
import io
import json
import logging
import os
import re
import tempfile
import time
import uuid
from collections import defaultdict, deque
from html import escape
from pathlib import Path
from threading import Lock

import genanki
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
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


# =========================================================
# CONFIGURATION
# =========================================================

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
ANKI_CONNECT_URL = os.getenv("ANKI_CONNECT_URL", "http://127.0.0.1:8765").strip()
FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"

def env_int(name, default, minimum=1):
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger_temp = logging.getLogger("ai_flashcards.config")
        logger_temp.warning("Invalid integer for %s=%r; using %s.", name, raw, default)
        value = default
    return max(minimum, value)

PORT = env_int("PORT", 5000, minimum=1)
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai_flashcards")

_gemini_client = None
_tts_languages_cache = None
_rate_lock = Lock()
_rate_buckets = defaultdict(deque)

CARD_FIELDS = (
    "word",
    "meaning",
    "part_of_speech",
    "article",
    "gender",
    "plural",
    "target_sentence",
    "english_sentence",
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

# =========================================================
# HELPERS
# =========================================================


def json_data():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def client_identifier():
    # Use the immediate peer by default. If you deploy behind a trusted
    # reverse proxy, configure the proxy so REMOTE_ADDR is reliable.
    return request.remote_addr or "unknown"


def rate_limit(bucket_name, limit, window_seconds):
    now = time.monotonic()
    key = (bucket_name, client_identifier())
    with _rate_lock:
        bucket = _rate_buckets[key]
        cutoff = now - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            retry_after = max(1, int(window_seconds - (now - bucket[0])))
            return False, retry_after
        bucket.append(now)
        return True, 0


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
    _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
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
    },
    "required": list(CARD_FIELDS),
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
                temperature=0.2,
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


def validate_card(card):
    if not isinstance(card, dict):
        return False
    if any(not isinstance(card.get(field, ""), str) for field in CARD_FIELDS):
        return False
    return all(
        card.get(field, "").strip()
        for field in ("word", "meaning", "part_of_speech", "target_sentence", "english_sentence")
    )



def generate_word_card(word, language):
    source_word_json = json.dumps(word, ensure_ascii=False)
    prompt = f"""
You are an expert language teacher creating a vocabulary flashcard.
Create exactly ONE useful flashcard from the learner's English input word.

English input word: {source_word_json}
Target language: {language}

IMPORTANT LANGUAGE DIRECTION:
- The learner enters the vocabulary word in English.
- Translate that English input into the selected target language.
- The `word` field must contain the target-language vocabulary item, NOT the original
  English input.
- The `meaning` field must contain the English meaning of the target-language word.
- Do NOT put a target-language translation into the `meaning` field.

Example:
English input: "cook"
Target language: German
A correct result has a German word/form in `word` (for example, "kocht" when that
form is used in the example sentence) and an English meaning such as "cook" or
"to cook" in `meaning`.

The target-language word/form should naturally match the example sentence. If an
inflected form is used in the sentence, the `word` field may use that same form.

Rules:
- Use an accurate English meaning.
- Use the correct part of speech.
- Fill article/gender/plural when they genuinely apply; otherwise use an empty string.
- target_sentence must be a natural example sentence in the target language and
  should use the exact target-language form shown in `word`.
- english_sentence must accurately translate target_sentence.
- Every field must be a string.
Return only the required structured JSON object.
"""

    card = gemini_generate(prompt, FLASHCARD_SCHEMA)
    if not isinstance(card, dict):
        raise ValueError("The AI returned an invalid flashcard object.")

    if not validate_card(card):
        raise ValueError("The AI returned an invalid flashcard structure.")
    return card


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
    # The filesystem basename must exactly match the filename referenced by
    # Anki's [sound:...] tag. The filename itself is already unique.
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


def build_anki_back(card, language, word_audio=None, sentence_audio=None):
    values = card_values(card)
    audio_html = ""
    if word_audio:
        audio_html += (
            "<br><br><b>🔊 Word Pronunciation:</b><br>"
            f"[sound:{escape(word_audio)}]"
        )
    if sentence_audio:
        audio_html += (
            "<br><br><b>🔊 Sentence Pronunciation:</b><br>"
            f"[sound:{escape(sentence_audio)}]"
        )

    return f"""
<div>
<div style="font-size: 28px; font-weight: bold; margin-bottom: 20px;">
{escape(values['word'])}
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
    if language.title() not in SUPPORTED_LANGUAGES:
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
- Every field must be a string.
- Use useful vocabulary rather than repeated variants.
- Use accurate English meanings.
- Article/gender/plural should be filled only when applicable.
- target_sentence must be a natural target-language example sentence.
- english_sentence must accurately translate target_sentence.
Return only the required structured JSON object.
"""
        generated = gemini_generate(prompt, FLASHCARDS_SCHEMA)
        cards = generated.get("flashcards") if isinstance(generated, dict) else None
        if not isinstance(cards, list) or len(cards) != card_count:
            raise ValueError(
                f"The AI returned an unexpected number of flashcards; expected {card_count}."
            )

        for index, card in enumerate(cards, start=1):
            if not validate_card(card):
                raise ValueError(f"The AI returned an invalid flashcard at position {index}.")

        return jsonify({
            "flashcards": cards,
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
    if language.title() not in SUPPORTED_LANGUAGES:
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
    if language.title() not in SUPPORTED_LANGUAGES:
        return jsonify({"error": "That language is not supported."}), 400
    if not validate_card(card):
        return jsonify({"error": "The flashcard data is invalid."}), 400

    word_audio_file = None
    sentence_audio_file = None
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

        back = build_anki_back(card, language)
        note = genanki.Note(
            model=model,
            fields=[escape(card["word"]), back],
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

        final_back = build_anki_back(
            card,
            language,
            word_audio=word_filename,
            sentence_audio=sentence_filename,
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
        for path in (temp_apkg, word_audio_file, sentence_audio_file):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=FLASK_DEBUG)
