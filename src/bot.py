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
# httpx logs every Telegram getUpdates request at INFO level, including the full
# URL — which embeds the bot token in cleartext. Silence it to WARNING so the
# token never lands in journald/log files.
logging.getLogger("httpx").setLevel(logging.WARNING)

# Constants
ATUDO_TYPES = "101,102,103,104,105,106,107,108,109,110,111,112,113,115,117,114,ts,0,1,2,3,4,5,6"
# Zoom level passed to the atudo API. This does NOT change the geographic box;
# it only controls server-side clustering. At z < 16 the API collapses dense
# areas into {type:"cluster"} centroids that carry no id/info/vmax/desc, which
# is useless for proximity warnings. z=16 returns individual POIs everywhere
# (verified 2026-06-05: same box, z=16 fully de-clusters Berlin/München, no cap).
POI_ZOOM = 16
# atudo POI type taxonomy (from map.blitzer.de/v5 leaflet_project.js type_arr):
#   101..113,115,117 = stationäre Blitzer (haben i.d.R. info.desc)
#   114 = Tunnel, ts = Section Control, 0..6 = mobile Blitzer (KEIN info.desc)
MOBILE_TYPES = {"0", "1", "2", "3", "4", "5", "6"}
# Cache for 20 minutes (exceeds the required 10 min)
POI_CACHE = TTLCache(maxsize=200, ttl=1200)
USER_DATA = {} # Stores user state: last_location, last_time, warned_pois


def describe_poi(poi):
    """Build a human-readable label for a POI against the CURRENT atudo schema.
    Two schema facts handled here (verified 2026-06-05 against the live API):
      * vmax lives at the TOP level of the POI, not inside info.
      * mobile POIs (type 0-6) carry no info.desc, so we synthesise one.
    """
    info = poi.get("info") or {}
    desc = info.get("desc")
    if not desc:
        t = str(poi.get("type", ""))
        if t in MOBILE_TYPES:
            desc = "Mobiler Blitzer"
        elif t == "114":
            desc = "Tunnelblitzer"
        elif t == "ts":
            desc = "Section Control"
        else:
            desc = "Gefahrenstelle"
    # vmax is a top-level string; can be '0', '', 'v' (Speed=V) or '?' (zeitlich
    # bedingt). Only render a numeric limit > 0.
    try:
        v = int(poi.get("vmax", ""))
        if v > 0:
            return f"{desc} ({v} km/h)"
    except (ValueError, TypeError):
        pass
    return desc

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
    
    url = f"https://cdn2.atudo.net/api/4.0/pois.php?type={ATUDO_TYPES}&z={POI_ZOOM}&box={box[0]:.4f},{box[1]:.4f},{box[2]:.4f},{box[3]:.4f}"
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
        # Skip server-side cluster aggregates: they have no id/info/vmax and only
        # an averaged centroid, so warning on them would point at a phantom spot
        # and hide the real cameras inside. With z=POI_ZOOM these should not
        # appear, but filter defensively.
        if poi.get('type') == 'cluster':
            continue
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
            label = describe_poi(nearest_poi)

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
                            label = describe_poi(poi)

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
