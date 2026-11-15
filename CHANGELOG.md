# Änderungen

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
