FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu passwd \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "gunicorn>=23,<24"

COPY app.py .
COPY gunicorn.conf.py .
COPY vod_dashboard ./vod_dashboard
COPY templates ./templates
COPY static ./static
COPY cleanup-vods.py .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --home-dir /data --shell /usr/sbin/nologin app \
    && mkdir -p /data /downloads \
    && chown app:app /data /downloads \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
