import os
import re
import html
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from google.cloud import bigquery
from streamlit_autorefresh import st_autorefresh

try:
    from streamlit_mic_recorder import speech_to_text
    MIC_RECORDER_AVAILABLE = True
except ImportError:
    speech_to_text = None
    MIC_RECORDER_AVAILABLE = False

try:
    from google import genai
    VERTEX_AI_AVAILABLE = True
except ImportError:
    genai = None
    VERTEX_AI_AVAILABLE = False

# =========================================================
# CONFIGURATION
# =========================================================
# For local testing you can set this environment variable before running Streamlit:
# export GOOGLE_APPLICATION_CREDENTIALS="path/to/your-service-account.json"
# Do NOT commit the service account JSON file to GitHub.

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "durable-will-487916-n1")
BQ_LOCATION = os.getenv("BQ_LOCATION", "europe-west6")
TABLE_ID = os.getenv(
    "BQ_TABLE_ID",
    "durable-will-487916-n1.Lab4_IoT_datasets.weather-records",
)

REFRESH_INTERVAL_MS = 300_000
LOW_HUMIDITY_THRESHOLD = 40
HIGH_CO2_THRESHOLD = 1000
HIGH_TVOC_THRESHOLD = 300

# Optional real AI summary using Google Gemini.
# Add GEMINI_API_KEY to your environment variables to activate it.
USE_VERTEX_AI_SUMMARY = os.getenv("USE_VERTEX_AI_SUMMARY", "false").lower() == "true"
VERTEX_PROJECT_ID = os.getenv("VERTEX_PROJECT_ID", PROJECT_ID)
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
VERTEX_MODEL_NAME = os.getenv("VERTEX_MODEL_NAME", "gemini-2.0-flash")

# Load enough rows for one month of charts and date-based questions.
QUERY_DAYS_BACK = 35
QUERY_LIMIT = 20_000

# Optional local fallback for development only.
# Keep this file in .gitignore if you use it.
LOCAL_CREDENTIALS_FILES = [
    "durable-will-487916-n1-bb31185321b7.json",
    "durable-will-487916-n1-bb31185321b7.JSON",
]
if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    for credentials_file in LOCAL_CREDENTIALS_FILES:
        if os.path.exists(credentials_file):
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_file
            break


# =========================================================
# PAGE SETUP
# =========================================================
st.set_page_config(
    page_title="Smart Home IoT Weather Monitor",
    page_icon="🌤️",
    layout="wide",
)

st_autorefresh(interval=REFRESH_INTERVAL_MS, key="data_refresh")

st.markdown(
    """
    <style>
        .main-title {
            font-size: 2.2rem;
            font-weight: 800;
            margin-bottom: 0.2rem;
        }
        .subtitle {
            color: #666;
            font-size: 1rem;
            margin-bottom: 1.4rem;
        }
        .status-ok {
            background-color: #e8f5e9;
            padding: 0.75rem 1rem;
            border-radius: 0.75rem;
            border-left: 6px solid #2e7d32;
            color: #1b5e20;
            font-weight: 600;
        }
        .status-warning {
            background-color: #fff8e1;
            padding: 0.75rem 1rem;
            border-radius: 0.75rem;
            border-left: 6px solid #f9a825;
            color: #7a5200;
            font-weight: 600;
        }
        .status-critical {
            background-color: #ffebee;
            padding: 0.75rem 1rem;
            border-radius: 0.75rem;
            border-left: 6px solid #c62828;
            color: #7f0000;
            font-weight: 600;
        }
        .ai-box {
            background-color: #f6f8fa;
            color: #111827 !important;
            padding: 1rem;
            border-radius: 0.75rem;
            border: 1px solid #e5e7eb;
            font-size: 1rem;
            line-height: 1.55;
        }
        .ai-box * {
            color: #111827 !important;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-title">🌤️ Smart Home - IoT Weather Monitor</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="subtitle">Cloud dashboard connected to M5Stack sensors and Google BigQuery.</div>',
    unsafe_allow_html=True,
)


# =========================================================
# DATA LOADING
# =========================================================
@st.cache_data(ttl=10)
def load_data(days_back: int = QUERY_DAYS_BACK, limit: int = QUERY_LIMIT) -> pd.DataFrame:
    client = bigquery.Client(project=PROJECT_ID, location=BQ_LOCATION)

    query = f"""
        SELECT
            date,
            time,
            indoor_temp,
            indoor_humidity,
            outdoor_temp,
            outdoor_humidity,
            outdoor_weather,
            indoor_co2,
            indoor_tvoc,
            motion_detected
        FROM `{TABLE_ID}`
        WHERE DATE(date) >= DATE_SUB(CURRENT_DATE(), INTERVAL {days_back} DAY)
        ORDER BY date DESC, time DESC
        LIMIT {limit}
    """

    df = client.query(query).to_dataframe()

    if df.empty:
        return df

    df["datetime"] = pd.to_datetime(
        df["date"].astype(str) + " " + df["time"].astype(str),
        errors="coerce",
    )
    df["date_only"] = pd.to_datetime(df["date"].astype(str), errors="coerce").dt.date
    df = df.dropna(subset=["datetime", "date_only"]).sort_values("datetime")

    numeric_columns = [
        "indoor_temp",
        "indoor_humidity",
        "outdoor_temp",
        "outdoor_humidity",
        "indoor_co2",
        "indoor_tvoc",
        "motion_detected",
    ]
    for col in numeric_columns:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


# =========================================================
# BUSINESS LOGIC
# =========================================================
def classify_air_quality(co2: float, tvoc: float) -> str:
    if pd.isna(co2) or pd.isna(tvoc):
        return "Unknown"
    if co2 > HIGH_CO2_THRESHOLD or tvoc > HIGH_TVOC_THRESHOLD:
        return "Poor"
    if co2 > 800 or tvoc > 200:
        return "Moderate"
    return "Good"


def get_alerts(row: pd.Series) -> list[str]:
    alerts = []

    if row["indoor_humidity"] < LOW_HUMIDITY_THRESHOLD:
        alerts.append("⚠️ Indoor humidity is below 40%. Consider using a humidifier.")

    if row["indoor_co2"] > HIGH_CO2_THRESHOLD:
        alerts.append("⚠️ CO₂ level is high. Ventilation is recommended.")

    if row["indoor_tvoc"] > HIGH_TVOC_THRESHOLD:
        alerts.append("⚠️ TVOC level is elevated. Indoor air quality may be poor.")

    weather = str(row["outdoor_weather"]).lower()
    if "rain" in weather or "storm" in weather or "drizzle" in weather:
        alerts.append("☂️ Rain or storm conditions detected. Take an umbrella.")

    if int(row["motion_detected"] or 0) == 1:
        alerts.append("👤 Motion detected near the device.")

    if not alerts:
        alerts.append("✅ Everything looks good. No critical alert detected.")

    return alerts


def get_system_status(alerts: list[str]) -> tuple[str, str]:
    warning_keywords = ["⚠️", "☂️"]
    critical_keywords = ["CO₂ level is high", "TVOC level is elevated"]

    joined_alerts = " ".join(alerts)

    if any(keyword in joined_alerts for keyword in critical_keywords):
        return "Critical", "status-critical"
    if any(keyword in joined_alerts for keyword in warning_keywords):
        return "Warning", "status-warning"
    return "OK", "status-ok"


def generate_fallback_summary(row: pd.Series) -> str:
    """Deterministic backup summary used when Gemini is not configured."""
    air_quality = classify_air_quality(row["indoor_co2"], row["indoor_tvoc"])
    motion_text = "Motion was detected near the device." if int(row["motion_detected"] or 0) == 1 else "No motion was detected."

    if row["indoor_humidity"] < LOW_HUMIDITY_THRESHOLD:
        humidity_text = "Indoor humidity is too low, so using a humidifier is recommended."
    else:
        humidity_text = "Indoor humidity is within a comfortable range."

    if air_quality == "Poor":
        air_text = "Air quality may be poor, so ventilating the room is recommended."
    elif air_quality == "Moderate":
        air_text = "Air quality is acceptable, but ventilation could improve comfort."
    elif air_quality == "Good":
        air_text = "Air quality is good."
    else:
        air_text = "Air quality status is currently unknown."

    weather = str(row["outdoor_weather"])
    if any(word in weather.lower() for word in ["rain", "storm", "drizzle"]):
        weather_text = "Outdoor weather suggests taking an umbrella."
    else:
        weather_text = f"Outdoor weather is currently {weather}."

    return (
        f"The current indoor temperature is {row['indoor_temp']:.1f}°C and humidity is "
        f"{row['indoor_humidity']:.1f}%. {humidity_text} CO₂ is {row['indoor_co2']:.0f} ppm "
        f"and TVOC is {row['indoor_tvoc']:.0f} ppb. {air_text} {weather_text} {motion_text}"
    )


def generate_ai_home_summary_cached(
    indoor_temp: float,
    indoor_humidity: float,
    outdoor_temp: float,
    outdoor_humidity: float,
    outdoor_weather: str,
    indoor_co2: float,
    indoor_tvoc: float,
    motion_detected: int,
    air_quality: str,
    alerts_text: str,
    refresh_token: int,
) -> str:
    """
    Generate AI Home Summary using Vertex AI Gemini.

    If Vertex AI is disabled, unavailable, or returns a weak answer,
    the dashboard uses a stable rule-based fallback summary.
    """
    fallback_row = pd.Series({
        "indoor_temp": indoor_temp,
        "indoor_humidity": indoor_humidity,
        "outdoor_temp": outdoor_temp,
        "outdoor_humidity": outdoor_humidity,
        "outdoor_weather": outdoor_weather,
        "indoor_co2": indoor_co2,
        "indoor_tvoc": indoor_tvoc,
        "motion_detected": motion_detected,
    })

    fallback_summary = generate_fallback_summary(fallback_row)

    if not (USE_VERTEX_AI_SUMMARY and VERTEX_AI_AVAILABLE):
        return fallback_summary

    try:
        client = genai.Client(
            vertexai=True,
            project=VERTEX_PROJECT_ID,
            location=VERTEX_LOCATION,
        )

        prompt = f"""
You are an AI home climate assistant for a smart home IoT dashboard.

Task:
Write exactly 3 complete sentences based only on the sensor data below.

Rules:
- Do not invent values.
- Do not write a letter.
- Do not use greetings, signatures, or bullet points.
- Mention indoor comfort, air quality, and outdoor recommendation.
- If air quality is poor, recommend ventilation.
- If outdoor weather suggests rain or storm, recommend an umbrella.

Sensor data:
Indoor temperature: {indoor_temp:.1f}°C
Indoor humidity: {indoor_humidity:.1f}%
Outdoor temperature: {outdoor_temp:.1f}°C
Outdoor humidity: {outdoor_humidity:.1f}%
Outdoor weather: {outdoor_weather}
CO2: {indoor_co2:.0f} ppm
TVOC: {indoor_tvoc:.0f} ppb
Air quality status: {air_quality}
Motion detected: {"yes" if motion_detected == 1 else "no"}
Current alerts: {alerts_text}
"""

        response = client.models.generate_content(
            model=VERTEX_MODEL_NAME,
            contents=prompt,
        )

        ai_text = ""
        if response is not None and getattr(response, "text", None):
            ai_text = response.text.strip().replace("\n", " ")

        ai_text_lower = ai_text.lower().strip()

        too_short = len(ai_text.split()) < 25
        too_long = len(ai_text.split()) > 120
        looks_like_letter = any(
            phrase in ai_text_lower
            for phrase in ["sincerely", "regards", "best regards", "dear "]
        )
        incomplete_end = ai_text_lower.endswith(
            ("you're", "you are", "is", "are", "and", "but", "with", "to", ",", ":", ";")
        )
        missing_sentence_end = not ai_text.endswith((".", "!", "?"))

        if (
            ai_text
            and not too_short
            and not too_long
            and not looks_like_letter
            and not incomplete_end
            and not missing_sentence_end
        ):
            return ai_text

        return fallback_summary

    except Exception:
        return fallback_summary


def speak_text_browser(text: str, enabled: bool = True) -> None:
    """Read text aloud in the browser using the Web Speech API."""
    if not enabled or not text:
        return
    safe_text = text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
    components.html(
        f"""
        <script>
        const text = `{safe_text}`;
        if ("speechSynthesis" in window) {{
            window.speechSynthesis.cancel();
            const utterance = new SpeechSynthesisUtterance(text);
            utterance.lang = "en-US";
            utterance.rate = 1.0;
            utterance.pitch = 1.0;
            window.speechSynthesis.speak(utterance);
        }}
        </script>
        """,
        height=0,
    )

def parse_requested_date(question: str, default_year: int) -> date | None:
    """Parse simple English dates from questions, e.g. '16 May', 'May 16', or '2026-05-16'."""
    q = question.lower()

    month_map = {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }

    # Pattern: 16 May / 16 May 2026
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\s*(\d{4})?\b", q)
    if m:
        day = int(m.group(1))
        month_text = m.group(2)
        year = int(m.group(3)) if m.group(3) else default_year
        month = month_map.get(month_text)
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                return None

    # Pattern: May 16 / May 16 2026
    m = re.search(r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\s*(\d{4})?\b", q)
    if m:
        month_text = m.group(1)
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else default_year
        month = month_map.get(month_text)
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                return None

    # Pattern: 2026-05-16
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", q)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    return None


def detect_statistic(question: str) -> str:
    """Detect whether the user asks for average, minimum, maximum, or a full summary."""
    q = question.lower()
    if any(word in q for word in ["maximum", "max", "highest", "peak"]):
        return "maximum"
    if any(word in q for word in ["minimum", "min", "lowest"]):
        return "minimum"
    if any(word in q for word in ["average", "avg", "mean"]):
        return "average"
    return "summary"


def detect_metric(question: str) -> tuple[str | None, str | None]:
    """Return the dataframe column and a readable metric name based on the question."""
    q = question.lower()
    if "outdoor" in q and ("temperature" in q or "temp" in q):
        return "outdoor_temp", "outdoor temperature"
    if "temperature" in q or "temp" in q:
        return "indoor_temp", "indoor temperature"
    if "humidity" in q:
        return "indoor_humidity", "indoor humidity"
    if "co2" in q or "co₂" in q:
        return "indoor_co2", "CO₂ level"
    if "tvoc" in q:
        return "indoor_tvoc", "TVOC level"
    return None, None


def unit_for_metric(column: str) -> str:
    units = {
        "indoor_temp": "°C",
        "outdoor_temp": "°C",
        "indoor_humidity": "%",
        "indoor_co2": "ppm",
        "indoor_tvoc": "ppb",
    }
    return units.get(column, "")


def calculate_metric_for_date(df: pd.DataFrame, requested_date: date, metric_col: str, metric_name: str, statistic: str) -> str:
    """AI Assistant calculation layer: compute requested statistics from BigQuery-loaded data."""
    day_df = df[df["date_only"] == requested_date].copy()

    if day_df.empty:
        return (
            f"I do not have measurements for {requested_date.strftime('%Y-%m-%d')} in the loaded BigQuery data. "
            f"The dashboard currently loads the last {QUERY_DAYS_BACK} days."
        )

    values = pd.to_numeric(day_df[metric_col], errors="coerce").dropna()
    if values.empty:
        return f"I found records for {requested_date.strftime('%Y-%m-%d')}, but no valid {metric_name} values."

    unit = unit_for_metric(metric_col)
    count = len(values)

    avg_value = values.mean()
    min_value = values.min()
    max_value = values.max()

    if statistic == "average":
        return (
            f"On {requested_date.strftime('%Y-%m-%d')}, based on {count} measurements, "
            f"the average {metric_name} was {avg_value:.1f}{unit}."
        )

    if statistic == "minimum":
        return (
            f"On {requested_date.strftime('%Y-%m-%d')}, based on {count} measurements, "
            f"the minimum {metric_name} was {min_value:.1f}{unit}."
        )

    if statistic == "maximum":
        return (
            f"On {requested_date.strftime('%Y-%m-%d')}, based on {count} measurements, "
            f"the maximum {metric_name} was {max_value:.1f}{unit}."
        )

    return (
        f"On {requested_date.strftime('%Y-%m-%d')}, based on {count} measurements, "
        f"the average {metric_name} was {avg_value:.1f}{unit}, "
        f"the minimum was {min_value:.1f}{unit}, and the maximum was {max_value:.1f}{unit}."
    )

def summarize_temperature_for_date(df: pd.DataFrame, requested_date: date) -> str:
    day_df = df[df["date_only"] == requested_date]

    if day_df.empty:
        return (
            f"I do not have measurements for {requested_date.strftime('%Y-%m-%d')} in the loaded BigQuery data. "
            f"The dashboard currently loads the last {QUERY_DAYS_BACK} days."
        )

    avg_indoor = day_df["indoor_temp"].mean()
    min_indoor = day_df["indoor_temp"].min()
    max_indoor = day_df["indoor_temp"].max()
    avg_outdoor = day_df["outdoor_temp"].mean()
    count = len(day_df)

    return (
        f"On {requested_date.strftime('%Y-%m-%d')}, based on {count} measurements, "
        f"the average indoor temperature was {avg_indoor:.1f}°C "
        f"with a range from {min_indoor:.1f}°C to {max_indoor:.1f}°C. "
        f"The average outdoor temperature was {avg_outdoor:.1f}°C."
    )


def summarize_humidity_for_date(df: pd.DataFrame, requested_date: date) -> str:
    day_df = df[df["date_only"] == requested_date]

    if day_df.empty:
        return f"I do not have humidity measurements for {requested_date.strftime('%Y-%m-%d')} in the loaded BigQuery data."

    avg_humidity = day_df["indoor_humidity"].mean()
    min_humidity = day_df["indoor_humidity"].min()
    max_humidity = day_df["indoor_humidity"].max()
    below_threshold = int((day_df["indoor_humidity"] < LOW_HUMIDITY_THRESHOLD).sum())

    return (
        f"On {requested_date.strftime('%Y-%m-%d')}, the average indoor humidity was {avg_humidity:.1f}%, "
        f"with a range from {min_humidity:.1f}% to {max_humidity:.1f}%. "
        f"Humidity was below 40% in {below_threshold} measurements."
    )


def answer_assistant_question(question: str, row: pd.Series, df: pd.DataFrame, selected_date: date | None = None) -> str:
    q = question.lower().strip()
    air_quality = classify_air_quality(row["indoor_co2"], row["indoor_tvoc"])
    default_year = int(pd.to_datetime(df["datetime"].max()).year)
    parsed_date = parse_requested_date(q, default_year=default_year)
    statistic = detect_statistic(q)
    metric_col, metric_name = detect_metric(q)

    # The user can either say/type a date in the question, or choose it in the dashboard.
    # If both are provided, the date mentioned in the question has priority.
    requested_date = parsed_date or selected_date

    if not q:
        return "Please ask a question about temperature, humidity, air quality, motion, or umbrella recommendation."

    # Date-based analytical questions, e.g.:
    # "What was the average temperature on May 16?"
    # "What was the maximum CO2 level on the selected date?"
    if requested_date and metric_col:
        return calculate_metric_for_date(df, requested_date, metric_col, metric_name, statistic)

    if requested_date and not metric_col:
        return (
            "Please specify what metric you want to analyze, for example: "
            "What was the average temperature on the selected date?"
        )

    if statistic in ["average", "minimum", "maximum"] and metric_col and not requested_date:
        return (
            "Please choose a date in the dashboard or mention it in the question, for example: "
            "What was the average temperature on May 16?"
        )

    if "temperature" in q or "temp" in q:
        return (
            f"The current indoor temperature is {row['indoor_temp']:.1f}°C, "
            f"and the outdoor temperature is {row['outdoor_temp']:.1f}°C."
        )

    if "humidity" in q:
        status = "too low" if row["indoor_humidity"] < LOW_HUMIDITY_THRESHOLD else "comfortable"
        return f"The current indoor humidity is {row['indoor_humidity']:.1f}%, which is {status}."

    if "air" in q or "co2" in q or "co₂" in q or "tvoc" in q or "quality" in q:
        return (
            f"Air quality is {air_quality}. CO₂ is {row['indoor_co2']:.0f} ppm "
            f"and TVOC is {row['indoor_tvoc']:.0f} ppb."
        )

    if "motion" in q or "movement" in q or "presence" in q:
        if int(row["motion_detected"] or 0) == 1:
            return "Yes, motion was detected near the device."
        return "No, motion was not detected in the latest measurement."

    if "umbrella" in q or "rain" in q or "weather" in q:
        weather = str(row["outdoor_weather"])
        if any(word in weather.lower() for word in ["rain", "storm", "drizzle"]):
            return f"Yes, you should take an umbrella. The current weather is {weather}."
        return f"An umbrella is probably not necessary right now. The current weather is {weather}."

    return (
        "I can answer questions such as: What is the temperature now? "
        "What was the average temperature on the selected date? What was the minimum humidity on May 16? "
        "What was the maximum CO2 level on the selected date? Is air quality good? Was motion detected? "
        "Should I take an umbrella?"
    )

def metric_value(value, suffix="", decimals=1):
    if pd.isna(value):
        return "N/A"
    if isinstance(value, (int, float)):
        return f"{value:.{decimals}f}{suffix}"
    return str(value)


def filter_history(df: pd.DataFrame, selected_range: str) -> pd.DataFrame:
    max_dt = df["datetime"].max()
    if selected_range == "Last 24 hours":
        start_dt = max_dt - timedelta(days=1)
    elif selected_range == "Last 7 days":
        start_dt = max_dt - timedelta(days=7)
    elif selected_range == "Last 30 days":
        start_dt = max_dt - timedelta(days=30)
    else:
        return df.copy()

    return df[df["datetime"] >= start_dt].copy()


# =========================================================
# DASHBOARD
# =========================================================
try:
    with st.spinner("Fetching the latest data from BigQuery..."):
        df = load_data()
except Exception as exc:
    st.error("Could not load data from BigQuery. Check credentials, table name, and internet connection.")
    st.exception(exc)
    st.stop()

if df.empty:
    st.warning("No data in the BigQuery table. Check the M5Stack device and sensor pipeline.")
    st.stop()

latest = df.iloc[-1].copy()

alerts = get_alerts(latest)
system_status, status_class = get_system_status(alerts)
air_quality_status = classify_air_quality(latest["indoor_co2"], latest["indoor_tvoc"])

# Top status
status_col, update_col = st.columns([2, 1])
with status_col:
    st.markdown(
        f'<div class="{status_class}">System status: {system_status}</div>',
        unsafe_allow_html=True,
    )
with update_col:
    st.info(f"Last update: {latest['datetime'].strftime('%Y-%m-%d %H:%M:%S')}")

st.markdown("---")

# Current conditions
st.subheader("🏠 Current Conditions")

col1, col2, col3, col4 = st.columns(4)
col1.metric("🌡️ Indoor Temp", metric_value(latest["indoor_temp"], " °C"))
col2.metric("💧 Indoor Humidity", metric_value(latest["indoor_humidity"], " %"))
col3.metric("🌍 Outdoor Temp", metric_value(latest["outdoor_temp"], " °C"))
col4.metric("🌦️ Weather", str(latest["outdoor_weather"]).title())

col5, col6, col7, col8 = st.columns(4)
col5.metric("🫁 CO₂", metric_value(latest["indoor_co2"], " ppm", decimals=0))
col6.metric("🧪 TVOC", metric_value(latest["indoor_tvoc"], " ppb", decimals=0))
col7.metric("🍃 Air Quality", air_quality_status)
col8.metric("👤 Motion", "Detected" if int(latest["motion_detected"] or 0) == 1 else "No motion")

st.markdown("---")

# Alerts + AI summary
alert_col, ai_col = st.columns([1, 2])

with alert_col:
    st.subheader("🚨 Alerts")
    for alert in alerts:
        if alert.startswith("✅"):
            st.success(alert)
        elif alert.startswith("⚠️"):
            st.warning(alert)
        elif alert.startswith("☂️"):
            st.info(alert)
        else:
            st.info(alert)

with ai_col:
    st.subheader("🤖 AI Home Summary")
    st.caption("Generated with Vertex AI Gemini when enabled; otherwise a stable rule-based fallback is used.")
    
    if "summary_refresh_token" not in st.session_state:
        st.session_state["summary_refresh_token"] = 0

    if st.button("Regenerate AI summary"):
        st.session_state["summary_refresh_token"] += 1

    ai_summary = generate_ai_home_summary_cached(
        indoor_temp=float(latest["indoor_temp"]),
        indoor_humidity=float(latest["indoor_humidity"]),
        outdoor_temp=float(latest["outdoor_temp"]),
        outdoor_humidity=float(latest["outdoor_humidity"]),
        outdoor_weather=str(latest["outdoor_weather"]),
        indoor_co2=float(latest["indoor_co2"]),
        indoor_tvoc=float(latest["indoor_tvoc"]),
        motion_detected=int(latest["motion_detected"] or 0),
        air_quality=air_quality_status,
        alerts_text="; ".join(alerts),
        refresh_token=st.session_state["summary_refresh_token"],
    )

    st.markdown(
        f'<div class="ai-box">{html.escape(ai_summary)}</div>',
        unsafe_allow_html=True,
    )

st.markdown("---")

# AI assistant
st.subheader("🎙️ AI Assistant")
st.caption(
    "Choose or type a question manually, or use the microphone below. "
    "Manual questions can use the selected date. Voice questions should include the date in the spoken sentence if needed."
)

available_dates = sorted(df["date_only"].dropna().unique())
default_assistant_date = available_dates[-1] if available_dates else date.today()

assistant_date = st.date_input(
    "Choose date for analytical questions",
    value=default_assistant_date,
    min_value=available_dates[0] if available_dates else None,
    max_value=available_dates[-1] if available_dates else None,
    help="Used for manual analytical questions. Voice questions ignore this field unless the date is spoken.",
    key="assistant_date",
)

selected_date_label = assistant_date.strftime("%B %d, %Y")
example_questions = [
    "What is the temperature now?",
    "What is the humidity now?",
    "Is air quality good?",
    "Was motion detected?",
    "Should I take an umbrella?",
    f"What was the average temperature on {selected_date_label}?",
    f"What was the minimum temperature on {selected_date_label}?",
    f"What was the maximum temperature on {selected_date_label}?",
    f"What was the average humidity on {selected_date_label}?",
    f"What was the maximum CO2 level on {selected_date_label}?",
]

st.markdown("### Manual question")
selected_question = st.selectbox("Choose a question", example_questions)
manual_question = st.text_input("Or type your own question", value=selected_question)
read_answer_aloud = st.checkbox("Read answer aloud", value=True, key="read_answer_aloud")

if st.button("Ask assistant"):
    manual_answer = answer_assistant_question(manual_question, latest, df, selected_date=assistant_date)
    st.session_state["last_assistant_answer"] = manual_answer
    st.success(manual_answer)
    speak_text_browser(manual_answer, enabled=read_answer_aloud)

st.markdown("### Voice question")
st.caption("Record a full question, for example: 'What is the temperature now?' or 'What was the average temperature on May 18?'")

spoken_question = None

if MIC_RECORDER_AVAILABLE:
    spoken_question = speech_to_text(
        language="en",
        use_container_width=True,
        just_once=True,
        key="speech_to_text_single_mode",
    )

    if spoken_question:
        st.session_state["last_spoken_question"] = spoken_question
        st.success(f"Recognized question: {spoken_question}")
else:
    st.warning(
        "Microphone input is not installed. Add `streamlit-mic-recorder` to requirements.txt "
        "and run: `pip install streamlit-mic-recorder`."
    )

voice_question = st.session_state.get("last_spoken_question", "")

if voice_question:
    st.text_area("Recognized voice question", value=voice_question, height=80, disabled=True)

if st.button("Ask from voice"):
    if not voice_question:
        st.warning("Please record a voice question first.")
    else:
        # Voice mode ignores selected date. The user should say the date in the question if needed.
        voice_answer = answer_assistant_question(voice_question, latest, df, selected_date=None)
        st.session_state["last_assistant_answer"] = voice_answer
        st.success(voice_answer)
        speak_text_browser(voice_answer, enabled=read_answer_aloud)

if "last_assistant_answer" in st.session_state:
    st.markdown("**Last assistant answer:**")
    st.info(st.session_state["last_assistant_answer"])

st.markdown("---")

# Historical charts
st.subheader("📈 Historical Data")

range_option = st.selectbox(
    "Select data range shown on charts",
    ["Last 24 hours", "Last 7 days", "Last 30 days"],
)

chart_df = filter_history(df, range_option)

if chart_df.empty:
    st.warning(f"No measurements available for: {range_option}.")
else:
    chart_col1, chart_col2 = st.columns(2)
    with chart_col1:
        st.markdown("**Temperature: Indoor vs Outdoor**")
        temp_chart = chart_df.set_index("datetime")[["indoor_temp", "outdoor_temp"]]
        temp_chart.columns = ["Indoor Temp", "Outdoor Temp"]
        st.line_chart(temp_chart)

    with chart_col2:
        st.markdown("**Humidity: Indoor vs Outdoor**")
        humidity_chart = chart_df.set_index("datetime")[["indoor_humidity", "outdoor_humidity"]]
        humidity_chart.columns = ["Indoor Humidity", "Outdoor Humidity"]
        st.line_chart(humidity_chart)

    chart_col3, chart_col4 = st.columns(2)
    with chart_col3:
        st.markdown("**CO₂ Level**")
        co2_chart = chart_df.set_index("datetime")[["indoor_co2"]]
        co2_chart.columns = ["CO₂ ppm"]
        st.line_chart(co2_chart)

    with chart_col4:
        st.markdown("**TVOC Level**")
        tvoc_chart = chart_df.set_index("datetime")[["indoor_tvoc"]]
        tvoc_chart.columns = ["TVOC ppb"]
        st.line_chart(tvoc_chart)

    st.markdown("**Motion Detection History**")
    motion_chart = chart_df.set_index("datetime")[["motion_detected"]]
    motion_chart.columns = ["Motion Detected"]
    st.bar_chart(motion_chart)

st.markdown("---")

# Basic statistics
st.subheader("📊 Quick Statistics")
stat_source_df = chart_df if not chart_df.empty else df
stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
stat_col1.metric("Avg Indoor Temp", metric_value(stat_source_df["indoor_temp"].mean(), " °C"))
stat_col2.metric("Avg Humidity", metric_value(stat_source_df["indoor_humidity"].mean(), " %"))
stat_col3.metric("Max CO₂", metric_value(stat_source_df["indoor_co2"].max(), " ppm", decimals=0))
stat_col4.metric("Motion Events", int(stat_source_df["motion_detected"].sum()))

# Raw data
with st.expander("🗄️ View raw logs from BigQuery"):
    display_columns = [
        "date",
        "time",
        "indoor_temp",
        "indoor_humidity",
        "outdoor_temp",
        "outdoor_humidity",
        "outdoor_weather",
        "indoor_co2",
        "indoor_tvoc",
        "motion_detected",
    ]
    display_df = df[display_columns].sort_values(by=["date", "time"], ascending=[False, False])
    st.dataframe(display_df, use_container_width=True)
