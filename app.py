from flask import Flask, render_template, request, jsonify, send_file
import os
import json
import requests
import base64
import re
import hashlib
import tempfile
import uuid
import io
from html import escape

from dotenv import load_dotenv
from google import genai
from gtts import gTTS
from gtts.lang import tts_langs
import genanki


app = Flask(__name__)

load_dotenv()


# =========================================================
# GEMINI SETUP
# =========================================================

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"]
)


# =========================================================
# ANKI CONNECT SETUP
# =========================================================

ANKI_CONNECT_URL = "http://127.0.0.1:8765"


def anki_request(action, params=None):

    payload = {
        "action": action,
        "version": 6
    }

    if params is not None:
        payload["params"] = params

    try:

        response = requests.post(
            ANKI_CONNECT_URL,
            json=payload,
            timeout=10
        )

        response.raise_for_status()

        result = response.json()

        return result

    except requests.exceptions.ConnectionError:

        raise Exception(
            "Could not connect to AnkiConnect. "
            "Make sure Anki is running and the AnkiConnect add-on is installed."
        )

    except requests.exceptions.Timeout:

        raise Exception(
            "AnkiConnect request timed out."
        )

    except requests.exceptions.RequestException as e:

        raise Exception(
            f"AnkiConnect connection error: {str(e)}"
        )


# =========================================================
# GTTS LANGUAGE CACHE
# =========================================================
#
# tts_langs() fetches the supported-language list from Google
# over the network. Calling it on every single pronunciation
# request is slow and wasteful, so we fetch it once and reuse
# it. If the fetch fails at import time (e.g. no internet yet),
# we fall back to fetching lazily on first use.

_TTS_LANGS_CACHE = None


def get_tts_langs():

    global _TTS_LANGS_CACHE

    if _TTS_LANGS_CACHE is None:

        _TTS_LANGS_CACHE = tts_langs()

    return _TTS_LANGS_CACHE


def get_language_code(language):

    available_languages = get_tts_langs()

    for code, language_name in available_languages.items():

        if language_name.lower() == language.lower():

            return code

    return None


# =========================================================
# HOME PAGE
# =========================================================

@app.route("/")
def home():
    return render_template("index.html")


# =========================================================
# GET ANKI DECKS
# =========================================================

@app.route("/decks")
def get_decks():

    try:

        result = anki_request("deckNames")

        if result.get("error"):

            return jsonify({
                "error": result["error"]
            }), 500

        decks = result.get(
            "result",
            []
        )

        return jsonify({
            "decks": decks
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


# =========================================================
# CREATE NEW ANKI DECK
# =========================================================

@app.route("/create-deck", methods=["POST"])
def create_deck():

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "No data received."
        }), 400

    deck_name = str(
        data.get(
            "deck_name",
            ""
        )
    ).strip()

    if not deck_name:

        return jsonify({
            "error": "Please enter a deck name."
        }), 400

    try:

        result = anki_request(
            "createDeck",
            {
                "deck": deck_name
            }
        )

        if result.get("error"):

            return jsonify({
                "error": result["error"]
            }), 500

        return jsonify({
            "success": True,
            "deck": deck_name
        })

    except Exception as e:

        return jsonify({
            "error": str(e)
        }), 500


# =========================================================
# CLEAN GEMINI RESPONSE
# =========================================================

def clean_json_response(text):

    if not text:

        raise ValueError(
            "Gemini returned an empty response."
        )

    text = text.strip()

    # Remove Markdown code fences if Gemini
    # accidentally adds them.

    if text.startswith("```"):

        lines = text.splitlines()

        if (
            lines
            and lines[0].strip().lower()
            in ("```json", "```")
        ):

            lines = lines[1:]

        if (
            lines
            and lines[-1].strip() == "```"
        ):

            lines = lines[:-1]

        text = "\n".join(lines).strip()

    return text


# =========================================================
# VALIDATE FLASHCARD
# =========================================================

def validate_card(card):

    if not isinstance(card, dict):

        return False

    required_fields = [
        "word",
        "meaning",
        "part_of_speech",
        "article",
        "gender",
        "plural",
        "german_sentence",
        "english_sentence"
    ]

    for field in required_fields:

        if field not in card:

            return False

        if not isinstance(
            card[field],
            str
        ):

            return False

    if not card["word"].strip():

        return False

    return True


# =========================================================
# GENERATE FLASHCARD
# =========================================================

@app.route("/generate", methods=["POST"])
def generate():

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "No data received."
        }), 400

    word = str(
    data.get(
        "word",
        ""
    )
).strip()
    topic = str(
        data.get(
            "topic",
            ""
        )
    ).strip()

    language = str(
        data.get(
            "language",
            ""
        )
    ).strip()

    if not language:

        return jsonify({
            "error": "Please select a language."
        }), 400

    user_input = word if word else topic

    if not user_input:

        return jsonify({
            "error": "Please enter a word or topic."
        }), 400


    # =====================================================
    # CARD COUNT
    # =====================================================

    card_count = data.get(
        "card_count",
        1
    )

    try:

        card_count = int(card_count)

    except (TypeError, ValueError):

        card_count = 1

    # Prevent unreasonable requests.

    card_count = max(
        1,
        min(card_count, 50)
    )


    # =====================================================
    # SINGLE WORD
    # =====================================================

    if word:

        prompt = f"""
You are an expert language teacher.

Create exactly ONE vocabulary flashcard for this word:

{word}

The target language is:
{language}

The word must be analyzed and presented as vocabulary for learning the target language.

Return ONLY a valid JSON object.

Do not return Markdown.
Do not return ```json.
Do not add any explanation before or after the JSON.

Use exactly these fields, all as strings:

{{
    "word": "",
    "meaning": "",
    "part_of_speech": "",
    "article": "",
    "gender": "",
    "plural": "",
    "german_sentence": "",
    "english_sentence": ""
}}

Rules:

1. "word" must contain the vocabulary word requested by the user.
2. "meaning" must contain its English meaning.
3. "part_of_speech" must be Noun, Verb, Adjective, Adverb, etc.
4. If the target language uses articles, provide the correct article when applicable.
5. If the target language has grammatical gender and it applies, provide it.
6. If the word has a plural form, provide it.
7. If article, gender, or plural does not apply, use an empty string.
8. Create a natural example sentence in the target language appropriate for a language learner.
9. Provide the English translation of that sentence.
10. Every field must be a JSON string.
11. "german_sentence" must contain the example sentence in the target language. The field name is kept only for compatibility with the existing application.
12. "english_sentence" must contain the English translation.
13. Return ONLY the JSON object.
"""


    # =====================================================
    # TOPIC
    # =====================================================

    else:

        prompt = f"""
You are an expert language teacher.

Create exactly {card_count} useful vocabulary flashcards
about this topic:

{topic}

The target language is:
{language}

Create vocabulary that is useful for a learner of this target language.

Return ONLY a valid JSON object.

Do not return Markdown.
Do not return ```json.
Do not add any explanation before or after the JSON.

Use exactly this structure:

{{
    "flashcards": [
        {{
            "word": "",
            "meaning": "",
            "part_of_speech": "",
            "article": "",
            "gender": "",
            "plural": "",
            "german_sentence": "",
            "english_sentence": ""
        }}
    ]
}}

Rules:

1. Return exactly {card_count} flashcards.
2. Every field must be a JSON string.
3. "word" must be a vocabulary word in the target language.
4. "meaning" must be its English meaning.
5. "part_of_speech" must be Noun, Verb, Adjective, Adverb, etc.
6. If the target language uses articles, provide the correct article when applicable.
7. If the target language has grammatical gender and it applies, provide it.
8. If the word has a plural form, provide it.
9. If article, gender, or plural does not apply, use an empty string.
10. Create natural example sentences in the target language appropriate for a language learner.
11. Provide English translations.
12. "german_sentence" must contain the example sentence in the target language. The field name is kept only for compatibility with the existing application.
13. "english_sentence" must contain the English translation.
14. Return ONLY the JSON object.
"""


    # =====================================================
    # CALL GEMINI
    # =====================================================

    try:

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt
        )

        text = clean_json_response(
            response.text
        )

        result = json.loads(text)


        # =================================================
        # SINGLE WORD
        # =================================================

        if word:

            if not validate_card(result):

                raise ValueError(
                    "Gemini returned an invalid flashcard."
                )

            return jsonify({
                "flashcards": [
                    result
                ]
            })


        # =================================================
        # TOPIC
        # =================================================

        if not isinstance(
            result,
            dict
        ):

            raise ValueError(
                "Gemini returned an invalid flashcard response."
            )

        flashcards = result.get(
            "flashcards"
        )

        if not isinstance(
            flashcards,
            list
        ):

            raise ValueError(
                "Gemini response does not contain a valid flashcards array."
            )

        if len(flashcards) != card_count:

            raise ValueError(
                f"Gemini returned {len(flashcards)} cards "
                f"instead of {card_count}."
            )

        for card in flashcards:

            if not validate_card(card):

                raise ValueError(
                    "Gemini returned an invalid flashcard."
                )

        return jsonify({
            "flashcards": flashcards
        })


    except json.JSONDecodeError:

        return jsonify({
            "error": "Gemini returned invalid JSON.",
            "details": text if "text" in locals() else ""
        }), 500


    except Exception as e:

        return jsonify({
            "error": f"Gemini error: {str(e)}"
        }), 500


# =========================================================
# GENERATE PRONUNCIATION AUDIO
# =========================================================

def create_pronunciation(word, language):

    if not word:

        raise Exception(
            "Cannot generate pronunciation because the word is empty."
        )

    # gTTS contains a built-in list of supported languages.
    # We match the language name selected by the user
    # against that list (cached after the first lookup).

    language_code = get_language_code(language)

    if not language_code:

        raise Exception(
            f"Pronunciation is not currently available for {language}. "
            "The flashcard can still be generated, but this language is not supported by gTTS."
        )

    # -----------------------------------------------------
    # Create safe filename
    # -----------------------------------------------------

    safe_word = re.sub(
        r"[^a-zA-Z0-9äöüÄÖÜß_-]",
        "_",
        word
    )

    safe_language = re.sub(
        r"[^a-zA-Z0-9_-]",
        "_",
        language.lower()
    )

    filename = f"{safe_language}_{safe_word}.mp3"

    # A unique temp path avoids two concurrent requests for the
    # same word/language colliding on the same file on disk.

    temp_path = os.path.join(
        tempfile.gettempdir(),
        f"{uuid.uuid4().hex}_{filename}"
    )


    try:

        # -------------------------------------------------
        # Generate pronunciation
        # -------------------------------------------------

        tts = gTTS(
            text=word,
            lang=language_code,
            slow=False
        )

        tts.save(temp_path)


        # -------------------------------------------------
        # Read MP3 and convert to Base64
        # -------------------------------------------------

        with open(
            temp_path,
            "rb"
        ) as audio_file:

            audio_base64 = base64.b64encode(
                audio_file.read()
            ).decode("utf-8")


        # -------------------------------------------------
        # Store audio inside Anki media collection
        # -------------------------------------------------

        result = anki_request(
            "storeMediaFile",
            {
                "filename": filename,
                "data": audio_base64
            }
        )

        if result.get("error"):

            raise Exception(
                result["error"]
            )

        return filename


    finally:

        # Remove temporary MP3 from computer.

        if os.path.exists(temp_path):

            try:

                os.remove(temp_path)

            except OSError:

                pass


# =========================================================
# GENERATE PRONUNCIATION AUDIO FOR BROWSER
# =========================================================

@app.route("/pronunciation-audio", methods=["POST"])
def pronunciation_audio():

    data = request.get_json()

    if not data:
        return jsonify({
            "error": "No pronunciation data received."
        }), 400

    text = str(
    data.get(
        "text",
        data.get("word", "")
    )
).strip()

    language = str(
        data.get(
            "language",
            ""
        )
    ).strip()

    if not text:
       return jsonify({
        "error": "No pronunciation text was provided."
    }), 400

    if not language:
        return jsonify({
            "error": "No language was selected."
        }), 400

    try:

        language_code = get_language_code(
            language
        )

        if not language_code:

            return jsonify({
                "error":
                    f"Pronunciation is not currently available for {language}."
            }), 400


        # -------------------------------------------------
        # Create a safe filename
        # -------------------------------------------------

        safe_word = re.sub(
            r"[^a-zA-Z0-9äöüÄÖÜß_-]",
            "_",
            text
        )

        safe_language = re.sub(
            r"[^a-zA-Z0-9_-]",
            "_",
            language.lower()
        )

        filename = (
            f"{safe_language}_{safe_word}.mp3"
        )


        # -------------------------------------------------
        # Create unique temporary MP3
        # -------------------------------------------------

        temp_path = os.path.join(
            tempfile.gettempdir(),
            f"{uuid.uuid4().hex}_{filename}"
        )


        try:

            # -------------------------------------------------
            # Generate pronunciation
            # -------------------------------------------------

            tts = gTTS(
                text=text,
                lang=language_code,
                slow=True
            )

            tts.save(
                temp_path
            )


            # -------------------------------------------------
            # Convert MP3 to Base64
            # -------------------------------------------------

            with open(
                temp_path,
                "rb"
            ) as audio_file:

                audio_base64 = base64.b64encode(
                    audio_file.read()
                ).decode("utf-8")


            return jsonify({

                "success":
                    True,

                "filename":
                    filename,

                "data":
                    audio_base64

            })


        finally:

            # Remove temporary MP3.

            if os.path.exists(temp_path):

                try:

                    os.remove(
                        temp_path
                    )

                except OSError:

                    pass


    except Exception as e:

        return jsonify({

            "error":
                f"Could not generate pronunciation: {str(e)}"

        }), 500

# =========================================================
# CREATE PRONUNCIATION FILE FOR ANKI PACKAGE
# =========================================================

def create_pronunciation_file(word, language):

    if not word:
        raise Exception(
            "Cannot generate pronunciation because the word is empty."
        )

    # Find the gTTS language code from the selected
    # language name (cached after the first lookup).

    language_code = get_language_code(language)

    if not language_code:

        raise Exception(
            f"Pronunciation is not currently available for {language}."
        )

    # Create a safe filename.

    safe_word = re.sub(
        r"[^a-zA-Z0-9äöüÄÖÜß_-]",
        "_",
        word
    )

    safe_language = re.sub(
        r"[^a-zA-Z0-9_-]",
        "_",
        language.lower()
    )

    filename = f"{safe_language}_{safe_word}.mp3"

    # A unique temp path avoids two concurrent requests colliding
    # on the same file on disk.

    temp_path = os.path.join(
        tempfile.gettempdir(),
        f"{uuid.uuid4().hex}_{filename}"
    )

    # Generate the MP3.

    tts = gTTS(
        text=word,
        lang=language_code,
        slow=False
    )

    tts.save(temp_path)

    return temp_path


# =========================================================
# CHECK FOR DUPLICATE WORD IN ANKI
# =========================================================

def find_duplicate_word(deck, word):

    if not word:
        return None


    # -----------------------------------------------------
    # SEARCH THE ENTIRE ANKI COLLECTION
    # -----------------------------------------------------

    safe_word = str(
        word
    ).replace(
        '"',
        '\\"'
    )


    result = anki_request(
        "findNotes",
        {
            "query": f'front:"{safe_word}"'
        }
    )


    if result.get("error"):

        raise Exception(
            result["error"]
        )


    note_ids = result.get(
        "result",
        []
    )


    if not note_ids:

        return None


    # -----------------------------------------------------
    # GET NOTE INFORMATION
    # -----------------------------------------------------

    notes_result = anki_request(
        "notesInfo",
        {
            "notes": note_ids
        }
    )


    if notes_result.get("error"):

        raise Exception(
            notes_result["error"]
        )


    notes = notes_result.get(
        "result",
        []
    )


    target_word = (
        str(word)
        .strip()
        .casefold()
    )


    selected_deck_duplicate = None
    other_deck_duplicate = None


    # -----------------------------------------------------
    # CHECK EACH MATCH
    # -----------------------------------------------------

    for note in notes:

        fields = note.get(
            "fields",
            {}
        )


        front_field = fields.get(
            "Front"
        )


        if not front_field:

            continue


        existing_word = str(
            front_field.get(
                "value",
                ""
            )
        ).strip()


        # Remove HTML formatting.
        existing_word = re.sub(
            r"<[^>]*>",
            "",
            existing_word
        ).strip()


        # Compare case-insensitively.
        if existing_word.casefold() != target_word:

            continue


        # -------------------------------------------------
        # FIND THE DECK
        # -------------------------------------------------

        card_ids = note.get(
            "cards",
            []
        )


        existing_deck = None


        if card_ids:

            card_info_result = anki_request(
                "cardsInfo",
                {
                    "cards": card_ids
                }
            )


            if not card_info_result.get("error"):

                card_info = card_info_result.get(
                    "result",
                    []
                )


                if card_info:

                    existing_deck = card_info[0].get(
                        "deckName"
                    )


        duplicate_info = {
            "note_id": note.get(
                "noteId"
            ),

            "word": existing_word,

            "deck": existing_deck
        }


        # -------------------------------------------------
        # PRIORITIZE SELECTED DECK
        # -------------------------------------------------

        if existing_deck == deck:

            selected_deck_duplicate = duplicate_info

            break


        # Keep the first matching card from another deck
        # as a fallback.
        if other_deck_duplicate is None:

            other_deck_duplicate = duplicate_info


    # -----------------------------------------------------
    # RETURN SELECTED-DECK MATCH FIRST
    # -----------------------------------------------------

    if selected_deck_duplicate:

        return selected_deck_duplicate


    if other_deck_duplicate:

        return other_deck_duplicate


    return None



# =========================================================
# CHECK DUPLICATE FROM WEBSITE
# =========================================================

@app.route("/check-duplicate", methods=["POST"])
def check_duplicate():

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "No duplicate-check data received."
        }), 400


    deck = str(
        data.get(
            "deck",
            ""
        )
    ).strip()


    word = str(
        data.get(
            "word",
            ""
        )
    ).strip()


    if not deck:

        return jsonify({
            "error": "No Anki deck was selected."
        }), 400


    if not word:

        return jsonify({
            "error": "No word was provided."
        }), 400


    try:

        duplicate = find_duplicate_word(
            deck,
            word
        )


        if duplicate:
            return jsonify({

        "duplicate": True,

        "word":
            duplicate["word"],

        "note_id":
            duplicate["note_id"],

        "deck":
            duplicate["deck"]

    })

        return jsonify({

            "duplicate": False

        })


    except Exception as e:

        return jsonify({

            "error":
                f"Could not check for duplicates: {str(e)}"

        }), 500

# =========================================================
# ADD FLASHCARD TO ANKI
# =========================================================

@app.route("/add-to-anki", methods=["POST"])
def add_to_anki():

    data = request.get_json()

    if not data:

        return jsonify({
            "error": "No card data received."
        }), 400


    deck = str(
        data.get(
            "deck",
            ""
        )
    ).strip()

    card = data.get(
        "card"
    )

    language = str(
        data.get(
            "language",
            ""
        )
    ).strip()


    if not deck:

        return jsonify({
            "error": "No Anki deck was selected."
        }), 400


    if not language:

        return jsonify({
            "error": "No language was selected."
        }), 400


    if not isinstance(
        card,
        dict
    ):

        return jsonify({
            "error": "No valid flashcard data was received."
        }), 400


    try:

        # =================================================
        # CHECK DECK
        # =================================================

        decks_result = anki_request(
            "deckNames"
        )

        if decks_result.get("error"):

            raise Exception(
                decks_result["error"]
            )

        decks = decks_result.get(
            "result",
            []
        )

        if deck not in decks:

            return jsonify({
                "error":
                    f'Anki deck "{deck}" does not exist.'
            }), 400


        # =================================================
        # CHECK BASIC NOTE TYPE
        # =================================================

        models_result = anki_request(
            "modelNames"
        )

        if models_result.get("error"):

            raise Exception(
                models_result["error"]
            )

        models = models_result.get(
            "result",
            []
        )

        if "Basic" not in models:

            return jsonify({
                "error":
                    'The Anki note type "Basic" was not found.'
            }), 500


        # =================================================
        # GET CARD DATA
        # =================================================

        word = str(
            card.get(
                "word",
                ""
            )
        ).strip()

        meaning = str(
            card.get(
                "meaning",
                ""
            )
        ).strip()

        part_of_speech = str(
            card.get(
                "part_of_speech",
                ""
            )
        ).strip()

        article = str(
            card.get(
                "article",
                ""
            )
        ).strip()

        gender = str(
            card.get(
                "gender",
                ""
            )
        ).strip()

        plural = str(
            card.get(
                "plural",
                ""
            )
        ).strip()

        target_sentence = str(
            card.get(
                "german_sentence",
                ""
            )
        ).strip()

        english_sentence = str(
            card.get(
                "english_sentence",
                ""
            )
        ).strip()


        if not word:

            return jsonify({
                "error":
                    "The generated flashcard has no word."
            }), 400


        # =================================================
        # GENERATE PRONUNCIATION (non-fatal)
        # =================================================
        #
        # If gTTS/AnkiConnect media storage fails (unsupported
        # language, no internet, etc.) we still want the note
        # itself to be saved — just without audio — rather than
        # failing the whole request.

        pronunciation_html = ""

        try:

            pronunciation_file = create_pronunciation(
                word,
                language
            )

            pronunciation_html = (
                "<b>🔊 Pronunciation:</b>"
                "<br>"
                f'<div>[sound:{escape(pronunciation_file)}]</div>'
            )

        except Exception as pronunciation_error:

            pronunciation_file = None

            pronunciation_html = (
                "<b>🔊 Pronunciation:</b>"
                "<br>"
                f"<div>{escape(str(pronunciation_error))}</div>"
            )


        # =================================================
        # ESCAPE HTML
        # =================================================

        word_html = escape(
            word
        )

        meaning_html = escape(
            meaning
        )

        part_html = escape(
            part_of_speech
        )

        article_html = escape(
            article
        )

        gender_html = escape(
            gender
        )

        plural_html = escape(
            plural
        )

        target_sentence_html = escape(
            target_sentence
        )

        english_sentence_html = escape(
            english_sentence
        )


        # =================================================
        # BUILD ANKI BACK
        # =================================================

        back = f"""
<div>

<div style="font-size: 28px; font-weight: bold; margin-bottom: 20px;">
{word_html}
</div>

<b>Meaning:</b>
{meaning_html}

<br><br>

<b>Part of speech:</b>
{part_html}

<br><br>

<b>Article:</b>
{article_html}

<br>

<b>Gender:</b>
{gender_html}

<br>

<b>Plural:</b>
{plural_html}

<br><br>

<b>{escape(language)} sentence:</b>

<br>

{target_sentence_html}

<br><br>

<b>English sentence:</b>

<br>

{english_sentence_html}

<br><br>

{pronunciation_html}

</div>
"""


        # =================================================
        # CREATE SAFE LANGUAGE TAG
        # =================================================

        language_tag = re.sub(
            r"[^a-zA-Z0-9_-]",
            "-",
            language.lower()
        )


        # =================================================
        # ADD NOTE
        # =================================================

        result = anki_request(
            "addNote",
            {
                "note": {

                    "deckName": deck,

                    "modelName": "Basic",

                    "fields": {

                        "Front": word_html,

                        "Back": back
                    },

                    "options": {

                        "allowDuplicate": False
                    },

                    "tags": [
                        "ai-flashcards",
                        f"language-{language_tag}"
                    ]
                }
            }
        )


        # =================================================
        # CHECK ANKI RESULT
        # =================================================

        if result.get("error"):

            return jsonify({
                "error": result["error"]
            }), 500


        note_id = result.get(
            "result"
        )


        if not note_id:

            return jsonify({
                "error":
                    "Anki did not return a note ID.",
                "details": result
            }), 500


        # =================================================
        # SUCCESS
        # =================================================

        return jsonify({

            "success": True,

            "message":
                "Flashcard and pronunciation added to Anki!"
                if pronunciation_file
                else "Flashcard added to Anki (pronunciation unavailable).",

            "note_id":
                note_id,

            "audio":
                pronunciation_file
        })


    except Exception as e:

        return jsonify({

            "error":
                f"Could not add the flashcard to Anki: {str(e)}"

        }), 500


# =========================================================
# DOWNLOAD FLASHCARD AS ANKI PACKAGE
# =========================================================

@app.route("/download-flashcard", methods=["POST"])
def download_flashcard():

    data = request.get_json()

    if not data:
        return jsonify({
            "error": "No flashcard data received."
        }), 400

    card = data.get("card")

    language = str(
        data.get(
            "language",
            ""
        )
    ).strip()

    if not isinstance(card, dict):
        return jsonify({
            "error": "No valid flashcard data was received."
        }), 400

    if not language:
        return jsonify({
            "error": "No language was selected."
        }), 400

    # -----------------------------------------------------
    # GET CARD DATA
    # -----------------------------------------------------

    word = str(
        card.get(
            "word",
            ""
        )
    ).strip()

    meaning = str(
        card.get(
            "meaning",
            ""
        )
    ).strip()

    part_of_speech = str(
        card.get(
            "part_of_speech",
            ""
        )
    ).strip()

    article = str(
        card.get(
            "article",
            ""
        )
    ).strip()

    gender = str(
        card.get(
            "gender",
            ""
        )
    ).strip()

    plural = str(
        card.get(
            "plural",
            ""
        )
    ).strip()

    target_sentence = str(
        card.get(
            "german_sentence",
            ""
        )
    ).strip()

    english_sentence = str(
        card.get(
            "english_sentence",
            ""
        )
    ).strip()

    if not word:
        return jsonify({
            "error": "The flashcard has no word."
        }), 400

    try:

        # -------------------------------------------------
        # CREATE ANKI MODEL
        # -------------------------------------------------

        model = genanki.Model(
            1607392319,
            "AI Flashcards Basic Model",
            fields=[
                {
                    "name": "Front"
                },
                {
                    "name": "Back"
                }
            ],
            templates=[
                {
                    "name": "Card 1",
                    "qfmt": "{{Front}}",
                    "afmt": "{{FrontSide}}<hr id='answer'>{{Back}}"
                }
            ],
            css="""
.card {
    font-family: Arial, sans-serif;
    font-size: 20px;
    text-align: center;
    color: #ffffff;
    background-color: #2f2f2f;
    padding: 20px;
}

.card b {
    font-weight: bold;
}

.card hr {
    border: 0;
    border-top: 1px solid #555555;
    margin: 20px 0;
}
"""
        )

        # -------------------------------------------------
        # BUILD BACK OF CARD
        # -------------------------------------------------

        word_html = escape(word)

        meaning_html = escape(
            meaning
        )

        part_html = escape(
            part_of_speech
        )

        article_html = escape(
            article
        )

        gender_html = escape(
            gender
        )

        plural_html = escape(
            plural
        )

        target_sentence_html = escape(
            target_sentence
        )

        english_sentence_html = escape(
            english_sentence
        )

        back = f"""
<div>

<div style="font-size: 28px; font-weight: bold; margin-bottom: 20px;">
{word_html}
</div>

<b>Meaning:</b>
{meaning_html}

<br><br>

<b>Part of speech:</b>
{part_html}

<br><br>

<b>Article:</b>
{article_html}

<br>

<b>Gender:</b>
{gender_html}

<br>

<b>Plural:</b>
{plural_html}

<br><br>

<b>{escape(language)} sentence:</b>

<br>

{target_sentence_html}

<br><br>

<b>English sentence:</b>

<br>

{english_sentence_html}

</div>
"""

        # -------------------------------------------------
        # CREATE NOTE
        # -------------------------------------------------

        note = genanki.Note(
            model=model,
            fields=[
                word_html,
                back
            ]
        )

        # -------------------------------------------------
        # CREATE DECK
        # -------------------------------------------------

        deck_id = (
            int(
                hashlib.md5(
                    f"AI Flashcards {language}".encode("utf-8")
                ).hexdigest(),
                16
            ) % 9000000000
        ) + 1000000000

        deck = genanki.Deck(
            deck_id,
            f"AI Flashcards - {language}"
        )

        deck.add_note(
            note
        )

        # -------------------------------------------------
        # CREATE PACKAGE
        # -------------------------------------------------

        package = genanki.Package(
            deck
        )

        # -------------------------------------------------
        # GENERATE AUDIO
        # -------------------------------------------------

        word_pronunciation_file = None

        sentence_pronunciation_file = None


        # -------------------------------------------------
        # WORD PRONUNCIATION
        # -------------------------------------------------

        try:

            word_pronunciation_file = create_pronunciation_file(
                word,
                language
            )

        except Exception as audio_error:

            print(
                "Word pronunciation could not be generated:",
                audio_error
            )


        # -------------------------------------------------
        # SENTENCE PRONUNCIATION
        # -------------------------------------------------

        try:

            sentence_pronunciation_file = create_pronunciation_file(
                target_sentence,
                language
            )

        except Exception as audio_error:

            print(
                "Sentence pronunciation could not be generated:",
                audio_error
            )


        # -------------------------------------------------
        # ADD AUDIO TO PACKAGE
        # -------------------------------------------------

        back_with_audio = back


        if word_pronunciation_file:

            package.media_files.append(
                word_pronunciation_file
            )

            back_with_audio += (
                "<br><br>"
                "<b>🔊 Word Pronunciation:</b>"
                "<br>"
                f"[sound:{os.path.basename(word_pronunciation_file)}]"
            )


        if sentence_pronunciation_file:

            package.media_files.append(
                sentence_pronunciation_file
            )

            back_with_audio += (
                "<br><br>"
                "<b>🔊 Sentence Pronunciation:</b>"
                "<br>"
                f"[sound:{os.path.basename(sentence_pronunciation_file)}]"
            )


        # Update the note with the audio references.
        note.fields[1] = back_with_audio


        # -------------------------------------------------
        # WRITE PACKAGE TO MEMORY
        # -------------------------------------------------

        package_bytes = io.BytesIO()

        temp_apkg = os.path.join(
            tempfile.gettempdir(),
            f"ai_flashcard_download_{uuid.uuid4().hex}.apkg"
        )

        package.write_to_file(
            temp_apkg
        )

        with open(
            temp_apkg,
            "rb"
        ) as package_file:

            package_bytes.write(
                package_file.read()
            )

        package_bytes.seek(0)


        # -------------------------------------------------
        # CLEAN TEMPORARY FILES
        # -------------------------------------------------

        if os.path.exists(
            temp_apkg
        ):

            try:

                os.remove(
                    temp_apkg
                )

            except OSError:

                pass


        if (
            word_pronunciation_file
            and os.path.exists(
                word_pronunciation_file
            )
        ):

            try:

                os.remove(
                    word_pronunciation_file
                )

            except OSError:

                pass


        if (
            sentence_pronunciation_file
            and os.path.exists(
                sentence_pronunciation_file
            )
        ):

            try:

                os.remove(
                    sentence_pronunciation_file
                )

            except OSError:

                pass


        # -------------------------------------------------
        # DOWNLOAD FILENAME
        # -------------------------------------------------

        safe_word = re.sub(
            r"[^a-zA-Z0-9_-]",
            "_",
            word
        )

        filename = (
            f"AI_Flashcard_{safe_word}.apkg"
        )


        return send_file(
            package_bytes,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=filename
        )


    except Exception as e:

        return jsonify({
            "error":
                f"Could not create Anki package: {str(e)}"
        }), 500


# =========================================================
# RUN FLASK
# =========================================================

if __name__ == "__main__":

    app.run(
        debug=True
    )