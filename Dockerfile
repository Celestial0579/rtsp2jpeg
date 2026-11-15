# Basisabbild per Digest festgelegt; Dependabot schlaegt neue vor.
FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd

# ffmpeg macht die eigentliche Arbeit; tini raeumt Zombie-Prozesse ab,
# weil rtsp2jpeg staendig ffmpeg-Kindprozesse startet und beendet.
# Python-Pakete nur in fester Version und mit Pruefsumme.
COPY requirements.txt /tmp/requirements.txt
RUN apk add --no-cache ffmpeg tini \
 && pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

WORKDIR /app
COPY rtsp2jpeg/ /app/rtsp2jpeg/

RUN addgroup -S -g 10001 rtsp2jpeg \
 && adduser -S -u 10001 -G rtsp2jpeg rtsp2jpeg
USER 10001:10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

EXPOSE 8080

# Prueft nur, ob der Dienst Verbindungen annimmt -- ohne HTTP-Anfrage,
# es gibt keinen eigenen Pfad dafuer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,socket; \
socket.create_connection(('127.0.0.1', int(os.environ.get('PORT','8080'))), 4).close()"

ENTRYPOINT ["/sbin/tini", "--", "python", "-m", "rtsp2jpeg"]
