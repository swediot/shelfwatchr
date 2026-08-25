FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web

# Not root: this process parses CSVs and JSON from elsewhere, and there's no
# reason for a bug in that path to own the container.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin shelfwatchr \
 && mkdir -p /data && chown -R shelfwatchr:shelfwatchr /data /srv
USER shelfwatchr

# The database lives on a mounted volume so it survives rebuilds.
ENV SHELFWATCHR_DB=/data/shelfwatchr.db
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
