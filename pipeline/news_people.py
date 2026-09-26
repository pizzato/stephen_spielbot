"""Acquire real-person appearance references for a selected news idea.

Names must come from the story's subjects, never the posting account's avatar.
An exact, unambiguous Wikidata human identity and its Commons P18 photograph
are required. Images stay in the script's ``characters/`` directory; provenance
lives in ``news_people.json`` because character normalization drops extra fields.
"""
from __future__ import annotations

import html
import io
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import requests
from PIL import Image, ImageOps

_WIKIDATA_API = "https://www.wikidata.org/w/api.php"
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_USER_AGENT = "Stephen-Spielbot/1.0 (https://github.com/pizzato/stephen_spielbot)"
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_IMAGE_PIXELS = 24_000_000
MAX_PEOPLE = 4
_CONTEXT_STOP_WORDS = set((
    "the and for from with who whom whose that this these those was were are has had "
    "have about into their they them its also only news article post source story "
    "report reported reports reporting said says according named name person people "
    "subject involved public figure featured mentioned describes description role"
).split())


def _read_response(url: str, *, params=None, image=False) -> bytes:
    """Only fetch fixed Wikimedia hosts, without redirects, within byte/time caps."""
    parsed = urlsplit(url)
    allowed = {"upload.wikimedia.org", "thumb.wikimedia.org"} if image else {"www.wikidata.org", "commons.wikimedia.org"}
    if (parsed.scheme != "https" or parsed.hostname not in allowed
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("The reference URL is not on an allowed Wikimedia host.")
    limit = _MAX_IMAGE_BYTES if image else _MAX_JSON_BYTES
    started = time.monotonic()
    with requests.get(url, params=params, headers={"User-Agent": _USER_AGENT},
                      timeout=(5, 15), stream=True, allow_redirects=False) as response:
        if 300 <= response.status_code < 400:
            raise ValueError("Wikimedia redirected the reference request.")
        response.raise_for_status()
        if image and response.headers.get("Content-Type", "").split(";")[0].lower() not in {
                "image/jpeg", "image/png", "image/webp"}:
            raise ValueError("The reference is not a supported photograph.")
        if int(response.headers.get("Content-Length") or 0) > limit:
            raise ValueError("The reference response exceeds the download limit.")
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > limit or time.monotonic() - started > 30:
                raise ValueError("The reference response exceeds the download limit.")
            chunks.append(chunk)
        return b"".join(chunks)


def _api(url: str, **params) -> dict:
    value = json.loads(_read_response(url, params={"format": "json", "formatversion": 2, **params}))
    if not isinstance(value, dict) or value.get("error"):
        raise ValueError("Wikimedia could not resolve this reference.")
    return value


def _name_key(name: str) -> str:
    return " ".join(name.casefold().split())


def _claims(entity: dict, prop: str) -> list:
    return [claim.get("mainsnak", {}).get("datavalue", {}).get("value")
            for claim in entity.get("claims", {}).get(prop, [])
            if claim.get("rank") != "deprecated"]


def _identity(name: str, context: str = "") -> dict:
    hits = _api(_WIKIDATA_API, action="wbsearchentities", search=name,
                language="en", uselang="en", type="item", limit=10).get("search", [])
    ids = [hit["id"] for hit in hits if re.fullmatch(r"Q\d+", str(hit.get("id", "")))]
    if not ids:
        raise ValueError("No matching public-person identity was found.")
    entities = _api(_WIKIDATA_API, action="wbgetentities", ids="|".join(ids),
                    props="labels|aliases|descriptions|claims", languages="en").get("entities", {})
    matches = []
    for entity in entities.values():
        names = [entity.get("labels", {}).get("en", {}).get("value", "")]
        names.extend(alias.get("value", "") for alias in entity.get("aliases", {}).get("en", []))
        humans = [v for v in _claims(entity, "P31") if isinstance(v, dict) and v.get("id") == "Q5"]
        if humans and any(_name_key(n) == _name_key(name) for n in names):
            matches.append(entity)
    if len(matches) > 1 and context:
        # Shared names need independent role/place evidence. Repeated words,
        # the subject's name and generic news wording cannot break a tie.
        words = {word for word in re.findall(r"[^\W\d_]+", context.casefold()) if len(word) > 2}
        words -= set(re.findall(r"[^\W\d_]+", name.casefold())) | _CONTEXT_STOP_WORDS
        scores = [len(words.intersection(re.findall(
            r"[^\W\d_]+", entity.get("descriptions", {}).get("en", {}).get("value", "").casefold())))
            for entity in matches]
        best = max(scores)
        if best >= 2 and scores.count(best) == 1:
            matches = [matches[scores.index(best)]]
    if len(matches) != 1:
        raise ValueError("The name is ambiguous or lacks an exact human identity match.")
    entity = matches[0]
    if not re.fullmatch(r"Q\d+", str(entity.get("id", ""))):
        raise ValueError("The public-person identity is invalid.")
    return entity


def _metadata_text(metadata: dict, key: str) -> str:
    value = str(metadata.get(key, {}).get("value") or "")
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip()[:4000]


def _reference(entity: dict) -> dict:
    photos = [value for value in _claims(entity, "P18")
              if isinstance(value, str) and value.strip() and "|" not in value]
    if not photos:
        raise ValueError("This person has no identified Commons photograph.")
    title = "File:" + photos[0]
    pages = _api(_COMMONS_API, action="query", prop="imageinfo", titles=title,
                 iiprop="url|mime|extmetadata", iiurlwidth=1024).get("query", {}).get("pages", [])
    info = next((page["imageinfo"][0] for page in pages if page.get("imageinfo")), None)
    if not info or info.get("mime") not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("No supported Commons photograph is available.")
    metadata = info.get("extmetadata", {})
    license_name = _metadata_text(metadata, "LicenseShortName")
    if not license_name:
        raise ValueError("The photograph's license information is unavailable.")
    return {
        "wikidata_url": "https://www.wikidata.org/wiki/" + entity["id"],
        "source_url": "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"), safe=":"),
        "image_url": info.get("thumburl") or info.get("url") or "",
        "license": license_name,
        "license_url": _metadata_text(metadata, "LicenseUrl"),
        "attribution": _metadata_text(metadata, "Artist"),
        "credit": _metadata_text(metadata, "Credit"),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def _save_portrait(raw: bytes, target: Path) -> None:
    with Image.open(io.BytesIO(raw)) as image:
        if image.format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("The downloaded reference is not a supported photograph.")
        width, height = image.size
        if width < 64 or height < 64 or width * height > _MAX_IMAGE_PIXELS:
            raise ValueError("The reference photograph has unsuitable dimensions.")
        portrait = ImageOps.exif_transpose(image).convert("RGB")
        portrait.thumbnail((1024, 1024))
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".png.tmp")
        try:
            portrait.save(temp, "PNG")
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)


def resolve_people(people: list, work_dir: Path | str) -> dict:
    """Download up to four named subjects' looks and return characters/provenance.

    The caller merges ``characters`` into the script's existing character list
    before saving it through the normal app character writer. Missing/ambiguous
    people are reported in ``unresolved``; no replacement face is invented.
    """
    work_dir = Path(work_dir)
    result = {"characters": [], "references": [], "unresolved": []}
    seen = set()
    rows = people if isinstance(people, list) else []
    for person in rows[:MAX_PEOPLE]:
        name = str(person.get("name") or "") if isinstance(person, dict) else str(person or "")
        name = " ".join(name.split())[:160]
        if not name or _name_key(name) in seen:
            continue
        seen.add(_name_key(name))
        try:
            context = str(person.get("context") or person.get("description") or "") if isinstance(person, dict) else ""
            entity = _identity(name, context[:1000])
            ref = _reference(entity)
            cid = "news_" + entity["id"]
            filename = cid + ".png"
            _save_portrait(_read_response(ref["image_url"], image=True), work_dir / "characters" / filename)
            canonical = entity.get("labels", {}).get("en", {}).get("value") or name
            result["characters"].append({
                "id": cid, "name": name,
                "aliases": [canonical] if _name_key(canonical) != _name_key(name) else [],
                "description": (f"{name}; match this person's face to the attached source photograph. "
                                f"Photo source: {ref['source_url']}. License: {ref['license']}. "
                                f"Attribution: {ref['attribution'] or ref['credit'] or 'see source page'}."),
                "ref_image": filename, "ref_strength": 1.0, "enabled": True,
            })
            result["references"].append({"name": name, "character_id": cid, **ref})
        except requests.RequestException:
            result["unresolved"].append({"name": name, "reason": "Wikimedia reference lookup was unavailable."})
        except (ValueError, OSError, TypeError, KeyError, AttributeError, Image.DecompressionBombError) as exc:
            result["unresolved"].append({"name": name, "reason": str(exc)[:300]})
    work_dir.mkdir(parents=True, exist_ok=True)
    report = work_dir / "news_people.json"
    temp = report.with_suffix(".json.tmp")
    temp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temp.replace(report)
    return result
