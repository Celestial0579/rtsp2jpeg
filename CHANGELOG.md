# Änderungen

## 0.2.3

### Kann den Start verhindern

Diese Angaben ließ der Dienst bisher durchgehen; jetzt bricht er den Start
mit einer Meldung ab:

- `allow_from` / `ALLOW_FROM` gesetzt, aber leer (`""`, `[]`, ohne Wert).
  Wer keine Einschränkung will, lässt die Einstellung weg.
- Token mit anderen Zeichen als `A–Z a–z 0–9 - _ . ~`; solche Zeichen kämen
  in `?token=` verändert an. Ein neues Token liefert `--token-erzeugen`.
- `nan` oder `inf` in Kamera-Zahlenwerten (`fps`, `idle_timeout`, `max_age`,
  `timeout`, `read_timeout`).

### Sicherheit

- Kamera-Zugangsdaten werden auch geschwärzt, wenn sie nicht in
  `benutzer:passwort@` stehen: alle Werte nach `=` in Pfad und Abfrageteil
  der Kamera-URL sowie die eigenen Argumente aus `input_args`/`output_args`,
  in allen Protokollzeilen, auch auf DEBUG und in Meldungen von ffmpeg.
  Wer Zugangsdaten so angegeben hat: alte Protokolle löschen und das
  Kamera-Passwort wechseln.
- Internet-Beispiel: Caddy ersetzt das Token in seinen eigenen Protokollen,
  auch in Fehlereinträgen. Wer das Beispiel nutzt: den neuen `Caddyfile`
  übernehmen, den Caddy-Container neu anlegen und das Token wechseln.

### Geändert

- Internet-Beispiel: Caddy läuft eingeschränkt wie rtsp2jpeg (`read_only`,
  `cap_drop: ALL` außer `NET_BIND_SERVICE`, `no-new-privileges`).
- Das Internet-Beispiel nutzt das Image `:0.2.3`.

## 0.2.2

### Geändert

- Ist die Kamera aus oder nicht erreichbar und ffmpeg ohne Bild
  ausgestiegen, antwortet der Dienst sofort mit `503`, statt bis `timeout`
  zu warten.
- Vorgabe für `timeout` von 10 auf 3 Sekunden, weil die STARFACE höchstens
  5 Sekunden auf ein Bild wartet. Wer bisher auf die Vorgabe gesetzt hat und
  länger warten will, trägt `timeout` selbst ein.
- Das Image läuft auf Python 3.15; die Unittests laufen zusätzlich unter 3.15.
- README: Der Knopf „Verbindung testen“ in der STARFACE prüft nur den
  Browser des Admin-Rechners, nicht die Anlage.
- Das Internet-Beispiel nutzt das Image `:0.2.2`.

## 0.2.1

> **Achtung, Bruch bei `TRUST_PROXY` / `trust_proxy`:** Die Einstellung
> erwartet jetzt die Adresse des eigenen Proxys (IPv4-Adresse oder Netz,
> mehrere durch Komma getrennt), zum Beispiel `TRUST_PROXY=172.29.80.200`.
> `TRUST_PROXY=1` bzw. `trust_proxy: true` startet nicht mehr. Wer
> rtsp2jpeg hinter einem Proxy betreibt, muss das vor dem Update anpassen.

### Ebenfalls Bruch

- Token unter 24 Zeichen und Basic-Passwörter unter 16 Zeichen verhindern den
  Start; ebenso Leerzeichen am Anfang oder Ende. Ein neues Token liefert
  `--token-erzeugen`.

### Sicherheit

- Das Internet-Beispiel mit Caddy schreibt kein Zugriffsprotokoll mehr. Wer
  das alte Beispiel genutzt hat: die Protokolldatei samt Sicherungen löschen
  und das Token wechseln.

### Neu

- `allow_from` / `ALLOW_FROM`: Verbindungen nur aus diesen IPv4-Netzen.
- Höchstens 8 gleichzeitige Verbindungen je Gegenstelle (der eigene Proxy
  ausgenommen).
- Kopfzeilen müssen innerhalb von 2 Sekunden vollständig sein.

### Geändert

- Während der Sperre nach einem Fehlversuch werden weitere Versuche sofort
  abgewiesen.
- Listen in der Kamera-Konfiguration müssen Listen von Texten sein.
- Actions, Basis-Images und Python-Abhängigkeiten sind auf feste Prüfsummen
  festgelegt; Dependabot hält sie aktuell.
- Das Internet-Beispiel nutzt das Image mit fester Version (`:0.2.1`).
- Feste Proxy-Adresse in den Compose-Dateien außerhalb der automatischen
  Vergabe.

## 0.2.0

Der Dienst liefert nur noch das Kamerabild. Alles andere ist entfallen.

### Entfernt

- `/healthz`, `/health`, `/status`, die Übersichtsseite `/` und der
  MJPEG-Strom `/cam/<name>.mjpg`.
- `HEAD` und alle anderen Methoden außer `GET`.
- Die Abfrageparameter `max_age`, `timeout` und `stale`; es gelten die Werte
  aus der Konfiguration.
- Token als `Authorization: Bearer …` oder `X-Auth-Token`; das Token steht
  in der URL (`?token=…`), alternativ Basic-Anmeldung.
- Die Einstellung `protect_health`. Steht sie noch in einer Datei, wird sie
  ignoriert.
- Die Kopfzeile `X-Frame-Age`.

### Geändert

- Fehlerantworten sind leer, ohne Meldungstext.
- Eine Anfrage je Verbindung, danach wird sie geschlossen. Anfragen mit
  Rumpf werden abgewiesen.
- Feste Grenzen für Lesedauer, Verbindungsdauer, Kopfgröße und gleichzeitige
  Verbindungen.
- Die Fehlversuchsbremse gilt auch für gleichzeitige Versuche.
- Zugangsdaten werden in allen Protokollzeilen geschwärzt; das
  Zugriffsprotokoll enthält keinen Abfrageteil mehr.
- Schalter in der Konfiguration (`allow_anonymous`, `trust_proxy`,
  `access_log`) müssen echte Wahrheitswerte sein, sonst startet der Dienst
  nicht. In der Umgebung gelten nur eindeutige Werte.
- Hinter einem Proxy zählt der letzte Eintrag in `X-Forwarded-For`.
- Der Docker-Healthcheck prüft nur noch, ob der Dienst Verbindungen annimmt.
- Kamera-`timeout` höchstens 30 Sekunden.

## 0.1.0

Erste Fassung.
