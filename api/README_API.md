# Cloud API - Smart Home Weather Monitor

This folder contains the middleware layer of the project.

## Endpoints

### GET `/health`
Checks whether the API is running.

### GET `/latest`
Returns the latest sensor record from BigQuery.

### POST `/ask-text`
Receives a text question and returns an answer based on BigQuery data.

Example:

```json
{
  "question": "What is the temperature now?"
}
```

or:

```json
{
  "question": "What was the average temperature on May 16?"
}
```

### POST `/ask-audio`
Placeholder for the M5Stack microphone workflow. It currently confirms that audio upload works. Speech-to-text will be added in the next step.

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

Open:

```text
http://localhost:8080/docs
```

## Deploy to Cloud Run

```bash
gcloud run deploy smart-home-weather-api \
  --source . \
  --region europe-west6 \
  --allow-unauthenticated \
  --set-env-vars GCP_PROJECT_ID=durable-will-487916-n1,BQ_LOCATION=europe-west6,BQ_TABLE=durable-will-487916-n1.Lab4_IoT_datasets.weather-records
```

For local development, use `GOOGLE_APPLICATION_CREDENTIALS`.
For Cloud Run, assign the Cloud Run service account BigQuery permissions.
