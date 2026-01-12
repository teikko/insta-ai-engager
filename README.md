# Insta AI Engager

Playwright-based Instagram engagement bot with OpenAI-powered comment planning.

## Setup

1. Ensure you have Python 3.10+ installed.
2. Create and activate a virtualenv.
3. Install dependencies:
   
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

4. Copy the example config and fill in your details:

   ```bash
   mkdir -p accounts/<account>
   cp accounts/default/config.example.yaml accounts/<account>/config.yaml
   ```
   Commenting requires an OpenAI API key in the config.

## Run

```bash
python insta_bot.py <account>
```

If `<account>` is omitted, it defaults to `default`.

## Manual login session (optional)

```bash
export IG_USERNAME="your_username"
export IG_PASSWORD="your_password"
python insta_manual_session.py
```

You can also pass `--username` and `--password` flags.
This script is for debugging; it opens Chromium for manual browsing.

## Notes

- Account data (cookies, session state, and CSV tracking files) is stored under `accounts/<account>/`.
- Keep credentials out of Git; `accounts/*/config.yaml` is ignored by default.
- Commenting uses OpenAI; set `openai_api_key` in your account config.
- Currently supports English-language content only.
- Automation may violate Instagram's ToS. Use responsibly and at your own risk.
