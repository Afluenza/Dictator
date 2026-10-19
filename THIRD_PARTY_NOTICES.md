# Third-party notices

This project is MIT-licensed (see LICENSE). It depends on the following
components, which keep their own licences:

- num2words 0.5.14 (LGPL-2.1), used unmodified as a pip dependency, imported
  by misaki at load. Licence text: the `COPYING` file in the installed
  distribution (`num2words-0.5.14.dist-info/COPYING`).
- misaki 0.9.4 (Apache-2.0), English grapheme-to-phoneme.
- spaCy 3.8.16 (MIT) and en_core_web_sm 3.8.0 (MIT), local install.
- Kokoro-82M weights (Apache-2.0), loaded from a local file path.
- onnxruntime 1.31.0 (MIT), numpy 2.5.4 (BSD-3), soundfile 0.14.0 (BSD-3).
- customtkinter 6.0.0 (MIT), GUI toolkit. Its package metadata field says
  CC0-1.0, but the LICENSE file shipped in the distribution is MIT. The LICENSE
  file is the one that applies.
- darkdetect 0.8.0 (BSD-3-Clause), a dependency of customtkinter. Licence text:
  the `LICENSE` file in the installed distribution.
- packaging 26.3 (Apache-2.0 OR BSD-2-Clause), a dependency of customtkinter.
  Licence text: `licenses/` in the installed distribution.
- sounddevice 0.5.6 (MIT), audio playback. Its wheel bundles PortAudio
  (MIT, per the README in `_sounddevice_data/portaudio-binaries`).
- cffi 2.1.1 (MIT-0), a dependency of sounddevice; pycparser 3.11 (BSD-3-Clause).
  Licence text: `licenses/` in the installed distribution.
- pdfplumber 0.11.10 (MIT) for PDF text, with pdfminer.six (MIT) and pypdfium2
  (BSD-3-Clause / Apache-2.0, dependency licences).
- python-docx 1.2.0 (MIT) for DOCX text.
- trafilatura 2.3.1 (Apache-2.0) for web-page main text, with courlan and
  htmldate (Apache-2.0), justext (BSD-2-Clause), lxml (BSD-3-Clause),
  dateparser (BSD-3-Clause), and charset-normalizer (MIT).
- certifi (MPL-2.0), a dependency of the HTTP stack. MPL is file-level copyleft:
  the certifi files keep MPL terms, and the app does not modify them.
- tld 0.13.2 (MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later). The app takes the
  MPL-1.1 option. It is pulled in by courlan; the app does not modify it.
