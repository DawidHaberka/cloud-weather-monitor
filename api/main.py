import os
from datetime import datetime, timedelta

import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from google.cloud import bigquery

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    genai = None
    GEMINI_AVAILABLE = False


PROJECT_ID = os.getenv("GCP_PROJECT_ID", "durable-will-487916-n1")
BQ_LOCATION = os.getenv("BQ_LOCATION", "europe-west6")
TABLE_ID = os.getenv("BQ_TABLE_ID", "durable-will-487916-n1.Lab4_IoT_datasets.weather-records")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash")

app = FastAPI(title="Smart Home IoT API")


class QuestionRequest(BaseModel):
    question: str


def load_data(days_back: int = 35, limit: int = 20000) -> pd.DataFrame:
    client = bigquery.Client(project=PROJECT_ID, location=BQ_LOCATION)

    query = f"""
        SELECT
            date, time, indoor_temp, indoor_humidity, outdoor_temp, outdoor_humidity,
            outdoor_weather, indoor_co2, indoor_tvoc, motion_detected
        FROM `{TABLE_ID}`
        ORDER BY date DESC, time DESC
        LIMIT {limit}
    """

    df = client.query(query).to_dataframe()

    if df.empty:
        return df

    df["datetime"] = pd.to_datetime(
        df["date"].astype(str) + " " + df["time"].astype(str),
        errors="coerce"
    )

    df["date_only"] = pd.to_datetime(
        df["date"].astype(str),
        errors="coerce"
    ).dt.date

    df = df.dropna(subset=["datetime", "date_only"]).sort_values("datetime")

    cutoff = df["datetime"].max() - timedelta(days=days_back)
    df = df[df["datetime"] >= cutoff].copy()

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
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def stats_for_period(df: pd.DataFrame, days: int) -> dict:
    if df.empty:
        return {}

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
        "humidity_avg": round(float(period_df["indoor_humidity"].mean()), 2),
        "co2_avg": round(float(period_df["indoor_co2"].mean()), 2),
        "co2_max": round(float(period_df["indoor_co2"].max()), 2),
        "tvoc_avg": round(float(period_df["indoor_tvoc"].mean()), 2),
        "tvoc_max": round(float(period_df["indoor_tvoc"].max()), 2),
        "motion_events": int(period_df["motion_detected"].fillna(0).sum()),
    }


def gemini_answer(prompt: str, fallback: str) -> str:
    if not (GEMINI_AVAILABLE and GEMINI_API_KEY):
        return fallback

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel(GEMINI_MODEL_NAME)
        response = model.generate_content(prompt)
        if response and getattr(response, "text", None):
            return response.text.strip()
    except Exception:
        return fallback

    return fallback


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/latest")
def latest():
    try:
        df = load_data()
        if df.empty:
            raise HTTPException(status_code=404, detail="No data found")

        row = df.iloc[-1].to_dict()
        row["datetime"] = str(row["datetime"])
        row["date_only"] = str(row["date_only"])
        return row

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
def ask(req: QuestionRequest):
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

    fallback = "I can answer questions about temperature, humidity, air quality, motion, weather, and summary statistics."

    prompt = f"""
You are an AI assistant for a smart home IoT dashboard.

User question:
{req.question}

Evidence:
{evidence}

Rules:
- Use only the evidence.
- Answer questions about average, minimum, maximum, weather, air quality, clothing, and recommendations.
- If evidence is insufficient, say so.
- Keep the answer concise.
"""
    return {"answer": gemini_answer(prompt, fallback)}
