FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pht_bot ./pht_bot
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "-m", "pht_bot.main"]
