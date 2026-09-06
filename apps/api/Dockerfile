# Stage 1 — build the React dashboard (P11, F-026)
FROM node:20-alpine AS dashboard
WORKDIR /build
COPY apps/dashboard/package.json apps/dashboard/package-lock.json ./
RUN npm ci --silent
COPY apps/dashboard ./
RUN npm run build

# Stage 2 — API runtime (serves the built dashboard at "/")
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./
COPY packages ./packages
COPY apps ./apps
COPY --from=dashboard /build/dist ./dashboard-dist

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["uvicorn", "pia_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
