# Dictator (Listen app)

Local text-to-speech reader with a notes window. MIT licence, no accounts, no servers.

- **Listen:** upload a .txt, .md, .pdf or .docx file, paste a web link, or type text. Read it aloud with a speed slider and a voice picker (Kokoro-82M, open weights, Apache-2.0).
- **Notes:** a local notes list with rich text (H1 to H3, bullets, bold, italic, underline, strikethrough), colours, and mini sticky pop-outs that can be pinned on top or minimised to their title.
- **Summarize to Notes:** key points from the loaded text, added to a new note or an existing one. Local and offline.
- **Social and web links:** any pasted link is read through free anonymous routes only. Login-only platforms (Reddit, X, Instagram, ...) are refused rather than scraped.
- **Browser extension:** send the page, post, comment, or just your selection straight into the app from Chrome or Edge. No copy-paste, no account.

## Setup

1. Python 3.12. Create a venv and install the project: `pip install -e ".[test]"`.
2. Install the spaCy English model: `python -m spacy download en_core_web_sm` (run once, locally).
3. Place the Kokoro model files in `models/`: `kokoro-v1.0.onnx`, `voices-v1.0.bin`, and `kokoro-vocab.json`. Or set `LISTEN_APP_MODEL_DIR`. The model weights are not in this repo.

## Run

    python -m listen_app.gui

Tests: `python -m pytest -q`. Tests that need the Kokoro model skip when it is missing.

Notes are saved to `~/.listen-app/notes.json` (override with `LISTEN_APP_NOTES`).

## Browser extension

The extension in `extension/` sends what you are looking at into the Listen window.

1. Open Listen and press **Browser**. That window shows the pairing code.
2. In Chrome or Edge, open `chrome://extensions`, turn on **Developer mode**, choose **Load unpacked**, and pick the `extension` folder inside this project.
3. Open the extension's popup once and paste the code. It stays paired.

Then either press the popup's **Send this page to Listen**, or use a shortcut: `Alt+Shift+S` sends your selection, `Alt+Shift+P` sends the page. **Send the link only** hands the URL to the app instead, which reads it through its own route, so the article text is fetched by the app rather than by the browser.

What crosses the boundary: the page's text or its link, and the page title. Nothing else.

The app listens on `127.0.0.1:8765` only — the port is not reachable from your network. Every request needs the pairing code, the `Host` header must be loopback (so a DNS-rebinding page cannot reach in), and the only two payload kinds accepted are `text` and a `url`. Cookies, logged-in sessions, file paths, and commands are not part of the protocol and are refused. Nothing is logged. The code is stored owner-only at `~/.listen-app/bridge.json` (override with `DICTATOR_BRIDGE_CONFIG`).

If the bridge cannot bind its port (another copy of Listen is open), the status line says so and the rest of the app works normally.

## Licences

See `LICENSE` (MIT) and `THIRD_PARTY_NOTICES.md`. num2words is LGPL-2.1, used unmodified.
