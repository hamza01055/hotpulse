from datetime import date

from hotpulse.llm import extract_json
from hotpulse.text import (canonical_url, clean_html, find_dates, guess_deadline, keyword_hits, no_em_dash,
                           normalise_deadline, title_hash, truncate)


def test_canonical_url_strips_tracking_and_www():
    a = canonical_url("https://www.Example.com/news/story/?utm_source=x&id=5#top")
    b = canonical_url("http://example.com/news/story?id=5&fbclid=abc")
    assert a == b == "https://example.com/news/story?id=5"


def test_title_hash_ignores_order_and_stopwords():
    assert title_hash("OpenAI releases the new model") == title_hash("model: OpenAI releases new, the")
    assert title_hash("OpenAI releases the new model") != title_hash("Google releases the new model")


def test_find_dates_formats():
    found = find_dates("Deadline: March 5, 2027 or 6 April 2027 or 2027-05-07")
    assert date(2027, 3, 5) in found and date(2027, 4, 6) in found and date(2027, 5, 7) in found


def test_guess_deadline_needs_context():
    assert guess_deadline("Published on 1 January 2026. Apply by 15 February 2027.", date(2026, 1, 2)) == "2027-02-15"
    assert guess_deadline("Founded on 1 January 2020", date(2026, 1, 2)) is None


def test_normalise_deadline():
    assert normalise_deadline("2027-02-31") is None
    assert normalise_deadline("Jan 9, 2027") == "2027-01-09"
    assert normalise_deadline(None) is None


def test_keyword_hits_phrases():
    assert keyword_hits("A fully funded scholarship", ["fully funded", "scholarship", "job"]) == 2


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here you go: {"score": 80, "reason": "x {y}"} thanks') == {"score": 80, "reason": "x {y}"}
    assert extract_json("no json") is None
    assert extract_json("[1, 2]") is None


def test_no_em_dashes_in_site_text():
    dash = "\u2014"
    assert no_em_dash(f"Nimbus-3 is out {dash} first impressions") == "Nimbus-3 is out, first impressions"
    assert no_em_dash(f"Infinite Potential{dash}Insights") == "Infinite Potential, Insights"
    assert no_em_dash(f"{dash} Jane Doe\nGood point {dash}\nNext") == "Jane Doe\nGood point\nNext"
    assert no_em_dash("2026-10-05, no dashes here") == "2026-10-05, no dashes here"
    assert dash not in clean_html(f"<p>Scholarship {dash} apply now</p>")
    assert truncate(f"A {dash} B", 50) == "A, B"
