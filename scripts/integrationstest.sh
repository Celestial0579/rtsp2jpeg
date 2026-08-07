#!/usr/bin/env bash
# Integrationstest ohne echte Kamera und ohne Telefonanlage.
#
# Baut das Abbild, stellt mit mediamtx einen RTSP-Server samt bewegtem
# Testbild daneben und prueft, dass am anderen Ende brauchbare JPEGs
# herauskommen.
#
#   scripts/integrationstest.sh            aufraeumen am Ende
#   scripts/integrationstest.sh --behalten Aufbau stehen lassen
set -euo pipefail

cd "$(dirname "$0")/.."

TOKEN="pruef-token"
BEHALTEN=0
[[ "${1:-}" == "--behalten" ]] && BEHALTEN=1

# Auf dem Entwicklungsrechner koennen mehrere Instanzen parallel testen --
# deshalb der Namensraum aus dem Branch. Ausserhalb genuegt ein fester Name.
if command -v arbeit >/dev/null 2>&1; then
    PROJEKT="$(arbeit ns)-rtsp2jpeg-test"
else
    PROJEKT="rtsp2jpeg-test"
fi

COMPOSE=(docker compose -f docker-compose.test.yml -p "$PROJEKT")
export TEST_TOKEN="$TOKEN"

aufraeumen() {
    if [[ $BEHALTEN -eq 1 ]]; then
        echo
        echo "Aufbau bleibt stehen. Abbauen mit:"
        echo "  ${COMPOSE[*]} down -v"
        return
    fi
    echo
    echo "--- Abbau ---"
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
}
trap aufraeumen EXIT

fehler=0
pruefe() {  # pruefe "<Beschreibung>" <Befehl...>
    local beschreibung="$1"; shift
    if "$@" >/tmp/pruefung.log 2>&1; then
        echo "  OK    $beschreibung"
    else
        echo "  FEHL  $beschreibung"
        sed 's/^/          /' /tmp/pruefung.log | head -20
        fehler=$((fehler + 1))
    fi
}

# Fuehrt einen Befehl im rtsp2jpeg-Container aus.
im_container() { "${COMPOSE[@]}" exec -T rtsp2jpeg sh -c "$1"; }

echo "--- Aufbau (Projekt: $PROJEKT) ---"
"${COMPOSE[@]}" up -d --build

echo
echo "--- Warte auf das erste Bild ---"
bereit=0
for _ in $(seq 1 60); do
    if im_container "python -c \"
import sys, urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:8080/cam/tuer.jpg?token=$TOKEN&timeout=3', timeout=6) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
\"" >/dev/null 2>&1; then
        bereit=1
        break
    fi
    sleep 2
done

if [[ $bereit -eq 0 ]]; then
    echo "  FEHL  Nach 120s kein Bild -- Protokolle:"
    "${COMPOSE[@]}" logs --tail 40
    exit 1
fi
echo "  OK    Kamera liefert"

echo
echo "--- Pruefungen ---"

pruefe "/healthz meldet ok" im_container "
python -c \"
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5) as r:
    daten = json.load(r)
assert daten['status'] == 'ok', daten
\""

pruefe "Standbild ist ein gueltiges JPEG mit 1280x720" im_container "
python -c \"
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/cam/tuer.jpg?token=$TOKEN', timeout=10) as r:
    daten = r.read()
    assert r.headers['Content-Type'] == 'image/jpeg', r.headers['Content-Type']
    assert len(daten) == int(r.headers['Content-Length'])
assert daten.startswith(b'\xff\xd8') and daten.endswith(b'\xff\xd9'), 'kein JPEG'
open('/tmp/bild.jpg','wb').write(daten)
print(len(daten), 'Bytes')
\"
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,codec_name \
        -of csv=p=0 /tmp/bild.jpg | tee /tmp/masse.txt
grep -qx 'mjpeg,1280,720' /tmp/masse.txt"

pruefe "Bilder aendern sich (bewegtes Testbild)" im_container "
python -c \"
import time, urllib.request
def hole():
    with urllib.request.urlopen('http://127.0.0.1:8080/cam/tuer.jpg?token=$TOKEN&max_age=0.5', timeout=10) as r:
        return r.read()
a = hole(); time.sleep(1.5); b = hole()
assert a != b, 'zweimal dasselbe Bild'
\""

pruefe "ohne Token kein Bild (401)" im_container "
python -c \"
import urllib.error, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8080/cam/tuer.jpg', timeout=5)
except urllib.error.HTTPError as e:
    with e:
        assert e.code == 401, e.code
else:
    raise SystemExit('Zugriff war ohne Token moeglich')
\""

pruefe "unbekannte Kamera ergibt 404" im_container "
python -c \"
import urllib.error, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8080/cam/gibtsnicht.jpg?token=$TOKEN', timeout=5)
except urllib.error.HTTPError as e:
    with e:
        assert e.code == 404, e.code
else:
    raise SystemExit('unbekannte Kamera lieferte ein Bild')
\""

pruefe "MJPEG-Strom liefert mehrere Bilder" im_container "
python -c \"
import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/cam/tuer.mjpg?token=$TOKEN', timeout=15) as r:
    assert r.headers['Content-Type'].startswith('multipart/x-mixed-replace')
    daten = r.read(200000)
assert daten.count(b'--rtsp2jpeg-frame') >= 2, daten.count(b'--rtsp2jpeg-frame')
\""

pruefe "Bild kommt auch durch den vorgelagerten Webserver" im_container "
python -c \"
import urllib.request
with urllib.request.urlopen('http://proxy/cam/tuer.jpg?token=$TOKEN', timeout=15) as r:
    daten = r.read()
    assert r.headers['Content-Type'] == 'image/jpeg', r.headers['Content-Type']
assert daten.startswith(b'\xff\xd8') and daten.endswith(b'\xff\xd9'), 'kein JPEG'
\""

pruefe "Proxy gibt die Statusseiten nicht nach aussen" im_container "
python -c \"
import urllib.error, urllib.request
for pfad in ('/status', '/', '/healthz'):
    try:
        urllib.request.urlopen('http://proxy' + pfad, timeout=5)
    except urllib.error.HTTPError as e:
        with e:
            assert e.code == 404, (pfad, e.code)
    else:
        raise SystemExit(pfad + ' war von aussen erreichbar')
\""

pruefe "ohne Token auch ueber den Proxy kein Bild" im_container "
python -c \"
import urllib.error, urllib.request
try:
    urllib.request.urlopen('http://proxy/cam/tuer.jpg', timeout=5)
except urllib.error.HTTPError as e:
    with e:
        assert e.code == 401, e.code
else:
    raise SystemExit('Zugriff war ohne Token moeglich')
\""

pruefe "Docker-Healthcheck steht auf healthy" bash -c "
zustand=\$(docker inspect --format '{{.State.Health.Status}}' \
    \$(${COMPOSE[*]} ps -q rtsp2jpeg))
[[ \"\$zustand\" == healthy ]] || { echo \"Zustand: \$zustand\"; exit 1; }"

pruefe "Dienst laeuft nicht als root" im_container "
test \"\$(id -u)\" != 0"

pruefe "kein Passwort in den Protokollen" bash -c "
! ${COMPOSE[*]} logs rtsp2jpeg 2>&1 | grep -i 'pruef-token'"

echo
echo "--- Kennzahlen (nur zur Information) ---"
"${COMPOSE[@]}" restart rtsp2jpeg >/dev/null 2>&1 || true
sleep 3
im_container "
python -c \"
import time, urllib.request
beginn = time.monotonic()
with urllib.request.urlopen(
        'http://127.0.0.1:8080/cam/tuer.jpg?token=$TOKEN&timeout=30', timeout=35) as r:
    groesse = len(r.read())
print('  Erstes Bild nach Neustart: %.2f s (%d Bytes)'
      % (time.monotonic() - beginn, groesse))

beginn = time.monotonic()
for _ in range(10):
    with urllib.request.urlopen(
            'http://127.0.0.1:8080/cam/tuer.jpg?token=$TOKEN', timeout=10) as r:
        r.read()
print('  Zehn weitere Abrufe: %.0f ms im Mittel'
      % ((time.monotonic() - beginn) * 100))
\"" 2>/dev/null || echo "  (Messung nicht moeglich)"

echo
if [[ $fehler -eq 0 ]]; then
    echo "Alle Pruefungen bestanden."
else
    echo "$fehler Pruefung(en) fehlgeschlagen."
fi
exit "$fehler"
