#!/usr/bin/env python3
"""Discord-News-Bot: postet neue News zu Aion 2 und League of Legends per Webhook.

Nur Python-Standardbibliothek. Konfiguration:
  DISCORD_WEBHOOK_URL  Standard-Webhook (als Secret/Umgebungsvariable, nie im Code)
                       Eine Quelle kann mit "webhook_env" einen eigenen Webhook (eigenen Channel) nutzen.
  sources.json         Liste der Quellen (neben diesem Skript)
  state.json           Bereits gepostete Einträge (wird automatisch gepflegt)

Aufruf:
  python news_bot.py                 einmal prüfen und posten (für GitHub Actions / cron)
  python news_bot.py --loop 300      dauerhaft laufen, alle 300 Sekunden prüfen
  python news_bot.py --dry-run       nichts posten, nur anzeigen
"""
import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

HERE = Path(__file__).resolve().parent
SOURCES_FILE = Path(os.environ.get("SOURCES_FILE", HERE / "sources.json"))
STATE_FILE = Path(os.environ.get("STATE_FILE", HERE / "state.json"))
USER_AGENT = "Mozilla/5.0 (compatible; DiscordNewsBot/1.0)"
MAX_SEEN_PER_SOURCE = 300
MAX_POSTS_PER_RUN = 10  # Schutz gegen Spam, falls eine Quelle plötzlich alles neu liefert


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def clean_text(text, limit=300):
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    text = re.sub(r"\[/?[a-z0-9*]+[^\]]*\]", " ", text)  # BBCode aus Steam-News
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + " …"


def html_to_discord(text, limit=None):
    """Wandelt HTML/BBCode in lesbaren Discord-Text (fett, Aufzählungen, Absätze) um."""
    t = text or ""
    # BBCode (Steam) auf HTML abbilden
    t = re.sub(r"\[(/?)(h[1-6]|b|i|u|p|list|olist|table|tr|td|th)\]", r"<\1\2>", t)
    t = re.sub(r"\[\*\]", "<li>", t)
    t = re.sub(r"\[img\][^\[]*\[/img\]", "", t)
    t = re.sub(r"\[url=[^\]]*\](.*?)\[/url\]", r"\1", t, flags=re.S)
    t = re.sub(r"\[/?[a-z0-9*]+[^\]]*\]", "", t)
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", t)
    t = re.sub(r"(?is)<(strong|b|h[1-6])(\s[^>]*)?>(\s|&nbsp;|<br\s*/?>)*</\1>", " ", t)  # leere Fett-/Überschrift-Tags
    t = re.sub(r"(?i)<h[1-6][^>]*>\s*", "\n\n**", t)
    t = re.sub(r"(?i)\s*</h[1-6]>", "**\n", t)
    t = re.sub(r"(?i)</?(strong|b)(\s[^>]*)?>", "**", t)
    t = re.sub(r"(?i)<li(\s[^>]*)?>\s*", "\n• ", t)
    t = re.sub(r"(?i)</t[dh]>\s*<t[dh](\s[^>]*)?>", " | ", t)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|ul|ol|table|list|olist)>", "\n", t)
    t = re.sub(r"(?i)<(p|div|ul|ol|table)(\s[^>]*)?>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ").replace("\u200b", "")
    t = re.sub(r"\*\*([ \t]*)\*\*(?=\S)", r"\1", t)  # direkt aneinanderstoßende Fett-Blöcke zusammenführen
    t = re.sub(r"[ \t]+", " ", t)
    t = "\n".join(_fix_bold(line) for line in t.split("\n"))
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"•\s*\n+\s*(?=\S)", "• ", t)          # Aufzählungspunkt und Text zusammenführen
    t = re.sub(r"\n+\|\n+", " | ", t)                   # Tabellenzellen in eine Zeile
    t = re.sub(r"\n\n+(?=• )", "\n", t)                 # keine Leerzeilen zwischen Aufzählungspunkten
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if limit and len(t) > limit:
        cut = t[:limit]
        cut = cut[: max(cut.rfind("\n"), limit // 2)].rstrip()
        if cut.count("**") % 2:
            cut += "**"
        t = cut + "\n…"
    return t


def _fix_bold(line):
    """Fettdruck pro Zeile reparieren: kaputte/leere ** entfernen, Leerzeichen innen kürzen."""
    if line.count("**") % 2 or "***" in line:
        return re.sub(r"\*+", "", line)
    parts = line.split("**")
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(part)
        elif part.strip():
            lead = " " if part[:1].isspace() else ""
            trail = " " if part[-1:].isspace() else ""
            out.append(f"{lead}**{part.strip()}**{trail}")
    return "".join(out)


def first_image(text):
    m = re.search(r'<img[^>]+src="([^"]+)"', text or "") or re.search(r"\[img\]([^\[]+)\[/img\]", text or "")
    if not m:
        return None
    url = m.group(1).replace("{STEAM_CLAN_IMAGE}", "https://clan.akamai.steamstatic.com/images")
    return url if url.startswith("http") else None


def parse_date(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


# ---------- Quellen ----------

def parse_rss(text, base_url):
    """RSS 2.0 und Atom."""
    root = ET.fromstring(text)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        desc = it.findtext("description") or ""
        img = None
        enc = it.find("enclosure")
        if enc is not None and (enc.get("type") or "").startswith("image"):
            img = enc.get("url")
        items.append({
            "id": guid or link,
            "title": clean_text(it.findtext("title"), 250),
            "url": link,
            "summary": html_to_discord(desc),
            "image": img or first_image(desc),
            "date": parse_date(it.findtext("pubDate")),
        })
    ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(f"{ns}entry"):
        link_el = entry.find(f"{ns}link")
        link = urljoin(base_url, link_el.get("href")) if link_el is not None else ""
        items.append({
            "id": (entry.findtext(f"{ns}id") or link).strip(),
            "title": clean_text(entry.findtext(f"{ns}title"), 250),
            "url": link,
            "summary": clean_text(entry.findtext(f"{ns}summary") or entry.findtext(f"{ns}content")),
            "date": parse_date(entry.findtext(f"{ns}updated") or entry.findtext(f"{ns}published")),
            "image": None,
        })
    return items


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _find_url(d):
    action = d.get("action")
    if isinstance(action, dict):
        payload = action.get("payload") or {}
        if isinstance(payload, dict) and payload.get("url"):
            return payload["url"]
        if action.get("url"):
            return action["url"]
    for key in ("url", "link", "href"):
        val = d.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict) and val.get("url"):
            return val["url"]
    return None


def _find_image(d):
    for key in ("media", "image", "banner", "thumbnail"):
        val = d.get(key)
        if isinstance(val, dict):
            if isinstance(val.get("url"), str):
                return val["url"]
            for sub in val.values():
                if isinstance(sub, dict) and isinstance(sub.get("url"), str):
                    return sub["url"]
        elif isinstance(val, str) and val.startswith("http"):
            return val
    return None


def parse_lol(text, base_url):
    """leagueoflegends.com News-Seite (Next.js). Liest __NEXT_DATA__, sonst Fallback auf Links im HTML."""
    items, seen = [], set()
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', text, re.S)
    if m:
        data = json.loads(m.group(1))
        for d in _walk(data):
            title = d.get("title")
            date = d.get("publishedAt") or d.get("date")
            if not isinstance(title, str) or not isinstance(date, str):
                continue
            url = _find_url(d)
            if not url:
                continue
            url = urljoin(base_url, url)
            if url in seen:
                continue
            seen.add(url)
            desc = d.get("description")
            if isinstance(desc, dict):
                desc = desc.get("body")
            items.append({
                "id": url,
                "title": clean_text(title, 250),
                "url": url,
                "summary": clean_text(desc if isinstance(desc, str) else ""),
                "date": parse_date(date),
                "image": _find_image(d),
            })
    if not items:
        path = re.escape(re.sub(r"^https?://[^/]+", "", base_url).rstrip("/"))
        for m in re.finditer(r'<a[^>]+href="((?:https?://[^"]+)?' + path + r'/[^"#?]+)"[^>]*>(.*?)</a>', text, re.S):
            url = urljoin(base_url, m.group(1))
            if url in seen or url.rstrip("/").count("/") < base_url.rstrip("/").count("/") + 2:
                continue  # Kategorie-Seiten überspringen, nur Artikel
            inner = m.group(2)
            t = re.search(r'<time[^>]+datetime="([^"]+)"', inner)
            title = clean_text(inner, 250)
            if not title:
                continue
            seen.add(url)
            items.append({"id": url, "title": title, "url": url, "summary": "",
                          "date": parse_date(t.group(1)) if t else None, "image": None})
    return items


def fetch_lol_article(item):
    """Holt den Artikeltext einer leagueoflegends.com-Seite (für den Post-Inhalt)."""
    page = fetch(item["url"])
    parts = []
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if m:
        for d in _walk(json.loads(m.group(1))):
            for key in ("body", "richText", "html", "content"):
                val = d.get(key)
                if (isinstance(val, str) and "<" in val and len(val) > 40 and val not in parts
                        and "Passwort vergessen" not in val and "forgot your" not in val.lower()):
                    parts.append(val)
    text = html_to_discord("\n".join(parts)) if parts else ""
    if not text:
        og = re.search(r'<meta[^>]+(?:property|name)="(?:og:)?description"[^>]+content="([^"]*)"', page)
        text = html.unescape(og.group(1)) if og else ""
    if text:
        item["summary"] = text
    if not item.get("image"):
        img = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
        if img:
            item["image"] = img.group(1)


def parse_plaync(text, base_url):
    """NCSoft-Community-Board (z. B. koreanische Aion-2-Updatenotes) über die JSON-API."""
    items = []
    for c in json.loads(text).get("contentList", []):
        meta = c.get("contentMeta", c)
        aid = meta.get("id") or c.get("id")
        pattern = (meta.get("categoryBoard") or {}).get("boardUrlPattern") or "https://aion2.plaync.com/ko-kr/board/update/view?articleId={articleId}"
        items.append({
            "id": aid,
            "title": clean_text(meta.get("title"), 250),
            "url": pattern.replace("{articleId}", aid),
            "api": base_url.split("?")[0].rstrip("/") + "/" + aid,
            "summary": "",
            "date": parse_date((meta.get("timestamps") or {}).get("postedAt")),
            "image": meta.get("thumbnailUrl"),
        })
    return items


HANGUL = re.compile(r"[\uac00-\ud7a3]")


def _gtx(text, target, source):
    data = urllib.parse.urlencode({"q": text}).encode()
    url = f"https://translate.googleapis.com/translate_a/single?client=gtx&sl={source}&tl={target}&dt=t"
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                res = json.loads(resp.read().decode("utf-8"))
            time.sleep(0.3)
            return "".join(seg[0] for seg in res[0] if seg and seg[0])
        except (urllib.error.URLError, ValueError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def translate(text, target="de", source="auto"):
    """Übersetzt Text über Google Translate (ohne API-Key), in Stücken, Zeilenumbrüche bleiben erhalten.

    Prüft danach jede Zeile: steht noch Koreanisch drin oder fehlen Zeilen, wird zeilenweise nachübersetzt.
    """
    lines = text.split("\n")
    out = []
    batch = []

    def flush(b):
        if not "".join(b).strip():
            out.extend(b)
            return
        res = _gtx("\n".join(b), target, source).split("\n")
        if len(res) != len(b):  # Zeilen gingen verloren oder wurden zusammengezogen
            log(f"  Übersetzung: {len(b)} Zeilen rein, {len(res)} raus, übersetze dieses Stück zeilenweise")
            res = [_gtx(l, target, source) if l.strip() else l for l in b]
        out.extend(res)

    size = 0
    for line in lines:
        if batch and size + len(line) > 1500:
            flush(batch)
            batch, size = [], 0
        batch.append(line)
        size += len(line) + 1
    flush(batch)
    first = sum(1 for l in out if HANGUL.search(l))
    if first:
        log(f"  Übersetzung: {first} Zeile(n) im ersten Durchgang unübersetzt, übersetze sie einzeln nach")
    # Zeilen, in denen noch Koreanisch steht, einzeln nachübersetzen
    for i, l in enumerate(out):
        if HANGUL.search(l):
            retry = _gtx(lines[i] if i < len(lines) else l, target, "ko")
            out[i] = retry if not HANGUL.search(retry) or len(HANGUL.findall(retry)) < len(HANGUL.findall(l)) else l
    left = sum(1 for l in out if HANGUL.search(l))
    if left:
        log(f"  WARNUNG: {left} Zeile(n) enthalten nach der Übersetzung noch Koreanisch")
    return "\n".join(_fix_bold(l) for l in out)


def fetch_plaync_article(item):
    data = json.loads(fetch(item["api"]))
    article = data.get("article") or {}
    body = (article.get("content") or {}).get("content") or ""
    item["summary"] = html_to_discord(body)


PARSERS = {"rss": parse_rss, "lol": parse_lol, "plaync": parse_plaync}
DETAIL_FETCHERS = {"lol": fetch_lol_article, "plaync": fetch_plaync_article}



# ---------- Serverstatus ----------

MONTHS_DE = {m: i for i, m in enumerate(["januar", "februar", "märz", "april", "mai", "juni", "juli", "august",
                                         "september", "oktober", "november", "dezember"], 1)}
TZ_OFFSETS = {"CEST": 2, "MESZ": 2, "CET": 1, "MEZ": 1, "UTC": 0, "GMT": 0, "PDT": -7, "PST": -8}
_TIME_RE = (r"(?:(\d{1,2})\.\s*(%s)\s*(?:\d{4})?\s*)?(?:um\s+)?(\d{1,2})\s*[h:.]\s*(\d{2})\s*(?:Uhr\s*)?\(?(%s)\)?"
            % ("|".join(MONTHS_DE), "|".join(TZ_OFFSETS)))


def berlin(dt):
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Europe/Berlin"))
    except Exception:
        return dt.astimezone(timezone(timedelta(hours=2)))


def _to_dt(m, ref):
    day, month, hh, mm, tz = m.groups()
    day = int(day) if day else ref.day
    month = MONTHS_DE[month.lower()] if month else ref.month
    year = ref.year + (1 if month < ref.month - 6 else 0)
    local = datetime(year, month, day, int(hh), int(mm), tzinfo=timezone(timedelta(hours=TZ_OFFSETS[tz.upper()])))
    return local.astimezone(timezone.utc)


def maintenance_window(text, posted):
    """Liest Beginn und Ende einer Wartung aus einer Ankündigung (deutsch, Steam).

    Bevorzugt Zeiten in deutscher Zeit (CEST/CET), das Ende kommt aus "bis ..." oder "Dauer: N Stunden".
    """
    t = re.sub(r"[*​]", "", text or "")
    ref = berlin(posted or datetime.now(timezone.utc))
    de_tz = ("CEST", "MESZ", "CET", "MEZ")
    # 1) Bereich "07h00 bis 15h00 CEST" / "07:00 - 15:00 Uhr (MESZ)"
    rng = re.compile(r"(?:(\d{1,2})\.\s*(%s)\s*(?:\d{4})?\s*)?(?:um\s+|von\s+)?(\d{1,2})\s*[h:.]\s*(\d{2})\s*(?:Uhr)?\s*"
                     r"(?:bis|-|–)\s*(\d{1,2})\s*[h:.]\s*(\d{2})\s*(?:Uhr\s*)?\(?(%s)\)?"
                     % ("|".join(MONTHS_DE), "|".join(TZ_OFFSETS)), re.I)
    ranges = list(rng.finditer(t))
    ranges = [m for m in ranges if m.group(7).upper() in de_tz] or ranges
    if ranges:
        d, mo, h1, m1, h2, m2, tz = ranges[0].groups()
        fake = lambda h, m: re.match(_TIME_RE, f"{d + '. ' + mo + ' ' if d and mo else ''}{h}:{m} {tz}", re.I)
        start, end = _to_dt(fake(h1, m1), ref), _to_dt(fake(h2, m2), ref)
        return start, end + timedelta(days=1) if end <= start else end
    times = list(re.finditer(_TIME_RE, t, re.I))
    if not times:
        return None, None
    # 2) "bis 17:00 Uhr (MESZ)", z. B. bei einer Verlängerung
    untils = [(m.start(1), re.match(_TIME_RE, m.group(1), re.I))
              for m in re.finditer(r"bis\s+(?:\w+,\s*)?(" + _TIME_RE + ")", t, re.I)]
    untils = [u for u in untils if u[1].group(5).upper() in de_tz] or untils
    # Zeitangaben in deutscher Zeit bevorzugen (die Ankündigung nennt oft auch PDT)
    de = [m for m in times if m.group(5).upper() in de_tz] or times
    if untils:
        end = _to_dt(untils[0][1], ref)
        others = [m for m in de if m.start() != untils[0][0]]
        start = _to_dt(others[0], ref) if others else None
    else:
        # 3) Beginn plus "Dauer: 8 Stunden"
        start, end = _to_dt(de[0], ref), None
        dur = re.search(r"Dauer\s*:?\s*(?:ca\.\s*)?(\d+(?:[.,]\d+)?)\s*(Stunde|Std|Minute|Min)", t, re.I)
        if dur:
            n = float(dur.group(1).replace(",", "."))
            end = start + (timedelta(hours=n) if dur.group(2).lower().startswith(("stunde", "std")) else timedelta(minutes=n))
        elif len(de) > 1:
            end = _to_dt(de[1], ref)
    if start and end and end <= start:
        end += timedelta(days=1)
    return start, end


def _status_message(source, title, text, color=None):
    msg = {"embeds": [{
        "title": title[:256],
        "description": text[:EMBED_TEXT_LIMIT],
        "color": int((color or source.get("color", "#43B581")).lstrip("#"), 16),
        "author": {"name": f"{source['game']} · {source.get('label', '')}".strip(" ·")},
        "footer": {"text": "News-Bot · Serverstatus"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }], "allowed_mentions": {"parse": []}}
    role = str(source.get("mention_role_id") or "").strip()
    if role:
        msg["content"] = f"<@&{role}>"
        msg["allowed_mentions"] = {"roles": [role]}
    elif source.get("mention_everyone"):
        msg["content"] = "@everyone"
        msg["allowed_mentions"] = {"parse": ["everyone"]}
    return msg


def check_steam_maintenance(src, state, now):
    """Aion 2: kein öffentlicher Live-Serverstatus. Der Bot liest die offizielle Wartungsankündigung
    und meldet, sobald das angekündigte Ende erreicht ist (Verlängerungen werden ebenfalls angekündigt)."""
    st = state if isinstance(state, dict) else {}
    done = st.setdefault("notified", [])
    items = [i for i in parse_rss(fetch(src["url"]), src["url"])
             if re.search(src.get("title_match", "Wartung"), i["title"], re.I)]
    items.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc))
    if not items:
        log(f"{src['id']}: keine Wartungsankündigung gefunden")
        return st, []
    item = items[-1]  # neueste Ankündigung zählt (z. B. Verlängerung)
    if item["id"] in done:
        log(f"{src['id']}: '{item['title']}' bereits gemeldet")
        return st, []
    if re.search(r"beendet|abgeschlossen|vorbei", item["title"], re.I):
        start, end = None, item["date"]
    else:
        start, end = maintenance_window(item["summary"], item["date"])
    if not end:
        log(f"{src['id']}: '{item['title']}': kein Ende in der Ankündigung gefunden")
        return st, []
    st["current"] = {"id": item["id"], "title": item["title"], "end": end.isoformat()}
    if now < end:
        log(f"{src['id']}: Wartung läuft laut Ankündigung bis {berlin(end):%d.%m. %H:%M} Uhr (deutsche Zeit)")
        return st, []
    done.append(item["id"])
    st["notified"] = done[-50:]
    if now - end > timedelta(hours=6):
        log(f"{src['id']}: Wartung '{item['title']}' ist schon länger vorbei, keine Meldung")
        return st, []
    when = (f"{berlin(start):%d.%m. %H:%M} bis {berlin(end):%H:%M} Uhr" if start else f"{berlin(end):%d.%m. %H:%M} Uhr")
    text = (f"Die Wartung ist laut offizieller Ankündigung beendet ({when}, deutsche Zeit). "
            f"Ihr könnt euch wieder einloggen.\n\n[Zur Ankündigung]({item['url']})\n\n"
            "*Grundlage ist die offizielle Ankündigung. Wird die Wartung verlängert, postet der Bot "
            "die neue Ankündigung und meldet sich zum neuen Ende erneut.*")
    return st, [_status_message(src, "✅ Server wieder online", text)]


def _riot_text(entries, locale):
    for want in (locale, "en_US"):
        for e in entries or []:
            if e.get("locale") == want and e.get("content"):
                return e["content"]
    return (entries or [{}])[0].get("content", "")


def check_riot_status(src, state, now):
    """LoL: offizielle Riot-Statusseite (status.riotgames.com) für eine Region."""
    st = state if isinstance(state, dict) else {}
    first = "active" not in st
    known = st.get("active", {})
    data = json.loads(fetch(src["url"]))
    locale = src.get("locale", "de_DE")
    region = data.get("name") or src.get("region", "")
    active = {}
    for kind, entries in (("maintenance", data.get("maintenances")), ("incident", data.get("incidents"))):
        for e in entries or []:
            if kind == "maintenance" and e.get("maintenance_status") not in ("in_progress",):
                continue
            if kind == "incident" and e.get("incident_severity") not in src.get("incident_severities", ["critical"]):
                continue
            updates = sorted(e.get("updates") or [], key=lambda u: u.get("created_at") or "")
            active[str(e.get("id"))] = {
                "kind": kind,
                "title": _riot_text(e.get("titles"), locale),
                "update": _riot_text(updates[-1].get("translations"), locale) if updates else "",
            }
    msgs = []
    if not first:
        for eid, e in active.items():
            if eid not in known:
                head = "🔧 Wartung läuft" if e["kind"] == "maintenance" else "⚠️ Serverstörung"
                msgs.append(_status_message(src, f"{head} ({region})",
                                            f"**{e['title']}**\n\n{e['update']}".strip(), "#E67E22"))
        for eid, e in known.items():
            if eid not in active:
                what = "Die Wartung" if e["kind"] == "maintenance" else "Die Störung"
                msgs.append(_status_message(src, f"✅ Server wieder online ({region})",
                                            f"{what} **{e['title']}** ist beendet."))
    log(f"{src['id']}: {len(active)} aktive Wartung(en)/Störung(en)" + (" (erster Lauf, nur gemerkt)" if first else ""))
    st["active"] = active
    return st, msgs


STATUS_CHECKERS = {"steam_maintenance": check_steam_maintenance, "riot_status": check_riot_status}


def is_relevant(src, item):
    """Filter aus sources.json: nur bestimmte Domains / Titel, Ausschlüsse."""
    domain = re.sub(r"^https?://([^/]+).*", r"\1", item["url"] or "")
    if src.get("include_domains") and domain not in src["include_domains"]:
        return False
    if domain in src.get("exclude_domains", []):
        return False
    title = item["title"]
    if src.get("include_title") and not re.search(src["include_title"], title, re.I):
        return False
    if src.get("exclude_title") and re.search(src["exclude_title"], title, re.I):
        return False
    return True


# ---------- Discord ----------

EMBED_TEXT_LIMIT = 4000  # Discord erlaubt 4096 Zeichen pro Embed-Beschreibung


def split_text(text, limit=EMBED_TEXT_LIMIT):
    """Teilt langen Text an Absatz-/Zeilengrenzen in Stücke <= limit. Es geht nichts verloren."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # extrem lange Zeile hart teilen
            cut = line[:limit].rfind(" ")
            cut = cut if cut > limit // 2 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    # Fettdruck (**) über Stückgrenzen hinweg sauber schließen und wieder öffnen
    fixed, carry = [], False
    for c in chunks:
        c = ("**" if carry else "") + c.strip("\n")
        carry = c.count("**") % 2 == 1
        fixed.append(c + ("**" if carry else ""))
    return [c for c in fixed if c.strip("* \n")]


def _send(webhook, payload):
    body = json.dumps(payload).encode()
    for attempt in range(5):
        req = urllib.request.Request(webhook, data=body, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Rate-Limit: kurz warten
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(min(wait, 30) + 0.5)
                continue
            log(f"Discord-Fehler {e.code}: {e.read().decode(errors='replace')[:200]}")
            return False
        except urllib.error.URLError as e:
            log(f"Discord nicht erreichbar: {e}")
            time.sleep(2 ** attempt)
    return False


def build_messages(source, item):
    """Baut die Discord-Nachrichten: erste mit Titel (und Rollen-Ping), weitere als Fortsetzung."""
    color = int(source.get("color", "#5865F2").lstrip("#"), 16)
    chunks = split_text(item["summary"] or "") or [""]
    # Prüfen, dass der komplette Text in den Teilen steckt
    flat = lambda t: re.sub(r"[\s*]", "", t)
    if flat("".join(chunks)) != flat(item["summary"] or ""):
        log(f"  WARNUNG: Text von '{item['title']}' wurde beim Aufteilen nicht vollständig übernommen")
    role = str(source.get("mention_role_id") or "").strip()
    messages = []
    for n, chunk in enumerate(chunks, 1):
        embed = {"color": color, "description": chunk or None}
        if n == 1:
            embed.update({
                "title": item["title"][:256],
                "url": item["url"] or None,
                "author": {"name": f"{source['game']} · {source.get('label', '')}".strip(" ·")},
            })
            if item.get("image"):
                embed["image"] = {"url": item["image"]}
        if n == len(chunks):
            embed["footer"] = {"text": "News-Bot" + (f" · Teil {n}/{len(chunks)}" if len(chunks) > 1 else "")}
            if item.get("date"):
                embed["timestamp"] = item["date"].isoformat()
        elif len(chunks) > 1:
            embed["footer"] = {"text": f"Teil {n}/{len(chunks)}"}
        msg = {"embeds": [{k: v for k, v in embed.items() if v is not None}], "allowed_mentions": {"parse": []}}
        if n == 1 and role:
            msg["content"] = f"<@&{role}>"
            msg["allowed_mentions"] = {"roles": [role]}
        elif n == 1 and source.get("mention_everyone"):
            msg["content"] = "@everyone"
            msg["allowed_mentions"] = {"parse": ["everyone"]}
        messages.append(msg)
    return messages


def post_to_discord(webhook, source, item):
    messages = build_messages(source, item)
    for n, msg in enumerate(messages, 1):
        if not _send(webhook, msg):
            if n == 1:
                return False
            log(f"  Teil {n}/{len(messages)} konnte nicht gesendet werden")
            return True  # Anfang ist schon im Channel, nicht doppelt posten
        if len(messages) > 1:
            time.sleep(0.8)
    log(f"  gepostet: {item['title']} ({len(messages)} Nachricht(en), {len(item['summary'] or '')} Zeichen)")
    return True


def preview(url, source_type):
    """Zeigt, was für einen Artikel gepostet würde (zum Prüfen, ohne zu posten)."""
    item = {"id": url, "title": "Vorschau", "url": url, "api": url, "summary": "", "date": None, "image": None}
    if "api-community.plaync.com" in url:
        source_type = "plaync"
    if source_type in DETAIL_FETCHERS:
        DETAIL_FETCHERS[source_type](item)
    if source_type == "plaync":
        orig = item["summary"]
        item["summary"] = translate(orig, "de", "ko")
        print(f"Original: {len(orig.splitlines())} Zeilen, {sum(1 for l in orig.splitlines() if HANGUL.search(l))} mit Koreanisch")
        print(f"Übersetzt: {len(item['summary'].splitlines())} Zeilen, "
              f"{sum(1 for l in item['summary'].splitlines() if HANGUL.search(l))} noch mit Koreanisch")
    msgs = build_messages({"game": "Vorschau"}, item)
    print(f"{len(item['summary'])} Zeichen -> {len(msgs)} Nachricht(en)")
    bad = [l for l in item["summary"].split("\n") if l.count("**") % 2 or "***" in l]
    print(f"Zeilen mit kaputtem Fettdruck: {len(bad)}")
    print(f"Längster Teil: {max(len(m['embeds'][0].get('description', '')) for m in msgs)} Zeichen (Limit 4096)")
    for m in msgs:
        d = m["embeds"][0].get("description", "")
        print(f"--- Teil ({len(d)} Zeichen) ---")
        print(d[:600] + ("\n[...]\n" + d[-300:] if len(d) > 900 else ""))


# ---------- Ablauf ----------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def run_once(dry_run=False):
    sources = [s for s in json.loads(SOURCES_FILE.read_text()) if s.get("enabled", True)]
    state = load_state()
    posted = 0
    for src in sources:
        env_name = src.get("webhook_env", "DISCORD_WEBHOOK_URL")
        webhook = os.environ.get(env_name, "").strip()
        if not webhook and not dry_run:
            log(f"{src['id']}: Secret {env_name} ist nicht gesetzt, Quelle wird übersprungen")
            continue
        if src["type"] in STATUS_CHECKERS:
            try:
                st, msgs = STATUS_CHECKERS[src["type"]](src, state.get(src["id"]), datetime.now(timezone.utc))
            except Exception as e:
                log(f"{src['id']}: Statusprüfung fehlgeschlagen: {e}")
                continue
            ok = True
            for msg in msgs:
                if dry_run:
                    log(f"  [dry-run] Status: {msg['embeds'][0]['title']}: {msg['embeds'][0]['description'][:300]}")
                else:
                    ok = _send(webhook, msg) and ok
                    log(f"  Status gepostet: {msg['embeds'][0]['title']}")
            if ok:  # bei Fehler Zustand nicht speichern, damit es beim nächsten Lauf erneut versucht wird
                state[src["id"]] = st
            continue
        try:
            items = PARSERS[src["type"]](fetch(src["url"]), src["url"])
        except Exception as e:  # eine kaputte Quelle soll die anderen nicht stoppen
            log(f"{src['id']}: Abruf fehlgeschlagen: {e}")
            continue
        items = [i for i in items if i["id"] and i["title"]]
        if not items:
            log(f"{src['id']}: keine Einträge gefunden (Seitenaufbau geändert?)")
            continue
        seen = state.get(src["id"])
        first_run = seen is None
        seen = seen or []
        new = [i for i in items if i["id"] not in seen]
        irrelevant = [i for i in new if not is_relevant(src, i)]
        new = [i for i in new if is_relevant(src, i)]
        new.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc))
        if first_run:
            # Beim allerersten Lauf nicht das ganze Archiv posten, nur den neuesten Eintrag.
            to_post, skip = new[-1:], new[:-1]
        else:
            to_post, skip = new, []
        log(f"{src['id']}: {len(items)} Einträge, {len(to_post)} neu und relevant, {len(irrelevant)} ignoriert")
        for i in irrelevant:
            log(f"  ignoriert: {i['title']}")
        for item in to_post:
            if posted >= MAX_POSTS_PER_RUN:
                log("Maximale Posts pro Lauf erreicht, Rest folgt beim nächsten Lauf")
                break
            if src["type"] in DETAIL_FETCHERS:
                try:
                    DETAIL_FETCHERS[src["type"]](item)
                except Exception as e:
                    log(f"  Artikeltext nicht abrufbar ({e}), poste Kurzfassung")
            if src.get("translate_to"):
                try:
                    lang = src.get("translate_from", "auto")
                    item["title"] = translate(item["title"], src["translate_to"], lang)
                    item["summary"] = translate(item["summary"], src["translate_to"], lang)
                except Exception as e:
                    log(f"  Übersetzung fehlgeschlagen ({e}), poste Original")
            if src.get("title_prefix"):
                item["title"] = f"{src['title_prefix']} {item['title']}"[:256]
            if src.get("intro"):
                item["summary"] = f"*{src['intro']}*\n\n{item['summary']}"
            if dry_run:
                log(f"  [dry-run] {src['game']}: {item['title']} -> {item['url']}")
                msgs = build_messages(src, item)
                log(f"  [dry-run] {len(item['summary'] or '')} Zeichen -> {len(msgs)} Nachricht(en)")
                print((item["summary"] or "")[:2500])
                ok = True
            else:
                ok = post_to_discord(webhook, src, item)
                time.sleep(1)
            if ok:
                seen.append(item["id"])
                posted += 1
        seen.extend(i["id"] for i in skip + irrelevant)
        state[src["id"]] = seen[-MAX_SEEN_PER_SOURCE:]
    if not dry_run:
        save_state(state)
    return posted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="SEKUNDEN", help="dauerhaft laufen und alle N Sekunden prüfen")
    ap.add_argument("--dry-run", action="store_true", help="nichts posten, nur anzeigen")
    ap.add_argument("--preview", metavar="URL", help="Artikeltext einer LoL-Seite anzeigen, ohne zu posten")
    ap.add_argument("--probe", nargs="+", metavar="URL", help="testen, ob URLs abrufbar sind")
    args = ap.parse_args()
    if args.probe:
        for url in args.probe:
            try:
                body = fetch(url)
                snippet = re.sub(r"\s+", " ", body if len(args.probe) == 1 else body[:400])
                if body.lstrip().startswith("{"):
                    def tree(o, path="", depth=0):
                        if depth > 6:
                            return
                        if isinstance(o, dict):
                            for k, v in o.items():
                                tree(v, f"{path}.{k}", depth + 1)
                        elif isinstance(o, list):
                            print(f"     {path}[] ({len(o)})")
                            if o:
                                tree(o[0], f"{path}[0]", depth + 1)
                        else:
                            print(f"     {path} = {str(o)[:120]!r}")
                    tree(json.loads(body))
                    continue
                print(f"OK   {len(body):>8} Zeichen  {url}\n     {snippet}")
            except Exception as e:
                print(f"FAIL {e}  {url}")
        return
    if args.preview:
        preview(args.preview, "lol")
        return
    while True:
        n = run_once(args.dry_run)
        log(f"Fertig, {n} Beiträge gepostet")
        if not args.loop:
            break
        time.sleep(max(args.loop, 60))


if __name__ == "__main__":
    main()
