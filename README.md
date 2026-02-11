# Telegram Blitzer Bot

Dieser Bot warnt vor POIs (Blitzer, etc.) basierend auf dem Live-Standort.

## Installation

1. Installiere die Abhängigkeiten:
   ```bash
   pip install -r requirements.txt
   ```

2. Erstelle eine `.env` Datei mit deinem Telegram Bot Token:
   ```env
   TELEGRAM_TOKEN=dein_bot_token_hier
   ```

3. Starte den Bot:
   ```bash
   python src/bot.py
   ```

## Nutzung
- Sende `/start` an den Bot.
- Teile deinen **Live-Standort** (nicht nur den aktuellen Standort einmalig).
- Der Bot warnt dich 60 und 30 Sekunden bevor du einen POI erreichst.
