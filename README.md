# AI Powered Flashcards

A web application that turns an English word into a complete vocabulary flashcard in a target language. It uses Google Gemini to write the card, and it can add the card directly to Anki or export it as a file.

Live app: https://ai-powered-anki.vercel.app/

## What it does

You enter an English word and choose a target language. The app creates a flashcard with:

- the word in the target language
- its English meaning
- the part of speech
- article, gender and plural (when they apply)
- an example sentence in the target language
- an English translation of the example sentence
- pronunciation for the word and the sentence

You can then add the card to a local Anki deck or download it as an `.apkg` file.

## Target languages

The app supports 20 target languages: English, Spanish, French, German, Italian, Portuguese, Russian, Japanese, Korean, Chinese, Arabic, Hindi, Turkish, Dutch, Polish, Ukrainian, Swedish, Greek, Indonesian and Vietnamese.

## German vocabulary levels

German has extra support for learners. For German, the app can create vocabulary at two different levels: **A1** (beginner) and **B1/B2** (intermediate to upper intermediate). [Add 1-2 sentences here: how the level is chosen in the app, and what changes between the levels, for example the choice of words or the length of the example sentence.]

German cards also show the article, gender and plural of each noun, which helps learners remember them together with the word.

## Features

- Gemini creates the card in a defined structure (structured output).
- Pronunciation in the browser, with a server-side fallback using gTTS.
- Add cards directly to a local Anki deck through AnkiConnect, including the audio.
- Create a new Anki deck from the website.
- Duplicate check: the app looks for an existing word before it adds a new card.
- Download a complete `.apkg` package for manual import, with audio when available.
- Works on desktop and smaller screens.
- Basic input checks, rate limiting and error handling.

## How it works

```
User enters an English word
            |
            v
      Flask /generate
            |
            v
      Google Gemini
            |
            v
 Flashcard in the target language
            |
    +-------+-------+
    |               |
    v               v
Browser speech   gTTS (server)
    |               |
    +-------+-------+
            |
            v
   Anki or file download
```

Example: English word `cook`, target language German. The card uses the German word as the main word and shows its English meaning separately.

## Tech stack

| Technology | Purpose |
| --- | --- |
| Python and Flask | Backend and API routes |
| Google Gemini (`google-genai`) | Vocabulary generation |
| gTTS | Server-side pronunciation audio |
| AnkiConnect | Connection from the browser to local Anki |
| genanki | Creating `.apkg` packages |
| HTML, CSS and JavaScript | Frontend |

## Requirements

- Python 3.10 or newer
- A Google Gemini API key
- An internet connection (for Gemini and gTTS)
- Anki Desktop with the AnkiConnect add-on (only for direct Anki features)

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/f-rehmandev/AI-Powered-Anki.git
   cd AI-Powered-Anki
   ```

2. Create and activate a virtual environment (Windows PowerShell):

   ```powershell
   python -m venv ai_flashcards
   .\ai_flashcards\Scripts\Activate.ps1
   ```

3. Install the packages:

   ```bash
   pip install flask google-genai python-dotenv gTTS genanki
   ```

   Optional, for a more production-like local server: `pip install waitress`

## Settings

Create a `.env` file in the project folder:

```
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-3.5-flash-lite
PORT=5000
FLASK_DEBUG=0
```

Never commit your real `.env` file or API key. Your `.gitignore` should include:

```
.env
__pycache__/
*.pyc
.venv/
venv/
```

## Run the app

```bash
python app.py
```

Open http://127.0.0.1:5000 in your browser. Do not open `index.html` directly, because the page needs the Flask server.

## Anki connection

The Anki connection is optional. Without it, you can still create and download cards.

To set it up once:

1. Open Anki Desktop and go to **Tools > Add-ons > Get Add-ons...**
2. Enter the AnkiConnect code `2055492159` and install it.
3. Restart Anki.

When you use the website, keep Anki open and running. The browser talks directly to AnkiConnect on your own computer, and the Flask server does not need access to your Anki.

If your browser asks for permission to access local services, choose **Allow**.

### Audio in Anki

When you add a card to Anki, the server creates audio for the word and the sentence with gTTS. The browser sends the audio to AnkiConnect, Anki stores it, and the card refers to it.

### Duplicates

Before a card is added, the app checks the front word in Anki. If the word already exists, it asks whether you still want to add the card.

## API routes

| Route | Purpose |
| --- | --- |
| `GET /` | Serves the frontend |
| `POST /generate` | Creates a flashcard. Example body: `{ "word": "cook", "language": "German" }` |
| `POST /pronunciation-audio` | Creates server-side audio. Example body: `{ "text": "kocht", "language": "German" }` |
| `POST /download-flashcard` | Creates and returns an Anki `.apkg` file |

## Reliability and security

- The API key is loaded from environment variables.
- Request size and input length are limited.
- Generation, audio and downloads have in-memory rate limits.
- Generated file names are made safe, and temporary files are removed.
- Generated text is HTML-escaped before it goes into Anki fields.
- Uncertain Anki write operations are not retried automatically, to avoid duplicate cards.

For a public deployment, use a shared rate-limit store and a proper production server setup.

## Limitations

- There is no second AI step to check or correct the card. Each card is generated once, to keep the app fast and to save API calls. Check cards before you learn from them.
- Direct Anki features need Anki Desktop to be open.

## Troubleshooting

**The website cannot connect to Anki.** Check that Anki Desktop is running, AnkiConnect is installed, Anki was restarted after the installation, and your browser allowed the local connection. You can still create and download cards.

**Generation fails.** Check `GEMINI_API_KEY` and `GEMINI_MODEL` in your `.env` file, and make sure all packages are installed in your virtual environment.

**Pronunciation does not play.** For browser audio, your browser needs speech synthesis and a suitable voice. For server audio, the server must be able to reach gTTS. For Anki audio, AnkiConnect must be running.
