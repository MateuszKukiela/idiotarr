import os
import httpx
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from fastapi import FastAPI, Query, Request
from fastapi.responses import Response

app = FastAPI()

PROWLARR_URL = os.environ["PROWLARR_URL"].rstrip("/")
PROWLARR_API_KEY = os.environ["PROWLARR_API_KEY"]
FRESH_DAYS = int(os.getenv("FRESH_DAYS", "30"))
FRESH_TAG = os.getenv("FRESH_TAG", "NZB-FRESH")
API_KEY = os.getenv("API_KEY", "idiotarr")


def tag_title(title: str, tag: str) -> str:
    return f"{title} {tag}"


def age_days(pub_date_str: str) -> float | None:
    try:
        dt = parsedate_to_datetime(pub_date_str)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return None


def process_results(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        is_torrent = item.get("downloadUrl", "").endswith(".torrent") \
            or item.get("protocol", "") == "torrent" \
            or "magnet:" in item.get("downloadUrl", "")

        if is_torrent:
            continue

        age = age_days(item.get("publishDate", ""))
        if age is not None and age <= FRESH_DAYS:
            item["title"] = tag_title(item["title"], FRESH_TAG)

        result.append(item)
    return result


def build_xml(items: list[dict], search_type: str = "search") -> str:
    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:newznab", "http://www.newznab.com/DTD/2010/feeds/attributes/")
    rss.set("xmlns:torznab", "http://torznab.com/schemas/2015/feed")
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = "idiotarr"
    ET.SubElement(channel, "link").text = "http://idiotarr"
    ET.SubElement(channel, "description").text = "idiotarr proxy"

    for item in items:
        entry = ET.SubElement(channel, "item")
        ET.SubElement(entry, "title").text = item.get("title", "")
        ET.SubElement(entry, "link").text = item.get("downloadUrl", "")
        ET.SubElement(entry, "pubDate").text = item.get("publishDate", "")
        ET.SubElement(entry, "size").text = str(item.get("size", 0))

        guid = ET.SubElement(entry, "guid")
        guid.text = item.get("guid", item.get("downloadUrl", ""))
        guid.set("isPermaLink", "false")

        enclosure = ET.SubElement(entry, "enclosure")
        enclosure.set("url", item.get("downloadUrl", ""))
        enclosure.set("length", str(item.get("size", 0)))
        enclosure.set("type", "application/x-nzb" if not (
            item.get("downloadUrl", "").endswith(".torrent") or
            "magnet:" in item.get("downloadUrl", "")
        ) else "application/x-bittorrent")

        def attr(name, value):
            a = ET.SubElement(entry, "newznab:attr")
            a.set("name", name)
            a.set("value", str(value))

        if item.get("categories"):
            attr("category", item["categories"][0])
        attr("size", str(item.get("size", 0)))
        if item.get("imdbId"):
            attr("imdb", str(item["imdbId"]).lstrip("tt"))
        if item.get("tvdbId"):
            attr("tvdbid", str(item["tvdbId"]))

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


CAPS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="idiotarr"/>
  <searching>
    <search available="yes" supportedParams="q"/>
    <tv-search available="yes" supportedParams="q,tvdbid,season,ep"/>
    <movie-search available="yes" supportedParams="q,imdbid"/>
  </searching>
  <categories>
    <category id="2000" name="Movies"/>
    <category id="5000" name="TV"/>
  </categories>
</caps>"""


async def prowlarr_search(params: dict) -> list[dict]:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{PROWLARR_URL}/api/v1/search",
            params={**params, "apikey": PROWLARR_API_KEY},
        )
        resp.raise_for_status()
        return resp.json()


@app.get("/api")
async def newznab(
    t: str = Query(...),
    apikey: str = Query(default=""),
    q: str = Query(default=""),
    imdbid: str = Query(default=""),
    tvdbid: str = Query(default=""),
    season: str = Query(default=""),
    ep: str = Query(default=""),
    cat: str = Query(default=""),
):
    if t == "caps":
        return Response(content=CAPS_XML, media_type="application/xml")

    prowlarr_params: dict = {}

    if t in ("search", "movie", "tvsearch"):
        if q:
            prowlarr_params["query"] = q
        if imdbid:
            prowlarr_params["imdbId"] = imdbid.lstrip("tt")
        if tvdbid:
            prowlarr_params["tvdbId"] = tvdbid
        if season:
            prowlarr_params["season"] = season
        if ep:
            prowlarr_params["episode"] = ep
        if cat:
            prowlarr_params["categories"] = cat
        if t == "movie":
            prowlarr_params["categories"] = prowlarr_params.get("categories", "2000")
        if t == "tvsearch":
            prowlarr_params["categories"] = prowlarr_params.get("categories", "5000")

        items = await prowlarr_search(prowlarr_params)
        items = process_results(items)
        xml = build_xml(items, t)
        return Response(content=xml, media_type="application/rss+xml")

    return Response(content="<error>unsupported</error>", media_type="application/xml", status_code=400)
