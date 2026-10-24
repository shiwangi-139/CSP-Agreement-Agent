"""A 'Publish to the web' calling-sheet link is read as the published CSV."""
from app.comms import sheet_source


def test_published_link_becomes_its_csv_address():
    pid = "2PACX-1vTestTestTestTestTestTestTestTestTest"
    assert sheet_source.published_csv_url(f"https://docs.google.com/spreadsheets/d/e/{pid}/pubhtml?gid=123&single=true") \
        == f"https://docs.google.com/spreadsheets/d/e/{pid}/pub?output=csv&single=true&gid=123"
    assert sheet_source.published_csv_url(f"https://docs.google.com/spreadsheets/d/e/{pid}/pub?output=csv") \
        == f"https://docs.google.com/spreadsheets/d/e/{pid}/pub?output=csv"
    assert sheet_source.published_csv_url("https://docs.google.com/spreadsheets/d/1AbcdefghijAbcdefghij12/edit#gid=5") == ""


def test_published_link_is_read_first(monkeypatch):
    monkeypatch.setattr(sheet_source, "CALLING_SHEET_LINK",
                        "https://docs.google.com/spreadsheets/d/e/2PACX-1vTestTestTestTestTestTest/pubhtml?gid=9")
    seen = {}
    monkeypatch.setattr(sheet_source, "_read_published_csv", lambda url: seen.setdefault("url", url) and [["CSP ID"], ["1A850001"]])
    assert sheet_source._read_live() == ("published", [["CSP ID"], ["1A850001"]])
    assert seen["url"].endswith("output=csv&single=true&gid=9")
