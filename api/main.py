from __future__ import annotations

import os

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

PROJECT_ID = os.getenv("GCP_PROJECT_ID", "durable-will-487916-n1")
BQ_LOCATION = os.getenv("BQ_LOCATION", "europe-west6")
BQ_TABLE = os.getenv(
    "BQ_TABLE",
    "durable-will-487916-n1.Lab4_IoT_datasets.weather-records",
)

app = FastAPI(
    title="Smart Home Weather Monitor API",
    description="Middleware layer for M5Stack, Streamlit, BigQuery and AI assistant.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AskTextRequest(BaseModel):
    question: str
    selected_date: Optional[str] = None  # YYYY-MM-DD fallback date

class AskTextResponse(BaseModel):
    question: str
    answer: str
    source: str = "BigQuery"

class LatestResponse(BaseModel):
    data: dict[str, Any]


def get_bq_client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT_ID, location=BQ_LOCATION)


def load_data(limit: int = 1000) -> pd.DataFrame:
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
    df["datetime"] = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), errors="coerce")
    df["date_only"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    return df.sort_values("datetime")


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


def detect_operation(question: str) -> str:
    q = question.lower()
    if "minimum" in q or "lowest" in q or "min" in q:
        return "min"
    if "maximum" in q or "highest" in q or "max" in q:
        return "max"
    return "average"


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


def answer_question(question: str, selected_date: Optional[str] = None) -> str:
    if not question or not question.strip():
        return "Please ask a question."
    row = latest_record()
    current_answer = answer_current_question(question, row)
    if current_answer:
        return current_answer
    return answer_analytical_question(question, selected_date)


def stats_for_period(df, days: int) -> dict:
    if df.empty:
        return {}

    from datetime import timedelta

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


def gemini_answer(prompt: str, fallback: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY")
    model_name = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")

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

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/latest", response_model=LatestResponse)
def latest() -> LatestResponse:
    return LatestResponse(data=latest_record())

@app.post("/ask-text", response_model=AskTextResponse)
def ask_text(payload: AskTextRequest) -> AskTextResponse:
    answer = answer_question(payload.question, payload.selected_date)
    return AskTextResponse(question=payload.question, answer=answer)

@app.post("/ask-audio")
async def ask_audio(audio: UploadFile = File(...)) -> dict[str, Any]:
    content = await audio.read()
    return {
        "status": "received",
        "filename": audio.filename,
        "size_bytes": len(content),
        "message": "Audio upload works. Speech-to-text will be added in the next step."
    }

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




@app.post("/ask")
def ask(payload: AskTextRequest) -> dict:
    df = load_data()
    if df.empty:
        raise HTTPException(status_code=404, detail="No data found")

    latest_row = df.iloc[-1].to_dict()

    evidence = {
        "latest": {k: str(v) for k, v in latest_row.items()},
        "last_24h": stats_for_period(df, 1),
        "last_7d": stats_for_period(df, 7),
        "last_30d": stats_for_period(df, 30),
    }

    fallback = (
        "Based on the available sensor data, I can answer questions about "
        "temperature, humidity, air quality, motion, weather, clothing recommendations, "
        "and summary statistics."
    )

    prompt = f"""
You are an AI assistant for a smart home IoT dashboard.

User question:
{payload.question}

Evidence from sensor data:
{evidence}

Instructions:
- Use only the evidence above.
- Answer questions about current temperature, humidity, air quality, motion, weather, clothing recommendations, averages, minimums, maximums and trends.
- If the user asks what to wear, use outdoor temperature and weather.
- If CO2 or TVOC are high, recommend ventilation.
- If evidence is insufficient, say so clearly.
- Keep the answer concise: 1 to 4 sentences.
"""

    answer = gemini_answer(prompt, fallback)

    return {
        "question": payload.question,
        "answer": answer,
    }
