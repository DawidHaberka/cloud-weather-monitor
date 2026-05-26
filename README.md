# Smart Home Weather Monitor — Cloud Analytics Project

---

## 👥 Team

| Name | Role |
|------|------|
| *Selene Calzavara* | M5Stack device UI, sensors integration, TTS/STT, AI assistant, alert system, Cloud Run backend (Flask) |
| *Dawid Haberka* | Streamlit cloud dashboard, FastAPI middleware API, BigQuery integration, Gemini AI assistant, Cloud Run deployment |

---

## 📽️ Demo Video

> 🎬 https://youtu.be/oi4xvm_lhag

---

## 📁 Project Structure

```text
/
├── api/
│   ├── main.py              # FastAPI middleware for Streamlit dashboard, BigQuery, Gemini and forecast API
│   ├── Dockerfile           # Container configuration for Cloud Run
│   └── requirements.txt     # Python dependencies for FastAPI service
├── dashboard/
│   ├── app.py               # Streamlit cloud dashboard
│   ├── Dockerfile           # Container configuration for Cloud Run
│   └── requirements.txt     # Python dependencies for dashboard
├── device/
│   ├── main.py              # MicroPython UIFlow code for M5Stack Core2
│   └── config.py            # Device credentials (NOT included in Git — see .gitignore)
├── server/
│   ├── main.py              # Flask backend deployed on Google Cloud Run for M5Stack communication
│   ├── Dockerfile           # Container configuration for Cloud Run
│   ├── requirements.txt     # Python dependencies
│   └── send_request.sh      # Local testing script for Flask endpoints
├── .gitignore
└── README.md
```

---

## 🏗️ Architecture

The project follows a *3-tier architecture*:

```text
┌────────────────────────────┐     ┌────────────────────────────┐     ┌──────────────────┐
│  M5Stack Core2 / Dashboard │────▶️│ Cloud Run Middleware APIs   │────▶️│   BigQuery   │
│  Device UI + Streamlit UI  │◀️────│ Flask + FastAPI REST APIs   │◀️────│  (Storage)   │
└────────────────────────────┘     └────────────────────────────┘      └─────────────────┘
          │                                      │
          │ UIFlow MicroPython                   ├── OpenWeatherMap API (weather + forecast)
          │ Streamlit Cloud Dashboard            ├── OpenAI TTS nova (text → WAV audio)
          │ Touchscreen + Voice Interaction      ├── OpenAI Whisper (audio → text)
          │ Sensors: ENV3, TVOC, PIR             ├── OpenAI GPT-4o-mini (device AI answers)
          └────────────────────────────          └── Google Gemini 2.5 Flash (dashboard AI assistant)
```

The first layer is the *data layer*, where BigQuery stores historical sensor and weather measurements.

The second layer is the *middleware/services layer*, deployed on Google Cloud Run. It includes the Flask backend used by the M5Stack and the FastAPI middleware used by the Streamlit dashboard.

The third layer is the *user interface layer*, consisting of the M5Stack Core2 device interface and the Streamlit web dashboard.

---

## ✨ Features

### Device (M5Stack Core2)

- *Screen 1 — Dashboard*: real-time indoor temperature and humidity (ENV3 sensor), CO2 and TVOC air quality with gradient bar (GOOD/MODERATE/POOR), outdoor temperature and weather with custom drawn icons. Clock and date synced via NTP and kept by the internal RTC.
- *Screen 2 — Forecast + AI*: 3-day weather forecast with large weather icons (always shown in daytime mode since data is at 14:00 local time). AI voice assistant activated by touchscreen — touch the microphone button, speak a question, get a voice answer powered by GPT-4o-mini.
- *Screen 3 — History*: last 5 records fetched from BigQuery with color-coded CO2 and TVOC values (green/yellow/red).
- *Smart Alerts*: visual popup + voice announcements triggered by sensor threshold breaches:
  - CO2 > 1200 ppm → poor air quality (max once per hour)
  - CO2 750–1200 ppm → moderate air quality (max once per hour)
  - TVOC > 300 ppb → high pollutants (max once per hour)
  - Humidity < 40% or > 70% (max once every 2 hours)
  - Indoor temp > 28°C or < 16°C (max once every 4 hours)
  - Outdoor temp > 35°C or < -5°C (max once per day)
  - Storm or snow in 3-day forecast (max once per day)
- *PIR Motion Sensor*: contextual voice announcements when motion is detected (max once per hour):
  - Morning (6–9h): umbrella reminder if rain in forecast, or temperature greeting
  - Evening (18–21h): indoor/outdoor temperature recap
  - Other hours: current weather update
- *Offline resilience*: on startup the device loads the last known values from a local JSON cache on flash memory, then syncs with BigQuery. Tested with WiFi disconnected.

### Streamlit Cloud Dashboard

- *Current Conditions*: displays the latest indoor temperature, indoor humidity, outdoor temperature, weather description, CO2, TVOC, air quality status and motion detection.
- *3-Day Forecast*: retrieves the next 3 days of weather forecast from the FastAPI middleware and displays daily temperature with weather icons.
- *Alerts Panel*: shows automatic alerts based on humidity, air quality, rain/storm conditions and motion detection.
- *AI Home Summary*: provides a short natural-language summary of the selected period: latest reading, last 7 days, last 30 days, or selected date.
- *AI Assistant*: allows the user to ask natural-language questions about temperature, humidity, air quality, motion, clothing recommendations, statistics and future weather.
- *Voice Interaction*: supports spoken questions and browser-based text-to-speech answers.
- *Statistics Explorer*: calculates averages, minimums, maximums, motion events, CO2 and TVOC statistics for selected periods.
- *Historical Charts*: visualizes indoor/outdoor temperature, humidity, CO2, TVOC and motion history from BigQuery.
- *Raw BigQuery Logs*: allows inspection of the underlying records loaded from the cloud database.

### FastAPI Middleware for Dashboard

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Checks whether the FastAPI middleware is running |
| `/latest` | GET | Returns the most recent sensor and weather record from BigQuery |
| `/stats` | GET | Returns summary statistics for the last 24 hours, 7 days and 30 days |
| `/forecast` | GET | Returns 3-day forecast for Lausanne using OpenWeatherMap |
| `/ask` | POST | Answers natural-language questions using Gemini with latest data, historical statistics and forecast context |
| `/ask-text` | POST | Text-based assistant endpoint |
| `/ask-audio` | POST | Audio upload endpoint prepared for speech-to-text integration |

### Backend (Flask on Google Cloud Run)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/send-to-bigquery` | POST | Receives sensor readings, enriches them with current OpenWeatherMap data, inserts into BigQuery |
| `/get_outdoor_weather` | POST | Returns the latest outdoor weather from the most recent BigQuery row |
| `/get_forecast` | POST | Returns 3-day forecast from OpenWeatherMap; one entry per day at 12:00 UTC (= 14:00 CEST) |
| `/get_latest` | POST | Returns the most recent indoor sensor record from BigQuery (used at device startup) |
| `/get_history` | POST | Returns the last 5 records from BigQuery (newest first) |
| `/speak/<text>` | GET | Calls OpenAI TTS (voice: nova, speed: 0.85), converts signed→unsigned PCM, returns WAV at 24000Hz |
| `/transcribe` | POST | Receives base64-encoded WAV from device, sends to OpenAI Whisper, returns transcribed text |
| `/ask` | POST | Answers weather/environment questions using GPT-4o-mini with current sensor context + 3-day BigQuery history + forecast |

---

## 🛠️ Technologies Used

| Technology | Usage |
|------------|-------|
| M5Stack Core2 | IoT device with 320×240 touchscreen |
| MicroPython (UIFlow) | Device firmware language |
| Streamlit | Cloud dashboard interface |
| FastAPI | Middleware for dashboard, Gemini, BigQuery statistics and forecast |
| Flask | Middleware for M5Stack device communication |
| Google Cloud Run | Serverless hosting for backend services and dashboard |
| Google BigQuery | Time-series sensor data storage |
| OpenWeatherMap API | Current weather + 3-day forecast |
| Google Gemini 2.5 Flash | Dashboard AI assistant and natural-language Q&A |
| OpenAI TTS (tts-1, nova) | Text-to-speech for voice announcements |
| OpenAI Whisper (whisper-1) | Speech-to-text for AI assistant |
| OpenAI GPT-4o-mini | Device AI question answering with context |

---

## 🚀 How to Deploy

### Prerequisites

- Google Cloud project with BigQuery, Cloud Run and Cloud Build enabled
- BigQuery dataset and table for sensor records
- OpenAI API key
- Gemini API key
- OpenWeatherMap API key

### 1. BigQuery Setup

Create a dataset, for example:

```text
Lab4_IoT_datasets
```

Create a table, for example:

```text
weather-records
```

with the following schema:

```text
date              DATE
time              TIME
indoor_temp       FLOAT
indoor_humidity   FLOAT
indoor_co2        INTEGER
indoor_tvoc       INTEGER
motion_detected   INTEGER
outdoor_temp      FLOAT
outdoor_humidity  FLOAT
outdoor_weather   STRING
```

### 2. Deploy the Flask Backend for M5Stack

From the `server/` folder in Google Cloud Shell:

```bash
gcloud builds submit --tag gcr.io/<YOUR_PROJECT_ID>/server-meteo

gcloud run deploy server-meteo \
  --image gcr.io/<YOUR_PROJECT_ID>/server-meteo \
  --platform managed \
  --region europe-west6 \
  --allow-unauthenticated \
  --set-env-vars "OPENAI_API_KEY=<your_key>,OPENWEATHER_API_KEY=<your_key>,HASH_PASSWD=<your_sha256_hash>"
```

### 3. Deploy the FastAPI Middleware for Dashboard

From the project root:

```bash
gcloud run deploy smart-home-api \
  --source api \
  --region europe-west6 \
  --allow-unauthenticated \
  --update-env-vars GCP_PROJECT_ID="<YOUR_PROJECT_ID>",BQ_LOCATION="europe-west6",BQ_TABLE="durable-will-487916-n1.Lab4_IoT_datasets.weather-records",GEMINI_API_KEY="<your_key>",GEMINI_MODEL_NAME="gemini-2.5-flash",OPENWEATHER_API_KEY="<your_key>",FORECAST_CITY="Lausanne"
```

### 4. Deploy the Streamlit Dashboard

From the project root:

```bash
gcloud run deploy smart-home-dashboard \
  --source dashboard \
  --region europe-west6 \
  --allow-unauthenticated \
  --update-env-vars API_BASE_URL="https://<your-fastapi-cloud-run-url>",USE_GEMINI_SUMMARY="true",GEMINI_API_KEY="<your_key>",GEMINI_MODEL_NAME="gemini-2.5-flash"
```

### 5. Configure the Device

Before flashing the device code to the M5Stack Core2, update the configuration values directly in the device `main.py` file.

The values that need to be configured are:

```python
URL_CLOUD_RUN = "https://<your-cloud-run-url>"
YOUR_HASH_PASSWD = "<your_sha256_hash>"
```

### 6. Change WiFi on Device

Hold the side button on startup to access the UIFlow WiFi configuration menu.


---

## ⚙️ Environment Variables

### Flask Server

| Variable | Description |
|----------|-------------|
| OPENAI_API_KEY | OpenAI API key — used for TTS (`/speak`), STT (`/transcribe`), and LLM (`/ask`) |
| OPENWEATHER_API_KEY | OpenWeatherMap API key — used in `/send-to-bigquery`, `/get_forecast`, `/ask` |
| HASH_PASSWD | SHA-256 hash of the shared device password — used to authenticate all requests |

### FastAPI Middleware

| Variable | Description |
|----------|-------------|
| GCP_PROJECT_ID | Google Cloud project ID |
| BQ_LOCATION | BigQuery location, e.g. `europe-west6` |
| BQ_TABLE | Full BigQuery table ID |
| GEMINI_API_KEY | Gemini API key for dashboard AI assistant |
| GEMINI_MODEL_NAME | Gemini model name, e.g. `gemini-2.5-flash` |
| OPENWEATHER_API_KEY | OpenWeatherMap API key for `/forecast` |
| FORECAST_CITY | City used for forecast, default: Lausanne |

### Streamlit Dashboard

| Variable | Description |
|----------|-------------|
| API_BASE_URL | URL of the FastAPI middleware |
| USE_GEMINI_SUMMARY | Enables Gemini-generated dashboard summaries |
| GEMINI_API_KEY | Gemini API key |
| GEMINI_MODEL_NAME | Gemini model name |

---

## 🔌 Hardware Components

| Component | Connection | Function |
|-----------|-----------|----------|
| M5Stack Core2 | — | Main IoT device with 320×240 touchscreen and internal speaker/microphone |
| ENVIII Sensor | Port A | Indoor temperature (°C) and humidity (%) |
| TVOC/eCO2 Sensor | Pins 14, 13 | Indoor air quality: estimated CO2 (ppm) and Total VOC (ppb) |
| PIR Motion Sensor | Port B | Presence detection — triggers contextual voice announcements |

---

## 🔐 Security

All sensitive credentials are excluded from Git. API keys are stored as Google Cloud Run environment variables, while device credentials are stored in `device/config.py`, which is ignored by Git.

The M5Stack Flask endpoints require a password hash sent in the request body through the `"passwd"` field. The server compares it against the `HASH_PASSWD` environment variable. Plain-text passwords are not stored in the repository.

The Streamlit dashboard communicates with the FastAPI middleware using the `API_BASE_URL` environment variable, so the dashboard does not need to hard-code backend URLs or secrets directly in the source code.

---

## 🤖 AI-Assisted Development

During the project, we used AI tools such as Google Gemini Pro and OpenAI ChatGPT to support coding, debugging, documentation and architecture design. These tools helped us structure the FastAPI and Flask backends, improve the Streamlit dashboard, debug deployment issues and prepare clearer documentation.

However, the final code was reviewed, adapted, tested and integrated by the team. AI was used as a development assistant, not as a replacement for understanding or validating the implementation.
