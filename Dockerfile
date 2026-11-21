FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY alembic.ini ./
COPY migrations ./migrations
COPY eumbms ./eumbms
COPY scripts/start.sh ./start.sh
RUN chmod +x start.sh && useradd --system --uid 10001 app && chown -R app /app
USER app
EXPOSE 8000
CMD ["./start.sh"]
