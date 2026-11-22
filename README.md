<div align="center">

# `Instagram & TikTok Casino Parser`

**AI-powered Instagram & TikTok scraper, ASR/OCR extractor, and vector/graph risk-scoring dashboard for detecting illegal gambling & betting ads**

![Python](https://img.shields.io/badge/Python-3.10%2B-000000?style=for-the-badge&logo=python&logoColor=00E5FF)
![Streamlit](https://img.shields.io/badge/Streamlit-Dashboard-000000?style=for-the-badge&logo=streamlit&logoColor=10B981)
![Playwright](https://img.shields.io/badge/Playwright-Chromium-000000?style=for-the-badge&logo=playwright&logoColor=EF4444)
![Whisper](https://img.shields.io/badge/Faster--Whisper-ASR-000000?style=for-the-badge&logo=openai&logoColor=00E5FF)

</div>

---

## Features

- **Multi-Platform Scraping** — Automated video collection by tags/keywords from **Instagram** (`parsers/instagram.py`) and **TikTok** (`parsers/tiktok.py`) via Playwright with optional VPN rotation (`vpn_rotator.py`).
- **Multimodal Extraction** — Speech-to-text transcription (`faster-whisper`) and on-screen frame text recognition (`easyocr`).
- **Vector & Graph Analytics** — Embedding generation (`vector_builder.py`), clustering (`ml_processor.py`), and automated risk scoring (`CRITICAL` / `WARNING` / `CLEAN`).
- **Interactive Dashboard** — **Streamlit** web UI (`app.py`) for launching tag-based scans, inspecting risk cards, and exporting results.

---

## Installation

```bash
git clone https://github.com/morpheuscs/instagram-tiktok-casino-parser.git
cd instagram-tiktok-casino-parser

python -m venv .venv
# Linux / macOS:
source .venv/bin/activate
# Windows PowerShell:
# .venv\Scripts\Activate.ps1

pip install -r requirements.txt
playwright install chromium
```

---

## Configuration

1. Copy `.env.example` to `.env` and fill in credentials if needed:
   ```bash
   cp .env.example .env
   ```
2. Add target keywords/hashtags to `keywords.txt` (one per line), or pass them directly via `--tags`.

---

## Usage

### CLI (`main.py`)

```bash
# Run with tags from keywords.txt + analytics
python main.py

# Run with specific tags
python main.py --tags casino 1xbet "#слоты"

# TikTok only, max 10 videos per tag
python main.py --tags "казино,ставки" --platform tiktok --max-videos 10

# Run analytics only on existing database
python main.py --only-analytics
```

### Web Dashboard (`app.py`)

```bash
streamlit run app.py
```

---

## Project Structure

```text
instagram-tiktok-casino-parser/
├── app.py               # Streamlit web dashboard
├── main.py              # CLI pipeline entrypoint
├── config.py            # Environment & runtime configuration
├── db_manager.py        # Database storage & queries
├── data_loader.py       # Dataset & keyword loading
├── ml_processor.py      # Clustering & ML scoring
├── vector_builder.py    # Vector embeddings & similarity graph
├── vpn_rotator.py       # Optional VPN rotation helper
├── parsers/
│   ├── instagram.py     # Instagram Playwright parser
│   └── tiktok.py        # TikTok Playwright parser
├── keywords.txt         # Default target keywords
├── requirements.txt     # Python dependencies
└── .env.example         # Environment variables template
```
