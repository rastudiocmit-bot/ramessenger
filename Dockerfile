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

# Chromium's runtime libraries come from apt. The public Debian mirror is not
# usable at this speed from the production host (deb.debian.org stalls on files
# of a few megabytes), so the build points apt at a mirror that responds in
# fractions of a second. The apt lists and deb cache live in BuildKit cache
# mounts: they stay out of the image, and a rebuild reuses them instead of
# downloading again. Force IPv4 and bound the timeouts so a broken mirror fails
# the step instead of hanging it. Playwright must already be installed, hence
# this step comes after pip.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i 's|http://deb.debian.org|http://mirror.yandex.ru|g' /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "3";\nAcquire::http::Timeout "20";\nAcquire::https::Timeout "20";\n' \
        > /etc/apt/apt.conf.d/99-network \
    && /opt/venv/bin/playwright install-deps chromium

RUN /opt/venv/bin/playwright install chromium

COPY --chown=10001:10001 app/backend/src ./backend/src
COPY --chown=10001:10001 app/backend/migrations ./backend/migrations
COPY --chown=10001:10001 app/alembic.ini ./alembic.ini

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "center_messenger.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
