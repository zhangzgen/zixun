FROM node:22-alpine AS frontend
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NEWS_DB_PATH=/tmp/zixun-index/index.sqlite3
WORKDIR /app
COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt && useradd --create-home app
COPY news_web/ ./news_web/
COPY ai_news_daily/ ./ai_news_daily/
COPY technical_report_daily/ ./technical_report_daily/
COPY --from=frontend /build/web/dist ./web/dist
USER app
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn news_web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
