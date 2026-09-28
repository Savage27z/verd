FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY odify_bot/ odify_bot/
ENV ODIFY_DB=/data/odify.db
VOLUME /data
CMD ["python", "-m", "odify_bot"]
