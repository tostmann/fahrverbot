import logging
import time
import math
import requests
from datetime import datetime, timedelta
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from geopy.distance import geodesic
from cachetools import TTLCache

# Logging setup
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Constants
ATUDO_TYPES = "101,102,103,104,105,106,107,108,109,110,111,112,113,115,117,114,ts,0,1,2,3,4,5,6"
# Cache for 20 minutes (exceeds the required 10 min)
POI_CACHE = TTLCache(maxsize=200, ttl=1200) 
USER_DATA = {} # Stores user state: last_location, last_time, warned_pois

def get_pois(lat, lng):
    # Grid-based caching (0.1 degree resolution ~11km)
    # This ensures that users in the same area share the same cache entry.
    grid_lat = round(lat, 1)
    grid_lng = round(lng, 1)
    cache_key = (grid_lat, grid_lng)
    
    if cache_key in POI_CACHE:
        return POI_CACHE[cache_key]
    
    # Fetch a significantly larger window (approx. 0.5° x 0.7° -> ~55km x 50km)
    # This reduces API calls and provides data for a larger area at once.
    lat_margin = 0.25
    lng_margin = 0.35 
    
    box = (grid_lat - lat_margin, grid_lng - lng_margin, 
           grid_lat + lat_margin, grid_lng + lng_margin)
    
    url = f"https://cdn2.atudo.net/api/4.0/pois.php?type={ATUDO_TYPES}&z=10&box={box[0]:.4f},{box[1]:.4f},{box[2]:.4f},{box[3]:.4f}"
    logging.info(f"Fetching POIs from: {url}")
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            pois = data.get('pois', [])
            POI_CACHE[cache_key] = pois
            return pois
    except Exception as e:
        logging.error(f"Error fetching POIs: {e}")
    return []

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Hallo {user.first_name}! 👋\n"
        "Ich bin dein Blitzer-Warner. Bitte sende mir deinen **Live-Standort**, "
        "damit ich dich vor Gefahren warnen kann.",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Standort teilen", request_location=True)]],
            one_time_keyboard=True
        )
    )

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg or not msg.location:
        return
    location = msg.location
    is_live = location.live_period is not None

    user_id = update.effective_user.id
    now = datetime.now()
    current_pos = (location.latitude, location.longitude)
    
    # Initialize user data if not present
    if user_id not in USER_DATA:
        USER_DATA[user_id] = {
            'last_pos': None,
            'last_time': None,
            'warned_pois': {}, # poi_id -> set of intervals warned (30, 60)
            'sent_locations': set(), # set of poi_ids
            'first_run': True
        }
    
    state = USER_DATA[user_id]
    pois = get_pois(current_pos[0], current_pos[1])
    
    # Find nearest POI
    nearest_dist = float('inf')
    nearest_poi = None
    nearest_poi_pos = None
    valid_pois = []
    for poi in pois:
        try:
            p_lat = float(poi.get('lat'))
            p_lng = float(poi.get('lng'))
            p_pos = (p_lat, p_lng)
            dist = geodesic(current_pos, p_pos).meters
            valid_pois.append((poi, p_pos, dist))
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_poi = poi
                nearest_poi_pos = p_pos
        except (TypeError, ValueError):
            continue

    # Handle static location (not live)
    if not is_live:
        if nearest_dist != float('inf'):
            dist_km = nearest_dist / 1000
            info = nearest_poi.get('info', {})
            desc = info.get('desc', 'POI')
            vmax = info.get('vmax', '')
            label = f"{desc} ({vmax} km/h)" if vmax and vmax != '0' else desc
            
            await msg.reply_text(
                f"Statische Position empfangen. Der nächste POI ist ca. {dist_km:.2f} km entfernt:\n\n"
                f"📍 *{label}*\n\n"
                "💡 Tipp: Sende mir deinen **Live-Standort**, um während der Fahrt automatisch gewarnt zu werden!",
                parse_mode="Markdown"
            )
            await context.bot.send_location(chat_id=user_id, latitude=nearest_poi_pos[0], longitude=nearest_poi_pos[1])
        else:
            await msg.reply_text(
                "Statische Position empfangen, aber keine POIs in der Nähe gefunden.\n\n"
                "💡 Tipp: Sende mir deinen **Live-Standort**, um während der Fahrt automatisch gewarnt zu werden!"
            )
        return

    # Handle first reception of live location
    if state['first_run']:
        if nearest_dist != float('inf'):
            dist_km = nearest_dist / 1000
            await msg.reply_text(f"Live-Standort aktiv! Der nächste POI ist ca. {dist_km:.2f} km entfernt.")
        else:
            await msg.reply_text("Live-Standort aktiv! Keine POIs in der Nähe gefunden.")
        state['first_run'] = False

    if not pois:
        return

    # Warning logic
    if state['last_pos'] and state['last_time']:
        dt = (now - state['last_time']).total_seconds()
        if dt > 0:
            dist_moved = geodesic(state['last_pos'], current_pos).meters
            speed = dist_moved / dt # meters per second
            
            if speed > 2: # Only warn if moving faster than 7.2 km/h
                for poi, p_pos, dist_to_poi in valid_pois:
                    try:
                        poi_id = f"{p_pos[0]},{p_pos[1]}"
                        time_to_poi = dist_to_poi / speed
                        
                        # Only warn if heading towards it (roughly)
                        old_dist = geodesic(state['last_pos'], p_pos).meters
                        if dist_to_poi < old_dist:
                            info = poi.get('info', {})
                            desc = info.get('desc', 'Gefahrenstelle')
                            vmax = info.get('vmax', '')
                            label = f"{desc} ({vmax} km/h)" if vmax and vmax != '0' else desc

                            # 60s warning
                            if 50 < time_to_poi <= 70:
                                if 60 not in state['warned_pois'].get(poi_id, set()):
                                    await context.bot.send_message(chat_id=user_id, text=f"⚠️ Warnung in 60s: {label}")
                                    if poi_id not in state['sent_locations']:
                                        await context.bot.send_location(chat_id=user_id, latitude=p_pos[0], longitude=p_pos[1])
                                        state['sent_locations'].add(poi_id)
                                    state['warned_pois'].setdefault(poi_id, set()).add(60)
                            
                            # 30s warning
                            if 20 < time_to_poi <= 40:
                                if 30 not in state['warned_pois'].get(poi_id, set()):
                                    await context.bot.send_message(chat_id=user_id, text=f"🚨 ACHTUNG in 30s: {label}")
                                    if poi_id not in state['sent_locations']:
                                        await context.bot.send_location(chat_id=user_id, latitude=p_pos[0], longitude=p_pos[1])
                                        state['sent_locations'].add(poi_id)
                                    state['warned_pois'].setdefault(poi_id, set()).add(30)
                    except Exception as e:
                        logging.error(f"Error in warning loop: {e}")
                        continue

    # Update state
    state['last_pos'] = current_pos
    state['last_time'] = now

def main():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("Please set TELEGRAM_TOKEN environment variable")
        return

    application = ApplicationBuilder().token(token).build()
    
    application.add_handler(CommandHandler("start", start))
    # Listen for both new locations and live location updates (edited_message)
    application.add_handler(MessageHandler(filters.LOCATION | filters.UpdateType.EDITED_MESSAGE, handle_location))
    
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logging.error(f"Exception while handling an update: {context.error}")

    application.add_error_handler(error_handler)

    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
