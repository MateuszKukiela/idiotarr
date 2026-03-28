import os
import httpx
import xml.etree.ElementTree as ET
from fastapi import FastAPI, Query
from fastapi.responses import Response

app = FastAPI()

PROWLARR_URL = os.environ["PROWLARR_URL"].rstrip("/")
PROWLARR_API_KEY = os.environ["PROWLARR_API_KEY"]
FRESH_DAYS = int(os.getenv("FRESH_DAYS", "30"))
FRESH_TAG = os.getenv("FRESH_TAG", "NZB-FRESH")
TORRENT_TAG = os.getenv("TORRENT_TAG", "TORRENT-LAST-RESORT")
API_KEY = os.getenv("API_KEY", "idiotarr")


def tag_title(title: str, tag: str) -> str:
    return f"{title} {tag}"


def is_torrent(item: dict) -> bool:
    return (
        item.get("downloadUrl", "").endswith(".torrent")
        or item.get("protocol", "") == "torrent"
        or "magnet:" in item.get("downloadUrl", "")
    )


def process_usenet(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        if is_torrent(item):
            continue
        age = item.get("age")
        if age is not None and age <= FRESH_DAYS:
            item["title"] = tag_title(item["title"], FRESH_TAG)
        result.append(item)
    return result


def process_torrent(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        if not is_torrent(item):
            continue
        item["title"] = tag_title(item["title"], TORRENT_TAG)
        result.append(item)
    return result


def build_xml(items: list[dict], ns: str = "newznab") -> str:
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
        enclosure.set("type", "application/x-nzb" if ns == "newznab" else "application/x-bittorrent")

        def attr(name, value):
            a = ET.SubElement(entry, f"{ns}:attr")
            a.set("name", name)
            a.set("value", str(value))

        if item.get("categories"):
            attr("category", item["categories"][0]["id"])
        attr("size", str(item.get("size", 0)))
        if item.get("imdbId"):
            attr("imdb", str(item["imdbId"]).lstrip("tt"))
        if item.get("tvdbId"):
            attr("tvdbid", str(item["tvdbId"]))
        if ns == "torznab" and item.get("seeders") is not None:
            attr("seeders", item["seeders"])
        if ns == "torznab" and item.get("magnetUrl"):
            attr("magneturl", item["magnetUrl"])

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode")


USENET_CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="idiotarr-usenet"/>
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

TORRENT_CAPS = """<?xml version="1.0" encoding="UTF-8"?>
<caps>
  <server title="idiotarr-torrent"/>
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
            params=params,
            headers={"X-Api-Key": PROWLARR_API_KEY},
        )
        resp.raise_for_status()
        return resp.json()


def build_prowlarr_params(t, q, imdbid, tvdbid, season, ep, cat) -> dict:
    params: dict = {}
    if q:
        params["query"] = q
    if imdbid:
        params["imdbId"] = imdbid.lstrip("tt")
    if tvdbid:
        params["tvdbId"] = tvdbid
    if season:
        params["season"] = season
    if ep:
        params["episode"] = ep
    if cat:
        params["categories"] = cat
    if t == "movie":
        params.setdefault("categories", "2000")
    if t == "tvsearch":
        params.setdefault("categories", "5000")
    return params


@app.get("/usenet")
async def usenet(
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
        return Response(content=USENET_CAPS, media_type="application/xml")

    if t in ("search", "movie", "tvsearch"):
        items = await prowlarr_search(build_prowlarr_params(t, q, imdbid, tvdbid, season, ep, cat))
        items = process_usenet(items)
        return Response(content=build_xml(items, "newznab"), media_type="application/rss+xml")

    return Response(content="<error>unsupported</error>", media_type="application/xml", status_code=400)


@app.get("/torrent")
async def torrent(
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
        return Response(content=TORRENT_CAPS, media_type="application/xml")

    if t in ("search", "movie", "tvsearch"):
        items = await prowlarr_search(build_prowlarr_params(t, q, imdbid, tvdbid, season, ep, cat))
        items = process_torrent(items)
        return Response(content=build_xml(items, "torznab"), media_type="application/rss+xml")

    return Response(content="<error>unsupported</error>", media_type="application/xml", status_code=400)
