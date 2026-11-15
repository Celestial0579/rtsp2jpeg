# rtsp2jpeg

Macht aus einer RTSP-Kamera ein einzelnes JPEG, das per HTTP abrufbar ist —
gedacht für das Feld **Kamera-URL** einer STARFACE-Türsprechstelle.

Der Dienst kann genau eines: auf `GET /cam/<name>.jpg` das aktuelle Bild
ausliefern. Status-, Übersichts- und Stromseiten gibt es nicht — so bleibt
die Angriffsfläche so klein wie möglich.

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

Der Knopf **Verbindung testen** ruft die URL im Browser des Admin-Rechners ab,
nicht in der Anlage. Ob die STARFACE selbst an den Dienst herankommt, zeigt
erst ein Anruf. Beim Klingeln wartet die Anlage höchstens 5 s auf jedes Bild;
deshalb steht `timeout` auf 3 s.

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

Ebenso, wenn der Zugang zu schwach ist: ein Token unter 24 Zeichen, ein
Passwort in `basic_auth` unter 16 Zeichen oder Leerraum am Anfang oder Ende
(meist ein Zeilenende aus einer `.env`-Datei).

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

- **Nur `/cam/*` wird nach außen gereicht.** Der Dienst selbst kennt
  ohnehin nichts anderes; Caddy grenzt zusätzlich ab.
- **Immer HTTPS.** Ohne TLS steht das Token bei jedem Abruf im Klartext im
  Netz.
- **`trust_proxy` nennt die Adresse des Proxys**, nicht bloß `true`.
  `X-Forwarded-For` zählt nur bei Verbindungen von dort, und zwar der letzte
  Eintrag — den hängt der eigene Proxy an. Wer am Proxy vorbei direkt kommt,
  zählt mit seiner echten Adresse. Das Beispiel gibt Caddy dafür eine feste
  Adresse.
- **`allow_from` schließt alle anderen aus.** Im Beispiel darf nur Caddy
  verbinden; im LAN genügt die Adresse der STARFACE. Andere Verbindungen
  schließt der Dienst, bevor er etwas liest.
- **Kein Zugriffsprotokoll mit Abfrageteil.** Das Token steht in der URL.
  Das Caddy-Beispiel schreibt deshalb kein Zugriffsprotokoll; jeder andere
  vorgelagerte Proxy muss die URL ebenso behandeln.
- Nach einem Fehlversuch ist die Gegenstelle für eine kurze Frist gesperrt
  (ansteigend bis zwei Sekunden): Versuche in dieser Zeit prüft der Dienst
  nicht, sondern weist sie sofort ab, auch wenn sie gleichzeitig eintreffen.

## Schnittstelle

| Anfrage | Antwort |
|---|---|
| `GET /cam/<name>.jpg` | aktuelles Einzelbild — **das ist die Kamera-URL** |
| alles andere | leere Fehlerantwort (`401`, `404`, `405` oder `400`) |

- Jede Verbindung trägt genau eine Anfrage und wird danach geschlossen.
- Nur `GET`, ohne Rumpf. Andere Methoden und Anfragen mit Rumpf weist der
  Dienst ab.
- Fehlerantworten haben keinen Inhalt — auch keine Meldung der Kamera.
- Feste Grenzen: 2 s bis Anfragezeile und Kopfzeilen vollständig sind,
  5 s je Lese- oder Schreibvorgang, eine Höchstdauer je Verbindung, 8 KiB
  für Anfragezeile und Kopfzeilen, höchstens 32 gleichzeitige Verbindungen
  und davon 8 je Adresse (der Proxy aus `trust_proxy` ausgenommen).
- Liefert die Kamera innerhalb von `timeout` kein Bild, antwortet der Dienst
  mit einem leeren `503`. Ist ffmpeg bereits ohne Bild ausgestiegen (Kamera
  aus oder nicht erreichbar), kommt das `503` sofort.

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
| `timeout` | 3.0 | wie lange eine Anfrage auf ein Bild wartet (höchstens 30); die STARFACE wartet höchstens 5 s |
| `read_timeout` | 10.0 | wie lange ffmpeg auf Daten der Kamera wartet |

Auf der Serverseite:

| Einstellung | Vorgabe | Bedeutung |
|---|---|---|
| `token` | — | Zugangstoken für `?token=…` |
| `basic_auth` | — | `benutzer:passwort` für den URL-Vorspann |
| `allow_anonymous` | false | Betrieb ganz ohne Zugangsschutz zulassen |
| `trust_proxy` | — | Adresse(n) des eigenen Proxys; nur von dort zählt `X-Forwarded-For` |
| `allow_from` | — | nur diese Adressen oder Netze bedienen, alle anderen sofort trennen |
| `access_log` | true | jeden Abruf protokollieren (ohne Abfrageteil der URL) |

`trust_proxy` und `allow_from` nehmen IPv4-Adressen oder Netze, in der Datei
als Liste, in der Umgebung (`TRUST_PROXY`, `ALLOW_FROM`) durch Kommas
getrennt, etwa `172.29.80.200` oder `10.0.0.0/24`.

Schalter wie `allow_anonymous` müssen echte Wahrheitswerte sein: `true` oder
`false` ohne Anführungszeichen. Text wie `"false"` — auch aus einer
`${VARIABLE}` — lehnt der Dienst ab und startet nicht. In der Umgebung gelten
`1`/`true`/`yes` und `0`/`false`/`no`; alles andere verhindert ebenfalls den
Start.

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

Bei Störungen steht die Ursache im Protokoll des Containers; mit
`LOG_LEVEL=DEBUG` erscheinen auch die Meldungen von ffmpeg:

```bash
docker logs rtsp2jpeg
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

Kamera-Passwörter werden in allen Protokollzeilen geschwärzt, auch in
Meldungen von ffmpeg. Das Zugriffsprotokoll enthält nur den Pfad, nie den
Abfrageteil mit dem Token. Der Docker-Healthcheck prüft nur, ob der Dienst
Verbindungen annimmt.

## Entwicklung

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest        # Unittests, brauchen kein ffmpeg
.venv/bin/ruff check rtsp2jpeg tests
```

Die Unittests kommen ohne Kamera und ohne ffmpeg aus — ein Ersatzprogramm
liefert stattdessen die Bilder.

Der Integrationstest braucht Docker und stellt sich seine Kamera selbst hin
(mediamtx als RTSP-Server, ein ffmpeg-Testbild als Klingel, Caddy als
vorgelagerter Webserver). Eine echte Kamera oder Telefonanlage ist dafür nicht
nötig:

```bash
scripts/integrationstest.sh
```

Dasselbe läuft als GitHub-CI. Auf `main` veröffentlicht sie das Abbild als
`ghcr.io/celestial0579/rtsp2jpeg:latest`, ein Release-Tag `v1.2.3` zusätzlich
als `:1.2.3`; Pull Requests bauen es nur. Montags wird das Abbild neu gebaut,
damit Basisabbild und ffmpeg aktuell bleiben.

## Was ungetestet ist

Alles bis zur HTTP-Schnittstelle ist automatisch geprüft, einschließlich des
Wegs durch einen vorgelagerten Webserver. Nicht geprüft ist naturgemäß das
Zusammenspiel mit **echter Hardware**: eine konkrete Türsprechstelle und eine
STARFACE-Anlage. Beides braucht Geräte, die in keiner Testumgebung stehen.

## Lizenz

[MIT](LICENSE)
