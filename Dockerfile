FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=settings.production \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# pg_dump for nightly backups (must be at least the Postgres server's major version).
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Collect static files into the image. These placeholder values only satisfy the settings checks;
# the real ones come from the environment at runtime.
RUN DJANGO_SECRET_KEY=build-only FIELD_ENCRYPTION_KEY=build-only BACKUP_ENCRYPTION_KEY=build-only \
    python manage.py collectstatic --noinput

RUN useradd --create-home --uid 1000 app && mkdir -p /backups /app/media && chown app /backups /app/media
USER app

EXPOSE 8000
CMD ["gunicorn", "config.wsgi", "-c", "gunicorn.conf.py"]
