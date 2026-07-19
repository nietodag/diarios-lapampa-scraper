#!/usr/bin/env python3
"""Escanea los diarios de La Pampa cada 5 minutos y avisa por WhatsApp (CallMeBot) las notas nuevas."""

import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SEEN_PATH = BASE_DIR / "seen_store.json"
QUEUE_PATH = BASE_DIR / "pending_queue.json"
LOG_PATH = BASE_DIR / "scraper.log"
LOCK_PATH = BASE_DIR / "scraper.lock"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) DiariosScraper/1.0"
REQUEST_TIMEOUT = 12
MAX_NEW_PER_SITE = 15  # si supera esto, se asume rediseño del sitio y se re-baselinea sin mandar mensajes
MAX_SEEN_PER_SITE = 20000
SEND_DELAY_SECONDS = 4  # margen entre mensajes de WhatsApp para no saturar CallMeBot
MAX_SEND_ATTEMPTS = 20  # si un link falla siempre (no solo por limite de tasa), se descarta tras estos intentos


class CallMeBotError(Exception):
    pass

SITES = [
    {"name": "Pampa Diario", "url": "https://www.pampadiario.com/", "method": "html"},
    {"name": "Info Pico", "url": "https://www.infopico.com/feed/", "method": "rss"},
    {"name": "La Arena", "url": "https://www.laarena.com.ar/", "method": "html"},
    {"name": "LetraP", "url": "https://www.letrap.com.ar/sitemap.xml", "method": "sitemap"},
    {"name": "El Diario de La Pampa", "url": "https://www.eldiariodelapampa.com.ar/", "method": "html"},
    {"name": "Dos Bases", "url": "https://www.dosbases.com.ar/sitemap.xml", "method": "sitemap"},
    {"name": "Plan B Noticias", "url": "https://www.planbnoticias.com.ar/?feed=rss2", "method": "rss"},
    {"name": "Infotec Realicó", "url": "https://infotecrealico.com.ar/sitemap.xml", "method": "sitemap"},
    {"name": "Diario Textual", "url": "https://diariotextual.com/inicio/", "method": "html"},
    {"name": "Radio Kermes", "url": "https://www.radiokermes.com/", "method": "html"},
    {"name": "En Boca de Todos HD", "url": "https://www.enbocadetodoshd.com.ar/", "method": "html"},
    {"name": "Mará Codigital", "url": "https://www.maracodigital.net/sitemap.xml", "method": "sitemap"},
]

BAD_PATH_KEYWORDS = [
    "/tag/", "/categoria/", "/category/", "/autor/", "/author/", "/page/",
    "/wp-content/", "/wp-json/", "/feed", "/contacto", "/nosotros", "/publicidad",
    "/login", "/wp-admin", "/buscar", "/search", "/privacidad", "/terminos",
    "/newsletter", "/suscri",
]
BAD_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".css", ".js")
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref", "share"}


class LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.hrefs.append(value)


def fetch(url, accept_binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = resp.read()
    return data if accept_binary else data.decode("utf-8", errors="ignore")


def looks_like_article(path, query=""):
    clean_path = path.rstrip("/")
    if not clean_path:
        return False
    low = clean_path.lower()
    if any(bad in low for bad in BAD_PATH_KEYWORDS):
        return False
    if low.endswith(BAD_EXTENSIONS):
        return False

    # slug-style: /seccion/titulo-de-la-nota-con-muchos-guiones
    last_seg = clean_path.split("/")[-1]
    if last_seg.count("-") >= 2 and len(last_seg) > 15:
        return True

    # id-style: single-post.php?id=12345 (CMS que arma la nota por parametro numerico)
    if query:
        qs = urllib.parse.parse_qs(query)
        if "id" in qs and qs["id"][0].isdigit():
            basename = last_seg
            if not any(bad in basename for bad in ("index", "categoria", "seccion", "tag")):
                return True

    return False


def clean_query(query):
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k not in TRACKING_PARAMS]
    return urllib.parse.urlencode(kept)


def extract_html_links(base_url, page_html):
    parser = LinkExtractor()
    parser.feed(page_html)
    site_netloc = urllib.parse.urlparse(base_url).netloc.replace("www.", "")
    found = {}
    for href in parser.hrefs:
        absolute = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(absolute)
        if parsed.netloc.replace("www.", "") != site_netloc:
            continue
        if not looks_like_article(parsed.path, parsed.query):
            continue
        clean = parsed._replace(query=clean_query(parsed.query), fragment="").geturl()
        found[clean] = None  # el titulo se busca al momento de enviar
    return found


def extract_rss_links(xml_text):
    found = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return found
    for item_el in root.iter():
        tag = item_el.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        title = None
        url = None
        for child in item_el:
            child_tag = child.tag.split("}")[-1]
            if child_tag == "title" and child.text:
                title = child.text.strip()
            elif child_tag == "link":
                url = (child.text or "").strip() or child.attrib.get("href", "").strip()
        if not url or not url.startswith("http"):
            continue
        path = urllib.parse.urlparse(url).path
        if path.rstrip("/") in ("", "/feed", "/index.php/feed"):
            continue  # link del canal/feed, no de una nota
        found[url] = title
    return found


def fetch_title(url):
    try:
        page = fetch(url)
    except Exception:
        return None
    match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']*)["\']', page, re.IGNORECASE
    )
    if not match:
        match = re.search(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:title["\']', page, re.IGNORECASE
        )
    if match:
        title = match.group(1)
    else:
        match2 = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
        title = match2.group(1) if match2 else None
    if not title:
        return None
    title = html.unescape(title).strip()
    # varios sitios agregan " - Nombre del sitio" o " | Nombre del sitio" al final del <title>
    title = re.sub(r"\s+[-|]\s+[^-|]{3,40}$", "", title).strip()
    return title or None


def extract_sitemap_links(url, depth=0, max_subsitemaps=5):
    found = set()
    try:
        xml_text = fetch(url)
        root = ET.fromstring(xml_text)
    except (urllib.error.URLError, ET.ParseError, TimeoutError):
        return found
    tag = root.tag.split("}")[-1]
    locs = [el.text.strip() for el in root.iter() if el.tag.split("}")[-1] == "loc" and el.text]
    if tag == "sitemapindex" and depth < 2:
        sub_sitemaps = locs[:max_subsitemaps]
        for sub_url in sub_sitemaps:
            found |= extract_sitemap_links(sub_url, depth=depth + 1)
    else:
        found |= set(locs)
    return found


def get_candidate_links(site):
    """Devuelve {url: titulo_o_None}. El titulo solo viene resuelto para RSS;
    para sitemap/html se busca despues, unicamente para los links que resulten nuevos."""
    method = site["method"]
    url = site["url"]
    if method == "html":
        page_html = fetch(url)
        return extract_html_links(url, page_html)
    if method == "rss":
        xml_text = fetch(url)
        return extract_rss_links(xml_text)
    if method == "sitemap":
        links = extract_sitemap_links(url)
        return {u: None for u in links if looks_like_article(urllib.parse.urlparse(u).path)}
    return {}


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def load_config():
    # En GitHub Actions las credenciales llegan por variables de entorno (secrets);
    # localmente se usa config.json (que nunca se sube al repo).
    file_config = load_json(CONFIG_PATH, {})
    return {
        "phone": os.environ.get("CALLMEBOT_PHONE", file_config.get("phone")),
        "apikey": os.environ.get("CALLMEBOT_APIKEY", file_config.get("apikey")),
    }


def send_whatsapp(config, text):
    phone = urllib.parse.quote(config["phone"])
    apikey = urllib.parse.quote(config["apikey"])
    message = urllib.parse.quote(text)
    api_url = f"https://api.callmebot.com/whatsapp.php?phone={phone}&text={message}&apikey={apikey}"
    req = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    if "error" in body.lower():
        raise CallMeBotError(body.strip())
    return body


def main():
    handlers = [logging.FileHandler(LOG_PATH)]
    if os.environ.get("GITHUB_ACTIONS"):
        handlers.append(logging.StreamHandler())  # visible en el log de la corrida de Actions
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )

    if LOCK_PATH.exists():
        age = time.time() - LOCK_PATH.stat().st_mtime
        if age < 600:
            logging.warning("Lock activo (%.0fs), se omite esta corrida", age)
            return
        logging.warning("Lock viejo (%.0fs), se ignora y continua", age)
    LOCK_PATH.write_text(str(time.time()))

    try:
        config = load_config()
        if not config.get("apikey") or not config.get("phone"):
            logging.error("Falta config.json con phone/apikey de CallMeBot")
            return

        # seen[name] es {url: timestamp_primera_vez_visto} para poder recortar
        # el historico por antiguedad real (un set no preserva orden de insercion).
        seen = load_json(SEEN_PATH, {})
        queue = load_json(QUEUE_PATH, [])
        queued_urls = {item["url"] for item in queue}
        now = time.time()

        for site in SITES:
            name = site["name"]
            try:
                candidates = get_candidate_links(site)
            except Exception as exc:
                logging.warning("Error escaneando %s: %s", name, exc)
                continue

            if not candidates:
                continue

            is_first_run = name not in seen
            site_seen = seen.get(name, {})
            site_seen_urls = set(site_seen.keys())
            new_links = set(candidates.keys()) - site_seen_urls

            if is_first_run:
                logging.info("%s: baseline inicial con %d links (sin avisos)", name, len(candidates))
            elif len(new_links) > MAX_NEW_PER_SITE:
                logging.warning(
                    "%s: %d links nuevos de golpe (posible rediseno), se re-baseliza sin avisar",
                    name, len(new_links),
                )
            else:
                for link in sorted(new_links):
                    if link not in queued_urls:
                        queue.append({"site": name, "url": link, "title": candidates.get(link), "attempts": 0})
                        queued_urls.add(link)
                        logging.info("Encolado: %s -> %s", name, link)

            for url in new_links:
                site_seen[url] = now

            if len(site_seen) > MAX_SEEN_PER_SITE:
                oldest_first = sorted(site_seen.items(), key=lambda kv: kv[1])
                excess = len(site_seen) - MAX_SEEN_PER_SITE
                for url, _ in oldest_first[:excess]:
                    del site_seen[url]

            seen[name] = site_seen

        # Envio de la cola: si un mensaje falla (p.ej. limite de CallMeBot alcanzado),
        # se corta el intento por esta corrida y se reintenta en la proxima (5 min despues).
        messages_sent = 0
        remaining_queue = []
        stop_sending = False
        for item in queue:
            if stop_sending:
                remaining_queue.append(item)
                continue
            if not item.get("title"):
                item["title"] = fetch_title(item["url"])  # se cachea en la cola para no reintentar el fetch
            title = item.get("title")
            text = f"{title}\n{item['url']}" if title else f"{item['site']}: {item['url']}"
            try:
                send_whatsapp(config, text)
                messages_sent += 1
                logging.info("Enviado: %s -> %s", item["site"], item["url"])
                time.sleep(SEND_DELAY_SECONDS)
            except Exception as exc:
                item["attempts"] = item.get("attempts", 0) + 1
                if item["attempts"] >= MAX_SEND_ATTEMPTS:
                    logging.warning(
                        "Descartado tras %d intentos fallidos (%s): %s",
                        item["attempts"], item["url"], exc,
                    )
                    continue
                logging.warning("No se pudo enviar (se reintenta despues): %s (%s)", item["url"], exc)
                remaining_queue.append(item)
                stop_sending = True

        SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=2))
        QUEUE_PATH.write_text(json.dumps(remaining_queue, ensure_ascii=False, indent=2))
        logging.info(
            "Corrida completa. Mensajes enviados: %d. En cola para despues: %d",
            messages_sent, len(remaining_queue),
        )
    finally:
        LOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
