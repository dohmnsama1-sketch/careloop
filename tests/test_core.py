import json

import pytest

from careloop.core import CODE_ALPHABET, CareError, CareLoop, spoken_time
from careloop.store import Store


def test_every_result_has_short_speech(care):
    for result in (care.household(), care.meds_due(), care.adherence(), care.refill_status(), care.agenda("week"),
                   care.alerts(), care.briefing("sara"), care.whats_new("omar")):
        assert isinstance(result["speech"], str) and result["speech"]
        assert len(result["speech"]) < 420, result["speech"]


def test_briefing_orchestrates_everything(care):
    b = care.briefing("sara")
    assert b["greeting"] == "Good morning, Sara"
    assert {r["med"] for r in b["due"]} == {"Lisinopril", "Metformin", "Vitamin D3"}
    assert [r["med"] for r in b["low"]] == ["Lisinopril"]
    assert b["appointments"][0]["driver_name"] == "Omar"
    assert b["new_notes"][0]["author_name"] == "Omar"
    assert "wedding photos" in b["speech"]
    # handoff notes are per person and remembered: a second briefing has nothing new
    assert care.briefing("sara")["new_notes"] == []


def test_medication_matching_by_everyday_name(care):
    assert care.med("blood pressure pill")["name"] == "Lisinopril"
    assert care.med("the sugar pill")["name"] == "Metformin"
    assert care.med("LISINOPRIL")["id"] == "med_1"
    with pytest.raises(CareError, match="couldn't find"):
        care.med("aspirin")


def test_member_aliases_and_unknown_speaker(care):
    assert care.member("Mom")["id"] == "nora"
    with pytest.raises(CareError, match="don't know"):
        care.member("Bob")
    with pytest.raises(CareError, match="Who is speaking"):
        care.member("")


def test_dose_log_matches_slot_and_counts_down_supply(care):
    before = care.med("lisinopril")["pills_on_hand"]
    r = care.dose_log("sara", "blood pressure pill")
    assert r["logged"] and r["dose"]["slot"].startswith("2026-10-23T08:00")
    assert care.med("lisinopril")["pills_on_hand"] == before - 1
    assert "refill" in r["speech"]
    states = {x["med"]: x["state"] for x in care.meds_due()["today"] if x["time"].startswith("2026-10-23T08")}
    assert states["Lisinopril"] == "taken" and states["Metformin"] == "due"


def test_double_dose_guard_blocks_then_alerts_on_explicit_confirmation(care):
    care.dose_log("sara", "lisinopril")
    blocked = care.dose_log("omar", "lisinopril")
    assert blocked["logged"] is False and blocked["blocked"] == "possible_double_dose"
    assert "Sara already logged" in blocked["speech"]
    assert len([d for d in care.d["doses"] if d["med_id"] == "med_1" and d["at"].startswith("2026-10-23")]) == 1
    forced = care.dose_log("omar", "lisinopril", confirm_extra_dose=True)
    assert forced["logged"] and forced["alert"]["severity"] == "high"
    assert set(forced["alert"]["notified"]) == {"sara", "leila"}


def test_guard_allows_dose_after_interval(care, clock):
    care.dose_log("sara", "metformin")
    clock.advance(hours=12)
    r = care.dose_log("omar", "metformin")
    assert r["logged"] and r["dose"]["slot"].startswith("2026-10-23T20:00")


def test_skipped_dose_and_future_time_rejected(care):
    r = care.dose_log("nora", "vitamin d", status="skipped")
    assert r["logged"] and "skipped" in r["speech"]
    with pytest.raises(CareError, match="future"):
        care.dose_log("sara", "metformin", at="2026-10-23T11:00")
    with pytest.raises(CareError):
        care.dose_log("sara", "metformin", status="maybe")


def test_missed_dose_detection(care, clock):
    clock.advance(hours=3)  # 11:05, the 08:00 doses are now missed
    due = care.meds_due()
    assert {r["med"] for r in due["missed"]} == {"Lisinopril", "Metformin", "Vitamin D3"}
    assert "Not logged yet" in due["speech"]


def test_adherence_report(care):
    a = care.adherence(7)
    assert a["overall_pct"] == 93
    worst = min(a["medications"], key=lambda m: m["pct"])
    assert worst["med"] == "Atorvastatin"
    assert len(a["days"]) == 7


def test_refill_two_step_purchase(care):
    q = care.refill_quote("sara", "lisinopril")["quote"]
    assert q["status"] == "open" and len(q["code"]) == 4 and set(q["code"]) <= set(CODE_ALPHABET)
    assert q["quantity"] == 30 and q["total_cents"] == 30 * 12 + 499
    assert care.d["orders"] == []  # a quote never places an order
    o = care.refill_confirm("omar", " ".join(q["code"]).lower())  # spoken back with spaces, lower case
    assert o["order"]["simulated"] and not o["duplicate"]
    again = care.refill_confirm("sara", q["code"])
    assert again["duplicate"] and again["order"]["id"] == o["order"]["id"]
    assert len(care.d["orders"]) == 1
    assert care.refill_status()["low"] == []  # pending order means no further nagging


def test_purchase_permissions_expiry_replacement_and_cap(care, clock):
    q = care.refill_quote("leila", "lisinopril")["quote"]
    with pytest.raises(CareError, match="isn't allowed"):
        care.refill_confirm("leila", q["code"])
    q2 = care.refill_quote("sara", "lisinopril")["quote"]
    with pytest.raises(CareError, match="replaced"):
        care.refill_confirm("sara", q["code"])
    clock.advance(minutes=11)
    with pytest.raises(CareError, match="expired"):
        care.refill_confirm("sara", q2["code"])
    with pytest.raises(CareError, match="don't have a quote"):
        care.refill_confirm("sara", "QQQQ")
    care.d["settings"]["monthly_spend_cap_cents"] = 500
    q3 = care.refill_quote("sara", "lisinopril")["quote"]
    assert q3["status"] == "blocked_over_cap"
    with pytest.raises(CareError, match="spending limit"):
        care.refill_confirm("sara", q3["code"])


def test_checkin_red_flags_and_low_mood(care):
    r = care.checkin("nora", "I fell in the kitchen")
    assert r["alert"]["severity"] == "urgent" and "emergency" in r["speech"]
    assert set(r["alert"]["notified"]) == {"sara", "omar", "leila"}
    low = care.checkin("nora", "I feel lonely today", mood=2)
    assert low["alert"]["severity"] == "normal"
    fine = care.checkin("nora", "Lovely walk this morning", mood=5)
    assert fine["alert"] is None
    with pytest.raises(CareError):
        care.checkin("nora", "ok", mood=9)
    al = care.alerts()["alerts"]
    assert al[0]["severity"] == "urgent"
    ack = care.alert_ack("omar", al[0]["id"])
    assert ack["alert"]["acknowledged_by"] == "omar"
    assert "already handled" in care.alert_ack("sara", al[0]["id"])["speech"]
    assert len(care.alerts()["alerts"]) == 1


def test_notes_and_whats_new_are_per_person(care):
    care.note_add("sara", "Mom skipped lunch, please offer soup at 6")
    news = care.whats_new("omar")
    assert any("soup" in n["text"] for n in news["notes"])
    assert "soup" in news["speech"]
    assert care.whats_new("omar")["notes"] == []
    sara_news = care.whats_new("sara")["notes"]
    assert [n["author"] for n in sara_news] == ["omar"]  # Omar's note is news to Sara; her own note never is
    with pytest.raises(CareError):
        care.note_add("sara", "   ")


def test_appointments(care):
    r = care.appointment_add("sara", "Eye exam", "2026-10-23T15:00", where="Main St", driver="Leila")
    assert "Leila is driving" in r["speech"]
    today = care.agenda("today")
    assert today["appointments"][0]["title"] == "Eye exam"
    assert "Cardiology" in care.agenda("tomorrow")["speech"]
    assert len(care.agenda("week")["appointments"]) == 2


def test_med_add_validation(care):
    r = care.med_add("sara", "Amlodipine", ["20:00"], strength="5 mg")
    assert "8 PM" in r["speech"]
    with pytest.raises(CareError, match="already"):
        care.med_add("sara", "amlodipine", ["08:00"])
    with pytest.raises(CareError, match="isn't a time"):
        care.med_add("sara", "Other", ["25:00"])


def test_state_survives_restart(care, data_file, clock):
    care.note_add("omar", "Pharmacy closes at 6")
    care.dose_log("sara", "lisinopril")
    reloaded = CareLoop(Store(data_file), clock)
    assert any(n["text"] == "Pharmacy closes at 6" for n in reloaded.d["notes"])
    assert reloaded.dose_log("omar", "lisinopril")["blocked"] == "possible_double_dose"
    json.loads(data_file.read_text())  # file is valid JSON (atomic writes)


def test_audit_trail(care):
    care.dose_log("sara", "lisinopril")
    q = care.refill_quote("sara", "lisinopril")["quote"]
    care.refill_confirm("sara", q["code"])
    actions = [a["action"] for a in care.d["audit"]]
    assert actions[-3:] == ["dose_log", "refill_quote", "refill_confirm"]


def test_spoken_time(clock):
    assert spoken_time(clock.parse("08:00")) == "8 AM"
    assert spoken_time(clock.parse("20:30")) == "8:30 PM"
    assert spoken_time(clock.parse("00:05")) == "12:05 AM"


def test_alerts_carry_names_for_cards_and_speech(care):
    care.checkin("nora", "I fell in the hallway")
    a = care.alerts()
    assert a["alerts"][0]["notified_names"] == ["Sara", "Omar", "Leila"]
    assert a["speech"].startswith("1 open alert, from 8:05 AM: Nora said")
    care.checkin("nora", "Feeling low", mood=1)
    assert care.alerts()["speech"].startswith("2 open alerts. Most urgent")


def test_note_addressed_to_reader_is_spoken_naturally(care):
    care.note_add("sara", "For Omar: Mom wants to see the wedding photos tonight")
    assert "Sara left you a note: Mom wants to see the wedding photos tonight" in care.whats_new("omar")["speech"]
