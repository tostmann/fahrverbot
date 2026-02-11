# Telegram Blitzer Bot

Dieser Bot warnt vor POIs (Blitzer, etc.) basierend auf dem Live-Standort.

## Installation

### Installation (Systemweit)
Da `python-telegram-bot` in Version 20+ benötigt wird, empfiehlt sich die Installation via `pip`. Auf Ubuntu 24.04+ (Noble) kann dies systemweit mit dem Flag `--break-system-packages` erfolgen:

```bash
sudo apt update
sudo apt install python3-pip python3-geopy python3-requests python3-cachetools python3-dotenv
sudo pip3 install python-telegram-bot --break-system-packages
```

Alternativ wird die Verwendung eines `venv` empfohlen.

2. Erstelle eine `.env` Datei mit deinem Telegram Bot Token:
   ```env
   TELEGRAM_TOKEN=dein_bot_token_hier
   ```

3. Starte den Bot:
   ```bash
   python src/bot.py
   ```

## Systemd Service
Ein Beispiel-Service-File liegt unter `fahrverbot.service`.
1. Pfade in `fahrverbot.service` anpassen.
2. `cp fahrverbot.service /etc/systemd/system/`
3. `systemctl daemon-reload`
4. `systemctl enable fahrverbot && systemctl start fahrverbot`

## Nutzung
- Sende `/start` an den Bot.
- Teile deinen **Live-Standort** (nicht nur den aktuellen Standort einmalig).
- Der Bot warnt dich 60 und 30 Sekunden bevor du einen POI erreichst.
