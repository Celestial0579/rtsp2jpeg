# rtsp2jpeg

Macht aus einer RTSP-Kamera ein einzelnes JPEG, das per HTTP abrufbar ist —
gedacht für das Feld **Kamera-URL** einer STARFACE-Türsprechstelle.

## Wozu das gut ist

STARFACE zeigt beim Klingeln ein Bild der Türsprechstelle an. Dafür trägt man
in den erweiterten Einstellungen des Telefonkontos eine **Kamera-URL** ein, die
die Anlage beim Gespräch abruft. Zwei Eigenheiten machen das mit gewöhnlichen
Kameras schwierig:

1. **Nur `.jpg` und `.jpeg` werden unterstützt.** Ein RTSP- oder sonstiger
   Videostrom führt lediglich zu einem schwarzen Bild.
2. **Die Anlage holt das Bild selbst ab.** Die Verbindung läuft
   Kamera → STARFACE → Callmanager/App. Bei einer Cloud-Anlage muss die
   Kamera-URL deshalb aus dem Internet erreichbar sein.

Die meisten günstigen IP-Kameras und Türstationen liefern aber nur RTSP.
rtsp2jpeg schließt genau diese Lücke: es hält die RTSP-Verbindung, wandelt
laufend Einzelbilder um und gibt bei jedem Abruf das aktuelle Bild als JPEG
heraus.

```
Kamera ──RTSP──> rtsp2jpeg ──HTTP/JPEG──> STARFACE ──> Callmanager / App
```

## Schnellstart

Eine einzelne Kamera, ohne Konfigurationsdatei:

Zuerst ein Zugangstoken erzeugen — ohne startet der Dienst nicht:

```bash
docker run --rm ghcr.io/celestial0579/rtsp2jpeg:latest --token-erzeugen
```

Das gibt das Token aus und dazu die fertige URL, die in der STARFACE
eingetragen wird. Dann den Dienst starten:

```bash
docker run -d --name rtsp2jpeg -p 8080:8080 \
  -e CAMERA_URL="rtsp://benutzer:passwort@192.168.1.50:554/stream1" \
  -e CAMERA_NAME="tuer" \
  -e TOKEN="DAS_ERZEUGTE_TOKEN" \
  ghcr.io/celestial0579/rtsp2jpeg:latest
```

Danach liefert `http://<host>:8080/cam/tuer.jpg?token=…` das aktuelle Bild.
Zum Ausprobieren im Browser öffnen — es muss ein Kamerabild erscheinen.

Für mehrere Kameras oder feinere Einstellungen:

```bash
cp config.example.yaml config.yaml   # anpassen
docker compose up -d
```

## Einrichtung in der STARFACE

1. Telefonkonto der Türsprechstelle öffnen, Reiter **Erweiterte
   Einstellungen**.
2. Als **Kamera-URL** eintragen:

   ```
   https://tuerkamera.example.de/cam/tuer.jpg?token=DEIN_TOKEN
   ```

3. Speichern und einmal klingeln lassen.

Die URL endet bewusst auf `.jpg` — STARFACE prüft das Format, und ein Pfad
ohne diese Endung ist eine häufige Fehlerquelle.

### Der Login muss in die URL

STARFACE bietet nur ein einziges URL-Feld, kein getrenntes Feld für Benutzer
und Passwort. Deshalb gibt es zwei Wege, und beide funktionieren:

| Weg | URL | Hinweis |
|---|---|---|
| Token (empfohlen) | `https://host/cam/tuer.jpg?token=…` | funktioniert mit jedem Client |
| Benutzer/Passwort | `https://benutzer:passwort@host/cam/tuer.jpg` | nur, wenn der Client den Vorspann auswertet |

Das Token ist der verlässlichere Weg: den Vorspann `benutzer:passwort@` wertet
nicht jeder HTTP-Client aus, und manche Java-Clients ignorieren ihn
stillschweigend. Falls das Bild bei Variante 2 leer bleibt, ist das die erste
Vermutung — dann auf das Token wechseln.

Token erzeugen — gibt gleich die fertige Kamera-URL zum Kopieren mit aus:

```bash
docker run --rm ghcr.io/celestial0579/rtsp2jpeg:latest --token-erzeugen --host tuerkamera.example.de --kamera tuer
```

### Ohne Zugangsschutz startet der Dienst nicht

Das ist Absicht. Eine Warnzeile im Protokoll übersieht man, und das Bild der
eigenen Haustür gehört nicht ins offene Netz. Fehlen `token` und `basic_auth`,
bricht der Start mit einer Anleitung ab und Exit-Code 3.

Soll der Dienst wirklich ohne Schutz laufen — etwa in einem abgeschotteten
Netz, in dem er ohnehin niemand erreicht —, muss das ausdrücklich dastehen:

```bash
-e ALLOW_ANONYMOUS=1        # bzw. server.allow_anonymous: true
```

Auch dann weist das Protokoll bei jedem Start darauf hin.

## Aus dem Internet erreichbar machen

Nötig bei einer **STARFACE-Cloud-Anlage** — die Anlage steht dann nicht im
eigenen Netz und kommt sonst nicht an die Kamera heran. Bei einer Anlage im
eigenen Haus genügt der Zugriff im LAN, und dieser Abschnitt entfällt.

Unter [`beispiele/internet/`](beispiele/internet/) liegt ein fertiger Aufbau
mit Caddy als vorgelagertem Webserver: der besorgt selbsttätig ein
Let's-Encrypt-Zertifikat, und rtsp2jpeg selbst bekommt gar keinen offenen Port.

```bash
cd beispiele/internet
$EDITOR Caddyfile                                # Name und E-Mail eintragen
echo "RTSP2JPEG_TOKEN=$(openssl rand -hex 24)" > .env
docker compose up -d
```

Dabei gilt:

- **Nur `/cam/*` wird nach außen gereicht.** Die Status- und Übersichtsseiten
  bleiben innen, sie werden von der Anlage nicht gebraucht.
- **Immer HTTPS.** Ohne TLS steht das Token bei jedem Abruf im Klartext im
  Netz.
- **`trust_proxy` nur mit echtem Proxy davor.** Andernfalls könnte jeder seine
  Herkunft per `X-Forwarded-For` frei erfinden und damit die Bremse gegen das
  Durchprobieren von Token umgehen.
- Wiederholte Fehlversuche werden verzögert beantwortet (ansteigend bis zwei
  Sekunden je Anfrage), damit ein Token nicht durchprobiert werden kann.

## Endpunkte

| Pfad | Zweck |
|---|---|
| `/cam/<name>.jpg` | aktuelles Einzelbild — **das ist die Kamera-URL** |
| `/cam/<name>.mjpg` | fortlaufender MJPEG-Strom (Browser, andere Systeme; **nicht** für STARFACE) |
| `/healthz` | Zustand als JSON, ohne Token erreichbar |
| `/status` | ausführlicher Zustand je Kamera, inkl. der letzten ffmpeg-Meldungen |
| `/` | Übersichtsseite mit allen Kameras |

Am Einzelbild-Endpunkt lassen sich Werte pro Abruf überschreiben:

| Parameter | Bedeutung |
|---|---|
| `max_age=2` | Bild darf höchstens 2 s alt sein |
| `timeout=5` | höchstens 5 s auf ein Bild warten |
| `stale=1` | lieber ein altes Bild liefern als einen Fehler |

`stale=1` ist nützlich, wenn im Callmanager statt eines Fehlersymbols besser
das letzte bekannte Bild erscheinen soll.

## Konfiguration

Alles Weitere steht kommentiert in
[`config.example.yaml`](config.example.yaml). Die wichtigsten Werte:

| Einstellung | Vorgabe | Bedeutung |
|---|---|---|
| `url` | — | RTSP-Adresse der Kamera (Pflicht) |
| `fps` | 2 | Bilder pro Sekunde, die ffmpeg holt |
| `width` / `height` | — | herunterskalieren; fehlt eine Angabe, folgt sie dem Seitenverhältnis |
| `quality` | 5 | JPEG-Güte, 2 = beste, 31 = schlechteste |
| `rotate` | 0 | 0, 90, 180 oder 270 Grad |
| `transport` | tcp | `tcp` ist bei Türsprechstellen deutlich stabiler als `udp` |
| `idle_timeout` | 60 | Sekunden ohne Abruf, bis ffmpeg einschläft; `0` = dauerhaft |
| `max_age` | 5.0 | ab wann ein Bild als abgestanden gilt |
| `timeout` | 10.0 | wie lange eine Anfrage auf ein Bild wartet |
| `read_timeout` | 10.0 | wie lange ffmpeg auf Daten der Kamera wartet |

Auf der Serverseite:

| Einstellung | Vorgabe | Bedeutung |
|---|---|---|
| `token` | — | Zugangstoken für `?token=…` |
| `basic_auth` | — | `benutzer:passwort` für den URL-Vorspann |
| `allow_anonymous` | false | Betrieb ganz ohne Zugangsschutz zulassen |
| `protect_health` | false | auch `/healthz` hinter den Zugangsschutz stellen |
| `trust_proxy` | false | `X-Forwarded-For` auswerten — nur mit echtem Proxy davor |

Passwörter gehören nicht in die Datei — `${VARIABLE}` wird aus der Umgebung
ersetzt, `${VARIABLE:-vorgabe}` mit Rückfallwert.

### `idle_timeout` bei einer Türklingel auf 0 setzen

Gemessen im Integrationstest dieses Projekts:

| Zustand | Zeit bis zum Bild |
|---|---|
| ffmpeg schläft (`idle_timeout > 0`) | ~2,6 s |
| ffmpeg verbunden (`idle_timeout: 0`) | ~1 ms |

Wenn es klingelt, soll das Bild sofort da sein — deshalb ist `idle_timeout: 0`
für eine Türsprechstelle die richtige Wahl. Der Preis ist eine dauerhafte
Verbindung zur Kamera und etwas Rechenzeit. Für eine Kamera, die nur
gelegentlich angeschaut wird, lohnt der Schlafmodus dagegen.

## Betrieb

Bei Störungen zeigt `/status` je Kamera den Zustand und die letzten
ffmpeg-Meldungen — dort steht in aller Regel schon die Ursache:

```bash
curl -s http://localhost:8080/status | jq
```

Häufige Fälle:

| Meldung | Ursache |
|---|---|
| `401 Unauthorized` (von der Kamera) | Benutzer oder Passwort in der RTSP-URL falsch |
| `Connection refused` | falscher Port oder falscher Pfad in der RTSP-URL |
| `Immediate exit requested` / Abbrüche bei `udp` | auf `transport: tcp` umstellen |
| Bild bleibt in STARFACE schwarz | URL endet nicht auf `.jpg`, oder die Anlage kommt nicht an den Dienst heran |

Kameraadressen sind je Hersteller verschieden; der Pfad steht im Handbuch der
Kamera. Zum Ausprobieren:

```bash
ffprobe "rtsp://benutzer:passwort@192.168.1.50:554/stream1"
```

Passwörter tauchen weder in den Protokollen noch unter `/status` auf; das
Token wird im Zugriffsprotokoll durch `token=***` ersetzt.

## Entwicklung

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest        # Unittests, brauchen kein ffmpeg
.venv/bin/ruff check rtsp2jpeg tests
```

Die Unittests kommen ohne Kamera und ohne ffmpeg aus — ein Ersatzprogramm
liefert stattdessen einen MJPEG-Strom.

Der Integrationstest braucht Docker und stellt sich seine Kamera selbst hin
(mediamtx als RTSP-Server, ein ffmpeg-Testbild als Klingel, Caddy als
vorgelagerter Webserver). Eine echte Kamera oder Telefonanlage ist dafür nicht
nötig:

```bash
scripts/integrationstest.sh
```

## Was ungetestet ist

Alles bis zur HTTP-Schnittstelle ist automatisch geprüft, einschließlich des
Wegs durch einen vorgelagerten Webserver. Nicht geprüft ist naturgemäß das
Zusammenspiel mit **echter Hardware**: eine konkrete Türsprechstelle und eine
STARFACE-Anlage. Beides braucht Geräte, die in keiner Testumgebung stehen.

## Lizenz

[MIT](LICENSE)
