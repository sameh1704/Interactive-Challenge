# Al Manar Interactive Challenge - backend image.
#
# Single-stage build on purpose: the image must stay small and simple enough for
# a school server, and there is no compiler toolchain or asset pipeline to carry.
#
# Build context is the project root so that the Dockerfile can stay in backend/
# while still being able to ignore project-level secrets.

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    DJANGO_SETTINGS_MODULE=config.settings.development \
    APP_HOME=/app \
    PATH=/app/.venv/bin:$PATH

WORKDIR ${APP_HOME}

# Dependencies first: this layer is only rebuilt when requirements.txt changes.
COPY backend/requirements.txt ./requirements.txt
RUN python -m venv "${APP_HOME}/.venv" \
 && "${APP_HOME}/.venv/bin/pip" install --no-cache-dir --require-virtualenv -r requirements.txt

# Application code.
COPY backend/ ./
# Verification suite, so the image can also run the documented test command.
COPY tests/ ./tests/

# Normalise the entrypoint in case the repository was checked out on Windows.
RUN sed -i 's/\r$//' ./docker/entrypoint.sh \
 && chmod +x ./docker/entrypoint.sh

# Collect static files at build time so that the container never writes to its
# own code layer. Runs with the development settings module, which tolerates a
# missing DJANGO_SECRET_KEY at build time.
RUN DJANGO_ENV=development python manage.py collectstatic --noinput

# Run as an unprivileged user.
RUN addgroup --system --gid 1001 challenge \
 && adduser --system --uid 1001 --ingroup challenge --no-create-home challenge \
 && mkdir -p "${APP_HOME}/media" "${APP_HOME}/staticfiles" \
 && chown -R challenge:challenge "${APP_HOME}"

USER challenge

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD ["python", "-c", "import sys,urllib.request;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/',timeout=4).status==200 else 1)"]

ENTRYPOINT ["/bin/sh", "./docker/entrypoint.sh"]

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]