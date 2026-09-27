"""Subject identity, reference provenance, and bounded image acquisition."""
import io
import json
from unittest.mock import Mock, patch

import pytest
import requests
from PIL import Image

from pipeline import news_people


def _photo(size=(200, 300)):
    raw = io.BytesIO()
    Image.new("RGB", size, (40, 50, 60)).save(raw, "JPEG")
    return raw.getvalue()


def _claim(value):
    return {"mainsnak": {"datavalue": {"value": value}}}


def _entity(qid="Q123", name="Alex Example", human=True, description=""):
    return {
        "id": qid, "labels": {"en": {"value": name}}, "aliases": {},
        "descriptions": {"en": {"value": description}},
        "claims": {"P31": [_claim({"id": "Q5" if human else "Q43229"})],
                   "P18": [_claim("Alex Example.jpg")]},
    }


def _info(url="https://upload.wikimedia.org/wikipedia/commons/a/ab/Alex_Example.jpg"):
    return {"query": {"pages": [{"title": "File:Alex Example.jpg", "imageinfo": [{
        "mime": "image/jpeg", "thumburl": url,
        "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"},
                        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"},
                        "Artist": {"value": '<a href="/wiki/User:Photographer">A. Photographer</a>'}},
    }]}]}}


def _response(payload, *, image=False, status=200, headers=None):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.status_code = status
    response.headers = {"Content-Type": "image/jpeg" if image else "application/json", **(headers or {})}
    response.iter_content.return_value = [payload if image else json.dumps(payload).encode()]
    return response


def _lookup_responses(*, entity=None, info=None, photo=None):
    entity = entity or _entity()
    return [
        _response({"search": [{"id": entity["id"]}]}),
        _response({"entities": {entity["id"]: entity}}),
        _response(info or _info()),
        _response(photo or _photo(), image=True),
    ]


def test_subject_photo_saved_as_script_character_with_provenance(tmp_path):
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses()) as get:
        result = news_people.resolve_people([{
            "name": "Alex Example", "description": "Australian public figure",
            "avatar_url": "https://untrusted.example/not-the-subject.jpg",
        }], tmp_path)
    assert result["unresolved"] == []
    character = result["characters"][0]
    assert character["ref_image"] == "news_Q123.png"
    assert "CC BY-SA 4.0" in character["description"]
    assert "A. Photographer" in character["description"]
    with Image.open(tmp_path / "characters" / character["ref_image"]) as image:
        assert image.format == "PNG"
        assert image.size == (200, 300)
    ref = result["references"][0]
    assert ref["source_url"] == "https://commons.wikimedia.org/wiki/File:Alex_Example.jpg"
    assert ref["wikidata_url"] == "https://www.wikidata.org/wiki/Q123"
    assert ref["attribution"] == "A. Photographer"
    assert ref["retrieved_at"]
    assert json.loads((tmp_path / "news_people.json").read_text()) == result
    assert not (tmp_path / "characters.json").exists()  # caller owns merging existing cast
    assert all(call.kwargs["allow_redirects"] is False for call in get.call_args_list)
    assert all("untrusted.example" not in call.args[0] for call in get.call_args_list)


def test_nonhuman_match_creates_text_character_without_receiving_its_photo(tmp_path):
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses(entity=_entity(human=False))) as get:
        result = news_people.resolve_people(["Alex Example"], tmp_path)
    assert result["characters"][0]["name"] == "Alex Example"
    assert result["characters"][0]["ref_image"] == ""
    assert result["characters"][0]["enabled"] is True
    assert "identity" in result["unresolved"][0]["reason"]
    assert get.call_count == 2
    assert not (tmp_path / "characters").exists()


def test_inexact_name_uses_best_human_match_with_warning(tmp_path):
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses(entity=_entity(name="Alex Different"))):
        result = news_people.resolve_people(["Alex Example"], tmp_path)
    assert result["characters"][0]["aliases"] == ["Alex Different"]
    assert result["characters"][0]["ref_image"] == "news_Q123.png"
    assert "best match, Alex Different" in result["unresolved"][0]["reason"]


def test_ambiguous_people_use_first_ranked_human_and_warn(tmp_path):
    responses = [_response({"search": [{"id": "Q123"}, {"id": "Q456"}]}),
                 _response({"entities": {"Q456": _entity(qid="Q456"), "Q123": _entity()}}),
                 _response(_info()), _response(_photo(), image=True)]
    with patch.object(news_people.requests, "get", side_effect=responses) as get:
        result = news_people.resolve_people(["Alex Example"], tmp_path)
    assert "ambiguous" in result["unresolved"][0]["reason"]
    assert result["characters"][0]["id"] == "news_Q123"
    assert result["characters"][0]["ref_image"] == "news_Q123.png"
    assert get.call_count == 4


@pytest.mark.parametrize("field", ["context", "description"])
def test_shared_name_resolves_only_with_unique_role_and_place_evidence(tmp_path, field):
    entities = {"Q123": _entity(description="Australian association football player"),
                "Q456": _entity(qid="Q456", description="31st prime minister of Australia")}
    responses = [_response({"search": [{"id": "Q123"}, {"id": "Q456"}]}),
                 _response({"entities": entities}), _response(_info()), _response(_photo(), image=True)]
    with patch.object(news_people.requests, "get", side_effect=responses) as get:
        result = news_people.resolve_people([{"name": "Alex Example", field: "prime minister of Australia"}], tmp_path)
    assert result["unresolved"] == []
    assert result["characters"][0]["id"] == "news_Q456"  # never default to first search hit
    assert "descriptions" in get.call_args_list[1].kwargs["params"]["props"]
    assert result["references"][0]["wikidata_url"] == "https://www.wikidata.org/wiki/Q456"


@pytest.mark.parametrize("context, descriptions", [
    ("Australia Australia", ("prime minister of Australia", "association football player")),
    ("Alex Example is a named public figure in the news", ("Alex Example public figure", "association football player")),
    ("prime minister of Australia", ("prime minister of Australia", "former prime minister of Australia")),
    ("Australian person", ("Australian politician", "Canadian actor")),
])
def test_weak_generic_or_tied_context_creates_character_and_warns(tmp_path, context, descriptions):
    entities = {"Q123": _entity(description=descriptions[0]),
                "Q456": _entity(qid="Q456", description=descriptions[1])}
    responses = [_response({"search": [{"id": "Q123"}, {"id": "Q456"}]}),
                 _response({"entities": entities}), _response(_info()), _response(_photo(), image=True)]
    with patch.object(news_people.requests, "get", side_effect=responses) as get:
        result = news_people.resolve_people([{"name": "Alex Example", "context": context}], tmp_path)
    assert result["characters"][0]["id"] == "news_Q123"
    assert result["characters"][0]["ref_image"] == "news_Q123.png"
    assert "ambiguous" in result["unresolved"][0]["reason"]
    assert get.call_count == 4


def test_exact_alias_is_valid_identity(tmp_path):
    entity = _entity(name="Alexander Example")
    entity["aliases"] = {"en": [{"value": "Alex Example"}]}
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses(entity=entity)):
        result = news_people.resolve_people(["Alex Example"], tmp_path)
    assert result["characters"][0]["aliases"] == ["Alexander Example"]


def test_source_nickname_and_identity_warning_are_preserved(tmp_path):
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses()):
        result = news_people.resolve_people([{
            "name": "Alex Example", "source_name": "Lex", "aliases": ["Lex", "Example"],
            "identity_warning": "Lex is assumed to refer to Alex Example from the story context.",
        }], tmp_path)
    assert result["characters"][0]["aliases"] == ["Lex", "Example"]
    assert "Lex is assumed" in result["unresolved"][0]["reason"]


def test_surname_match_prefers_story_context_to_search_order(tmp_path):
    entities = {"Q123": _entity(name="Bob Example", description="Canadian actor"),
                "Q456": _entity(qid="Q456", name="Alex Example", description="Australian prime minister")}
    responses = [_response({"search": [{"id": "Q123"}, {"id": "Q456"}]}),
                 _response({"entities": entities}), _response(_info()), _response(_photo(), image=True)]
    with patch.object(news_people.requests, "get", side_effect=responses):
        result = news_people.resolve_people([{"name": "Example", "context": "Australian prime minister"}], tmp_path)
    assert result["characters"][0]["id"] == "news_Q456"
    assert result["characters"][0]["aliases"] == ["Alex Example"]
    assert "best match, Alex Example" in result["unresolved"][0]["reason"]


def test_names_resolving_to_same_identity_merge_character_and_reference(tmp_path):
    entity = _entity(name="Alexander Example")
    entity["aliases"] = {"en": [{"value": "Alex Example"}]}
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses(entity=entity) * 2):
        result = news_people.resolve_people(["Alex Example", "Alexander Example"], tmp_path)
    assert len(result["characters"]) == len(result["references"]) == 1
    assert result["characters"][0]["name"] == "Alex Example"
    assert result["characters"][0]["aliases"] == ["Alexander Example"]
    assert result["characters"][0]["ref_image"] == "news_Q123.png"


def test_missing_subject_photo_does_not_use_an_avatar(tmp_path):
    entity = _entity()
    entity["claims"].pop("P18")
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses(entity=entity)) as get:
        result = news_people.resolve_people([{"name": "Alex Example", "avatar_url": "https://x.com/avatar.jpg"}], tmp_path)
    assert result["characters"][0]["id"] == "news_Q123"
    assert result["characters"][0]["ref_image"] == ""
    assert "best-effort likeness" in result["characters"][0]["description"]
    assert "no identified" in result["unresolved"][0]["reason"]
    assert get.call_count == 2


def test_official_wikimedia_thumbnail_cdn_is_allowed(tmp_path):
    url = "https://thumb.wikimedia.org/wikipedia/commons/thumb/a/ab/Alex_Example.jpg/1024px-Alex_Example.jpg"
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses(info=_info(url))) as get:
        result = news_people.resolve_people(["Alex Example"], tmp_path)
    assert result["unresolved"] == []
    assert result["references"][0]["image_url"] == url
    assert get.call_args.args[0] == url
    assert (tmp_path / "characters" / "news_Q123.png").is_file()


@pytest.mark.parametrize("url", [
    "http://upload.wikimedia.org/image.jpg", "https://upload.wikimedia.org.evil.example/image.jpg",
    "https://thumb.wikimedia.org.evil.example/image.jpg",
    "https://upload.wikimedia.org@127.0.0.1/image.jpg", "https://127.0.0.1/image.jpg",
    "https://upload.wikimedia.org:8443/image.jpg", "file:///etc/passwd",
])
def test_image_host_validation_blocks_arbitrary_downloads(tmp_path, url):
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses(info=_info(url))) as get:
        result = news_people.resolve_people(["Alex Example"], tmp_path)
    assert result["characters"][0]["ref_image"] == ""
    assert "allowed Wikimedia host" in result["unresolved"][0]["reason"]
    assert get.call_count == 3


def test_redirects_are_not_followed(tmp_path):
    with patch.object(news_people.requests, "get", return_value=_response({}, status=302)) as get:
        result = news_people.resolve_people(["Alex Example"], tmp_path)
    assert "redirected" in result["unresolved"][0]["reason"]
    assert result["characters"][0]["ref_image"] == ""
    assert get.call_count == 1
    assert get.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("content_length", [None, "9000000"])
def test_image_download_is_bounded_with_and_without_content_length(content_length):
    headers = {"Content-Length": content_length} if content_length else {}
    response = _response(b"a", image=True, headers=headers)
    response.iter_content.return_value = [b"1234", b"5678"]
    with patch.object(news_people, "_MAX_IMAGE_BYTES", 5), \
            patch.object(news_people.requests, "get", return_value=response):
        with pytest.raises(ValueError, match="download limit"):
            news_people._read_response("https://upload.wikimedia.org/image.jpg", image=True)


@pytest.mark.parametrize("photo", [b"not an image", _photo((10, 10))])
def test_invalid_photographs_leave_no_reference_file(tmp_path, photo):
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses(photo=photo)):
        result = news_people.resolve_people(["Alex Example"], tmp_path)
    assert result["characters"][0]["ref_image"] == ""
    assert result["unresolved"]
    assert not (tmp_path / "characters" / "news_Q123.png").exists()


def test_unknown_license_is_reported_without_downloading(tmp_path):
    info = _info()
    info["query"]["pages"][0]["imageinfo"][0]["extmetadata"].pop("LicenseShortName")
    with patch.object(news_people.requests, "get", side_effect=_lookup_responses(info=info)) as get:
        result = news_people.resolve_people(["Alex Example"], tmp_path)
    assert "license" in result["unresolved"][0]["reason"]
    assert result["characters"][0]["ref_image"] == ""
    assert get.call_count == 3


def test_failed_lookup_does_not_prevent_other_people_or_leak_request_details(tmp_path):
    responses = [requests.ConnectionError("private proxy credentials")]+_lookup_responses()
    with patch.object(news_people.requests, "get", side_effect=responses):
        result = news_people.resolve_people(["Missing Person", "Alex Example"], tmp_path)
    assert [character["name"] for character in result["characters"]] == ["Missing Person", "Alex Example"]
    assert result["characters"][0]["ref_image"] == ""
    assert result["characters"][1]["ref_image"] == "news_Q123.png"
    assert result["unresolved"][0]["name"] == "Missing Person"
    assert "Wikimedia reference lookup was unavailable" in result["unresolved"][0]["reason"]
    assert "rendering can continue" in result["unresolved"][0]["reason"]
    assert "private proxy credentials" not in json.dumps(result)


def test_no_identity_creates_stable_text_character_with_story_context(tmp_path):
    with patch.object(news_people.requests, "get", return_value=_response({"search": []})):
        first = news_people.resolve_people([{
            "name": "Missing Person", "context": "Australian politician speaking at a lectern", "aliases": ["MP"],
        }], tmp_path)
        second = news_people.resolve_people([" missing PERSON "], tmp_path)
    character = first["characters"][0]
    assert character["id"] == second["characters"][0]["id"]
    assert character["ref_image"] == ""
    assert character["aliases"] == ["MP"]
    assert "Australian politician speaking at a lectern" in character["description"]
    assert first["references"] == []
    assert "identity" in first["unresolved"][0]["reason"]


def test_people_limit_and_duplicate_names_bound_network_work(tmp_path):
    with patch.object(news_people, "_identity", side_effect=ValueError("No identity")) as identity:
        result = news_people.resolve_people(["Alex Example", " alex EXAMPLE ", "B Person", "C Person", "D Person"], tmp_path)
    assert identity.call_count == 3
    assert len(result["unresolved"]) == 3
    assert len(result["characters"]) == 3
