"""
Flask Backend — Cloud Run Server
=================================
This is the middleware layer of the 3-tier architecture.
It runs on Google Cloud Run and acts as the bridge between:
  - The M5Stack device (sends/receives data via HTTP POST)
  - Google BigQuery (stores all historical sensor data)
  - OpenWeatherMap API (provides outdoor weather data)
  - OpenAI APIs (TTS, STT Whisper, GPT-4o-mini for AI answers)

All endpoints require a password hash for authentication.
Sensitive credentials are loaded from environment variables (not hardcoded).
"""

from flask import Flask, request
import os
from google.cloud import bigquery
import requests
from datetime import datetime
from openai import OpenAI
import io
from flask import send_file, jsonify

# ============================================================
# CLIENT INITIALIZATION
# OpenAI and BigQuery clients are initialized once at startup.
# Credentials are loaded from environment variables set in Cloud Run,
# never hardcoded in the source code for security reasons.
# ============================================================

# OpenAI client: used for TTS (/speak), STT Whisper (/transcribe), GPT-4o-mini (/ask)
openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

# BigQuery client: connects to the Google Cloud project
# Authentication is handled automatically by Cloud Run's service account
client = bigquery.Client(project="durable-will-487916-n1")

# Password hash loaded from environment variable.
# The device sends this hash with every request to authenticate.
# Never stored in plain text — only the SHA-256 hash is used.
YOUR_HASH_PASSWD = os.environ.get("HASH_PASSWD")

# Initialize the Flask application
app = Flask(__name__)

# Fetch a sample of the BigQuery table at startup to verify the connection
# and confirm the table schema is accessible.
q = """
SELECT * FROM `durable-will-487916-n1.Lab4_IoT_datasets.weather-records` LIMIT 10
"""
query_job = client.query(q)
df = query_job.to_dataframe()

# ============================================================
# ENDPOINT: /send-to-bigquery
# Called by the device every 5 minutes with the latest sensor readings.
# The server enriches the payload with outdoor weather from OpenWeatherMap,
# then inserts the complete row into BigQuery using a dynamic INSERT query.
# ============================================================

@app.route('/send-to-bigquery', methods=['GET', 'POST'])
def send_to_bigquery():
    """
    Receives indoor sensor data from the device and stores it in BigQuery.
    
    Steps:
    1. Authenticate the request using the password hash
    2. Extract sensor values from the JSON payload
    3. Fetch current outdoor weather from OpenWeatherMap API
    4. Add outdoor weather fields to the data dictionary
    5. Build and execute a dynamic BigQuery INSERT query
    6. Return success status with the stored data
    
    The INSERT query is built dynamically from the data dictionary keys,
    so adding new fields to the device payload automatically extends the table.
    String values are wrapped in single quotes; numbers are inserted as-is.
    """
    if request.method == 'POST':
        # Authentication: reject requests with wrong password
        if request.get_json(force=True)["passwd"] != YOUR_HASH_PASSWD:
            raise Exception("Incorrect Password!")
        
        # Extract the sensor values dictionary from the request body
        data = request.get_json(force=True)["values"]
        
        # --- Fetch outdoor weather from OpenWeatherMap and add to payload ---
        # This enriches every BigQuery row with outdoor conditions at the time of recording
        API_key = os.environ.get("OPENWEATHER_API_KEY")
        city = "Lausanne"
        url = f'http://api.openweathermap.org/data/2.5/weather?q={city}&appid={API_key}&units=metric'
        
        try:
            response = requests.get(url)
            weather_json = response.json()
            
            # Add outdoor fields to the data dictionary (rounded values)
            data["outdoor_temp"] = float(round(weather_json['main']['temp']))
            data["outdoor_humidity"] = float(round(weather_json['main']['humidity']))
            data["outdoor_weather"] = str(weather_json['weather'][0]['description'])
        except Exception as e:
            print(f"OpenWeather API error: {e}")
            # Safe fallback values to prevent BigQuery INSERT from failing
            data["outdoor_temp"] = 0.0
            data["outdoor_humidity"] = 0.0
            data["outdoor_weather"] = "Error"
        # -------------------------------------------------------------------
        
        # --- Build dynamic BigQuery INSERT query from the data dictionary ---
        # This approach allows adding new columns without changing the query code
        q = """INSERT INTO `durable-will-487916-n1.Lab4_IoT_datasets.weather-records` 
        """
        names = ""
        values = ""
        for k, v in data.items():
            names += f"""{k},"""
            
            # Numbers are inserted directly; strings need single quotes in SQL
            if isinstance(v, (int, float)):
                values += f"""{v},"""
            else:
                values += f"""'{v}',"""
                
        # Remove trailing commas from both lists
        names = names[:-1]
        values = values[:-1]
        q = q + f""" ({names})""" + f""" VALUES({values})"""
        query_job = client.query(q)
        # Wait for the INSERT to complete before returning — ensures data consistency
        query_job.result() 
        
        return {"status": "success", "data": data}
    return {"status": "failed"}
        

# ============================================================
# ENDPOINT: /get_outdoor_weather
# Called by the device to get the current outdoor weather conditions.
# Instead of calling OpenWeatherMap directly from the device,
# the server fetches the most recent row from BigQuery which already
# contains the outdoor weather stored by the last /send-to-bigquery call.
# This avoids the device needing to manage a second API key.
# ============================================================

@app.route('/get_outdoor_weather', methods=['GET', 'POST'])
def get_outdoor_weather():
    """
    Returns the latest outdoor weather data from BigQuery.
    Queries the most recent record ordered by date DESC, time DESC.
    Returns outdoor_temp, outdoor_humidity, and outdoor_weather description.
    """
    if request.method == 'POST':
        # Authentication check
        if request.get_json(force=True)["passwd"] != YOUR_HASH_PASSWD:
            raise Exception("Incorrect Password!")
            
        # Query to get only the outdoor columns from the latest row
        q_latest = """
        SELECT outdoor_temp, outdoor_humidity, outdoor_weather 
        FROM `durable-will-487916-n1.Lab4_IoT_datasets.weather-records`
        ORDER BY date DESC, time DESC 
        LIMIT 1
        """
        
        try:
            query_job = client.query(q_latest)
            results = list(query_job.result())
            
            # If at least one row exists, return its outdoor values
            if results:
                latest_row = results[0]
                return {
                    "status": "success",
                    "outdoor_temp": latest_row.outdoor_temp,
                    "outdoor_humidity": latest_row.outdoor_humidity,
                    "outdoor_weather": latest_row.outdoor_weather
                }
            else:
                return {"status": "failed", "error": "Empty table"}
                
        except Exception as e:
            return {"status": "error", "message": str(e)}
            
    return {"status": "failed"}


# ============================================================
# ENDPOINT: /get_latest
# Called by the device at startup to restore the last known sensor values.
# This is critical for the offline resilience requirement:
# even if the device was off for hours, it shows the last recorded
# indoor values instead of showing "--" everywhere.
# Tested during the presentation: device turned off → turned on → shows last values.
# ============================================================

@app.route('/get_latest', methods=['GET', 'POST'])
def get_latest():
    """
    Returns the most recent indoor sensor record from BigQuery.
    Called once at device startup to sync the display with the last known state.
    Returns: date, time, indoor_temp, indoor_humidity, indoor_co2, indoor_tvoc.
    """
    if request.method == 'POST':
        if request.get_json(force=True)["passwd"] != YOUR_HASH_PASSWD:
            raise Exception("Incorrect Password!")
        
        # Get the single most recent record ordered by date and time
        q_latest = """
        SELECT date, time, indoor_temp, indoor_humidity, indoor_co2, indoor_tvoc
        FROM `durable-will-487916-n1.Lab4_IoT_datasets.weather-records`
        ORDER BY date DESC, time DESC
        LIMIT 1
        """
        try:
            results = list(client.query(q_latest).result())
            if results:
                row = results[0]
                return {
                    "status":           "success",
                    "date":             str(row.date),
                    "time":             str(row.time),
                    "indoor_temp":      row.indoor_temp,
                    "indoor_humidity":  row.indoor_humidity,
                    "indoor_co2":       row.indoor_co2,
                    "indoor_tvoc":      row.indoor_tvoc
                }
            else:
                return {"status": "failed", "error": "No data found"}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "failed"}


# ============================================================
# ENDPOINT: /get_history
# Returns the last 5 sensor records for the History screen on the device.
# Records are ordered newest first (date DESC, time DESC).
# The device displays them in a table with color-coded CO2 and TVOC values.
# ============================================================

@app.route('/get_history', methods=['GET', 'POST'])
def get_history():
    """
    Returns the last 5 indoor sensor records from BigQuery.
    Used by Screen 3 (History) on the device to display a data table.
    Each row contains: date, time, indoor_temp, indoor_humidity, indoor_co2, indoor_tvoc.
    """
    if request.method == 'POST':
        if request.get_json(force=True)["passwd"] != YOUR_HASH_PASSWD:
            raise Exception("Incorrect Password!")
        
        # Get last 5 records, newest first
        q_history = """
        SELECT date, time, indoor_temp, indoor_humidity, indoor_co2, indoor_tvoc
        FROM `durable-will-487916-n1.Lab4_IoT_datasets.weather-records`
        ORDER BY date DESC, time DESC
        LIMIT 5
        """
        try:
            results = list(client.query(q_history).result())
            rows = []
            for row in results:
                # Convert BigQuery row to plain dict for JSON serialization
                rows.append({
                    "date":            str(row.date),
                    "time":            str(row.time),
                    "indoor_temp":     row.indoor_temp,
                    "indoor_humidity": row.indoor_humidity,
                    "indoor_co2":      row.indoor_co2,
                    "indoor_tvoc":     row.indoor_tvoc
                })
            return {"status": "success", "rows": rows}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "failed"}


# ============================================================
# ENDPOINT: /speak/<text>
# Text-to-Speech endpoint using OpenAI TTS API (model: tts-1, voice: nova).
# OpenAI returns raw PCM audio at 24000Hz signed 16-bit mono.
# The M5Stack Core2 speaker requires UNSIGNED 16-bit PCM,
# so every sample is converted: val = signed_val + 32768
# The converted audio is wrapped in a proper WAV file header and returned.
# ============================================================

@app.route('/speak/<path:text>', methods=['GET'])
def speak_get(text):
    """
    Converts text to speech and returns a WAV audio file.
    
    Audio pipeline:
    1. Call OpenAI TTS API → raw signed PCM (24000Hz, 16-bit, mono)
    2. Convert each sample from signed int16 to unsigned uint16 (required by Core2)
    3. Build a valid WAV file header (RIFF format)
    4. Return the complete WAV file as a binary HTTP response
    
    The WAV header fields:
    - ChunkSize: 36 + data size
    - AudioFormat: 1 (PCM)
    - NumChannels: 1 (mono)
    - SampleRate: 24000 Hz
    - ByteRate: SampleRate × 2 (16-bit = 2 bytes per sample)
    - BitsPerSample: 16
    """
    try:
        import struct

        # Call OpenAI TTS: returns raw PCM bytes (no WAV header)
        response = openai_client.audio.speech.create(
            model="tts-1",
            voice="nova",
            input=text,
            response_format="pcm",  # raw PCM, no WAV wrapper
            speed=0.85              # slightly slower for clearer audio on device
        )

        raw = response.content  # signed 16-bit PCM at 24000Hz

        # --- Convert signed int16 → unsigned uint16 ---
        # The M5Stack Core2 speaker expects unsigned PCM.
        # Process 2 bytes at a time (one 16-bit sample):
        #   1. Reconstruct the signed integer from two little-endian bytes
        #   2. If >= 32768, it's a negative number in two's complement → subtract 65536
        #   3. Add 32768 to shift the range from [-32768, 32767] to [0, 65535]
        converted = bytearray()
        for i in range(0, len(raw) - 1, 2):
            val = raw[i] | (raw[i+1] << 8)      # combine 2 bytes (little-endian)
            if val >= 32768:
                val -= 65536                      # convert from two's complement
            val = val + 32768                     # shift to unsigned range
            converted += struct.pack('<H', val)   # pack as unsigned 16-bit little-endian

        # --- Build WAV file header (RIFF format) ---
        sr = 24000        # sample rate in Hz
        data_size = len(converted)

        output = io.BytesIO()
        output.write(b'RIFF')                           # RIFF chunk identifier
        output.write(struct.pack('<I', 36 + data_size)) # total file size - 8 bytes
        output.write(b'WAVE')                           # format identifier
        output.write(b'fmt ')                           # format sub-chunk
        output.write(struct.pack('<I', 16))             # sub-chunk size (16 for PCM)
        output.write(struct.pack('<H', 1))              # audio format: 1 = PCM
        output.write(struct.pack('<H', 1))              # num channels: 1 = mono
        output.write(struct.pack('<I', sr))             # sample rate: 24000
        output.write(struct.pack('<I', sr * 2))         # byte rate = sr × channels × bits/8
        output.write(struct.pack('<H', 2))              # block align = channels × bits/8
        output.write(struct.pack('<H', 16))             # bits per sample
        output.write(b'data')                           # data sub-chunk
        output.write(struct.pack('<I', data_size))      # data size in bytes
        output.write(converted)                         # actual audio samples
        output.seek(0)

        return send_file(
            output,
            mimetype='audio/wav',
            as_attachment=False,
            download_name='speech.wav'
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# ENDPOINT: /transcribe
# Speech-to-Text endpoint using OpenAI Whisper.
# The device records 4 seconds of audio, base64-encodes the WAV file,
# and sends it here. The server decodes it and sends it to Whisper,
# which returns the transcribed text in English.
# ============================================================

@app.route('/transcribe', methods=['POST'])
def transcribe():
    """
    Transcribes audio recorded by the M5Stack microphone using OpenAI Whisper.
    
    Request body (JSON):
    - passwd: authentication hash
    - audio: base64-encoded WAV file from the device microphone
    
    Returns the transcribed text string which is then sent to /ask
    for an AI-generated response.
    
    Language is fixed to English to improve recognition accuracy.
    """
    data = request.get_json(force=True)
    if data.get("passwd") != YOUR_HASH_PASSWD:
        return jsonify({"error": "Unauthorized"}), 401

    audio_b64 = data.get("audio", "")
    if not audio_b64:
        return jsonify({"error": "No audio provided"}), 400

    try:
        import base64
        # Decode base64 audio back to raw bytes
        audio_bytes = base64.b64decode(audio_b64)
        # Wrap in BytesIO and set filename so Whisper recognizes the format
        audio_buffer = io.BytesIO(audio_bytes)
        audio_buffer.name = "recording.wav"

        # Send to Whisper STT model
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_buffer,
            language="en"  # fixed to English for better accuracy
        )
        return jsonify({
            "status": "success",
            "text": transcript.text
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# ENDPOINT: /get_forecast
# Returns a 3-day weather forecast from OpenWeatherMap's /forecast API.
# The API returns data every 3 hours for 5 days (up to 40 entries).
# We group entries by day and select the one closest to 14:00 local time
# (12:00 UTC = 14:00 CEST) as the representative forecast for each day.
# Today's data is excluded — only future days are returned.
# ============================================================

@app.route('/get_forecast', methods=['GET', 'POST'])
def get_forecast():
    """
    Returns a 3-day weather forecast with one entry per day.
    
    Algorithm:
    1. Fetch 40 forecast entries (5 days × 8 entries/day) from OpenWeatherMap
    2. Group entries by date into a dictionary
    3. Skip today's entries (we already show current weather on Screen 1)
    4. For each future day, prefer the entry at 12:00 UTC (= 14:00 CEST)
       falling back to 11:00 → 13:00 → first available
    5. Return at most 3 days
    
    Returns for each day: date string, temperature (°C), weather description.
    """
    if request.method == 'POST':
        if request.get_json(force=True)["passwd"] != YOUR_HASH_PASSWD:
            raise Exception("Incorrect Password!")
        
        API_key = os.environ.get("OPENWEATHER_API_KEY")
        city = "Lausanne"
        # cnt=40 requests 5 days of 3-hourly data (maximum available on free plan)
        url = f'http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={API_key}&units=metric&cnt=40'
        
        try:
            response = requests.get(url)
            data = response.json()
            today = datetime.now().strftime("%Y-%m-%d")
            
            # Group all forecast entries by date and hour
            # Structure: {date: {hour: item, ...}, ...}
            days_raw = {}
            for item in data['list']:
                date = item['dt_txt'][:10]   # extract 'YYYY-MM-DD'
                hour = item['dt_txt'][11:13] # extract 'HH' (UTC)
                if date == today:
                    continue  # skip today
                if date not in days_raw:
                    days_raw[date] = {}
                days_raw[date][hour] = item
            
            # For each day, pick the entry closest to 14:00 local (12:00 UTC)
            # Priority: 12:00 → 11:00 → 13:00 → first available
            days = []
            for date, hours in sorted(days_raw.items())[:3]:
                item = (hours.get("12") or
                        hours.get("11") or
                        hours.get("13") or
                        list(hours.values())[0])
                days.append({
                    "day":     date,
                    "temp":    round(item['main']['temp']),
                    "weather": item['weather'][0]['description']
                })
            
            return {"status": "success", "forecast": days}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "failed"}


# ============================================================
# ENDPOINT: /ask
# AI question-answering endpoint powered by GPT-4o-mini.
# The device sends a question + current sensor context.
# The server enriches the context with:
#   - Historical indoor data from BigQuery (last 3 days at 14:00)
#   - 3-day outdoor forecast from OpenWeatherMap
# Everything is combined into a prompt sent to GPT-4o-mini.
# Answers are limited to 10 words to fit on the small device screen.
# The AI is restricted to weather/environment topics only.
# ============================================================

@app.route('/ask', methods=['POST'])
def ask():
    """
    Answers a natural language question about weather and home environment.
    
    Request body (JSON):
    - passwd: authentication hash
    - question: transcribed question from the user (via Whisper STT)
    - context: dict with current sensor readings from the device
    
    The prompt includes:
    1. Current indoor/outdoor readings (from device context)
    2. Historical indoor data from the last 3 days at 14:00 (from BigQuery)
       Uses a CTE with ROW_NUMBER() to get the first reading after 14:00 per day
    3. 3-day outdoor forecast (from OpenWeatherMap, same logic as /get_forecast)
    
    GPT-4o-mini is used (cheaper, faster than GPT-4o, sufficient for short answers).
    max_tokens=50 enforces short answers suitable for the small screen display.
    """
    data = request.get_json(force=True)
    if data.get("passwd") != YOUR_HASH_PASSWD:
        return jsonify({"error": "Unauthorized"}), 401
    
    question = data.get("question", "")
    context = data.get("context", {})
    
    # --- Fetch historical indoor data from BigQuery (last days at 14:00) ---
    # Uses a Common Table Expression (CTE) with ROW_NUMBER() to get
    # the first available reading at or after 14:00 for each day.
    # PARTITION BY date: groups rows by calendar day
    # ORDER BY time ASC: orders within each day by time ascending
    # WHERE time >= '14:00:00': filters to afternoon readings only
    # rn = 1: takes the earliest reading at or after 14:00 per day
    q_all = """
    WITH ranked AS (
        SELECT 
            date, time, indoor_temp, indoor_humidity, indoor_co2, indoor_tvoc,
            outdoor_temp, outdoor_weather,
            ROW_NUMBER() OVER (PARTITION BY date ORDER BY time ASC) as rn
        FROM `durable-will-487916-n1.Lab4_IoT_datasets.weather-records`
        WHERE time >= '14:00:00'
    )
    SELECT date, time, indoor_temp, indoor_humidity, indoor_co2, indoor_tvoc,
           outdoor_temp, outdoor_weather
    FROM ranked
    WHERE rn = 1
    ORDER BY date DESC
    """
    all_data_str = ""
    try:
        results = list(client.query(q_all).result())
        for row in results:
            all_data_str += f"{row.date}: in={row.indoor_temp}C hum={row.indoor_humidity}% co2={row.indoor_co2}ppm out={row.outdoor_temp}C {row.outdoor_weather}\n"
    except Exception as e:
        all_data_str = "No data available"
    
    # --- Fetch 3-day outdoor forecast from OpenWeatherMap ---
    # Same logic as /get_forecast: prefer 12:00 UTC entry per day
    try:
        API_key = os.environ.get("OPENWEATHER_API_KEY")
        city = "Lausanne"
        url = f'http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={API_key}&units=metric&cnt=40'
        response = requests.get(url)
        forecast_raw = response.json()
        today = datetime.now().strftime("%Y-%m-%d")
        days_raw = {}
        for item in forecast_raw['list']:
            date = item['dt_txt'][:10]
            hour = item['dt_txt'][11:13]
            if date == today:
                continue
            if date not in days_raw:
                days_raw[date] = {}
            days_raw[date][hour] = item
        forecast_str = ""
        for date, hours in sorted(days_raw.items())[:3]:
            item = (hours.get("12") or
                    hours.get("11") or
                    hours.get("13") or
                    list(hours.values())[0])
            forecast_str += f"{date}: {round(item['main']['temp'])}C {item['weather'][0]['description']}\n"
    except:
        forecast_str = "No forecast available"

    # --- Build the GPT-4o-mini prompt ---
    # The system instruction restricts the AI to weather/environment topics only.
    # Current sensor data, historical data, and forecast are all included as context.
    # max 10 words ensures the answer fits in the AI answer box on the device screen.
    prompt = f"""You are a smart home weather and environment assistant.
Answer in max 10 words when answering weather questions.

Current data:
- Indoor temp: {context.get('in_temp')}C
- Indoor humidity: {context.get('in_hum')}%
- Indoor CO2: {context.get('in_co2')}ppm
- Outdoor temp: {context.get('out_temp')}C
- Outdoor weather: {context.get('out_weather')}

Historical indoor data (last 3 days at 14:00):
{all_data_str}

3-day outdoor forecast:
{forecast_str}

Question: {question}
Answer in max 10 words."""

    try:
        # Call GPT-4o-mini: cheaper and faster than GPT-4o, sufficient for short answers
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50  # enforce short answers
        )
        answer = response.choices[0].message.content.strip()
        return jsonify({"status": "success", "answer": answer})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    

# ============================================================
# ENTRY POINT
# When running locally (python main.py), Flask starts on port 8080.
# On Cloud Run, the container is started by gunicorn or the Cloud Run
# runtime directly — the if __name__ block is not executed in production.
# debug=True enables hot reload and detailed error messages locally.
# ============================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=True)