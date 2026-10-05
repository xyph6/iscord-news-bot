# Discord-News-Bot: Aion 2 + League of Legends

Postet neue offizielle News zu **Aion 2** und **League of Legends** automatisch in einen Discord-Channel.

## Was der Bot macht
- Prüft alle 10 Minuten diese Quellen (einstellbar in `sources.json`):
  - **Aion 2:** offizieller News-Feed auf Steam (Patchnotes, Wartungen, Events, Ankündigungen von NC)
  - **League of Legends:** offizielle News-Seite leagueoflegends.com/de-de/news (Patchnotes, Dev-Updates, Events)
  - Bewusst **nur offizielle Quellen** von NCSoft und Riot, keine Newsseiten von Dritten.
- Postet jeden neuen Eintrag als Embed (Titel, Link, Kurztext, Bild, farbig je Spiel).
- Merkt sich in `state.json`, was schon gepostet wurde, damit nichts doppelt kommt.
- Beim allerersten Lauf postet er pro Quelle nur die neueste News (kein Archiv-Spam).

## Einrichtung (ca. 5 Minuten, kostenlos über GitHub Actions)

### 1. Webhook in Discord anlegen
Server-Einstellungen → **Integrationen** → **Webhooks** → **Neuer Webhook** → Namen (z. B. „News-Bot“) und deinen **News-Channel** wählen → **Webhook-URL kopieren**.
Die URL ist wie ein Passwort: nirgends öffentlich posten.

### 2. Repository auf GitHub anlegen
1. Auf github.com → **New repository**, z. B. `discord-news-bot`, **Private** wählen, erstellen.
2. **Add file → Upload files** und diese Dateien hochladen: `news_bot.py`, `sources.json`, `README.md`, `.gitignore`.
3. Die Workflow-Datei anlegen: **Add file → Create new file**, als Namen `.github/workflows/news.yml` eintippen und den Inhalt der Datei `news.yml` hineinkopieren. Commit.

### 3. Webhook als Secret hinterlegen
Im Repo: **Settings → Secrets and variables → Actions → New repository secret**
- Name: `DISCORD_WEBHOOK_URL`
- Wert: die kopierte Webhook-URL

### 4. Starten
**Actions**-Tab → Workflow „Discord News“ → **Run workflow**. Nach ca. 1 Minute sollten die ersten zwei Posts (je Spiel die neueste News) im Channel sein. Danach läuft er automatisch alle 10 Minuten.

## Alternative: auf eigenem PC / Raspberry Pi / Server
```bash
export DISCORD_WEBHOOK_URL="..."   # Windows PowerShell: $env:DISCORD_WEBHOOK_URL="..."
python3 news_bot.py --loop 300     # prüft alle 5 Minuten, läuft dauerhaft
```
Testen ohne zu posten: `python3 news_bot.py --dry-run`

## Gut zu wissen
- GitHub startet geplante Läufe nicht sekundengenau, oft mit ein paar Minuten Verzögerung. Für echtes „sofort“ die Loop-Variante auf einem eigenen Rechner nutzen.
- GitHub pausiert geplante Workflows, wenn ein Repo 60 Tage keine Aktivität hat. Da der Bot bei jeder neuen News `state.json` committet, passiert das praktisch nicht; falls doch, im Actions-Tab wieder aktivieren.
- Weitere Quelle hinzufügen: neuen Block in `sources.json` (Typ `rss` für jeden RSS/Atom-Feed). Nur offizielle Kanäle eintragen.
- Ändert Riot den Aufbau der LoL-Seite, meldet der Bot im Actions-Log „keine Einträge gefunden“.
