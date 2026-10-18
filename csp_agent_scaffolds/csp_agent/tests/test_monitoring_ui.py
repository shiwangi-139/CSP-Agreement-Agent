"""The Monitoring page draws every tab from real API output without errors
(run in QuickJS; skipped if it isn't installed), and only admins get the data."""
import json
import re

import pytest
from app import auth
from tests.test_portal_and_api import client, _csp, _form, _pvr_pdf  # noqa: F401
from datetime import date, timedelta

def test_every_monitoring_tab_renders_and_is_admin_only(client, monkeypatch):
    quickjs = pytest.importorskip("quickjs")
    monkeypatch.setattr(auth, "ADMIN_API_KEY", "k")
    c, Session = client
    cid, _, token = _csp(Session)
    issue = date.today() - timedelta(days=30)
    c.post("/api/portal/upload", data=_form(token, iibf_issue_date=issue.isoformat()),
           files={"iibf_file": ("x.pdf", _pvr_pdf(issue), "application/pdf")})
    h = open("app/web/dashboard.html").read()
    js = re.findall(r"<script>([\s\S]*?)</script>", h)[0]
    helpers = js[js.index("// ------------------------------------------------------- monitoring page"):js.index("// ------------------------------------------------------------- views")]
    ctx = quickjs.Context()
    ctx.eval("""const esc=(s)=>String(s??'').replace(/[&<>"]/g,'');const when=(x)=>x?'W':'-';const fmt=(x)=>x||'-';""")
    ctx.eval(helpers)
    for tab in ["status","documents","accuracy","review","experience","messaging","network","quality","use"]:
        r = c.get(f"/api/hub/monitor/{tab}", headers={"X-API-Key": "k"})
        assert r.status_code == 200, (tab, r.text)
        ctx.set("data", json.dumps(r.json()))
        out = ctx.eval(f"MON['{tab}'](JSON.parse(data))")
        assert "undefined" not in out and len(out) > 200, (tab, out[:300])
    assert c.get("/api/hub/monitor/status").status_code in (401, 403)
