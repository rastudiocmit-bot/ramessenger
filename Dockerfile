FROM python:3.12.15-bookworm

ENV PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONPATH=/app/backend/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN groupadd --gid 10001 messenger \
    && useradd --uid 10001 --gid 10001 --no-create-home messenger \
    && python3 -m venv /opt/venv

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

# Chromium's runtime libraries come from apt, and that download is the least
# reliable part of the build. The apt lists and deb cache live in BuildKit cache
# mounts, so they stay out of the image and a rebuild reuses them instead of
# hitting the mirror again. Force IPv4 and bound the timeouts so a broken
# mirror path fails the step instead of hanging it forever. Playwright must be
# installed already, hence this step runs after pip.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "3";\nAcquire::http::Timeout "20";\nAcquire::https::Timeout "20";\n' \
        > /etc/apt/apt.conf.d/99-network \
    && /opt/venv/bin/playwright install-deps chromium

RUN /opt/venv/bin/playwright install chromium

COPY --chown=10001:10001 app/backend/src ./backend/src
COPY --chown=10001:10001 app/backend/migrations ./backend/migrations
COPY --chown=10001:10001 app/alembic.ini ./alembic.ini

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "center_messenger.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
