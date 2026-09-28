FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY odify_bot/ odify_bot/
# Mount a volume at /data (Railway: service → Volumes) so the database survives redeploys.
ENV ODIFY_DB=/data/odify.db
CMD ["python", "-m", "odify_bot"]
