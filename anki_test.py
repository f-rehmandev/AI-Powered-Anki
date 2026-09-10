import requests

url = "http://127.0.0.1:8765"

# Change this to the EXACT name of one of your Anki decks
deck_name = "abc"

# Information for our test flashcard
word = "Bewerbung"
meaning = "application"
part_of_speech = "noun"
article = "die"
gender = "feminine"
plural = "Bewerbungen"
german_sentence = "Ich habe meine Bewerbung gestern abgeschickt."
english_sentence = "I sent my application yesterday."

# Create the card content
front = word

back = f"""
<b>Meaning:</b> {meaning}<br>
<b>Part of speech:</b> {part_of_speech}<br>
<b>Article:</b> {article}<br>
<b>Gender:</b> {gender}<br>
<b>Plural:</b> {plural}<br>
<br>
<b>German:</b><br>
{german_sentence}<br>
<br>
<b>English:</b><br>
{english_sentence}
"""

# Tell AnkiConnect to create the note
payload = {
    "action": "addNote",
    "version": 6,
    "params": {
        "note": {
            "deckName": deck_name,
            "modelName": "Basic",
            "fields": {
                "Front": front,
                "Back": back
            },
            "tags": [
                "AI-Flashcards"
            ]
        }
    }
}

response = requests.post(url, json=payload)

print(response.json())