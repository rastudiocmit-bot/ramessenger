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

# The Debian mirrors publish AAAA records, but the build network has no IPv6
# route: apt resolves to IPv6 and then stalls mid-download. Force IPv4 and
# bound the retries so a slow mirror fails the build instead of hanging it.
RUN printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "3";\nAcquire::http::Timeout "30";\n' \
        > /etc/apt/apt.conf.d/99-network \
    && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check -r requirements.txt \
    && /opt/venv/bin/playwright install-deps chromium \
    && /opt/venv/bin/playwright install chromium \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=10001:10001 app/backend/src ./backend/src
COPY --chown=10001:10001 app/backend/migrations ./backend/migrations
COPY --chown=10001:10001 app/alembic.ini ./alembic.ini

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "center_messenger.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
