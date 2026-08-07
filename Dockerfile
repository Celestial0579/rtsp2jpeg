FROM python:3.12-alpine

# ffmpeg macht die eigentliche Arbeit; tini raeumt Zombie-Prozesse ab,
# weil rtsp2jpeg staendig ffmpeg-Kindprozesse startet und beendet.
RUN apk add --no-cache ffmpeg tini \
 && pip install --no-cache-dir "PyYAML>=6.0,<7"

WORKDIR /app
COPY rtsp2jpeg/ /app/rtsp2jpeg/

RUN addgroup -S -g 10001 rtsp2jpeg \
 && adduser -S -u 10001 -G rtsp2jpeg rtsp2jpeg
USER 10001:10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url='http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

ENTRYPOINT ["/sbin/tini", "--", "python", "-m", "rtsp2jpeg"]
