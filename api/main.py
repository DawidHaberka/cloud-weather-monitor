from __future__ import annotations

import os
import requests
from datetime import datetime

try:
    import google.generativeai as genai
except ImportError:
    genai = None
import re
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google.cloud import bigquery

# Cloud configuration.
# Values are read from environment variables in Cloud Run.
# Defaults are kept for local testing and easier deployment.
PROJECT_ID = os.getenv("GCP_PROJECT_ID", "durable-will-487916-n1")
BQ_LOCATION = os.getenv("BQ_LOCATION", "europe-west6")
BQ_TABLE = os.getenv(
    "BQ_TABLE",
    "durable-will-487916-n1.Lab4_IoT_datasets.weather-records",
)

# FastAPI application used as the middleware layer.
# The dashboard calls this API instead of connecting directly to BigQuery, Gemini or OpenWeatherMap.
app = FastAPI(
    title="Smart Home Weather Monitor API",
    description="Middleware layer for M5Stack, Streamlit, BigQuery and AI assistant.",
    version="1.0.0",
)

# CORS allows the Streamlit dashboard and other clients to call this API.
# In production, allow_origins could be restricted to the dashboard URL.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models define the expected request and response structure.
# This helps FastAPI validate incoming JSON automatically.
class AskTextRequest(BaseModel):
    question: str
    selected_date: Optional[str] = None  # YYYY-MM-DD fallback date

class AskTextResponse(BaseModel):
    question: str
    answer: str
    source: str = "BigQuery"

class LatestResponse(BaseModel):
    data: dict[str, Any]

# Creates a BigQuery client using the Google Cloud project and location.
def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT_ID, location=BQ_LOCATION)

# Loads the latest sensor and weather records from BigQuery.
# The data is converted into a pandas DataFrame because it is easier to calculate statistics later.
def load_data(limit: int = 1000) -> pd.DataFrame:
    # Query only the columns needed by the dashboard and AI assistant.
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
        FROM `{BQ_TABLE}`
        ORDER BY date DESC, time DESC
        LIMIT @limit
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("limit", "INT64", limit)]
    )
    df = get_bq_client().query(query, job_config=job_config).to_dataframe()
    if df.empty:
        return df
    # Combine date and time into one datetime column for sorting and period-based filtering.
    df["datetime"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), errors="coerce")
    df["date_only"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    return df.sort_values("datetime")

# Converts pandas/date values into JSON-safe Python values.
# This is needed because FastAPI cannot directly return pandas Timestamp or NumPy values.
def json_safe(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return value.item()
    except AttributeError:
        return value

# Returns the most recent record from BigQuery.
# This endpoint is used by the dashboard and can also be used by the device after restart.
def latest_record() -> dict[str, Any]:
    df = load_data(limit=1)
    if df.empty:
        raise HTTPException(status_code=404, detail="No data found in BigQuery.")
    return {key: json_safe(value) for key, value in df.iloc[-1].to_dict().items()}

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Extracts a date from a user question.
# Supported formats include YYYY-MM-DD, "16 May" and "May 16".
# If no date is found, the selected_date from the request can be used as fallback.
def parse_date_from_question(question: str, fallback: Optional[str] = None) -> Optional[date]:
    q = question.lower().strip()
    current_year = datetime.now().year

    match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", q)
    if match:
        y, m, d = map(int, match.groups())
        return date(y, m, d)

    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-zA-Z]+)\b", q)
    if match:
        day = int(match.group(1))
        month_name = match.group(2).lower()
        if month_name in MONTHS:
            return date(current_year, MONTHS[month_name], day)

    match = re.search(r"\b([a-zA-Z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b", q)
    if match:
        month_name = match.group(1).lower()
        day = int(match.group(2))
        if month_name in MONTHS:
            return date(current_year, MONTHS[month_name], day)

    if fallback:
        try:
            return datetime.strptime(fallback, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None

# Detects which sensor metric the user is asking about.
# This is used by the rule-based /ask-text endpoint.
def detect_metric(question: str) -> str:
    q = question.lower()
    if "humidity" in q:
        return "indoor_humidity"
    if "co2" in q or "co₂" in q or "carbon" in q:
        return "indoor_co2"
    if "tvoc" in q or "voc" in q:
        return "indoor_tvoc"
    if "outdoor" in q and "temperature" in q:
        return "outdoor_temp"
    return "indoor_temp"

# Detects whether the user asks for an average, minimum or maximum value.
def detect_operation(question: str) -> str:
    q = question.lower()
    if "minimum" in q or "lowest" in q or "min" in q:
        return "min"
    if "maximum" in q or "highest" in q or "max" in q:
        return "max"
    return "average"

# Handles simple current-condition questions without calling Gemini.
# This provides a deterministic fallback for basic questions.
def answer_current_question(question: str, row: dict[str, Any]) -> Optional[str]:
    q = question.lower()

    if "temperature" in q and ("now" in q or "current" in q):
        return f"The current indoor temperature is {row['indoor_temp']}°C. The outdoor temperature is {row['outdoor_temp']}°C."

    if "humidity" in q and ("now" in q or "current" in q):
        return f"The current indoor humidity is {row['indoor_humidity']}%. The outdoor humidity is {row['outdoor_humidity']}%."

    if "air quality" in q or "co2" in q or "co₂" in q or "tvoc" in q:
        co2 = float(row["indoor_co2"])
        tvoc = float(row["indoor_tvoc"])
        status = "not ideal. Ventilation is recommended" if co2 > 1000 or tvoc > 300 else "acceptable"
        return f"Air quality is {status}. CO2 is {co2:.0f} ppm and TVOC is {tvoc:.0f} ppb."

    if "motion" in q or "movement" in q:
        return "Motion was detected near the device." if int(row["motion_detected"]) == 1 else "No motion is currently detected near the device."

    if "umbrella" in q or "rain" in q:
        weather = str(row["outdoor_weather"]).lower()
        if "rain" in weather or "drizzle" in weather or "storm" in weather:
            return f"Yes, taking an umbrella is recommended. Current outdoor weather is {row['outdoor_weather']}."
        return f"An umbrella does not seem necessary right now. Current outdoor weather is {row['outdoor_weather']}."

    return None


def answer_analytical_question(question: str, selected_date: Optional[str]) -> str:
    target_date = parse_date_from_question(question, fallback=selected_date)
    if target_date is None:
        return "Please provide or select a date. For example: 'What was the average temperature on May 16?'"

    df = load_data(limit=5000)
    if df.empty:
        return "I cannot answer because there is no data available in BigQuery."

    day_df = df[df["date_only"] == target_date]
    if day_df.empty:
        return f"I found no measurements for {target_date.isoformat()}."

    metric = detect_metric(question)
    operation = detect_operation(question)
    values = pd.to_numeric(day_df[metric], errors="coerce").dropna()
    if values.empty:
        return f"I found data for {target_date.isoformat()}, but no valid values for {metric}."

    if operation == "min":
        result, label = values.min(), "minimum"
    elif operation == "max":
        result, label = values.max(), "maximum"
    else:
        result, label = values.mean(), "average"

    unit = {
        "indoor_temp": "°C",
        "outdoor_temp": "°C",
        "indoor_humidity": "%",
        "indoor_co2": "ppm",
        "indoor_tvoc": "ppb",
    }.get(metric, "")

    readable_metric = {
        "indoor_temp": "indoor temperature",
        "outdoor_temp": "outdoor temperature",
        "indoor_humidity": "indoor humidity",
        "indoor_co2": "CO2 level",
        "indoor_tvoc": "TVOC level",
    }.get(metric, metric)

    return f"On {target_date.isoformat()}, based on {len(values)} measurements, the {label} {readable_metric} was {result:.2f}{unit}."

# Rule-based question answering pipeline.
# It first tries to answer current-condition questions, then falls back to date-based analysis.
def answer_question(question: str, selected_date: Optional[str] = None) -> str:
    if not question or not question.strip():
        return "Please ask a question."
    row = latest_record()
    current_answer = answer_current_question(question, row)
    if current_answer:
        return current_answer
    return answer_analytical_question(question, selected_date)

def get_three_day_forecast():
    """
    Fetch a 3-day forecast for Lausanne from OpenWeatherMap.

    OpenWeatherMap returns 3-hour forecast entries.
    The function groups them by date, skips today, and selects one representative
    forecast entry per future day, preferably around 12:00 UTC.
    """

    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key:
        return {
            "status": "error",
            "message": "OPENWEATHER_API_KEY environment variable is not set",
            "forecast": []
        }

    city = os.getenv("FORECAST_CITY", "Lausanne")

    url = (
        "https://api.openweathermap.org/data/2.5/forecast"
        f"?q={city}&appid={api_key}&units=metric&cnt=40"
    )

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        today = datetime.now().strftime("%Y-%m-%d")

        days_raw = {}

        for item in data.get("list", []):
            date = item["dt_txt"][:10]
            hour = item["dt_txt"][11:13]

            if date == today:
                continue

            if date not in days_raw:
                days_raw[date] = {}

            days_raw[date][hour] = item

        forecast_days = []

        for date, hours in sorted(days_raw.items())[:3]:
            item = (
                hours.get("12")
                or hours.get("11")
                or hours.get("13")
                or list(hours.values())[0]
            )

            forecast_days.append({
                "day": date,
                "temp": round(item["main"]["temp"]),
                "weather": item["weather"][0]["description"]
            })

        return {
            "status": "success",
            "city": city,
            "forecast": forecast_days
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "forecast": []
        }
        
# Calculates summary statistics for a selected rolling period.
# Used by /stats and by the Gemini prompt as historical context.
def stats_for_period(df, days: int) -> dict:
    if df.empty:
        return {}

    from datetime import timedelta
    
    # Use the newest available timestamp as the reference point for the rolling window.
    max_dt = df["datetime"].max()
    period_df = df[df["datetime"] >= max_dt - timedelta(days=days)].copy()

    if period_df.empty:
        return {}

    return {
        "measurements": int(len(period_df)),
        "start": str(period_df["datetime"].min()),
        "end": str(period_df["datetime"].max()),
        "indoor_temp_avg": round(float(period_df["indoor_temp"].mean()), 2),
        "indoor_temp_min": round(float(period_df["indoor_temp"].min()), 2),
        "indoor_temp_max": round(float(period_df["indoor_temp"].max()), 2),
        "outdoor_temp_avg": round(float(period_df["outdoor_temp"].mean()), 2),
        "outdoor_temp_min": round(float(period_df["outdoor_temp"].min()), 2),
        "outdoor_temp_max": round(float(period_df["outdoor_temp"].max()), 2),
        "indoor_humidity_avg": round(float(period_df["indoor_humidity"].mean()), 2),
        "co2_avg": round(float(period_df["indoor_co2"].mean()), 2),
        "co2_max": round(float(period_df["indoor_co2"].max()), 2),
        "tvoc_avg": round(float(period_df["indoor_tvoc"].mean()), 2),
        "tvoc_max": round(float(period_df["indoor_tvoc"].max()), 2),
        "motion_events": int(period_df["motion_detected"].fillna(0).sum()),
    }

# Sends the prepared prompt to Gemini.
# If Gemini is not configured or fails, the function returns a safe fallback answer.
def gemini_answer(prompt: str, fallback: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")
    
    # Keep the API stable even when Gemini API key is missing.
    if not api_key or genai is None:
        return fallback

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)
        response = model.generate_content(prompt)

        answer = ""
        if response is not None and getattr(response, "text", None):
            answer = response.text.strip()

        return answer if answer else fallback

    except Exception as e:
        return fallback

# Public endpoint used by the dashboard to display the 3-day forecast.
@app.get("/forecast")
def forecast():
    return get_three_day_forecast()

# Health check endpoint used to verify that the Cloud Run service is running.
@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

# Returns the newest BigQuery record as JSON.
@app.get("/latest", response_model=LatestResponse)
def latest() -> LatestResponse:
    return LatestResponse(data=latest_record())

# Optional rule-based text endpoint.
# It is kept as a deterministic fallback, while /ask is the main Gemini endpoint.
@app.post("/ask-text", response_model=AskTextResponse)
def ask_text(payload: AskTextRequest) -> AskTextResponse:
    answer = answer_question(payload.question, payload.selected_date)
    return AskTextResponse(question=payload.question, answer=answer)

# Placeholder endpoint for audio upload testing.
# Final voice interaction is handled by the dashboard/device layer.
@app.post("/ask-audio")
async def ask_audio(audio: UploadFile = File(...)) -> dict[str, Any]:
    content = await audio.read()
    return {
        "status": "received",
        "filename": audio.filename,
        "size_bytes": len(content),
        "message": "Audio upload works. Speech-to-text will be added in the next step."
    }

# Returns historical summary statistics for the dashboard.
@app.get("/stats")
def stats():
    df = load_data()
    if df.empty:
        raise HTTPException(status_code=404, detail="No data found")

    return {
        "last_24h": stats_for_period(df, 1),
        "last_7d": stats_for_period(df, 7),
        "last_30d": stats_for_period(df, 30),
    }




# Main AI assistant endpoint.
# It combines latest sensor data, historical statistics and forecast data,
# then sends this structured evidence to Gemini.
@app.post("/ask")
def ask(payload: AskTextRequest) -> dict:
    
    # Load recent BigQuery data. This is the factual base for the assistant.
    df = load_data()
    if df.empty:
        raise HTTPException(status_code=404, detail="No data found")
    
    # Use the latest measurement for current-condition questions.
    latest_row = df.iloc[-1].to_dict()

    # Fetch live forecast so Gemini can answer future-weather questions.
    forecast_data = get_three_day_forecast()

    # Build one evidence object containing all information Gemini is allowed to use.
    # This reduces hallucination because the model receives structured project data.
    evidence = {
        "latest": {k: str(v) for k, v in latest_row.items()},
        "last_24h": stats_for_period(df, 1),
        "last_7d": stats_for_period(df, 7),
        "last_30d": stats_for_period(df, 30),
        "forecast_next_3_days": forecast_data,
    }

    fallback = (
        "Based on the available sensor data and 3-day forecast, I can answer questions about "
        "temperature, humidity, air quality, motion, current weather, future weather, "
        "clothing recommendations, umbrella recommendations and summary statistics."
    )

    prompt = f"""
You are an AI assistant for a smart home IoT dashboard in Lausanne.

User question:
{payload.question}

Evidence from sensor data and forecast:
{evidence}

Instructions:
- Use only the evidence above.
- For current conditions, use the latest sensor and weather measurements.
- For questions about tomorrow, the next days, forecast, rain, umbrella or future clothing recommendations, use forecast_next_3_days.
- For historical questions, use last_24h, last_7d or last_30d statistics.
- If the user asks what to wear, combine outdoor temperature, weather and forecast when relevant.
- If CO2 or TVOC are high, recommend ventilation.
- If evidence is insufficient, say so clearly.
- Keep the answer concise: 1 to 4 sentences.
"""
    
    # Generate the final natural-language answer.
    answer = gemini_answer(prompt, fallback)

    return {
        "question": payload.question,
        "answer": answer,
        "forecast": forecast_data
    }
