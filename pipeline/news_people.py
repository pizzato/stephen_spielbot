"""Acquire real-person appearance references for a selected news idea.

Names must come from the story's subjects, never the posting account's avatar.
Use the best matching Wikidata human and its Commons P18 photograph when
available, with warnings for uncertain identities and text-only fallbacks.
Images stay in the script's ``characters/`` directory; provenance lives in
``news_people.json`` because character normalization drops extra fields.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
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
    name_words = set(re.findall(r"[^\W\d_]+", name.casefold()))
    context_words = {word for word in re.findall(r"[^\W\d_]+", context.casefold()) if len(word) > 2}
    context_words -= name_words | _CONTEXT_STOP_WORDS
    matches = []
    for qid in ids:
        entity = entities.get(qid, {})
        names = [entity.get("labels", {}).get("en", {}).get("value", "")]
        names.extend(alias.get("value", "") for alias in entity.get("aliases", {}).get("en", []))
        humans = [v for v in _claims(entity, "P31") if isinstance(v, dict) and v.get("id") == "Q5"]
        if not humans or entity.get("id") != qid:
            continue
        exact = any(_name_key(n) == _name_key(name) for n in names)
        overlap = max(len(name_words.intersection(re.findall(r"[^\W\d_]+", n.casefold()))) for n in names)
        context_score = len(context_words.intersection(re.findall(
            r"[^\W\d_]+", entity.get("descriptions", {}).get("en", {}).get("value", "").casefold())))
        similarity = max(SequenceMatcher(None, _name_key(name), _name_key(n)).ratio() for n in names)
        matches.append(((exact, overlap, context_score, similarity), entity))
    if not matches:
        raise ValueError("No matching public-person identity was found.")
    # Python's stable sort keeps Wikidata search order as the final tie breaker.
    matches.sort(key=lambda match: match[0], reverse=True)
    score, entity = matches[0]
    exact_matches = [match for match in matches if match[0][0]]
    confident = score[0] and (len(exact_matches) == 1 or (
        score[2] >= 2 and sum(match[0][2] == score[2] for match in exact_matches) == 1))
    if not confident:
        canonical = entity.get("labels", {}).get("en", {}).get("value") or name
        entity = {**entity, "_identity_warning": (
            f"The identity is ambiguous or lacks an exact name match; using the best match, {canonical}. "
            "The character was created and rendering can continue.")}
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
    before saving it through the normal app character writer. Every named person
    gets a character, with a text description if a usable photograph is missing.
    ``unresolved`` retains non-blocking identity/reference warnings for callers.
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
        context = str(person.get("context") or person.get("description") or "")[:1000] if isinstance(person, dict) else ""
        aliases = person.get("aliases", []) if isinstance(person, dict) else []
        aliases = aliases if isinstance(aliases, list) else []
        if isinstance(person, dict) and person.get("source_name"):
            aliases = [*aliases, person["source_name"]]
        aliases = list(dict.fromkeys(alias.strip()[:160] for alias in aliases
                                     if isinstance(alias, str) and alias.strip() and _name_key(alias) != _name_key(name)))
        character = {
            "id": "news_" + hashlib.sha256(_name_key(name).encode()).hexdigest()[:12],
            "name": name, "aliases": aliases,
            "description": f"{name}. {context}".strip(),
            "ref_image": "", "ref_strength": 1.0, "enabled": True,
        }
        warnings = [str(person["identity_warning"])[:300]] if isinstance(person, dict) and person.get("identity_warning") else []
        try:
            entity = _identity(name, context)
            canonical = entity.get("labels", {}).get("en", {}).get("value") or name
            character["id"] = "news_" + entity["id"]
            if _name_key(canonical) not in {_name_key(n) for n in [name, *aliases]}:
                character["aliases"].append(canonical)
            description = entity.get("descriptions", {}).get("en", {}).get("value", "")
            character["description"] = f"{canonical}; {description}. {context}".strip()
            if entity.get("_identity_warning"):
                warnings.append(entity["_identity_warning"])
            ref = _reference(entity)
            cid = character["id"]
            filename = cid + ".png"
            _save_portrait(_read_response(ref["image_url"], image=True), work_dir / "characters" / filename)
            character.update({
                "description": (f"{name}; match this person's face to the attached source photograph. "
                                f"Photo source: {ref['source_url']}. License: {ref['license']}. "
                                f"Attribution: {ref['attribution'] or ref['credit'] or 'see source page'}."),
                "ref_image": filename, "ref_strength": 1.0, "enabled": True,
            })
            if not any(item["character_id"] == cid for item in result["references"]):
                result["references"].append({"name": name, "character_id": cid, **ref})
        except requests.RequestException:
            warnings.append("Wikimedia reference lookup was unavailable.")
        except (ValueError, OSError, TypeError, KeyError, AttributeError, Image.DecompressionBombError) as exc:
            warnings.append(str(exc)[:300])
        if not character["ref_image"]:
            character["description"] += " Use a best-effort likeness of this person based on the name and story context."
            warnings.append("Created a character using a best-effort text description; rendering can continue without a source photograph.")
        existing = next((item for item in result["characters"] if item["id"] == character["id"]), None)
        if existing:
            known_names = {_name_key(n) for n in [existing["name"], *existing["aliases"]]}
            for alias in [name, *character["aliases"]]:
                if _name_key(alias) not in known_names:
                    existing["aliases"].append(alias)
                    known_names.add(_name_key(alias))
            if character["ref_image"] and not existing["ref_image"]:
                existing.update(ref_image=character["ref_image"], description=character["description"])
        else:
            result["characters"].append(character)
        if warnings:
            result["unresolved"].append({"name": name, "reason": " ".join(warnings)})
    work_dir.mkdir(parents=True, exist_ok=True)
    report = work_dir / "news_people.json"
    temp = report.with_suffix(".json.tmp")
    temp.write_text(json.dumps(result, indent=2), encoding="utf-8")
    temp.replace(report)
    return result
