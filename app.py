from flask import Flask, render_template, request, jsonify
import os
import json
import requests
import base64
import re
import tempfile
from html import escape

from dotenv import load_dotenv
from google import genai
from gtts import gTTS
from gtts.lang import tts_langs


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
# HOME PAGE
# =========================================================

@app.route("/")
def home():

    try:

        result = anki_request("deckNames")

        if result.get("error"):

            decks = []

        else:

            decks = result.get(
                "result",
                []
            )

    except Exception:

        decks = []

    return render_template(
        "index.html",
        decks=decks
    )


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
            model="gemini-3.6-flash",
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
    # against that list.

    available_languages = tts_langs()

    language_code = None

    for code, language_name in available_languages.items():

        if language_name.lower() == language.lower():

            language_code = code
            break

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

    temp_path = os.path.join(
        tempfile.gettempdir(),
        filename
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
        # GENERATE PRONUNCIATION
        # =================================================

        pronunciation_file = create_pronunciation(
            word,
            language
        )

        pronunciation = (
            f'<div>[sound:{escape(pronunciation_file)}]</div>'
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

<b>🔊 Pronunciation:</b>

<br>

{pronunciation}

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
                "Flashcard and pronunciation added to Anki!",

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
# RUN FLASK
# =========================================================

if __name__ == "__main__":

    app.run(
        debug=True
    )