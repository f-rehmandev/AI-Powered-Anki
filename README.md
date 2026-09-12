AI Powered Flashcards

An AI-powered vocabulary flashcard web application that turns an English word into a complete flashcard in a selected target language.

The project combines Flask, Google Gemini, gTTS, AnkiConnect, and genanki to provide AI-generated vocabulary cards with example sentences, translations, pronunciation, and Anki integration.

Features

Enter an English word and choose a target language.

Generate a target-language vocabulary entry with Gemini.

Generate:

Target-language word

English meaning

Part of speech

Article, gender, and plural when applicable

Example sentence in the target language

English translation of the example sentence

Browser pronunciation for the word and example sentence.

Server-side pronunciation using gTTS as a fallback.

Add generated cards directly to a local Anki deck through AnkiConnect.

Store word and sentence pronunciation audio in Anki when available.

Detect an existing word before adding a duplicate card.

Create a new Anki deck directly from the website.

Download a complete .apkg Anki package for manual import.

Downloaded cards can include pronunciation audio.

Responsive interface for desktop and smaller screens.

Basic request validation, rate limiting, and error handling.

No AI quality-control/correction stage; each card is generated directly to keep the workflow fast and reduce unnecessary API calls.

How It Works

User enters English word
          |
          v
     Flask /generate
          |
          v
      Google Gemini
          |
          v
 Target-language flashcard
          |
    +-----+------+
    |            |
    v            v
 Browser TTS   gTTS server
    |            |
    +-----+------+
          |
          v
     Anki / Download

English → Target Language

The input word is treated as an English source word.

For example:

English word: cook
Target language: German

The generated card uses the German vocabulary item as the target-language word, with its English meaning shown separately.

Tech Stack

Technology

Purpose

Python

Application backend

Flask

Web server and API routes

Google Gemini

Vocabulary generation

google-genai

Gemini Python SDK

gTTS

Server-side pronunciation audio

AnkiConnect

Browser-to-local-Anki integration

genanki

.apkg package generation

HTML / CSS / JavaScript

Frontend

Project Structure

AI-Flashcards/
├── app.py
├── templates/
│   └── index.html
├── .env
├── .gitignore
├── README.md
└── ...

Requirements

Python 3.10+ recommended

A Google Gemini API key

Anki Desktop for direct Anki integration

AnkiConnect add-on for direct Anki integration

An internet connection for Gemini and gTTS requests

Installation

1. Clone the repository

git clone <your-repository-url>
cd AI-Flashcards

2. Create a virtual environment

Windows PowerShell:

python -m venv ai_flashcards
.\ai_flashcards\Scripts\Activate.ps1

If your virtual environment already exists, simply activate it.

3. Install dependencies

pip install flask google-genai python-dotenv gTTS genanki

For a production-oriented local server, Waitress can also be installed:

pip install waitress

Environment Variables

Create a .env file in the project root:

GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-3.5-flash-lite

PORT=5000
FLASK_DEBUG=0

Do not commit your real .env file or API key to GitHub.

A safe .gitignore should include:

.env
__pycache__/
*.pyc
.venv/
venv/

Run the Application

Start the Flask application:

python app.py

Then open:

http://127.0.0.1:5000

The application must be accessed through Flask rather than opening index.html directly because the frontend communicates with Flask API endpoints.

Anki Integration

Direct Anki integration is optional. The website can still generate and download flashcards without Anki.

Install AnkiConnect

Open Anki Desktop.

Go to Tools → Add-ons.

Select Get Add-ons...

Enter the AnkiConnect add-on code:

2055492159

Install AnkiConnect.

Restart Anki.

Keep Anki running while using the direct Add to Anki and Create New Deck features.

How the connection works

The browser connects directly to the user's local AnkiConnect service:

Browser → AnkiConnect → Anki Desktop

The Flask server does not need to connect to the user's local Anki instance.

The frontend tries these local endpoints internally:

http://127.0.0.1:8765
http://localhost:8765

These are implementation details and are hidden from normal user-facing connection errors.

Using the Application

Start Anki if you want direct Anki integration.

Start the Flask application.

Open the website.

Enter an English word.

Choose the target language.

Choose an Anki deck.

Click Create Flashcard.

Review the generated card.

Use Add to Anki, Download Flashcard, Pronounce Word, or Pronounce Sentence.

Audio

The application supports two pronunciation paths.

In the browser

The frontend first attempts to use the browser's built-in speechSynthesis API.

If browser speech synthesis is unavailable or fails, it requests audio from the Flask server.

In Anki

When adding a card directly to Anki:

English input
    ↓
Gemini creates target-language card
    ↓
Flask generates word + sentence audio with gTTS
    ↓
Browser sends audio to AnkiConnect
    ↓
Anki stores audio in its media collection
    ↓
The note references the stored files with [sound:...]

storeMediaFile success is handled according to AnkiConnect's API behavior, including successful responses whose result is null.

Downloaded APKG files

The manual download path uses genanki to create an .apkg package containing the card and available pronunciation files.

Import the resulting package into Anki Desktop.

Duplicate Detection

Before adding a card directly to Anki, the website checks for an existing note using the front word.

If a matching word is found, the application asks whether the user still wants to add the card. This helps prevent accidental duplicates while still allowing intentional duplicates.

API Routes

GET /

Serves the frontend.

POST /generate

Generates a flashcard from the submitted English word and selected target language.

Example:

{
  "word": "cook",
  "language": "German"
}

POST /pronunciation-audio

Generates server-side pronunciation audio.

Example:

{
  "text": "kocht",
  "language": "German"
}

POST /download-flashcard

Creates and returns an Anki .apkg package.

Reliability and Security

The application includes:

API key loading from environment variables.

Request-size limits.

Input length validation.

In-memory rate limiting for generation, TTS, and downloads.

Safe filenames for generated files.

Temporary-file cleanup.

Structured Gemini responses using a defined schema.

HTML escaping before placing generated content into Anki fields.

Duplicate detection before direct Anki insertion.

No automatic retry of uncertain Anki write operations, reducing duplicate-write risk.

For a public production deployment, consider a shared rate-limit store and a proper production WSGI/reverse-proxy setup.

Troubleshooting

Website cannot connect to Anki

Make sure:

Anki Desktop is running.

AnkiConnect is installed.

Anki has been restarted after installing AnkiConnect.

Your browser has allowed the local connection when prompted.

The website is being opened through Flask.

The application can still generate and download flashcards without a working Anki connection.

Gemini generation fails

Check:

GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.5-flash-lite

Also ensure the virtual environment has all required packages installed.

Pronunciation does not play

For browser pronunciation, make sure your browser supports speech synthesis and has a suitable voice installed.

For server-side pronunciation, make sure the Flask server can reach the gTTS service.

For direct Anki audio, make sure AnkiConnect is running and Anki Desktop is open.

The page does not work when opening index.html

Do not open the HTML file directly.

Run:

python app.py

and open:

http://127.0.0.1:5000

Development

Run locally:

python app.py

Check changes:

git status

Commit changes:

git add app.py templates/index.html
git commit -m "Describe your change"

Push changes:

git push origin main

Portfolio Value

This project demonstrates practical skills in:

AI API integration

Structured AI generation

Flask backend development

Frontend JavaScript

Browser-to-local-service communication

Text-to-speech integration

Binary/audio handling

Anki API integration

.apkg file generation

Input validation

Error handling

Git and GitHub workflow

License

Add your preferred license here, for example:

MIT License

Replace this section with the actual license used by the repository.
