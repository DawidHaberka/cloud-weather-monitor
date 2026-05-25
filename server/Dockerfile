FROM python:3.9-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY ./main.py ./
COPY ./requirements.txt ./

RUN pip3 install -r requirements.txt

ENTRYPOINT ["python"]
CMD ["main.py"]