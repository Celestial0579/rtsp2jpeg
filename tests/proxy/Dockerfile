# Der Testaufbau backt den Caddyfile ins Abbild, statt ihn einzuhaengen.
#
# Grund: Ein Bind-Mount wird vom Docker-Daemon aufgeloest, nicht von dem,
# der den Befehl absetzt. Laeuft der Test in einem Container, der nur den
# Socket des Wirts benutzt (CI, Docker-im-Docker), sucht der Wirt den Pfad
# in SEINEM Dateisystem -- dort gibt es ihn nicht, und er legt stillschweigend
# ein leeres VERZEICHNIS an. Caddy bekommt dann ein Verzeichnis statt seiner
# Konfiguration: "not a directory".
#
# Ein Bauauftrag schickt seinen Kontext als Datenstrom durch den Socket und
# kennt dieses Problem nicht -- innen wie aussen, hier wie auf GitHub.
FROM caddy:2-alpine
COPY Caddyfile /etc/caddy/Caddyfile
