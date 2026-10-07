"""
Follow-up schedules, as data. Changing when a message goes out means
editing these tables, not the engine (app/renewal_engine.py).

Every step says WHO gets a message and WHEN:
  RENEWAL ladders: `days_before` = days before the document's expiry.
  UPLOAD cycle:    `gap` = days after the previous message was SENT
                   (missing / expired / unreadable documents).

Escalation rules (agreed with the business):
  - the CSP always gets at least 2 follow-ups before the RM is involved,
    (counting messages actually sent, not drafts);
  - the RM gets at most 2 messages and the DC at most 1 per cycle;
  - the DC is contacted only after the RM's second message;
  - everything stops as soon as a newer valid document arrives.
MAX_RM_MESSAGES / MAX_DC_MESSAGES are enforced by the engine too, so a bad
edit here can't spam staff.
"""

MAX_RM_MESSAGES = 2
MAX_DC_MESSAGES = 1
MAX_CSP_UPLOAD_MESSAGES = 10

# ------------------------------------------------------------ renewal ladders
AGREEMENT_LADDER = [
    {"stage": "T-60", "days_before": 60, "to": "CSP", "template": "RENEWAL_NOTICE"},
    {"stage": "T-53", "days_before": 53, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-46", "days_before": 46, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-39", "days_before": 39, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-32-RM1", "days_before": 32, "to": "RM", "template": "ESCALATION_RM"},
    {"stage": "T-25", "days_before": 25, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-18-RM2", "days_before": 18, "to": "RM", "template": "ESCALATION_RM"},
    {"stage": "T-11-DC", "days_before": 11, "to": "DC", "template": "ESCALATION_DC"},
    {"stage": "T-7", "days_before": 7, "to": "CSP", "template": "RENEWAL_FINAL"},
    {"stage": "T-3", "days_before": 3, "to": "CSP", "template": "RENEWAL_FINAL"},
    {"stage": "T-1", "days_before": 1, "to": "CSP", "template": "RENEWAL_FINAL"},
]

PVR_12M_LADDER = [
    {"stage": "T-30", "days_before": 30, "to": "CSP", "template": "RENEWAL_NOTICE"},
    {"stage": "T-23", "days_before": 23, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-16", "days_before": 16, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-12-RM1", "days_before": 12, "to": "RM", "template": "ESCALATION_RM"},
    {"stage": "T-9", "days_before": 9, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-6-RM2", "days_before": 6, "to": "RM", "template": "ESCALATION_RM"},
    {"stage": "T-3-DC", "days_before": 3, "to": "DC", "template": "ESCALATION_DC"},
    {"stage": "T-1", "days_before": 1, "to": "CSP", "template": "RENEWAL_FINAL"},
]

PVR_6M_LADDER = [
    {"stage": "T-21", "days_before": 21, "to": "CSP", "template": "RENEWAL_NOTICE"},
    {"stage": "T-17", "days_before": 17, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-13", "days_before": 13, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-10-RM1", "days_before": 10, "to": "RM", "template": "ESCALATION_RM"},
    {"stage": "T-7", "days_before": 7, "to": "CSP", "template": "RENEWAL_FOLLOWUP"},
    {"stage": "T-5-RM2", "days_before": 5, "to": "RM", "template": "ESCALATION_RM"},
    {"stage": "T-3-DC", "days_before": 3, "to": "DC", "template": "ESCALATION_DC"},
    {"stage": "T-1", "days_before": 1, "to": "CSP", "template": "RENEWAL_FINAL"},
]

# -------------------------------------------------------- upload-link cycle
# Used for Cat 2 (missing/unreadable), Cat 3 (expired) and Cat 4 (nothing on
# file). The template is chosen by category at send time.
#
# A batch, not a calendar: step N+1 is drafted only after step N has been
# SENT (or rejected on the dashboard), and only `gap` days after that. While
# a message waits for review nothing new is drafted, so drafts never pile up.
UPLOAD_CYCLE = [
    {"stage": "U-1", "gap": 0, "to": "CSP"},                               # first message
    {"stage": "U-2", "gap": 4, "to": "CSP"},                               # 4 days later
    {"stage": "U-3", "gap": 7, "to": "CSP"},
    {"stage": "U-RM1", "gap": 7, "to": "RM", "template": "ESCALATION_RM"},  # RM calls the CSP
    {"stage": "U-4", "gap": 7, "to": "CSP"},
    {"stage": "U-RM2", "gap": 7, "to": "RM", "template": "ESCALATION_RM"},
    {"stage": "U-DC", "gap": 7, "to": "DC", "template": "ESCALATION_DC"},
]
# After the table ends the CSP keeps getting a weekly reminder until upload,
# capped at MAX_CSP_UPLOAD_MESSAGES CSP messages sent in total.
UPLOAD_WEEKLY_EVERY_DAYS = 7

LADDERS = {
    "AGREEMENT": AGREEMENT_LADDER,
    "PVR_12M": PVR_12M_LADDER,
    "PVR_6M": PVR_6M_LADDER,
    "UPLOAD": UPLOAD_CYCLE,
}


def renewal_ladder_key(document_type: str, validity_months: int | None) -> str | None:
    if document_type == "AGREEMENT":
        return "AGREEMENT"
    if document_type in ("POLICE_VERIFICATION", "CHARACTER_CERTIFICATE"):
        return "PVR_6M" if validity_months == 6 else "PVR_12M"
    return None  # IIBF is lifetime: no renewal ladder


def due_renewal_steps(ladder_key: str, days_left: int) -> list[dict]:
    """Steps whose day has arrived (days_left <= days_before), oldest first.
    The engine sends only the latest unsent one, so a CSP who shows up late
    in the ladder isn't sent a burst of old reminders."""
    return [s for s in LADDERS[ladder_key] if days_left <= s["days_before"]]


def upload_step(index: int) -> dict:
    """The upload cycle's step number `index` (0 = first message)."""
    if index < len(UPLOAD_CYCLE):
        return UPLOAD_CYCLE[index]
    n = index - len(UPLOAD_CYCLE) + 1
    return {"stage": f"U-W{n}", "gap": UPLOAD_WEEKLY_EVERY_DAYS, "to": "CSP"}


# Kept for the old /api/scheduler code paths and tests.
REMINDER_POLICY = [
    {"days": s["days_before"], "recipients": [s["to"]], "channels": ["WHATSAPP", "EMAIL"], "stage": s["stage"]}
    for s in AGREEMENT_LADDER
]


def policy_for_days_left(days_left: int) -> dict | None:
    candidates = [p for p in REMINDER_POLICY if days_left <= p["days"]]
    if not candidates:
        return None
    return min(candidates, key=lambda p: p["days"])
