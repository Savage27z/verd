"""Pure-function tests — no database needed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from odify_bot import places as gp
from odify_bot.emails import pick_best_email
from odify_bot.exports import _csv_safe, generate_csv, generate_vcard


@pytest.mark.parametrize("url,expected", [
    ("", "none"),
    ("https://www.facebook.com/mybarbershop", "social"),
    ("http://m.facebook.com/x", "social"),
    ("instagram.com/cuts", "social"),
    ("https://linktr.ee/cuts", "social"),
    ("https://wa.me/2348030000000", "social"),
    ("https://cuts.business.site/", "social"),
    ("https://cutsbarbers.com", "website"),
    ("https://notfacebook.com", "website"),
])
def test_classify_website(url, expected):
    assert gp.classify_website(url) == expected


def test_lead_score_prefers_reachable_established_businesses():
    cold = gp.lead_score({"reviews": 0, "rating": None})
    hot = gp.lead_score({"reviews": 250, "rating": 4.7, "phone": "1", "email": "a@b.co", "web_presence": "social"})
    assert cold == 0
    assert hot == 100
    assert gp.lead_score({"reviews": 20, "phone": "1"}) > gp.lead_score({"reviews": 20})


def test_csv_injection_guard():
    assert _csv_safe("=HYPERLINK(\"http://x\")").startswith("'")
    assert _csv_safe("@SUM(A1)").startswith("'")
    assert _csv_safe("-2+3").startswith("'")
    assert _csv_safe("+234 803 000 0000") == "+234 803 000 0000"
    assert _csv_safe("(0803) 000-0000") == "(0803) 000-0000"
    assert _csv_safe(None) == ""
    assert "'=cmd" in generate_csv([{"name": "=cmd"}])


def test_vcard_escapes_and_includes_details():
    v = generate_vcard({"name": "Tunde; Sons, Ltd", "intl_phone": "+234 1", "address": "1 Road\nLagos",
                        "rating": 4.5, "reviews": 12, "maps_url": "https://maps.google.com/?cid=1"})
    assert "FN:Tunde\\; Sons\\, Ltd" in v
    assert "TEL;TYPE=WORK,VOICE:+234 1" in v
    assert "ADR;TYPE=WORK:;;1 Road\\nLagos;;;;" in v
    assert "4.5★ (12 reviews)" in v
    assert v.startswith("BEGIN:VCARD\r\n") and v.endswith("END:VCARD\r\n")


def test_pick_best_email():
    assert pick_best_email(["noreply@x.com", "info@x.com"]) == "info@x.com"
    assert pick_best_email(["other@shop.com", "hello@tundebarbers.ng"], "Tunde Barbers") == "hello@tundebarbers.ng"
    assert pick_best_email(["random@dir.com"], "Tunde Barbers", strict=True) == ""
    assert pick_best_email(["logo@2x.png"]) == ""


# ---- Places API parsing (HTTP mocked) ----

class Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload
        self.ok = 200 <= status < 300
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


def _place(i, website="", status="OPERATIONAL", reviews=5):
    return {"id": f"id{i}", "displayName": {"text": f"Shop {i}"}, "formattedAddress": "Ikeja",
            "websiteUri": website, "nationalPhoneNumber": "0803", "internationalPhoneNumber": "+234 803",
            "rating": 4.2, "userRatingCount": reviews, "googleMapsUri": f"https://maps/{i}",
            "businessStatus": status, "primaryTypeDisplayName": {"text": "Barber shop"}}


@pytest.fixture()
def no_enrich(monkeypatch):
    monkeypatch.setattr(gp, "find_business_email", lambda *a, **k: "")
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    for var in ("PLACES_API_MODE", "PLACES_PROVIDER", "SERPER_API_KEY", "DEFAULT_COUNTRY_CODE"):
        monkeypatch.delenv(var, raising=False)


def test_new_api_filters_and_sorts(monkeypatch, no_enrich):
    pages = [
        {"places": [_place(0, "https://real.com"), _place(1, reviews=2), _place(2, "https://facebook.com/s2", reviews=300),
                    _place(3, status="CLOSED_PERMANENTLY"), _place(4)], "nextPageToken": "t"},
        {"places": [_place(5, reviews=50)]},
    ]
    posts = []

    def post(url, json, headers, timeout):
        posts.append(json)
        assert "places.websiteUri" in headers["X-Goog-FieldMask"]
        return Resp(200, pages[1 if "pageToken" in json else 0])

    monkeypatch.setattr(gp.requests, "post", post)
    leads = gp.search_without_website("barbers", "Ikeja", 10, exclude_place_ids={"id4"})
    assert [lead["place_id"] for lead in leads] == ["id2", "id5", "id1"]
    assert leads[0]["web_presence"] == "social"
    assert posts[1]["pageToken"] == "t"

    no_social = gp.search_without_website("barbers", "Ikeja", 10, include_social=False)
    assert "id2" not in [lead["place_id"] for lead in no_social]


def test_falls_back_to_legacy_when_new_api_disabled(monkeypatch, no_enrich):
    monkeypatch.setattr(gp.requests, "post", lambda *a, **k: Resp(403, {"error": "SERVICE_DISABLED"}))

    def get(url, params, timeout):
        if url.endswith("textsearch/json"):
            return Resp(200, {"status": "OK", "results": [
                {"place_id": "a", "name": "A", "rating": 4, "user_ratings_total": 9, "types": ["hair_care"]},
                {"place_id": "b", "name": "B"},
            ]})
        website = "https://b.com" if params["place_id"] == "b" else ""
        return Resp(200, {"result": {"name": params["place_id"].upper(), "website": website,
                                     "formatted_phone_number": "0803", "url": "https://maps/a"}})

    monkeypatch.setattr(gp.requests, "get", get)
    leads = gp.search_without_website("barbers", "Ikeja", 5)
    assert [lead["place_id"] for lead in leads] == ["a"]
    assert leads[0]["category"] == "Hair Care"


def test_new_api_mode_forced_raises_when_disabled(monkeypatch, no_enrich):
    monkeypatch.setenv("PLACES_API_MODE", "new")
    monkeypatch.setattr(gp.requests, "post", lambda *a, **k: Resp(403, {}))
    with pytest.raises(gp.PlacesError):
        gp.search_without_website("x", "y", 5)


# ---- provider selection + serper.dev ----

def test_provider_selection(monkeypatch, no_enrich):
    assert gp.provider() == "google"
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY")
    with pytest.raises(gp.PlacesError):
        gp.provider()
    monkeypatch.setenv("SERPER_API_KEY", "s")
    assert gp.provider() == "serper"
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    monkeypatch.setenv("PLACES_PROVIDER", "serper")
    assert gp.provider() == "serper"
    monkeypatch.setenv("PLACES_PROVIDER", "bing")
    with pytest.raises(gp.PlacesError):
        gp.provider()


@pytest.mark.parametrize("phone,cc,expected", [
    ("+234 803 123 4567", "", "+234 803 123 4567"),
    ("0803 123 4567", "234", "+234 803 123 4567"),
    ("0803 123 4567", "", ""),
    ("00234 803 123 4567", "234", "+234 803 123 4567"),
    ("(555) 123-4567", "1", ""),
    ("", "234", ""),
])
def test_to_international(phone, cc, expected):
    assert gp.to_international(phone, cc) == expected


def _serper_place(i, website=None, phone="0803 000 0000", reviews=5):
    p = {"title": f"Shop {i}", "address": "Ikeja", "phoneNumber": phone, "rating": 4.1,
         "ratingCount": reviews, "type": "Barber shop", "cid": str(1000 + i), "placeId": f"ChIJ{i}"}
    if website:
        p["website"] = website
    return p


@pytest.fixture()
def serper(monkeypatch, no_enrich):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY")
    monkeypatch.setenv("SERPER_API_KEY", "s-key")
    monkeypatch.setenv("DEFAULT_COUNTRY_CODE", "234")


def test_serper_parses_filters_and_paginates(monkeypatch, serper):
    pages = {
        1: [_serper_place(0, "https://real.ng"), _serper_place(1), _serper_place(2, "https://instagram.com/s2", reviews=90)],
        2: [_serper_place(1), _serper_place(3, phone="+234 701 000 0000")],  # dup of 1 is ignored
        3: [],
    }
    bodies = []

    def post(url, json, headers, timeout):
        assert url == gp.SERPER_API and headers["X-API-KEY"] == "s-key"
        bodies.append(json)
        return Resp(200, {"places": pages[json.get("page", 1)]})

    monkeypatch.setattr(gp.requests, "post", post)
    leads = gp.search_without_website("barbers", "Ikeja", 10)
    assert bodies[0] == {"q": "barbers in Ikeja"} and bodies[1]["page"] == 2
    ids = [lead["place_id"] for lead in leads]
    assert sorted(ids) == ["ChIJ1", "ChIJ2", "ChIJ3"]
    assert ids[0] == "ChIJ2"  # social-only with most reviews scores highest
    by_id = {lead["place_id"]: lead for lead in leads}
    assert by_id["ChIJ1"]["intl_phone"] == "+234 803 000 0000"  # converted via DEFAULT_COUNTRY_CODE
    assert by_id["ChIJ3"]["intl_phone"] == "+234 701 000 0000"
    assert by_id["ChIJ2"]["web_presence"] == "social"
    assert by_id["ChIJ1"]["maps_url"] == "https://maps.google.com/?cid=1001"


def test_serper_errors_are_friendly(monkeypatch, serper):
    monkeypatch.setattr(gp.requests, "post", lambda *a, **k: Resp(403, {"message": "Unauthorized"}))
    with pytest.raises(gp.PlacesError, match="SERPER_API_KEY"):
        gp.search_without_website("x", "y", 5)
    monkeypatch.setattr(gp.requests, "post", lambda *a, **k: Resp(429, {}))
    with pytest.raises(gp.PlacesError, match="credits"):
        gp.search_without_website("x", "y", 5)
