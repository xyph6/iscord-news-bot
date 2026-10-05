#!/usr/bin/env python3
"""Discord-News-Bot: postet neue News zu Aion 2 und League of Legends per Webhook.

Nur Python-Standardbibliothek. Konfiguration:
  DISCORD_WEBHOOK_URL  (Pflicht, als Secret/Umgebungsvariable, nie im Code)
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
import sys
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
        img = None
        enc = it.find("enclosure")
        if enc is not None and (enc.get("type") or "").startswith("image"):
            img = enc.get("url")
        items.append({
            "id": guid or link,
            "title": clean_text(it.findtext("title"), 250),
            "url": link,
            "summary": clean_text(it.findtext("description")),
            "date": parse_date(it.findtext("pubDate")),
            "image": img,
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


PARSERS = {"rss": parse_rss, "lol": parse_lol}


# ---------- Discord ----------

def post_to_discord(webhook, source, item):
    color = int(source.get("color", "#5865F2").lstrip("#"), 16)
    embed = {
        "title": item["title"][:256],
        "url": item["url"] or None,
        "description": item["summary"][:4000] or None,
        "color": color,
        "author": {"name": f"{source['game']} · {source.get('label', '')}".strip(" ·")},
        "footer": {"text": "News-Bot"},
    }
    if item.get("date"):
        embed["timestamp"] = item["date"].isoformat()
    if item.get("image"):
        embed["image"] = {"url": item["image"]}
    body = json.dumps({"embeds": [{k: v for k, v in embed.items() if v is not None}],
                       "allowed_mentions": {"parse": []}}).encode()
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


def run_once(webhook, dry_run=False):
    sources = [s for s in json.loads(SOURCES_FILE.read_text()) if s.get("enabled", True)]
    state = load_state()
    posted = 0
    for src in sources:
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
        new.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc))
        if first_run:
            # Beim allerersten Lauf nicht das ganze Archiv posten, nur den neuesten Eintrag.
            to_post, skip = new[-1:], new[:-1]
        else:
            to_post, skip = new, []
        log(f"{src['id']}: {len(items)} Einträge, {len(to_post)} neu")
        for item in to_post:
            if posted >= MAX_POSTS_PER_RUN:
                log("Maximale Posts pro Lauf erreicht, Rest folgt beim nächsten Lauf")
                break
            if dry_run:
                log(f"  [dry-run] {src['game']}: {item['title']} -> {item['url']}")
                ok = True
            else:
                ok = post_to_discord(webhook, src, item)
                time.sleep(1)
            if ok:
                seen.append(item["id"])
                posted += 1
        seen.extend(i["id"] for i in skip)
        state[src["id"]] = seen[-MAX_SEEN_PER_SOURCE:]
    if not dry_run:
        save_state(state)
    return posted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="SEKUNDEN", help="dauerhaft laufen und alle N Sekunden prüfen")
    ap.add_argument("--dry-run", action="store_true", help="nichts posten, nur anzeigen")
    args = ap.parse_args()
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook and not args.dry_run:
        sys.exit("DISCORD_WEBHOOK_URL ist nicht gesetzt.")
    while True:
        n = run_once(webhook, args.dry_run)
        log(f"Fertig, {n} Beiträge gepostet")
        if not args.loop:
            break
        time.sleep(max(args.loop, 60))


if __name__ == "__main__":
    main()
