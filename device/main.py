# ============================================================
# IMPORTS
# M5Stack libraries: control the hardware (screen, buttons, sensors)
# urequests: make HTTP requests to the Cloud Run server
# ntptime: sync the clock with internet time servers
# machine: access low-level hardware (RTC clock)
# ujson: read/write JSON files on the device flash memory
# ============================================================
from m5stack import *
from m5stack_ui import *
from uiflow import *
import unit
import urequests
import time
import ntptime
import machine
import ujson

# Path to the local cache file stored on the device's flash memory.
# This allows the device to remember the last known values even after a reboot.
CACHE_FILE = "/flash/last_values.json"

def save_cache():
    """
    Saves current sensor and weather values to a JSON file on flash memory.
    Called every time new sensor data is collected (every 5 minutes).
    This ensures that even if the device is turned off, the last known
    values can be restored on the next boot without needing WiFi.
    """
    try:
        data = {
            "in_temp":    in_temp,
            "in_hum":     in_hum,
            "in_co2":     in_co2,
            "in_tvoc":    in_tvoc,
            "out_temp":   out_temp,
            "out_weather": out_weather
        }
        with open(CACHE_FILE, "w") as f:
            ujson.dump(data, f)
    except Exception as e:
        print("Cache save error:", e)

def load_cache():
    """
    Loads the last saved values from flash memory into global variables.
    Called at startup BEFORE trying to connect to the cloud, so the screen
    always shows something meaningful even if there is no WiFi connection.
    If the cache file doesn't exist (first boot), all values remain "--".
    """
    global in_temp, in_hum, in_co2, in_tvoc, out_temp, out_weather
    try:
        with open(CACHE_FILE, "r") as f:
            data = ujson.load(f)
        in_temp     = data.get("in_temp",     "--")
        in_hum      = data.get("in_hum",      "--")
        in_co2      = data.get("in_co2",      "--")
        in_tvoc     = data.get("in_tvoc",     "--")
        out_temp    = data.get("out_temp",    "--")
        out_weather = data.get("out_weather", "--")
        print("Cache loaded OK")
    except Exception as e:
        print("No cache:", e)

# ============================================================
# 1. INIT
# Initialize the screen, sensors, and load credentials from config.py
# ============================================================

# Initialize the M5Stack screen and set background color (dark navy blue)
screen = M5Screen()
screen.clean_screen()
screen.set_screen_bg_color(0x080B14)

# Initialize the three physical sensors:
# - ENV3: temperature + humidity sensor connected to Port A
# - TVOC: air quality sensor (CO2 + TVOC) connected to pins 14 and 13
# - PIR: motion detection sensor connected to Port B
env3_0 = unit.get(unit.ENV3, unit.PORTA)
tvoc_0 = unit.get(unit.TVOC, (14, 13))
pir_0  = unit.get(unit.PIR, unit.PORTB)

# Load the Cloud Run URL and password hash from a separate config file.
# This file is NOT included in Git to keep credentials private.
from config import URL_CLOUD_RUN, YOUR_HASH_PASSWD

# Initialize the Real-Time Clock (RTC) — the internal clock of the device.
# After NTP sync, the RTC keeps track of time even without internet.
rtc = machine.RTC()

# ============================================================
# 2. NTP SYNC
# Synchronize the device clock with an internet time server.
# timezone=2 sets CEST (Central European Summer Time, UTC+2).
# If sync fails, the clock shows "--:--" and time-dependent features
# (like PIR morning/evening messages) are disabled.
# ============================================================
clock_synced = False
try:
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("Syncing time...", 80, 110, lcd.WHITE)
    ntp = ntptime.client(host='pool.ntp.org', timezone=2)
    # Write NTP time into the internal RTC so it keeps ticking independently
    # rtc.datetime format: (year, month, day, weekday, hour, min, sec, subsec)
    rtc.datetime((
        ntp.year(), ntp.month(), ntp.day(),
        0, ntp.hour(), ntp.minute(), ntp.second(), 0
    ))
    clock_synced = True
    lcd.clear()
except Exception as e:
    # If NTP fails (no WiFi), show error and continue without clock
    lcd.clear()
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("NTP Failed!", 90, 100, lcd.RED)
    lcd.print("No time available", 60, 125, lcd.ORANGE)
    lcd.print("Check WiFi and", 70, 150, lcd.ORANGE)
    lcd.print("restart device", 75, 170, lcd.ORANGE)
    time.sleep(3)
    lcd.clear()

# ============================================================
# 3. STATE VARIABLES
# Global variables that hold the current state of the application.
# All sensor values start as "--" until the first reading is taken.
# ============================================================

# Tracks which screen is currently visible (1=Dashboard, 2=Forecast+AI, 3=History)
active_screen = 1

# Indoor sensor readings (updated every 5 minutes from physical sensors)
in_temp   = "--"   # indoor temperature in °C
in_hum    = "--"   # indoor humidity in %
in_co2    = "--"   # CO2 concentration in ppm
in_tvoc   = "--"   # Total Volatile Organic Compounds in ppb
motion    = 0      # PIR sensor state (1=motion detected, 0=no motion)

# Outdoor weather data (fetched from OpenWeatherMap via Cloud Run server)
out_temp    = "--"
out_weather = "--"

# Local in-memory history buffer (last 5 readings, newest first)
# Used as fallback if BigQuery is unreachable
history = []

# Forecast data received from the server (list of 3 days)
forecast_data = []

# Sensor sync interval: 300000ms = 5 minutes
SENSOR_INTERVAL_MS    = 300000
# Set to negative so the first sync happens immediately on startup
last_sensor_ms        = -SENSOR_INTERVAL_MS
# Tracks the last minute displayed on the clock (to avoid unnecessary redraws)
last_displayed_minute = -1

# ============================================================
# 4. TIME HELPERS
# Functions to format time and date for display and for BigQuery payloads.
# All functions check clock_synced first — if NTP failed, they return placeholders.
# ============================================================

def get_time_str():
    """Returns current time as 'HH:MM' string for display on screen."""
    if not clock_synced:
        return "--:--"
    dt = rtc.datetime()
    return "{:02d}:{:02d}".format(dt[4], dt[5])

def get_weekday(year, month, day):
    """
    Calculates the day of the week using Zeller's congruence algorithm.
    Returns: 0=MON, 1=TUE, 2=WED, 3=THU, 4=FRI, 5=SAT, 6=SUN
    Used to display weekday names in the forecast and history screens.
    """
    if month < 3:
        month += 12
        year -= 1
    k = year % 100
    j = year // 100
    h = (day + (13 * (month + 1)) // 5 + k + k // 4 + j // 4 - 2 * j) % 7
    # Convert Zeller's output (0=Sat) to Monday-based (0=Mon)
    return (h + 5) % 7

def get_date_str():
    """Returns current date as 'WED  25 MAY 2026' string for display."""
    if not clock_synced:
        return "--- -- --- ----"
    DAYS   = ["MON","TUE","WED","THU","FRI","SAT","SUN"]
    MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN",
              "JUL","AUG","SEP","OCT","NOV","DEC"]
    dt = rtc.datetime()
    y, mo, d = dt[0], dt[1], dt[2]
    wd = get_weekday(y, mo, d)
    return "{}  {:02d} {} {}".format(DAYS[wd], d, MONTHS[mo - 1], y)

def get_date_for_payload():
    """Returns date in 'YYYY-MM-DD' format for BigQuery INSERT queries."""
    if not clock_synced:
        return "0000-00-00"
    dt = rtc.datetime()
    return "{:04d}-{:02d}-{:02d}".format(dt[0], dt[1], dt[2])

def get_time_for_payload():
    """Returns time in 'HH:MM:SS' format for BigQuery INSERT queries."""
    if not clock_synced:
        return "00:00:00"
    dt = rtc.datetime()
    return "{:02d}:{:02d}:{:02d}".format(dt[4], dt[5], dt[6])

def get_minute():
    """
    Returns the current minute (0-59) from the RTC.
    Used in the main loop to detect when the minute changes
    and trigger a clock redraw without flickering the whole screen.
    """
    if not clock_synced:
        return -1
    return rtc.datetime()[5]  # index 5 = minutes in rtc.datetime() tuple

# ============================================================
# 5. COLOR + LABEL HELPERS
# Functions that map sensor values to colors and labels for the UI.
# Color thresholds are based on standard indoor air quality guidelines.
# ============================================================

def co2_color(val):
    """
    Maps a CO2 value (ppm) to a display color:
    - Green  (< 800 ppm):  good air quality
    - Yellow (< 1200 ppm): moderate — ventilation recommended
    - Red    (>= 1200 ppm): poor — open a window immediately
    """
    try:
        v = int(val)
        if v < 800:  return 0x00E676
        if v < 1200: return 0xFFCC00
        return 0xFF4444
    except:
        return lcd.WHITE

def tvoc_color(val):
    """
    Maps a TVOC value (ppb) to a display color:
    - Green  (< 150 ppb):  good
    - Yellow (< 300 ppb):  moderate
    - Red    (>= 300 ppb): poor — high chemical pollutants
    """
    try:
        v = int(val)
        if v < 150: return 0x00E676
        if v < 300: return 0xFFCC00
        return 0xFF4444
    except:
        return lcd.WHITE

def air_quality_score(co2, tvoc):
    """
    Computes a composite air quality score from 0 (best) to 100 (worst).
    CO2 baseline is 400 ppm (outdoor air), ceiling is 2000 ppm.
    TVOC ceiling is 500 ppb.
    The score is the average of the two normalized sub-scores.
    Used to position the marker on the gradient air quality bar.
    """
    try:
        c = min(int(co2), 2000)
        t = min(int(tvoc), 500)
        score_co2  = max(0, (c - 400) / 1600) * 100
        score_tvoc = (t / 500) * 100
        return int(min(100, (score_co2 + score_tvoc) / 2))
    except:
        return -1

def air_quality_label(co2, tvoc):
    """
    Returns a human-readable air quality label and its display color.
    GOOD: both CO2 < 800 and TVOC < 150
    MODERATE: CO2 < 1200 and TVOC < 300
    POOR: either CO2 >= 1200 or TVOC >= 300
    """
    try:
        c = int(co2)
        t = int(tvoc)
        if c < 800 and t < 150:  return ("GOOD",     0x00E676)
        if c < 1200 and t < 300: return ("MODERATE", 0xFFCC00)
        return ("POOR", 0xFF4444)
    except:
        return ("--", lcd.WHITE)

def shorten_weather(weather_str):
    """
    Converts OpenWeatherMap description strings (e.g. 'scattered clouds')
    into two short lines that fit in the small outdoor weather panel.
    Returns a tuple (line1, line2) where line2 may be an empty string.
    """
    WEATHER_MAP = {
        "thunderstorm": ("Thunder-", "storm"),
        "drizzle":      ("Drizzle",  ""),
        "rain":         ("Rain",     ""),
        "snow":         ("Snow",     ""),
        "mist":         ("Mist",     ""),
        "fog":          ("Fog",      ""),
        "haze":         ("Haze",     ""),
        "overcast":     ("Overcast", "Clouds"),
        "scattered":    ("Scattered","Clouds"),
        "broken":       ("Broken",   "Clouds"),
        "few clouds":   ("Few",      "Clouds"),
        "clear":        ("Clear",    "Sky"),
        "cloud":        ("Cloudy",   ""),
    }
    w = str(weather_str).lower()
    for key, (l1, l2) in WEATHER_MAP.items():
        if key in w:
            return (l1, l2)
    # Fallback: split at first space if not matched
    parts = str(weather_str).split(" ")
    if len(parts) >= 2:
        return (parts[0], parts[1])
    return (str(weather_str), "")

# ============================================================
# 6. ICON DRAWING
# All icons are drawn using LCD primitives (lines, circles, rectangles)
# since the M5Stack in UIFlow MicroPython has no image file support.
# _lg variants are larger versions used in the forecast screen.
# ============================================================

def draw_thermometer(x, y, color):
    """Draws a simple thermometer icon: a rectangle tube + circle bulb."""
    lcd.rect(x-2, y, 5, 14, color, 0x080B14)
    lcd.circle(x, y+17, 5, color, color)
    lcd.circle(x, y+17, 3, color, color)

def draw_droplet(x, y, color):
    """Draws a water droplet icon used to represent humidity."""
    lcd.circle(x, y+8, 5, color, color)
    lcd.line(x, y, x-4, y+6, color)
    lcd.line(x, y, x+4, y+6, color)
    lcd.line(x-4, y+6, x-5, y+8, color)
    lcd.line(x+4, y+6, x+5, y+8, color)

def draw_sun(x, y, color):
    """Draws a sun icon: filled circle + 8 rays in cardinal and diagonal directions."""
    lcd.circle(x, y, 6, color, color)
    lcd.line(x, y-9, x, y-13, color)
    lcd.line(x, y+9, x, y+13, color)
    lcd.line(x-9, y, x-13, y, color)
    lcd.line(x+9, y, x+13, y, color)
    lcd.line(x-7, y-7, x-9, y-9, color)
    lcd.line(x+7, y-7, x+9, y-9, color)
    lcd.line(x-7, y+7, x-9, y+9, color)
    lcd.line(x+7, y+7, x+9, y+9, color)

def draw_cloud(x, y, color):
    """Draws a cloud icon using 3 overlapping circles + a rectangle base."""
    lcd.circle(x-5, y+3, 6, color, color)
    lcd.circle(x+2, y, 7, color, color)
    lcd.circle(x+9, y+4, 5, color, color)
    lcd.rect(x-5, y+3, 15, 6, color, color)

def draw_rain(x, y, color):
    """Draws a rain icon: cloud + 3 diagonal rain lines below."""
    draw_cloud(x, y, color)
    lcd.line(x-3, y+11, x-5, y+17, 0x4A90D9)
    lcd.line(x+3, y+11, x+1, y+17, 0x4A90D9)
    lcd.line(x+9, y+11, x+7, y+17, 0x4A90D9)

def is_daytime():
    """
    Returns True if current hour is between 7:00 and 20:00.
    Used to decide whether to show sun or moon icon for clear weather.
    If clock is not synced, assumes daytime.
    """
    if not clock_synced:
        return True
    return 7 <= rtc.datetime()[4] < 20

def draw_moon(x, y, color):
    """
    Draws a crescent moon by drawing a full circle, then overlapping it
    with a background-colored circle to simulate the crescent shape.
    """
    lcd.circle(x, y, 10, color, color)
    lcd.circle(x + 5, y - 3, 8, 0x080B14, 0x080B14)

def draw_weather_icon(x, y, weather_str):
    """
    Picks and draws the appropriate weather icon based on the
    OpenWeatherMap description string. Shows moon instead of sun
    if it's nighttime (outside 7-20h). Used in Screen 1 (Dashboard).
    """
    w = str(weather_str).lower()
    day = is_daytime()
    if "rain" in w or "drizzle" in w:
        draw_rain(x, y, 0x4A90D9)
    elif "thunder" in w:
        draw_rain(x, y, 0xFFCC00)
    elif "snow" in w:
        draw_cloud(x, y, lcd.WHITE)
    elif "cloud" in w or "overcast" in w:
        draw_cloud(x, y, 0x8AAFD4)
    elif "mist" in w or "fog" in w or "haze" in w:
        draw_cloud(x, y, 0x555577)
    elif "clear" in w:
        if day: draw_sun(x, y, 0xFFCC00)
        else:   draw_moon(x, y, 0xCCDDFF)
    else:
        if day: draw_sun(x, y, 0xFFCC00)
        else:   draw_moon(x, y, 0xCCDDFF)

def draw_sun_lg(x, y, color):
    """Large sun icon for the forecast panels (radius 14 + longer rays)."""
    lcd.circle(x, y, 14, color, color)
    lcd.line(x, y-18, x, y-24, color)
    lcd.line(x, y+18, x, y+24, color)
    lcd.line(x-18, y, x-24, y, color)
    lcd.line(x+18, y, x+24, y, color)
    lcd.line(x-13, y-13, x-17, y-17, color)
    lcd.line(x+13, y-13, x+17, y-17, color)
    lcd.line(x-13, y+13, x-17, y+17, color)
    lcd.line(x+13, y+13, x+17, y+17, color)

def draw_cloud_lg(x, y, color):
    """Large cloud icon for the forecast panels."""
    lcd.circle(x-8, y+5, 10, color, color)
    lcd.circle(x+4, y, 13, color, color)
    lcd.circle(x+16, y+6, 9, color, color)
    lcd.rect(x-8, y+5, 25, 10, color, color)

def draw_rain_lg(x, y, color):
    """Large rain icon for the forecast panels."""
    draw_cloud_lg(x, y, color)
    lcd.line(x-5, y+17, x-8, y+25, 0x4A90D9)
    lcd.line(x+4, y+17, x+1, y+25, 0x4A90D9)
    lcd.line(x+13, y+17, x+10, y+25, 0x4A90D9)

def draw_moon_lg(x, y, color):
    """Large crescent moon icon for the forecast panels."""
    lcd.circle(x, y, 14, color, color)
    lcd.circle(x+7, y-4, 11, 0x080B14, 0x080B14)

def draw_weather_icon_lg(x, y, weather_str, force_day=False):
    """
    Large version of draw_weather_icon for the forecast panels.
    force_day=True always shows the sun (used for future forecast
    because the data always refers to daytime conditions at 14:00).
    """
    w = str(weather_str).lower()
    day = True if force_day else is_daytime()
    if "rain" in w or "drizzle" in w:
        draw_rain_lg(x, y, 0x4A90D9)
    elif "thunder" in w:
        draw_rain_lg(x, y, 0xFFCC00)
    elif "snow" in w:
        draw_cloud_lg(x, y, lcd.WHITE)
    elif "cloud" in w or "overcast" in w:
        draw_cloud_lg(x, y, 0x8AAFD4)
    elif "mist" in w or "fog" in w or "haze" in w:
        draw_cloud_lg(x, y, 0x555577)
    elif "clear" in w:
        if day: draw_sun_lg(x, y, 0xFFCC00)
        else:   draw_moon_lg(x, y, 0xCCDDFF)
    else:
        if day: draw_sun_lg(x, y, 0xFFCC00)
        else:   draw_moon_lg(x, y, 0xCCDDFF)

# ============================================================
# 7. AIR QUALITY BAR
# Draws a horizontal gradient bar from green (left=good) to red (right=poor)
# with a white marker showing the current score position.
# Uses 16 precomputed color steps to simulate a smooth gradient,
# since MicroPython doesn't support float RGB interpolation reliably.
# ============================================================

def draw_air_bar(x, y, w, h, score):
    """
    Draws the air quality gradient bar.
    score: 0 (best/green) to 100 (worst/red), or -1 if unknown (gray).
    The white marker is clamped to stay within the bar boundaries.
    """
    if score < 0:
        # Unknown value: draw gray bar
        lcd.rect(x, y, w, h, 0x444444, 0x444444)
        return
    # 16-step gradient from pure green to pure red
    GRADIENT = [
        0x00C853, 0x2DD44A, 0x59E040, 0x86EC37,
        0xB2F82D, 0xD4EF22, 0xF5E516, 0xFFCC00,
        0xFFB000, 0xFF9300, 0xFF7600, 0xFF5900,
        0xFF3C00, 0xFF2800, 0xFF1400, 0xFF0000,
    ]
    n = len(GRADIENT)
    slice_w = max(1, w // n)
    for i in range(n):
        sx = x + i * slice_w
        # Last slice fills remaining pixels to avoid gap at the right edge
        sw = slice_w if i < n - 1 else (x + w - sx)
        lcd.rect(sx, y, sw, h, GRADIENT[i], GRADIENT[i])
    # Draw white marker at current score position
    marker_x = x + int(score / 100 * w)
    marker_x = max(x + 1, min(marker_x, x + w - 2))  # clamp within bar
    lcd.rect(marker_x - 1, y - 3, 3, h + 6, lcd.WHITE, lcd.WHITE)

# ============================================================
# 8. CLOUD FUNCTIONS
# All communication with the Flask backend on Google Cloud Run.
# Each function sends a POST request with the password hash for authentication.
# Errors are caught silently to prevent crashing if WiFi is unavailable.
# ============================================================

def send_to_bigquery():
    """
    Sends current sensor readings to the Flask /send-to-bigquery endpoint.
    The server then inserts the data into the BigQuery table along with
    the current outdoor weather fetched from OpenWeatherMap.
    """
    payload_send = {
        "passwd": YOUR_HASH_PASSWD,
        "values": {
            "date":             get_date_for_payload(),
            "time":             get_time_for_payload(),
            "indoor_temp":      in_temp,
            "indoor_humidity":  in_hum,
            "indoor_co2":       in_co2,
            "indoor_tvoc":      in_tvoc,
            "motion_detected":  motion
        }
    }
    try:
        res_send = urequests.post(URL_CLOUD_RUN + "/send-to-bigquery", json=payload_send)
        res_send.close()
    except Exception as e:
        print("Send error:", e)

def get_outdoor_weather():
    """
    Fetches the latest outdoor weather from the server.
    The server gets this from the last row in BigQuery (which was stored
    with the previous send_to_bigquery call and includes OpenWeatherMap data).
    Updates global out_temp and out_weather variables.
    """
    global out_temp, out_weather
    payload_get = {"passwd": YOUR_HASH_PASSWD}
    try:
        res_get = urequests.post(URL_CLOUD_RUN + "/get_outdoor_weather", json=payload_get)
        if res_get.status_code == 200:
            d = res_get.json()
            out_temp    = d.get("outdoor_temp",   "--")
            out_weather = d.get("outdoor_weather", "--")
        res_get.close()
    except Exception as e:
        print("Fetch error:", e)

def get_latest_from_bigquery():
    """
    Fetches the most recent indoor sensor record from BigQuery.
    Called at startup to restore the last known values even if the
    device was off for a while (important: tested during presentation).
    """
    global in_temp, in_hum, in_co2, in_tvoc
    payload = {"passwd": YOUR_HASH_PASSWD}
    try:
        res = urequests.post(URL_CLOUD_RUN + "/get_latest", json=payload)
        if res.status_code == 200:
            d = res.json()
            if d.get("status") == "success":
                in_temp = d.get("indoor_temp", "--")
                in_hum  = d.get("indoor_humidity", "--")
                in_co2  = d.get("indoor_co2", "--")
                in_tvoc = d.get("indoor_tvoc", "--")
        res.close()
    except Exception as e:
        print("Startup sync error:", e)

def get_history_from_bigquery():
    """
    Fetches the last 5 records from BigQuery for the History screen.
    Returns a list of dicts ordered newest first, or an empty list on failure.
    If it fails, the screen falls back to the local in-memory history buffer.
    """
    payload = {"passwd": YOUR_HASH_PASSWD}
    try:
        res = urequests.post(URL_CLOUD_RUN + "/get_history", json=payload)
        if res.status_code == 200:
            d = res.json()
            if d.get("status") == "success":
                res.close()
                return d.get("rows", [])
        res.close()
    except Exception as e:
        print("History fetch error:", e)
    return []

def get_forecast_from_server():
    """
    Fetches the 3-day weather forecast from the server.
    The server queries OpenWeatherMap's /forecast endpoint and returns
    one entry per day at approximately 14:00 local time (12:00 UTC).
    Updates the global forecast_data list.
    """
    global forecast_data
    payload = {"passwd": YOUR_HASH_PASSWD}
    try:
        res = urequests.post(URL_CLOUD_RUN + "/get_forecast", json=payload)
        if res.status_code == 200:
            d = res.json()
            if d.get("status") == "success":
                forecast_data = d.get("forecast", [])
        res.close()
    except Exception as e:
        print("Forecast error:", e)

# ============================================================
# SPEAK — TTS via OpenAI on Cloud Run
# Sends text to the /speak endpoint which calls OpenAI TTS API,
# returns a WAV file (PCM 24000Hz, converted to unsigned for Core2),
# saves it to flash, then plays it through the built-in speaker.
# Wait time is estimated from word count (~400ms per word).
# ============================================================

def speak(text):
    """
    Converts text to speech and plays it on the device speaker.
    The text is URL-encoded (spaces → %20) and sent to the Cloud Run
    /speak endpoint. The server returns a WAV file which is saved to
    /flash/speech.wav and played with speaker.playWAV().
    The device waits for the estimated audio duration before continuing.
    """
    try:
        power.setSpkEnable(True)  # enable the speaker amplifier
        safe_text = text.replace(" ", "%20")  # URL-encode spaces
        url = URL_CLOUD_RUN + "/speak/" + safe_text
        res = urequests.get(url)
        if res.status_code == 200:
            # Save WAV to flash memory
            with open("/flash/speech.wav", "wb") as f:
                f.write(res.content)
            res.close()
            # Play: F24B = 24000Hz format, channel 0 = stereo
            speaker.playWAV("/flash/speech.wav", speaker.F24B, 0)
            # Estimate playback duration based on word count
            words = len(text.split())
            wait_ms = max(3000, words * 400)
            time.sleep_ms(wait_ms)
        else:
            res.close()
    except Exception as e:
        print("Speak error:", e)

# ============================================================
# STT — Record and Transcribe
# Records 4 seconds of audio from the internal microphone using
# the hardware.microphone module (UIFlow native driver).
# The audio is base64-encoded and sent to the /transcribe endpoint
# which uses OpenAI Whisper to convert speech to text.
# The transcribed text is then sent to /ask for an AI response.
# ============================================================

def record_and_transcribe():
    """
    Full STT pipeline:
    1. Show 'Recording' UI feedback
    2. Record 4 seconds via microphone
    3. Show 'Thinking' UI feedback
    4. Base64-encode the WAV file
    5. POST to /transcribe → Whisper STT → question text
    6. POST to /ask → GPT-4o-mini → answer text
    7. Call speak() with the answer
    8. Update last_ai_answer and redraw screen 2
    """
    global last_ai_answer
    from hardware import microphone
    import gc, ubinascii
    gc.collect()  # free memory before audio operations

    # Show recording indicator in the AI answer box area
    lcd.rect(0, 137, 320, 75, 0x080B14, 0x080B14)
    lcd.rect(6, 140, 236, 63, 0x0D1526, 0x0D1526)
    lcd.rect(6, 140, 3, 63, 0xFF4444, 0xFF4444)  # red left border
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("Recording...", 14, 148, 0xFF4444)
    lcd.print("Speak now!", 14, 170, lcd.WHITE)

    # Record 4 seconds of audio from the internal microphone
    mic = microphone.MIC()
    mic.record2file(4, "/flash/recording.wav")

    # Update UI to show transcription in progress
    lcd.rect(0, 137, 320, 75, 0x080B14, 0x080B14)
    lcd.rect(6, 140, 236, 63, 0x0D1526, 0x0D1526)
    lcd.rect(6, 140, 3, 63, 0x4A90D9, 0x4A90D9)  # blue left border
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("Thinking...", 14, 155, 0x4A90D9)

    try:
        # Read WAV file and encode to base64 for JSON transport
        with open("/flash/recording.wav", "rb") as f:
            audio_bytes = f.read()
        audio_b64 = ubinascii.b2a_base64(audio_bytes).decode("utf-8").strip()
        # Send to Whisper STT endpoint
        payload = {"passwd": YOUR_HASH_PASSWD, "audio": audio_b64}
        res = urequests.post(URL_CLOUD_RUN + "/transcribe", json=payload)
        d = res.json()
        res.close()
        question = d.get("text", "")
        if question:
            # Send transcribed question to GPT-4o-mini with sensor context
            answer = generate_answer(question)
            last_ai_answer = answer
            speak(answer)
        else:
            last_ai_answer = "Could not understand"
    except Exception as e:
        last_ai_answer = "Error: " + str(e)[:15]
        print("STT error:", e)
    draw_screen_2()  # refresh screen to show the answer

# ============================================================
# ALERT POPUP
# draw_alert_popup: draws the popup box with title, message and warning icon.
# alert_countdown: draws an animated progress bar that counts down and
#                  can be interrupted early by button press or touch.
# These two are always called together: popup → speak → countdown.
# ============================================================

def draw_alert_popup(title, message, color):
    """
    Draws a centered alert popup on the full screen.
    - Dims the background to black
    - Draws a colored border box
    - Shows a warning triangle icon at the top center
    - Shows the title in the given color
    - Splits the message into multiple lines (text wrapping) if needed
    """
    # Popup coordinates (larger to fit long texts)
    POP_X, POP_Y, POP_W, POP_H = 10, 50, 300, 140

    # Background and box
    lcd.rect(0, 0, 320, 240, 0x000000, 0x000000)
    lcd.rect(POP_X, POP_Y, POP_W, POP_H, 0x0D1526, 0x0D1526)

    # Popup borders (top, bottom, left, right)
    lcd.rect(POP_X, POP_Y, POP_W, 3, color, color)
    lcd.rect(POP_X, POP_Y+POP_H-3, POP_W, 3, color, color)
    lcd.rect(POP_X, POP_Y, 3, POP_H, color, color)
    lcd.rect(POP_X+POP_W-3, POP_Y, 3, POP_H, color, color)

    # Warning triangle icon: 3 lines forming a triangle + "!" inside
    cx = 160
    ty = POP_Y + 10
    lcd.line(cx, ty, cx-10, ty+16, color)
    lcd.line(cx, ty, cx+10, ty+16, color)
    lcd.line(cx-10, ty+16, cx+10, ty+16, color)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("!", cx-3, ty+2, color)

    # Centered title (estimated ~13px per character for DejaVu18)
    title_x = max(POP_X + 5, POP_X + (POP_W - len(title) * 13) // 2)
    lcd.print(title, title_x, POP_Y + 38, color)

    # TEXT WRAPPING: splits message into multiple lines automatically
    # so long messages (e.g. weather descriptions) don't get cut off
    words = message.split(" ")
    lines = []
    curr_line = ""
    for w in words:
        if len(curr_line) + len(w) < 28:  # Max ~28 characters per line
            curr_line += w + " "
        else:
            lines.append(curr_line.strip())
            curr_line = w + " "
    if curr_line:
        lines.append(curr_line.strip())

    # Print text lines (always centered, up to 3 lines)
    y_text = POP_Y + 65
    for l in lines[:3]:  # Show up to 3 lines maximum
        msg_x = max(POP_X + 5, POP_X + (POP_W - len(l) * 10) // 2)
        lcd.print(l, msg_x, y_text, lcd.WHITE)
        y_text += 22

def alert_countdown(color, seconds=3):
    """
    Draws an animated progress bar at the bottom of the popup that
    shrinks from full width to zero over the given number of seconds.
    The user can dismiss the popup early by pressing any button or touching the screen.
    Steps = seconds × 2, with 500ms per step.
    """
    # Same dimensions as the popup to align the bar correctly
    POP_X, POP_Y, POP_W, POP_H = 10, 50, 300, 140
    BAR_X = POP_X + 10
    BAR_Y = POP_Y + POP_H - 18
    BAR_W = POP_W - 20
    BAR_H = 6
    steps = seconds * 2
    for step in range(steps, 0, -1):
        w = int(BAR_W * step / steps)
        lcd.rect(BAR_X, BAR_Y, BAR_W, BAR_H, 0x1C2A45, 0x1C2A45)  # clear bar
        lcd.rect(BAR_X, BAR_Y, w, BAR_H, color, color)              # draw remaining

        # Stop countdown if user touches screen or presses buttons
        if btnA.wasPressed() or btnB.wasPressed() or btnC.wasPressed():
            return
        if touch.status():
            return
        time.sleep_ms(500)

# ============================================================
# ALERT SYSTEM
# Two independent alert systems:
#
# 1. SENSOR ALERTS (check_sensor_alerts):
#    Called every 5 minutes after reading sensors.
#    Checks thresholds for CO2, TVOC, humidity, indoor/outdoor temperature.
#    Each alert type has its own cooldown to avoid repeating too often.
#
# 2. PIR ANNOUNCE (check_pir_announce):
#    Called every 100ms in the main loop.
#    Triggers a contextual voice message when motion is detected,
#    but no more than once per hour.
#
# 3. FORECAST ALERTS (check_forecast_alerts):
#    Called from check_pir_announce during morning hours.
#    Warns about upcoming storm or snow in the 3-day forecast,
#    but no more than once per day.
# ============================================================

# Dictionary tracking the timestamp (ms) of the last alert for each type.
# Initialized to 0 so all alerts fire on the first check after startup.
last_alert_ms = {
    "co2_poor":      0,
    "co2_moderate":  0,
    "hum_low":       0,
    "hum_high":      0,
    "tvoc_high":     0,
    "temp_in_hot":   0,
    "temp_in_cold":  0,
    "temp_out_hot":  0,
    "temp_out_cold": 0,
}
last_forecast_alert_day = -1  # day-of-month of last forecast alert (prevents repeat)
last_pir_announce_ms    = 0   # timestamp of last PIR announcement

# Cooldown constants in milliseconds
HOUR_MS  = 3600000   # 1 hour
HOUR2_MS = 7200000   # 2 hours
HOUR4_MS = 14400000  # 4 hours
DAY_MS   = 86400000  # 24 hours

def can_alert(key, cooldown_ms):
    """
    Returns True if enough time has passed since the last alert of this type.
    Also resets the timer for that alert type when it fires.
    Uses 0 as a special value meaning 'never fired' → always fires first time.
    """
    global last_alert_ms
    now = time.ticks_ms()
    last = last_alert_ms[key]
    if last == 0 or time.ticks_diff(now, last) >= cooldown_ms:
        last_alert_ms[key] = now
        return True
    return False

def check_forecast_alerts():
    """
    Checks the 3-day forecast for severe weather (storm or snow).
    If found, shows a popup and speaks a warning.
    Fires at most once per calendar day (tracked by day-of-month).
    Called from check_pir_announce during morning hours (6-9h).
    """
    global last_forecast_alert_day
    if not clock_synced:
        return
    today = rtc.datetime()[2]  # day of month (1-31)
    if today == last_forecast_alert_day:
        return  # already alerted today
    for row in forecast_data:
        weather = str(row.get("weather", "")).lower()
        date    = row.get("day", "")
        if "thunder" in weather or "storm" in weather:
            last_forecast_alert_day = today
            draw_alert_popup("STORM WARNING", date[5:], 0xFF1744)
            speak("Warning! Storm expected on " + date[5:])
            alert_countdown(0xFF1744, 3)
            return
        if "snow" in weather:
            last_forecast_alert_day = today
            draw_alert_popup("SNOW ALERT", date[5:], 0x4A90D9)
            speak("Snow expected on " + date[5:] + ". Drive carefully!")
            alert_countdown(0x4A90D9, 3)
            return

def check_sensor_alerts():
    """
    Checks all sensor values against thresholds and queues alerts.
    Each alert is a tuple: (title, display_message, color, speech_text).
    Cooldowns: CO2/TVOC = 1h, humidity = 2h, indoor temp = 4h, outdoor temp = 24h.
    All queued alerts are shown sequentially: popup → speak → countdown.
    Screen is redrawn once at the end after all alerts have been shown.
    """
    alerts = []

    # --- CO2 check ---
    try:
        co2 = int(in_co2)
        if co2 > 1200 and can_alert("co2_poor", HOUR_MS):
            alerts.append(("AIR QUALITY ALERT", "CO2: " + str(co2) + "ppm", 0xFF1744,
                "Air quality is poor. CO2 at " + str(co2) + " ppm. Please open a window."))
        elif 750 < co2 <= 1200 and can_alert("co2_moderate", HOUR_MS):
            alerts.append(("AIR QUALITY WARNING", "CO2: " + str(co2) + "ppm", 0xFFD600,
                "Air quality is moderate. CO2 at " + str(co2) + " ppm."))
    except:
        pass

    # --- TVOC check ---
    try:
        tvoc = int(in_tvoc)
        if tvoc > 300 and can_alert("tvoc_high", HOUR_MS):
            alerts.append(("TVOC ALERT", "TVOC: " + str(tvoc) + "ppb", 0xFF6D00,
                "High pollutant levels detected. TVOC at " + str(tvoc) + " ppb."))
    except:
        pass

    # --- Humidity check ---
    try:
        hum = float(in_hum)
        if hum < 40 and can_alert("hum_low", HOUR2_MS):
            alerts.append(("LOW HUMIDITY", str(hum) + "%", 0xFF6D00,
                "Humidity too low at " + str(hum) + " percent. Use a humidifier."))
        elif hum > 70 and can_alert("hum_high", HOUR2_MS):
            alerts.append(("HIGH HUMIDITY", str(hum) + "%", 0x4A90D9,
                "Humidity too high at " + str(hum) + " percent. Risk of mold!"))
    except:
        pass

    # --- Indoor temperature check ---
    try:
        tin = float(in_temp)
        if tin > 28 and can_alert("temp_in_hot", HOUR4_MS):
            alerts.append(("HIGH TEMP INDOOR", str(tin) + "C", 0xFF4444,
                "It is very hot inside at " + str(tin) + " degrees. Consider cooling down."))
        elif tin < 16 and can_alert("temp_in_cold", HOUR4_MS):
            alerts.append(("LOW TEMP INDOOR", str(tin) + "C", 0x4A90D9,
                "It is quite cold inside at " + str(tin) + " degrees."))
    except:
        pass

    # --- Outdoor temperature check ---
    try:
        tout = float(out_temp)
        if tout > 35 and can_alert("temp_out_hot", DAY_MS):
            alerts.append(("EXTREME HEAT", str(tout) + "C outside", 0xFF1744,
                "Extreme heat outside at " + str(tout) + " degrees. Stay hydrated!"))
        elif tout < -5 and can_alert("temp_out_cold", DAY_MS):
            alerts.append(("FREEZING TEMP", str(tout) + "C outside", 0x4A90D9,
                "Freezing temperatures outside. Dress warmly!"))
    except:
        pass

    # Show all queued alerts sequentially
    for title, message, color, speech_text in alerts:
        draw_alert_popup(title, message, color)
        speak(speech_text)
        alert_countdown(color, 3)

    # Redraw the active screen once after all alerts are done
    if alerts:
        if active_screen == 1:
            draw_screen_1()
        elif active_screen == 2:
            draw_screen_2()
        elif active_screen == 3:
            draw_screen_3()

def check_pir_announce():
    """
    Triggered every 100ms by the main loop.
    If the PIR detects motion AND at least 1 hour has passed since
    the last announcement, it generates a contextual voice message:
    - Morning (6-9h):  umbrella reminder if rain in forecast, else temperature
                       + checks for storm/snow alerts
    - Evening (18-21h): indoor/outdoor temperature recap
    - Other hours:      generic weather update
    All messages are shown as a popup AND spoken aloud.
    """
    global last_pir_announce_ms
    if not pir_0.state:
        return  # no motion detected, skip
    now_ms = time.ticks_ms()
    # Enforce 1-hour minimum between announcements
    if last_pir_announce_ms != 0 and time.ticks_diff(now_ms, last_pir_announce_ms) < HOUR_MS:
        return
    last_pir_announce_ms = now_ms
    if not clock_synced:
        return  # can't build contextual messages without time

    hour = rtc.datetime()[4]  # current hour (0-23)
    msg     = ""
    title   = ""
    message = ""
    color   = 0x4A90D9

    if 6 <= hour < 9:
        # Morning: check forecast for rain and remind user about umbrella
        rain_day = ""
        for row in forecast_data:
            w = str(row.get("weather", "")).lower()
            if "rain" in w or "drizzle" in w:
                rain_day = row.get("day", "")
                break
        if rain_day:
            title   = "GOOD MORNING!"
            message = "Rain on " + rain_day[5:]
            color   = 0x4A90D9
            msg     = "Good morning! Rain expected on " + rain_day[5:] + ". Don't forget your umbrella!"
        else:
            title   = "GOOD MORNING!"
            message = "Indoor: " + str(in_temp) + "C"
            color   = 0x00E676
            msg     = "Good morning! Current indoor temperature is " + str(in_temp) + " degrees."
        # Also check for storm/snow alerts in the morning
        check_forecast_alerts()

    elif 18 <= hour < 21:
        # Evening: recap indoor and outdoor conditions
        title   = "GOOD EVENING!"
        message = "Indoor: " + str(in_temp) + "C"
        color   = 0x4A90D9
        msg     = "Good evening! Indoor temperature is " + str(in_temp) + " degrees. Outdoor is " + str(out_temp) + " degrees and " + str(out_weather) + "."

    else:
        # Any other time: generic weather update
        title   = "WEATHER UPDATE"
        message = str(out_temp) + "C " + str(out_weather)
        color   = 0x4A90D9
        msg     = "Current temperature is " + str(in_temp) + " degrees indoor and " + str(out_temp) + " degrees outdoor. " + str(out_weather) + "."

    if msg:
        draw_alert_popup(title, message, color)
        speak(msg)
        alert_countdown(color, 3)
        # Redraw the current screen after the popup closes
        if active_screen == 1:
            draw_screen_1()
        elif active_screen == 2:
            draw_screen_2()
        elif active_screen == 3:
            draw_screen_3()

# ============================================================
# AI ANSWER
# Sends the user's question + current sensor context to the /ask endpoint.
# The Flask server enriches the context with historical BigQuery data
# and 3-day forecast, then passes everything to GPT-4o-mini.
# Answers are limited to 10 words for readability on the small screen.
# ============================================================

def generate_answer(question):
    """
    Sends a question to the AI endpoint and returns a short answer.
    The context includes all current sensor readings so the AI can
    answer questions like "is the air quality good?" or "what is the temperature?".
    The server also has access to historical data (last 3 days) and forecast.
    """
    try:
        payload = {
            "passwd": YOUR_HASH_PASSWD,
            "question": question,
            "context": {
                "in_temp":   in_temp,
                "in_hum":    in_hum,
                "in_co2":    in_co2,
                "in_tvoc":   in_tvoc,
                "out_temp":  out_temp,
                "out_weather": out_weather
            }
        }
        res = urequests.post(URL_CLOUD_RUN + "/ask", json=payload)
        d = res.json()
        res.close()
        return d.get("answer", "I could not answer")
    except Exception as e:
        print("Ask error:", e)
        return "Connection error"

# ============================================================
# 9. NAVBAR
# Draws the bottom navigation bar with 3 tabs.
# The active tab has a blue top border and brighter text.
# Inactive tabs are shown in gray.
# ============================================================

def draw_navbar(active):
    """
    Draws the bottom navigation bar.
    active: 1=DASHB., 2=FOREC., 3=HISTORY
    The bar occupies Y:212-240 (28px).
    Each tab is 107/107/106 pixels wide (total = 320px).
    """
    NAV_Y = 212
    NAV_H = 28
    lcd.rect(0, NAV_Y, 320, NAV_H, 0x0A0F1E, 0x0A0F1E)
    lcd.line(0, NAV_Y, 320, NAV_Y, 0x1C2A45)
    labels   = ["DASHB.", "FOREC.", "HISTORY"]
    x_starts = [0, 107, 214]
    widths   = [107, 107, 106]
    for i in range(3):
        txt_w  = len(labels[i]) * 10
        offset = (widths[i] - txt_w) // 2
        txt_y  = NAV_Y + 6
        if (i + 1) == active:
            # Active tab: highlighted background + blue top border
            lcd.rect(x_starts[i], NAV_Y, widths[i], NAV_H, 0x1A2744, 0x1A2744)
            lcd.rect(x_starts[i], NAV_Y, widths[i], 2, 0x4A90D9, 0x4A90D9)
            lcd.font(lcd.FONT_DejaVu18)
            lcd.print(labels[i], x_starts[i] + offset, txt_y, 0x4A90D9)
        else:
            # Inactive tab: gray text, no highlight
            lcd.font(lcd.FONT_DejaVu18)
            lcd.print(labels[i], x_starts[i] + offset, txt_y, 0x4A5568)
    lcd.line(107, NAV_Y, 107, 240, 0x1C2A45)
    lcd.line(214, NAV_Y, 214, 240, 0x1C2A45)

# ============================================================
# 10. CLOCK ZONE
# Draws only the top clock area (Y:0-57) without clearing the whole screen.
# This is called every minute to update the time display,
# avoiding full screen flicker by redrawing only the changed area.
# ============================================================

def draw_clock_zone():
    """
    Partially redraws only the clock area to update the displayed time.
    Called when get_minute() returns a different value than last_displayed_minute.
    Uses FONT_DejaVu40 (large) for the time and FONT_DejaVu18 for the date.
    """
    lcd.rect(0, 0, 320, 57, 0x080B14, 0x080B14)  # clear only clock area
    lcd.font(lcd.FONT_DejaVu40)
    lcd.print(get_time_str(), 100, 3, lcd.WHITE)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print(get_date_str(), 66, 38, 0x4A90D9)
    lcd.line(20, 56, 300, 56, 0x1C2A45)

# ============================================================
# 11. SCREEN 1 — DASHBOARD
# The main screen layout (320x240px):
#   Y 0-57:   Clock zone (time + date)
#   Y 57-163: Indoor panel (left) | Outdoor panel (right)
#   Y 165-210: Air quality strip (label + gradient bar)
#   Y 212-240: Navigation bar
# ============================================================

def draw_screen_1():
    """
    Draws the full Dashboard screen.
    - Left panel: indoor temperature (large font) + humidity
    - Right panel: outdoor temperature + weather icon + description
    - Bottom strip: air quality label + gradient score bar
    Full lcd.clear() is called because the whole screen changes.
    """
    lcd.clear()
    screen.set_screen_bg_color(0x080B14)
    draw_clock_zone()

    # --- Indoor panel: x=4-156, y=57-163 ---
    lcd.rect(4, 57, 152, 106, 0x0D1526, 0x0D1526)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("INDOOR", 46, 60, 0x4A90D9)
    lcd.line(4, 76, 156, 76, 0x1C2A45)
    draw_thermometer(18, 82, 0x4A90D9)
    lcd.font(lcd.FONT_DejaVu40)  # large font for temperature value
    lcd.print(str(in_temp), 36, 80, lcd.WHITE)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("\xb0C", 138, 80, 0x8AAFD4)  # \xb0 = degree symbol
    draw_droplet(18, 127, 0x4A90D9)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("Hum  " + str(in_hum) + "%", 36, 128, 0x8AAFD4)

    # Vertical divider between indoor and outdoor panels
    lcd.line(160, 55, 160, 210, 0x1C2A45)

    # --- Outdoor panel: x=164-316, y=57-163 ---
    lcd.rect(164, 57, 152, 106, 0x0D1526, 0x0D1526)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("OUTDOOR", 178, 60, 0x4A90D9)
    lcd.line(164, 76, 316, 76, 0x1C2A45)
    draw_weather_icon(178, 100, out_weather)  # icon based on weather description
    lcd.font(lcd.FONT_DejaVu40)
    lcd.print(str(out_temp), 202, 80, lcd.WHITE)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("\xb0C", 296, 80, 0x8AAFD4)
    line1, line2 = shorten_weather(out_weather)  # split long descriptions into 2 lines
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print(line1, 168, 126, 0x8AAFD4)
    if line2:
        lcd.print(line2, 168, 144, 0x8AAFD4)

    # --- Air quality strip: y=165-210 ---
    lcd.line(10, 165, 310, 165, 0x1C2A45)
    label, lcolor = air_quality_label(in_co2, in_tvoc)  # GOOD/MODERATE/POOR
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("AIR QUALITY", 95, 167, 0x4A90D9)
    # Small leaf icon (3 circles + stem line)
    lcd.circle(16, 186, 4, lcolor, lcolor)
    lcd.circle(16, 182, 3, lcolor, lcolor)
    lcd.line(16, 190, 16, 196, lcolor)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print(label, 28, 183, lcolor)
    score = air_quality_score(in_co2, in_tvoc)
    draw_air_bar(155, 190, 155, 14, score)
    draw_navbar(1)

# ============================================================
# 12. SCREEN 2 — FORECAST + AI
# Top half: 3-day forecast panels (each 106px wide)
# Bottom half: AI assistant with answer box and microphone touch button
# The forecast icons always use force_day=True since data is from 14:00.
# The mic button is a touch area (248-316, 138-204) — not a real button.
# ============================================================

# Stores the last AI answer to persist between screen refreshes
last_ai_answer = "Touch mic to ask"

def draw_screen_2():
    """
    Draws the Forecast + AI screen.
    - 3 forecast panels at top: weekday name + weather icon + temperature
    - AI ASSISTANT title bar in the middle
    - Answer box (left): shows last AI response split into 3 lines of 22 chars
    - Mic button (right): touch area that triggers record_and_transcribe()
    """
    lcd.clear()
    screen.set_screen_bg_color(0x080B14)
    WEEK_DAYS = ["MON","TUE","WED","THU","FRI","SAT","SUN"]

    # 3 forecast panels: first is highlighted (blue accent), others are dimmer
    panels = [
        {"x": 0,   "w": 106, "cx": 53,  "accent": 0x4A90D9, "lcolor": 0x4A90D9},
        {"x": 107, "w": 106, "cx": 160, "accent": 0x1C3A6E, "lcolor": 0x8AAFD4},
        {"x": 214, "w": 106, "cx": 267, "accent": 0x1C3A6E, "lcolor": 0x8AAFD4},
    ]

    for i, p in enumerate(panels):
        x  = p["x"]
        cx = p["cx"]  # horizontal center of this panel
        w  = p["w"]
        lcd.rect(x, 0, w, 115, 0x0D1526, 0x0D1526)
        lcd.rect(x, 0, w, 3, p["accent"], p["accent"])  # colored top accent bar
        if i < len(forecast_data):
            row = forecast_data[i]
            # Calculate weekday name from the date string "YYYY-MM-DD"
            try:
                parts = row["day"].split("-")
                wd = get_weekday(int(parts[0]), int(parts[1]), int(parts[2]))
                day_label = WEEK_DAYS[wd]
            except:
                day_label = "---"
            lcd.font(lcd.FONT_DejaVu18)
            lcd.print(day_label, x + 8, 5, p["lcolor"])
            # force_day=True: always show sun/cloud (not moon) for future forecasts
            draw_weather_icon_lg(cx, 55, row.get("weather", ""), True)
            temp_str = str(row.get("temp", "--"))
            lcd.font(lcd.FONT_DejaVu18)
            lcd.print(temp_str, cx - 18, 92, lcd.WHITE)
            lcd.print("\xb0C", cx + 6, 92, 0x8AAFD4)
        else:
            lcd.font(lcd.FONT_DejaVu18)
            lcd.print("---", cx - 15, 55, 0x4A5568)

    # Vertical dividers between forecast panels
    lcd.line(106, 0, 106, 115, 0x1C2A45)
    lcd.line(213, 0, 213, 115, 0x1C2A45)
    lcd.line(0, 116, 320, 116, 0x1C2A45)

    # AI ASSISTANT title bar
    lcd.rect(0, 117, 320, 20, 0x0D1526, 0x0D1526)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("AI ASSISTANT", 90, 119, 0x4A90D9)
    lcd.line(0, 137, 320, 137, 0x1C2A45)

    # Answer box: shows last AI response split into 3 lines of 22 characters
    lcd.rect(6, 140, 236, 63, 0x080B14, 0x080B14)
    lcd.rect(6, 140, 3, 63, 0x4A90D9, 0x4A90D9)  # blue left accent border
    answer = str(last_ai_answer)
    line1 = answer[:22]
    line2 = answer[22:44]
    line3 = answer[44:66]
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print(line1, 14, 145, lcd.WHITE)
    if line2:
        lcd.print(line2, 14, 165, 0x8AAFD4)
    if line3:
        lcd.print(line3, 14, 185, 0x8AAFD4)

    # Microphone button: drawn with LCD primitives (body + arc + stand)
    # Touch area: x=248-316, y=138-204 (detected in main loop)
    lcd.rect(248, 138, 68, 66, 0x1A2744, 0x1A2744)
    lcd.rect(248, 138, 68, 3, 0x4A90D9, 0x4A90D9)
    lcd.circle(281, 148, 7, 0x4A90D9, 0x4A90D9)   # top cap
    lcd.circle(281, 168, 7, 0x4A90D9, 0x4A90D9)   # bottom cap
    lcd.rect(274, 148, 14, 20, 0x4A90D9, 0x4A90D9) # body
    lcd.line(264, 172, 264, 178, 0x4A90D9)  # arc left side
    lcd.line(298, 172, 298, 178, 0x4A90D9)  # arc right side
    lcd.line(264, 178, 270, 184, 0x4A90D9)  # arc bottom left
    lcd.line(298, 178, 292, 184, 0x4A90D9)  # arc bottom right
    lcd.line(270, 184, 292, 184, 0x4A90D9)  # arc base
    lcd.line(281, 184, 281, 192, 0x4A90D9)  # stand vertical
    lcd.line(273, 192, 289, 192, 0x4A90D9)  # stand base
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("TOUCH", 246, 195, 0x4A90D9)
    draw_navbar(2)

# ============================================================
# 13. SCREEN 3 — HISTORY
# Shows the last 5 sensor readings from BigQuery in a table.
# Uses a 3-step approach to avoid blank screen during data fetch:
# 1. Show "Loading..." immediately
# 2. Fetch data from BigQuery (takes ~1-2 seconds)
# 3. Redraw the screen with the fetched data
# Falls back to the local in-memory history buffer if BigQuery fails.
# ============================================================

def draw_screen_3():

    # ---- STEP 1: Show loading screen immediately so user sees feedback ----
    lcd.clear(0x080B14)  # Clear with background color
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("HISTORY", 118, 5, 0xFF9900)
    lcd.line(10, 23, 310, 23, 0x1C2A45)
    lcd.print("Loading data...", 88, 110, 0x4A90D9)

    # ---- STEP 2: Fetch data while loading screen is visible ----
    rows = get_history_from_bigquery()
    if not rows:
        rows = history  # fallback to local buffer if BigQuery unreachable

    # ---- STEP 3: Draw the complete table with fetched data ----
    lcd.clear(0x080B14)

    # Header
    lcd.font(lcd.FONT_DejaVu18)
    lcd.print("HISTORY", 118, 5, 0xFF9900)
    lcd.line(10, 23, 310, 23, 0x1C2A45)

    # Column labels
    lcd.print("TIME", 16,  26, 0x4A5568)
    lcd.print("TEMP", 84,  26, 0x4A5568)
    lcd.print("HUM",  148, 26, 0x4A5568)
    lcd.print("CO2",  206, 26, 0x4A5568)
    lcd.print("TVOC", 262, 26, 0x4A5568)
    lcd.line(10, 43, 310, 43, 0x1C2A45)

    if not rows:
        lcd.print("No data available", 70, 110, 0x4A5568)
        draw_navbar(3)
        return

    # Draw up to 5 rows (each row is 27px tall)
    for i, row in enumerate(rows[:5]):
        y = 44 + i * 27

        if i == 0:
            # Most recent row: highlighted background + blue left accent
            lcd.rect(8, y, 304, 26, 0x1A2744, 0x1A2744)
            lcd.rect(8, y, 4,   26, 0x4A90D9, 0x4A90D9)
        else:
            # Older rows: darker background + gray left accent
            lcd.rect(8, y, 304, 26, 0x0D1526, 0x0D1526)
            lcd.rect(8, y, 4,   26, 0x1C2A45, 0x1C2A45)

        t_color = lcd.WHITE if i == 0 else 0x8AAFD4
        lcd.font(lcd.FONT_DejaVu18)

        # Time: show only HH:MM (first 5 characters)
        t_str = str(row.get("time", "--"))[:5]
        lcd.print(t_str, 16, y + 5, t_color)

        temp_val = row.get("indoor_temp", "--")
        lcd.print(str(temp_val) + "\xb0", 84, y + 5, lcd.WHITE)

        hum_val = row.get("indoor_humidity", "--")
        try:    hum_str = str(int(hum_val)) + "%"
        except: hum_str = "--"
        lcd.print(hum_str, 148, y + 5, lcd.WHITE)

        # CO2 and TVOC use color-coded values (green/yellow/red)
        co2_val = row.get("indoor_co2", "--")
        lcd.print(str(co2_val), 206, y + 5, co2_color(co2_val))

        tvoc_val = row.get("indoor_tvoc", "--")
        lcd.print(str(tvoc_val), 262, y + 5, tvoc_color(tvoc_val))

    # Color legend at the bottom
    lcd.line(10, 179, 310, 179, 0x1C2A45)
    lcd.font(lcd.FONT_DejaVu18)
    lcd.circle(13,  191, 3, 0x00E676, 0x00E676)
    lcd.print("Good", 20,  182, 0x4A5568)
    lcd.circle(73,  191, 3, 0xFFD600, 0xFFD600)
    lcd.print("Mod.", 80,  182, 0x4A5568)
    lcd.circle(133, 191, 3, 0xFF4444, 0xFF4444)
    lcd.print("Poor", 140, 182, 0x4A5568)
    lcd.print("ppm", 206, 182, 0x4A5568)  # unit for CO2
    lcd.print("ppb", 262, 182, 0x4A5568)  # unit for TVOC

    draw_navbar(3)

# ============================================================
# 14. STARTUP SEQUENCE
# Runs once when the device is powered on.
# Order is important:
# 1. load_cache() first → screen shows something even without WiFi
# 2. get_latest_from_bigquery() → restores last known values from cloud
# 3. get_outdoor_weather() → fetches current outdoor conditions
# 4. get_forecast_from_server() → fetches 3-day forecast
# 5. Set last_sensor_ms = -INTERVAL so first sync happens immediately
# 6. Draw the dashboard
# ============================================================
lcd.clear()
lcd.font(lcd.FONT_DejaVu18)
lcd.print("Initializing...", 80, 100, 0x4A90D9)

# Step 1: load local cache first (works without WiFi)
load_cache()

# Step 2: sync with BigQuery to restore last recorded values
lcd.rect(0, 95, 320, 55, 0x080B14, 0x080B14)
lcd.print("Restoring data...", 70, 100, 0x4A90D9)
get_latest_from_bigquery()

# Step 3: fetch current outdoor weather
lcd.rect(0, 95, 320, 55, 0x080B14, 0x080B14)
lcd.print("Getting weather...", 70, 100, 0x4A90D9)
get_outdoor_weather()

# Step 4: fetch 3-day forecast for Screen 2
lcd.rect(0, 95, 320, 55, 0x080B14, 0x080B14)
lcd.print("Getting forecast...", 70, 100, 0x4A90D9)
get_forecast_from_server()

# Step 5: set sensor timer to negative so first sync fires immediately in main loop
last_sensor_ms = -SENSOR_INTERVAL_MS
last_displayed_minute = get_minute()
draw_screen_1()

# ============================================================
# 15. MAIN LOOP
# Runs continuously at ~100ms per cycle (10 Hz).
# Handles:
# - Touch input (Screen 2 mic button)
# - Physical button navigation (A/B/C)
# - Clock minute update (partial redraw, no flicker)
# - Sensor + cloud sync every 5 minutes
# - PIR announce check every cycle
# ============================================================
while True:
    # --- Touch input: only active on Screen 2 ---
    # Detects touch in the microphone button area (x:248-316, y:138-204)
    if active_screen == 2:
        if touch.status():
            tx, ty = touch.read()
            if 248 <= tx <= 316 and 138 <= ty <= 204:
                record_and_transcribe()  # full STT → AI → TTS pipeline

    # --- Physical button navigation ---
    # BTN A → go to Screen 1 (Dashboard)
    # BTN B → go to Screen 2 (Forecast + AI), refresh forecast data
    # BTN C → go to Screen 3 (History), fetches fresh data from BigQuery
    if btnA.wasPressed():
        active_screen = 1
        last_displayed_minute = get_minute()
        draw_screen_1()
    elif btnB.wasPressed():
        active_screen = 2
        get_forecast_from_server()  # refresh forecast when opening screen 2
        draw_screen_2()
    elif btnC.wasPressed():
        active_screen = 3
        draw_screen_3()

    now_ms = time.ticks_ms()

    # --- Clock update: redraw only the top zone when minute changes ---
    # Avoids full screen redraw (which would cause visible flicker)
    if active_screen == 1:
        current_minute = get_minute()
        if current_minute != last_displayed_minute:
            last_displayed_minute = current_minute
            draw_clock_zone()  # partial redraw of Y:0-57 only

    # --- Sensor + cloud sync every 5 minutes ---
    # ticks_diff handles millisecond counter overflow correctly
    if time.ticks_diff(now_ms, last_sensor_ms) >= SENSOR_INTERVAL_MS:
        last_sensor_ms = now_ms

        # Read all physical sensors
        in_temp   = round(env3_0.temperature, 1)  # ENV3 temperature
        in_hum    = round(env3_0.humidity, 1)      # ENV3 humidity
        in_co2    = tvoc_0.eCO2                    # estimated CO2 from TVOC sensor
        in_tvoc   = tvoc_0.TVOC                    # Total VOC in ppb
        motion    = pir_0.state                    # PIR: 1=motion, 0=no motion

        # Push data to cloud and update outdoor weather
        send_to_bigquery()
        get_outdoor_weather()
        get_forecast_from_server()
        save_cache()           # persist to flash for offline resilience

        check_sensor_alerts()  # check thresholds and show alerts if needed

        # Add new entry to top of local history buffer (newest first)
        entry = {
            "time": get_time_str(),
            "date": get_date_for_payload(),
            "indoor_temp":     in_temp,
            "indoor_humidity": in_hum,
            "indoor_co2":      in_co2,
            "indoor_tvoc":     in_tvoc
        }
        history.insert(0, entry)  # insert at position 0 = newest first

        # Keep only the last 5 entries
        if len(history) > 5:
            history.pop()  # remove oldest entry from the end

        # Redraw the currently active screen with fresh data
        if active_screen == 1:
            draw_screen_1()
        elif active_screen == 2:
            draw_screen_2()
        elif active_screen == 3:
            draw_screen_3()

    # --- PIR check: runs every loop cycle (100ms) ---
    # The function itself checks the 1-hour cooldown internally
    check_pir_announce()

    time.sleep_ms(100)  # ~10Hz loop rate
