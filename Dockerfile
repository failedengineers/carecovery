FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements.txt

COPY . ./

# The initial custom-user migration is generated during image build because
# Django is not available in the source-authoring environment. Once generated,
# it is included in this immutable image and applied by the release command.
RUN DJANGO_DEBUG=1 DJANGO_SECRET_KEY=build-only-key-not-used-at-runtime \
    python manage.py makemigrations crm --noinput && \
    DJANGO_DEBUG=1 DJANGO_SECRET_KEY=build-only-key-not-used-at-runtime \
    python manage.py collectstatic --noinput

RUN useradd --create-home --uid 10001 carecovery && \
    chown -R carecovery:carecovery /app
USER carecovery

EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py merge_legacy_roles && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT} --workers ${WEB_CONCURRENCY:-1} --threads ${GUNICORN_THREADS:-2} --timeout 60 --access-logfile - --error-logfile -"]
