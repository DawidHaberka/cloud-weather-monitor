cd ~/cloud-weather-monitor

cat > README.md <<'EOF'
# Smart Home Weather Monitor — Cloud Analytics Project

## Project Overview

This project implements a cloud-based indoor and outdoor weather monitoring system using an M5Stack IoT device, environmental sensors, Google Cloud, BigQuery, Streamlit, FastAPI and Gemini AI.

The system collects indoor measurements such as temperature, humidity, air quality and motion detection, combines them with outdoor weather data, stores historical measurements in BigQuery, and provides two user interfaces:

1. **M5Stack on-device interface** — for local real-time monitoring and voice interaction.
2. **Streamlit cloud dashboard** — for remote monitoring, historical analysis and AI-based questions.

The system also includes a middleware API deployed on Google Cloud Run. This API connects the user interfaces with BigQuery and Gemini, allowing both the dashboard and the M5Stack device to use the same backend logic.

---

## Team Members and Contributions

- **Dawid Haberka**  
  Developed the Streamlit dashboard, FastAPI middleware layer, BigQuery integration, Cloud Run deployment, AI assistant integration with Gemini, API endpoints, and cloud architecture.

- **[Team member name]**  
  Developed the M5Stack device interface, sensor integration, Speech-to-Text, Text-to-Speech, OpenWeatherMap forecast integration, on-device alerts, and WiFi configuration.

- **[Team member name]**  
  [Add contribution]

---

## Architecture

The project follows a three-tier architecture:

```text
M5Stack Device / Streamlit Dashboard
        ↓
FastAPI Middleware on Google Cloud Run
        ↓
BigQuery + Gemini AI + OpenWeatherMap