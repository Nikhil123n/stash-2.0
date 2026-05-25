FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /tmp/stash
CMD ["python", "-m", "stash.bot"]
