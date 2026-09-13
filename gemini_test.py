import os
import json

from dotenv import load_dotenv
from google import genai


# =========================================================
# LOAD .ENV
# =========================================================

load_dotenv()


# =========================================================
# CHECK API KEY
# =========================================================

api_key = os.getenv("GEMINI_API_KEY")

if not api_key:
    raise RuntimeError(
        "GEMINI_API_KEY was not found.\n"
        "Make sure your .env file contains:\n"
        "GEMINI_API_KEY=your_key_here"
    )


print("✓ Gemini API key found.")


# =========================================================
# CONNECT TO GEMINI
# =========================================================

client = genai.Client(
    api_key=api_key
)

print("✓ Gemini client created.")


# =========================================================
# GET WORD
# =========================================================

word = input("\nEnter a German or English word: ").strip()

if not word:
    raise ValueError("Please enter a word.")


# =========================================================
# PROMPT
# =========================================================

prompt = f"""
You are an expert German language teacher.

Create a vocabulary flashcard for this word:

{word}

The user may enter either:
- a German word
- or an English word

If the user enters an English word, give the appropriate German vocabulary word.

Return ONLY valid JSON.

Use exactly these fields:

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

- "word" = the German vocabulary word.
- "meaning" = the English meaning.
- "part_of_speech" = noun, verb, adjective, adverb, etc.
- "article" = der, die, das, or empty string if not applicable.
- "gender" = masculine, feminine, neuter, or empty string if not applicable.
- "plural" = the plural form if applicable, otherwise empty string.
- "german_sentence" = one natural German sentence around A1/A2 level.
- "english_sentence" = the accurate English translation.
- Do not include Markdown.
- Do not include explanations outside the JSON.
"""


# =========================================================
# CALL GEMINI
# =========================================================

print("\nCalling Gemini...")
print("Please wait...\n")

try:

    response = client.models.generate_content(
        model="gemini-3.6-flash",
        contents=prompt
    )

except Exception as error:

    print("\n❌ GEMINI REQUEST FAILED")
    print("--------------------------------")
    print(type(error).__name__)
    print(str(error))
    print("--------------------------------")

    raise


# =========================================================
# SHOW RAW RESPONSE
# =========================================================

print("✓ Gemini responded.")

print("\n--- RAW GEMINI RESPONSE ---")
print(response.text)


# =========================================================
# PARSE JSON
# =========================================================

try:

    card = json.loads(response.text)

except json.JSONDecodeError as error:

    print("\n❌ GEMINI DID NOT RETURN VALID JSON.")
    raise RuntimeError(
        "Gemini responded, but its response was not valid JSON."
    ) from error


# =========================================================
# DISPLAY CARD
# =========================================================

print("\n--- FLASHCARD ---")

print("Word:", card.get("word"))
print("Meaning:", card.get("meaning"))
print("Part of speech:", card.get("part_of_speech"))
print("Article:", card.get("article"))
print("Gender:", card.get("gender"))
print("Plural:", card.get("plural"))
print("German sentence:", card.get("german_sentence"))
print("English sentence:", card.get("english_sentence"))

print("\n✓ TEST COMPLETED SUCCESSFULLY.")