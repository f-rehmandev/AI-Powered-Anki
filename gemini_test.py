import os
import json
from google import genai

# Connect to Gemini
client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

# Ask the user for a German word
word = input("Enter a German word: ")

# Tell Gemini exactly what information we want
prompt = f"""
You are an expert German language teacher.

Create a vocabulary flashcard for the German word even it is in german or in english:

{word}

Return ONLY valid JSON.

The JSON must have exactly these fields:

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

- "word" = the German word.
- "meaning" = the English meaning.
- "part_of_speech" = noun, verb, adjective, adverb, etc.
- "article" = der, die, das, or an empty string if it is not a noun.
- "gender" = masculine, feminine, neuter, or an empty string if it is not a noun.
- "plural" = the plural form if applicable, otherwise an empty string.
- "german_sentence" = one natural German sentence around A1 or A2 level.
- "english_sentence" = the English translation of that German sentence.
- Do not include Markdown.
- Do not include explanations outside the JSON.
"""

# Ask Gemini to generate the response
response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents=prompt
)


# Get Gemini's response
text = response.text

# Convert the JSON text into Python data
card = json.loads(text)

# Display the result
print("\n--- FLASHCARD ---")

print("Word:", card["word"])
print("Meaning:", card["meaning"])
print("Part of speech:", card["part_of_speech"])
print("Article:", card["article"])
print("Gender:", card["gender"])
print("Plural:", card["plural"])
print("German sentence:", card["german_sentence"])
print("English sentence:", card["english_sentence"])