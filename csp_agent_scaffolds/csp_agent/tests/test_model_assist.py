"""app/model_assist.py: the local model only helps the Review queue, within
limits, and this project's Ollama is stopped when the server's memory is high."""
import shutil
import subprocess
import time
import uuid
from datetime import date, datetime

import pymupdf

from app import model_assist as ma
from app.models import CSP, Document, DocumentStatus, ManualReviewQueue, ReviewStatus


def test_server_ram_percent_from_meminfo(tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:       1000000 kB\nMemFree:          100000 kB\nMemAvailable:     250000 kB\n")
    assert ma.server_ram_percent(f) == 75.0
    assert ma.server_ram_percent(tmp_path / "missing") is None


def test_hours_window_over_midnight():
    assert ma.in_hours(datetime(2026, 10, 18, 22), "20-8") and ma.in_hours(datetime(2026, 10, 18, 3), "20-8")
    assert not ma.in_hours(datetime(2026, 10, 18, 12), "20-8")
    assert ma.in_hours(datetime(2026, 10, 18, 12), "10-18") and not ma.in_hours(datetime(2026, 10, 18, 9), "10-18")


def test_only_this_projects_ollama_is_found_and_killed(tmp_path, monkeypatch):
    """A stand-in 'ollama' binary (a copy of bash): found by its path, stopped."""
    fake = tmp_path / "ollama"
    shutil.copy(shutil.which("bash"), fake)
    other = subprocess.Popen([shutil.which("bash"), "-c", "sleep 60; true"])   # not ours: must survive
    mine = subprocess.Popen([str(fake), "-c", "sleep 60; true"])
    try:
        monkeypatch.setattr(ma, "OLLAMA_BIN", str(fake))
        time.sleep(0.2)
        assert ma.own_ollama_pids() == [mine.pid]
        assert ma.stop_ollama("test") == [mine.pid]
        mine.wait(timeout=10)
        assert ma.own_ollama_pids() == [] and other.poll() is None
    finally:
        other.kill()
        mine.kill()


def test_guard_stops_ollama_when_the_server_is_short_of_memory(monkeypatch):
    stopped = []
    monkeypatch.setattr(ma, "own_ollama_pids", lambda: [4242])
    monkeypatch.setattr(ma, "server_ram_percent", lambda *a: 91.0)
    monkeypatch.setattr(ma, "stop_ollama", lambda why: stopped.append(why) or [4242])
    r = ma.guard()
    assert r["ollama"] == "stopped" and "91.0%" in stopped[0]
    monkeypatch.setattr(ma, "server_ram_percent", lambda *a: 40.0)
    monkeypatch.setattr(ma, "ollama_rss_gb", lambda pids: 1.0)
    assert ma.guard()["ollama"] == "running" and len(stopped) == 1


def _waiting_doc(db, tmp_path):
    code = f"9M{uuid.uuid4().int % 1000000:06d}"
    csp = CSP(name="Assist CSP", current_code=code, lookup_code=code, phone="9876543210",
              whatsapp_number="9876543210", is_active_in_calling_sheet=True)
    db.add(csp)
    db.flush()
    f = tmp_path / f"{code}.pdf"
    d = pymupdf.open()
    d.new_page().insert_text((40, 60), "handwritten agreement", fontsize=9)
    d.save(f)
    doc = Document(csp_id=csp.id, document_type="AGREEMENT", sha256=uuid.uuid4().hex,
                   status=DocumentStatus.NEEDS_REVIEW, readability="READABLE", is_current=False, storage_path=str(f))
    db.add(doc)
    db.flush()
    db.add(ManualReviewQueue(document_id=doc.id, status=ReviewStatus.PENDING, reason="The issue date could not be read"))
    db.commit()
    return doc


def test_batch_suggests_within_the_daily_limit_and_decides_nothing(db_session, tmp_path, monkeypatch):
    import app.ai.extraction.deterministic_extractor as ex
    db = db_session
    for row in db.query(ManualReviewQueue).filter_by(status=ReviewStatus.PENDING):
        row.status = ReviewStatus.APPROVED                      # only this test's documents wait
    db.commit()
    docs = [_waiting_doc(db, tmp_path), _waiting_doc(db, tmp_path)]
    calls, stopped = [], []
    monkeypatch.setattr(ma, "MODEL_ASSIST_DAILY_LIMIT", ma.used_today(db, date.today()) + 1)
    monkeypatch.setattr(ma, "too_heavy", lambda: None)
    monkeypatch.setattr(ma, "start_ollama", lambda: True)
    monkeypatch.setattr(ma, "stop_ollama", lambda why: stopped.append(why) or [])
    monkeypatch.setattr(ex, "suggest_with_local_model", lambda data, t: calls.append(t) or {
        "provider": "OLLAMA:test", "is_legible": True, "document_type": "AGREEMENT", "issue_date": "2026-04-15",
        "three_year_validity_stated": True, "issue_date_evidence": "On this day of, 15-04-2026"})
    assert ma.run_batch(db, datetime(2026, 10, 18, 12))["skipped"].startswith("outside")
    r = ma.run_batch(db, datetime.combine(date.today(), datetime.min.time()).replace(hour=22))
    assert r == {"done": 1} and calls == ["AGREEMENT"] and stopped == ["review helper batch finished"]
    db.expire_all()
    first = db.get(Document, docs[0].id)
    s = first.extracted_fields["model_suggestion"]
    assert s["issue_date"] == "2026-04-15" and s["validity_months"] == 36
    assert first.status == DocumentStatus.NEEDS_REVIEW and first.issue_date is None    # a person still decides
    assert "model_suggestion" not in (db.get(Document, docs[1].id).extracted_fields or {})  # over today's limit


def test_batch_does_not_start_when_memory_is_high(db_session, tmp_path, monkeypatch):
    db = db_session
    _waiting_doc(db, tmp_path)
    stopped = []
    monkeypatch.setattr(ma, "MODEL_ASSIST_DAILY_LIMIT", 10 ** 6)
    monkeypatch.setattr(ma, "too_heavy", lambda: "server RAM 88% > 75%")
    monkeypatch.setattr(ma, "start_ollama", lambda: (_ for _ in ()).throw(AssertionError("must not start")))
    monkeypatch.setattr(ma, "stop_ollama", lambda why: stopped.append(why) or [])
    r = ma.run_batch(db, datetime(2026, 10, 18, 22))
    assert r == {"skipped": "server RAM 88% > 75%"} and stopped == ["server RAM 88% > 75%"]
