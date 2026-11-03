"""Pending list by RM (app/rm_pending.py): one live CSV per RM of their CSPs in
Labels 2-4, district by district, with the DC; an RM gets only their own."""
import csv
import io
import uuid

from app import auth
from app.models import CSP, InternalUser
from app.rm_pending import district_from_address
from tests.test_login import CSRF, PUBLIC, _login, world  # noqa: F401  (fixture)
from tests.test_portal_and_api import client  # noqa: F401  (fixture)

HDR = {"X-API-Key": "k", "X-Requested-With": auth.CSRF_HEADER_VALUE}


def test_district_is_read_from_the_address():
    assert district_from_address("Vill Rampur, Post Bihta, Dist. Patna, Bihar 801103") == "Patna"
    assert district_from_address("At+Po- Sakra, District: Muzaffarpur (Bihar)") == "Muzaffarpur"
    assert district_from_address("Mohalla Kazi, DISTT-GAYA 823001") == "Gaya"
    assert district_from_address("Dumra, Dist.  SITAMARHI, Bihar") == "Sitamarhi"
    assert district_from_address("ग्राम सिमरी, जिला बक्सर, बिहार") == "बक्सर"
    assert district_from_address("Near SBI branch, Main Road") is None


def test_one_csv_per_rm_labels_2_to_4_by_district(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    tag = uuid.uuid4().hex[:6]
    rm, dc = InternalUser(name=f"RM {tag}", role="RM"), InternalUser(name=f"DC {tag}", role="DC")
    s.add_all([rm, dc])
    s.flush()
    def csp(n, cat, address=None, district=None, active=True):
        x = CSP(name=f"CSP {n} {tag}", current_code=f"7{n}{int(tag, 16) % 1000000:06d}", rm_id=rm.id, dc_id=dc.id,
                category=cat, kiosk_location=address, district=district, is_active_in_calling_sheet=active)
        s.add(x)
        return x
    csp("A", 3, "Vill X, Dist. Patna, Bihar")
    csp("B", 2, district="Gaya")
    csp("C", 4)
    csp("F", 2, district="  PATNA ")                 # another spelling of the same district
    csp("D", 1, "Dist. Patna")                       # all documents on file: not listed
    csp("E", 3, "Dist. Patna", active=False)         # not on the calling sheet: not listed
    s.commit()
    rm_id = rm.id
    s.close()
    listed = {r["id"]: r for r in c.get("/api/hub/rm-pending", headers=HDR).json()["rms"]}
    assert listed[rm_id]["csps"] == 4
    r = c.get(f"/api/hub/rm-pending/{rm_id}.csv", headers=HDR)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert rows[0][:2] == ["District", "DC"] and rows[0][-1] == "To upload"
    csps = [x for x in rows[1:] if x[2]]
    assert [x[0] for x in csps] == ["Gaya", "Patna", "Patna", "District not given"]   # SITAMARHI-style spellings merged
    assert [x[0] for x in rows[1:] if x[0] and not x[2]] == ["GAYA · 1 CSP", "PATNA · 2 CSPs", "DISTRICT NOT GIVEN · 1 CSP"]
    assert all(x[1] == f"DC {tag}" for x in csps) and "Pending" in csps[0]
    assert c.get("/api/hub/rm-pending/999999.csv", headers=HDR).status_code == 404


def test_an_rm_gets_only_their_own_list(world):
    c, w = world
    _login(c, w["rm"])
    rms = c.get("/api/hub/rm-pending", headers={**PUBLIC, **CSRF}).json()["rms"]
    assert [r["name"] for r in rms] == ["RM One"] and rms[0]["csps"] == 1
    assert c.get(f"/api/hub/rm-pending/{rms[0]['id']}.csv", headers={**PUBLIC, **CSRF}).status_code == 200


def test_the_list_as_a_table_with_rm_district_and_dc_filters(client, monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    s = Session()
    tag = uuid.uuid4().hex[:6]
    rm, dc, dc2 = (InternalUser(name=f"RM {tag}", role="RM"), InternalUser(name=f"DC {tag}", role="DC"),
                   InternalUser(name=f"DC2 {tag}", role="DC"))
    s.add_all([rm, dc, dc2])
    s.flush()
    n = int(tag, 16) % 1000000
    s.add_all([CSP(name=f"Asha {tag}", current_code=f"7A{n:06d}", rm_id=rm.id, dc_id=dc.id, category=3, state="BIHAR",
                   phone="9876500001", kiosk_location="Vill X, Dist. Patna, Bihar", is_active_in_calling_sheet=True),
               CSP(name=f"Binod {tag}", current_code=f"7B{n:06d}", rm_id=rm.id, dc_id=dc2.id, category=2,
                   district="PATNA", is_active_in_calling_sheet=True),
               CSP(name=f"Chandan {tag}", current_code=f"7C{n:06d}", rm_id=rm.id, category=4, district="Gaya",
                   is_active_in_calling_sheet=True),
               CSP(name=f"Done {tag}", current_code=f"7D{n:06d}", rm_id=rm.id, category=1, district="Gaya",
                   is_active_in_calling_sheet=True)])
    s.commit()
    rm_id = rm.id
    s.close()
    get = lambda q="": c.get(f"/api/hub/rm-pending/rows?rm={rm_id}{q}", headers=HDR).json()
    d = get()
    assert d["rm"] == rm_id and d["total"] == 3 and any(r["id"] == rm_id for r in d["rms"])
    assert d["districts"] == [{"name": "Gaya", "csps": 1}, {"name": "Patna", "csps": 2}]
    assert [r["district"] for r in d["rows"]] == ["Gaya", "Patna", "Patna"]
    a = next(r for r in d["rows"] if r["code"] == f"7A{n:06d}")
    assert (a["state"], a["rm"], a["dc"], a["phone"], a["category"]) == ("Bihar", f"RM {tag}", f"DC {tag}", "9876500001", 3)
    assert a["docs"]["AGREEMENT"] == {"word": "Pending", "tone": "warn"} and "AGREEMENT" in a["to_upload"]
    assert next(r for r in d["rows"] if r["district"] == "Gaya")["dc"] == "No DC"
    assert d["pending"]["AGREEMENT"] == 3
    p = get("&district=patna")
    assert p["total"] == 2 and {x["name"] for x in p["dcs"]} == {f"DC {tag}", f"DC2 {tag}"}
    assert p["districts"] == d["districts"]                      # the district list stays whole to switch
    assert get(f"&district=Patna&dc=DC2 {tag}")["total"] == 1
    assert [r["name"] for r in get("&q=asha")["rows"]] == [f"Asha {tag}"]
    assert get("&q=98765000")["total"] == 1


def test_an_rm_sees_only_their_own_rows_whatever_rm_they_ask_for(world):
    c, w = world
    _login(c, w["rm"])
    d = c.get("/api/hub/rm-pending/rows?rm=999999", headers={**PUBLIC, **CSRF}).json()
    assert [r["name"] for r in d["rows"]] == ["Mine"] and d["rms"] == []
    assert d["districts"] == [{"name": "District not given", "csps": 1}]
