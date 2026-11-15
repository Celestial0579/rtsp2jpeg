# Änderungen

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
