# Discord-News-Bot: Aion 2 + League of Legends

Postet neue offizielle News zu **Aion 2** und **League of Legends** automatisch in einen Discord-Channel.

## Was der Bot macht
- Prüft alle 10 Minuten diese Quellen (einstellbar in `sources.json`):
  - **Aion 2:** offizieller News-Feed auf Steam (Patchnotes, Wartungen, Events, Ankündigungen von NC)
  - **Aion 2 Korea-Vorschau:** offizielle koreanische Update-Notes (Korea ist der globalen Version voraus), automatisch ins Deutsche übersetzt
  - **League of Legends:** offizielle News-Seite leagueoflegends.com/de-de/news (Patchnotes, Dev-Updates, Events)
  - Bewusst **nur offizielle Quellen** von NCSoft und Riot, keine Newsseiten von Dritten.
- Postet jeden neuen Eintrag als Embed (Titel, Link, Kurztext, Bild, farbig je Spiel).
- Merkt sich in `state.json`, was schon gepostet wurde, damit nichts doppelt kommt.
- Beim allerersten Lauf postet er pro Quelle nur die neueste News (kein Archiv-Spam).

## Einrichtung (ca. 5 Minuten, kostenlos über GitHub Actions)

### 1. Webhooks in Discord anlegen (einer pro Spiel)
Für jeden Channel: Rechtsklick auf den Channel → **Kanal bearbeiten** → **Integrationen** → **Webhooks** → **Neuer Webhook** → **Webhook-URL kopieren**.
Mach das einmal im Aion-2-Channel und einmal im LoL-Channel. Die URLs sind wie Passwörter: nirgends öffentlich posten.

### 2. Repository auf GitHub anlegen
Privates Repo anlegen und `news_bot.py`, `sources.json`, `README.md`, `.gitignore` und `.github/workflows/news.yml` hochladen.

### 3. Webhooks als Secrets hinterlegen
Im Repo: **Settings → Secrets and variables → Actions → New repository secret**
- `DISCORD_WEBHOOK_AION2` = Webhook-URL des Aion-2-Channels
- `DISCORD_WEBHOOK_LOL` = Webhook-URL des LoL-Channels (alternativ wird `DISCORD_WEBHOOK_URL` genutzt)

Fehlt ein Secret, wird das jeweilige Spiel übersprungen, statt in den falschen Channel zu posten.

### 4. Starten
**Actions**-Tab → Workflow „Discord News“ → **Run workflow**. Nach ca. 1 Minute sollten die ersten zwei Posts (je Spiel die neueste News) im Channel sein. Danach läuft er automatisch alle 10 Minuten.

## Alternative: auf eigenem PC / Raspberry Pi / Server
```bash
export DISCORD_WEBHOOK_AION2="..." DISCORD_WEBHOOK_LOL="..."
python3 news_bot.py --loop 300     # prüft alle 5 Minuten, läuft dauerhaft
```
Testen ohne zu posten: `python3 news_bot.py --dry-run`

## Gut zu wissen
- GitHub startet geplante Läufe nicht sekundengenau, oft mit ein paar Minuten Verzögerung. Für echtes „sofort“ die Loop-Variante auf einem eigenen Rechner nutzen.
- GitHub pausiert geplante Workflows, wenn ein Repo 60 Tage keine Aktivität hat. Da der Bot bei jeder neuen News `state.json` committet, passiert das praktisch nicht; falls doch, im Actions-Tab wieder aktivieren.
- **Inhalt statt Link:** Der Bot postet den Text der Meldung (Überschriften, Aufzählungen, Tabellen) direkt in Discord, bei sehr langen Meldungen (z. B. Patchnotes) die ersten ca. 3.800 Zeichen.
- **Filter in `sources.json`:** `include_domains` (nur diese Seiten), `exclude_domains`, `include_title` / `exclude_title` (Regex auf den Titel). Für LoL kommen nur Artikel von leagueoflegends.com (keine Videos, Merch, Esports); für Aion 2 werden Dankes-, Twitch-Drops-, Gewinnspiel- und ähnliche Posts ignoriert.
- Weitere Quelle hinzufügen: neuen Block in `sources.json` (Typ `rss` für jeden RSS/Atom-Feed). Nur offizielle Kanäle eintragen.
- Ändert Riot den Aufbau der LoL-Seite, meldet der Bot im Actions-Log „keine Einträge gefunden“.
