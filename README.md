# Ultimate Telegram Session Manager Bot v3.0

## Features in v3.0:
1. **Multi-Format Session Import & Export:** Supports `.session` (Pyrogram/Telethon), `.zip`, `.txt` string sessions, and direct text.
2. **Direct SQLite Telethon Export:** Fast, lock-free unpacking of session strings directly into `.session` and `.zip` files.
3. **Safe Mode Auto-Terminate Guard:** When Safe Mode is enabled for any important ID, any unauthorized new device login is instantly detected, automatically blocked/terminated (`ResetAuthorization`), and a fast alert DM is sent.
4. **Live OTP & Device Warning Listener:** 24/7 background monitor for Telegram service chat (`777000`) and active authorizations with precise Device Name, IP, and Country info.
5. **Session Control Panel:** Live status, spam check, account age estimation, OTP reader, chat cleaner, active device manager, and session termination.

## Setup Instructions:
1. Fill in your credentials in `config.py` (`API_ID`, `API_HASH`, `BOT_TOKEN`).
2. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```
3. Run the bot:
   ```bash
   python bot.py
   ```
