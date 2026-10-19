# Dictator (Listen app)

Local text-to-speech reader with a notes window. MIT licence, no accounts, no servers.

- **Listen:** upload a .txt, .md, .pdf or .docx file, paste a web link, or type text. Read it aloud with a speed slider and a voice picker (Kokoro-82M, open weights, Apache-2.0).
- **Notes:** a local notes list with rich text (H1 to H3, bullets, bold, italic, underline, strikethrough), colours, and mini sticky pop-outs that can be pinned on top or minimised to their title.
- **Summarize to Notes:** key points from the loaded text, added to a new note or an existing one. Local and offline.

## Setup

1. Python 3.12. Create a venv and install the project: `pip install -e ".[test]"`.
2. Install the spaCy English model: `python -m spacy download en_core_web_sm` (run once, locally).
3. Place the Kokoro model files in `models/`: `kokoro-v1.0.onnx`, `voices-v1.0.bin`, and `kokoro-vocab.json`. Or set `LISTEN_APP_MODEL_DIR`. The model weights are not in this repo.

## Run

    python -m listen_app.gui

Tests: `python -m pytest -q`. Tests that need the Kokoro model skip when it is missing.

Notes are saved to `~/.listen-app/notes.json` (override with `LISTEN_APP_NOTES`).

## Licences

See `LICENSE` (MIT) and `THIRD_PARTY_NOTICES.md`. num2words is LGPL-2.1, used unmodified.
