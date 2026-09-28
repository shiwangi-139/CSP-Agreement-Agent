"""scripts/reorganize_vault.py on the test DB with a fake project folder:
old-layout files move into the vault, stray files go to legacy/, nothing is
deleted."""
import hashlib
import sys
import uuid
from datetime import date

from sqlalchemy.orm import sessionmaker

from app import vault
from app.models import CSP, Document, DocumentStatus
from scripts import reorganize_vault


def _setup(tmp_path, monkeypatch, test_engine):
    project = tmp_path / "project"
    monkeypatch.setattr(vault, "PROJECT_ROOT", project)
    monkeypatch.setattr(vault, "ROOT", project / "storage" / "sbi_kiosk" / "documents")
    monkeypatch.setattr(reorganize_vault, "OLD_ROOTS", [project / "storage", project / "scripts" / "storage"])
    Session = sessionmaker(bind=test_engine)
    monkeypatch.setattr(reorganize_vault, "SessionLocal", Session)
    return project, Session


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["reorganize_vault", *argv])
    reorganize_vault.main()


def test_dry_run_changes_nothing_and_apply_moves_everything(tmp_path, monkeypatch, test_engine):
    project, Session = _setup(tmp_path, monkeypatch, test_engine)
    code = f"6W{uuid.uuid4().int % 1000000:06d}"
    old_dir = project / "storage" / "documents" / code
    old_dir.mkdir(parents=True)
    agr_bytes, pvr_bytes, stray_bytes = (f"%PDF {uuid.uuid4().hex}".encode() for _ in range(3))
    (old_dir / "AGREEMENT_2025_scan_1_abcd1234.pdf").write_bytes(agr_bytes)
    (old_dir / "POLICE_VERIFICATION_2026_x_1234abcd.pdf").write_bytes(pvr_bytes)
    shard = project / "storage" / "ab"
    shard.mkdir(parents=True)
    (shard / "abcdef.pdf").write_bytes(stray_bytes)                 # nobody points at this
    (shard / "copy_of_agreement.pdf").write_bytes(agr_bytes)        # same bytes as a vault file

    s = Session()
    csp = CSP(name="Mukesh Saini", current_code=code, lookup_code=code, is_active_in_calling_sheet=True)
    s.add(csp)
    s.flush()
    sha = lambda b: hashlib.sha256(b).hexdigest()
    agr = Document(csp_id=csp.id, document_type="AGREEMENT", sha256=sha(agr_bytes), status=DocumentStatus.VALID,
                   readability="READABLE", is_current=True, issue_date=date(2025, 3, 1), expiry_date=date(2028, 2, 29),
                   mime_type="application/pdf",
                   storage_path=f"storage/documents/{code}/AGREEMENT_2025_scan_1_abcd1234.pdf")
    pvr = Document(csp_id=csp.id, document_type="POLICE_VERIFICATION", sha256=sha(pvr_bytes),
                   status=DocumentStatus.VALID, readability="READABLE", is_current=True,
                   issue_date=date(2024, 1, 10), expiry_date=date(2025, 1, 9), mime_type="application/pdf",
                   storage_path=str(old_dir / "POLICE_VERIFICATION_2026_x_1234abcd.pdf"))
    s.add_all([agr, pvr])
    s.commit()
    agr_id, pvr_id = agr.id, pvr.id
    s.close()

    _run(monkeypatch)                                                  # dry run
    assert (old_dir / "AGREEMENT_2025_scan_1_abcd1234.pdf").exists() and (shard / "abcdef.pdf").exists()
    s = Session()
    assert s.get(Document, agr_id).storage_path.startswith("storage/documents/")
    s.close()
    assert list((project / "logs").glob("vault_plan_*_dryrun.csv"))

    _run(monkeypatch, "--apply")
    s = Session()
    a, p = s.get(Document, agr_id), s.get(Document, pvr_id)
    assert a.storage_path == f"{code}_MUKESH_SAINI/{code}_AGREEMENT_ACTIVE_2025-03-01_to_2028-02-29.pdf"
    assert p.storage_path == f"{code}_MUKESH_SAINI/{code}_PVR_EXPIRED_2024-01-10_to_2025-01-09.pdf"
    assert vault.abs_path(a.storage_path).read_bytes() == agr_bytes
    s.close()
    legacy = vault.ROOT.parent / "legacy"
    assert (legacy / "unreferenced" / "storage" / "ab" / "abcdef.pdf").read_bytes() == stray_bytes
    assert (legacy / "duplicates" / "storage" / "ab" / "copy_of_agreement.pdf").read_bytes() == agr_bytes
    assert not old_dir.exists() and not shard.exists()                 # emptied folders removed
    assert (vault.ROOT / "INDEX.xlsx").exists()

    _run(monkeypatch, "--apply")                                       # second run: nothing to do
    s = Session()
    assert s.get(Document, agr_id).storage_path == a.storage_path
    s.close()


def test_vault_inside_storage_keeps_cache_and_sweeps_leftover_copies(tmp_path, monkeypatch, test_engine):
    # The rack-server layout: STORAGE_ROOT=storage/documents, OCR cache beside it.
    project, Session = _setup(tmp_path, monkeypatch, test_engine)
    monkeypatch.setattr(vault, "ROOT", project / "storage" / "documents")
    code = f"6V{uuid.uuid4().int % 1000000:06d}"
    good = vault.ROOT / f"{code}_RAJESH_RAJESH" / f"{code}_PVR_ACTIVE_2026-09-07_to_2027-09-07.pdf"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"%PDF live")
    leftover = vault.ROOT / code / "AGREEMENT_2026_old_copy_1f35f7db.pdf"     # an old code-only folder
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"%PDF live")
    cache = project / "storage" / "ocr_cache" / "ab" / "abcd.json"
    cache.parent.mkdir(parents=True)
    cache.write_text("{}")
    s = Session()
    csp = CSP(name="Rajesh Rajesh", current_code=code, lookup_code=code, is_active_in_calling_sheet=True)
    s.add(csp)
    s.flush()
    s.add(Document(csp_id=csp.id, document_type="POLICE_VERIFICATION", sha256=hashlib.sha256(b"%PDF live").hexdigest(),
                   status=DocumentStatus.VALID, readability="READABLE", is_current=True, issue_date=date(2026, 9, 7),
                   expiry_date=date(2027, 9, 7), mime_type="application/pdf", storage_path=vault.rel(good)))
    s.commit()
    s.close()

    _run(monkeypatch, "--apply")
    assert good.exists() and cache.exists()                     # live file and OCR cache untouched
    assert not leftover.exists() and not leftover.parent.exists()
    assert (vault.ROOT.parent / "legacy" / "duplicates" / "documents" / code / leftover.name).exists()
