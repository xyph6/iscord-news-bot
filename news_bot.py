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
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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
    t = html.unescape(t).replace("\xa0", " ")
    t = re.sub(r"\*\*([ \t]*)\*\*(?=\S)", r"\1", t)  # direkt aneinanderstoßende Fett-Blöcke zusammenführen
    t = re.sub(r"[ \t]+", " ", t)
    t = "\n".join(_fix_bold(line) for line in t.split("\n"))
    t = re.sub(r" *\n *", "\n", t)
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


PARSERS = {"rss": parse_rss, "lol": parse_lol}
DETAIL_FETCHERS = {"lol": fetch_lol_article}


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
    item = {"id": url, "title": "Vorschau", "url": url, "summary": "", "date": None, "image": None}
    if source_type in DETAIL_FETCHERS:
        DETAIL_FETCHERS[source_type](item)
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
            if dry_run:
                log(f"  [dry-run] {src['game']}: {item['title']} -> {item['url']}")
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
