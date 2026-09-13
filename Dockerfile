# Atlas Chokepoint — produktionsbild för molnhosting (Render).
FROM python:3.13-slim

# ca-certificates ger ett korrekt CA-lager → strikt TLS fungerar i molnet
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Beroenden först (lager-cache)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Kopiera appen (cache-databaser + .env exkluderas av .dockerignore)
COPY . .

ENV ATLAS_STRICT_TLS=1 \
    PYTHONUNBUFFERED=1

# Render injicerar $PORT; speglas till ATLAS_PORT. ${PORT:-8060} gör att bilden
# även går att köra lokalt utan PORT satt.
CMD ["sh", "-c", "ATLAS_PORT=${PORT:-8060} exec uvicorn atlas_choke.server.app:app --host 0.0.0.0 --port ${PORT:-8060}"]
