# The arr-media-guard image: the script, its tools, the language detection venv and the pinned Whisper model. Nothing
# downloads at run time. Build it from the repo root: docker build -f docker/Dockerfile -t arr-media-guard .
FROM debian:trixie-slim

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv ffmpeg mkvtoolnix logrotate tzdata tini \
    && rm -rf /var/lib/apt/lists/*

# The venv from the lock file, each wheel checked by its sha256. arr_lid.py --fetch downloads the pinned model and
# checks its sha256, so a wrong model fails the build. The marker "ready" turns detection on.
COPY arr_lid.requirements.txt arr_lid.py /opt/arr-media-guard/
RUN python3 -m venv /opt/arr-media-guard-lid/venv \
    && /opt/arr-media-guard-lid/venv/bin/pip install --no-cache-dir --require-hashes --only-binary=:all: \
        -r /opt/arr-media-guard/arr_lid.requirements.txt \
    && /opt/arr-media-guard-lid/venv/bin/python /opt/arr-media-guard/arr_lid.py --fetch --model-dir /opt/arr-media-guard-lid/models \
    && touch /opt/arr-media-guard-lid/ready

COPY . /opt/arr-media-guard/
RUN ln -s /opt/arr-media-guard/docker/arr-media-guard.sh /usr/local/bin/arr-media-guard

ENV ARR_MEDIA_GUARD_ENV=/config/arr-media-guard.env PYTHONDONTWRITEBYTECODE=1 PUID=1000 PGID=1000
VOLUME /config
EXPOSE 8484
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8484/health', timeout=5)"]
# tini is PID 1. It sends a stop to the script's process group, as a native terminal or service stop does, and returns the
# script's exit code. Python as PID 1 would ignore a SIGTERM it has no handler for.
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/arr-media-guard"]
CMD ["--serve"]
