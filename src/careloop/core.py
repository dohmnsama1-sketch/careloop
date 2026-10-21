"""CareLoop domain logic, independent of MCP. Every public method returns a plain dict that always
contains a short `speech` string: what a voice assistant should say out loud. The MCP layer
(`server.py`) only adapts these dicts to tool results, so the logic is fully unit-testable.

Safety rules baked in here (not left to the model):
  * CareLoop never gives medical advice. It records what the family scheduled and what happened.
  * Double-dose guard: a dose logged within a medication's minimum interval is refused unless a
    caregiver explicitly confirms it really happened, and then everyone is alerted.
  * Purchases are two-step: a quote with a short spoken code, then an explicit confirmation by a
    member allowed to buy. Quotes expire, respect a monthly spending cap and confirm idempotently.
  * Red-flag words in a check-in ("fell", "chest pain", ...) raise an urgent alert to every
    caregiver and the reply always points to emergency services.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any

from .clock import Clock
from .pharmacy import Pharmacy, SimulatedPharmacy
from .store import Store


class CareError(ValueError):
    """A user-facing error; the message is safe to speak."""


RED_FLAGS = ("fell", "fall ", "fallen", "chest pain", "can't breathe", "cannot breathe", "short of breath",
             "faint", "passed out", "unconscious", "bleeding", "stroke", "confused", "slurred",
             "severe pain", "dizzy", "help me")
DUE_EARLY = timedelta(minutes=60)     # a dose shows as "due" from 60 min before its time
MISSED_AFTER = timedelta(hours=2)     # ...and as "missed" 2 h after its time
SLOT_MATCH = timedelta(hours=6)       # a logged dose is matched to a scheduled slot within 6 h
CODE_ALPHABET = "ACDEFHJKMNPRTUVWXY3479"  # no 0/O, 1/I/L, 2/Z, 5/S, 6/G, 8/B: easy to say and hear


def spoken_time(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{h} {suffix}" if dt.minute == 0 else f"{h}:{dt.minute:02d} {suffix}"


def spoken_day(dt: datetime, now: datetime) -> str:
    d = (dt.date() - now.date()).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(d, dt.strftime("%A, %B %-d"))


def money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


def group_by_time(rows: list[dict]) -> str:
    """'Lisinopril, Metformin and Vitamin D3 at 8 AM, then Atorvastatin at 8 PM'."""
    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(r["spoken_time"], []).append(r["med"])
    return ", then ".join(f"{join_words(meds)} at {t}" for t, meds in groups.items())


def join_words(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


class CareLoop:
    def __init__(self, store: Store, clock: Clock | None = None, pharmacy: Pharmacy | None = None):
        self.store = store
        self.d = store.data
        self.clock = clock or Clock(self.d["household"].get("timezone") or "UTC")
        self.pharmacy = pharmacy or SimulatedPharmacy()

    # ------------------------------------------------------------------ helpers
    def now(self) -> datetime:
        return self.clock.now()

    def _audit(self, actor: str, action: str, **detail: Any) -> None:
        self.d["audit"].append({"at": self.now().isoformat(), "actor": actor, "action": action, **detail})
        del self.d["audit"][:-500]

    def member(self, who: str | None, *, required: bool = True) -> dict | None:
        members = self.d["household"]["members"]
        if not who:
            if required:
                raise CareError(f"Who is speaking? Household members: {join_words([m['name'] for m in members])}.")
            return None
        key = who.strip().lower()
        for m in members:
            if key in (m["id"].lower(), m["name"].lower()) or key in [a.lower() for a in m.get("aliases", [])]:
                return m
        raise CareError(f"I don't know {who}. Household members: {join_words([m['name'] for m in members])}.")

    def recipient(self) -> dict:
        r = self.d["household"].get("recipient")
        if not r:
            raise CareError("No care recipient is set up yet.")
        return self.member(r)

    def med(self, query: str) -> dict:
        q = query.strip().lower()
        active = [m for m in self.d["meds"] if m.get("active", True)]
        for m in active:
            if q in (m["id"].lower(), m["name"].lower()):
                return m
        hits = [m for m in active if any(w in q or q in w for w in [m["name"].lower(), *[a.lower() for a in m.get("aliases", [])]])]
        if len(hits) == 1:
            return hits[0]
        names = join_words([m["name"] for m in (hits or active)])
        if hits:
            raise CareError(f"Which one did you mean: {names}?")
        raise CareError(f"I couldn't find a medication called {query}. On the list: {names}.")

    def _slots(self, med: dict, day: datetime) -> list[datetime]:
        out = []
        for t in med["times"]:
            h, m = (int(x) for x in t.split(":"))
            out.append(day.replace(hour=h, minute=m, second=0, microsecond=0))
        return out

    def _taken(self, med_id: str) -> list[dict]:
        return [x for x in self.d["doses"] if x["med_id"] == med_id and x["status"] == "taken"]

    def _slot_status(self, med: dict, slot: datetime) -> dict | None:
        for x in self.d["doses"]:
            if x["med_id"] == med["id"] and x.get("slot") == slot.isoformat():
                return x
        return None

    def _today_schedule(self) -> list[dict]:
        now = self.now()
        rows = []
        for med in self.d["meds"]:
            if not med.get("active", True):
                continue
            for slot in self._slots(med, now):
                log = self._slot_status(med, slot)
                if log:
                    state = log["status"]
                elif now >= slot + MISSED_AFTER:
                    state = "missed"
                elif now >= slot - DUE_EARLY:
                    state = "due"
                else:
                    state = "upcoming"
                rows.append({"med_id": med["id"], "med": med["name"], "strength": med.get("strength", ""),
                             "instructions": med.get("instructions", ""), "time": slot.isoformat(),
                             "spoken_time": spoken_time(slot), "state": state,
                             "logged_by": log.get("logged_by") if log else None,
                             "logged_at": log.get("at") if log else None})
        rows.sort(key=lambda r: r["time"])
        return rows

    # ------------------------------------------------------------------ household
    def household(self) -> dict:
        h = self.d["household"]
        rec = self.recipient()
        caregivers = [m["name"] for m in h["members"] if m.get("role") == "caregiver"]
        return {"household": h["name"], "timezone": h.get("timezone"), "recipient": rec["name"],
                "members": h["members"], "medications": [m["name"] for m in self.d["meds"] if m.get("active", True)],
                "speech": f"{h['name']}: caring for {rec['name']}, with {join_words(caregivers)} as caregivers."}

    # ------------------------------------------------------------------ meds
    def med_add(self, actor: str, name: str, times: list[str], strength: str = "", instructions: str = "",
                aliases: list[str] | None = None, pills_on_hand: int = 30, pills_per_dose: int = 1,
                min_interval_hours: float = 4, refill_threshold_days: int = 7, item_id: str | None = None) -> dict:
        who = self.member(actor)
        if not times:
            raise CareError("Tell me at least one time of day, like 08:00.")
        for t in times:
            try:
                h, m = (int(x) for x in t.split(":"))
                assert 0 <= h < 24 and 0 <= m < 60
            except Exception:
                raise CareError(f"{t} isn't a time I understand. Use 24-hour time like 08:00 or 20:30.")
        if any(m["name"].lower() == name.strip().lower() and m.get("active", True) for m in self.d["meds"]):
            raise CareError(f"{name} is already on the list.")
        with self.store.lock:
            med = {"id": self.store.next_id("med"), "name": name.strip(), "strength": strength,
                   "instructions": instructions, "aliases": aliases or [], "times": sorted(times),
                   "pills_on_hand": pills_on_hand, "pills_per_dose": pills_per_dose,
                   "min_interval_hours": min_interval_hours, "refill_threshold_days": refill_threshold_days,
                   "item_id": item_id or name.strip().lower().replace(" ", "-"), "active": True,
                   "added_by": who["id"]}
            self.d["meds"].append(med)
            self._audit(who["id"], "med_add", med=med["id"])
            self.store.save()
        when = join_words([spoken_time(self.clock.parse(t)) for t in med["times"]])
        return {"medication": med, "speech": f"Added {med['name']} at {when}."}

    def meds_due(self, window_hours: float = 3) -> dict:
        now = self.now()
        sched = self._today_schedule()
        horizon = now + timedelta(hours=window_hours)
        due = [r for r in sched if r["state"] == "due" or (r["state"] == "upcoming" and datetime.fromisoformat(r["time"]) <= horizon)]
        missed = [r for r in sched if r["state"] == "missed"]
        rec = self.recipient()["name"]
        parts = []
        if due:
            parts.append(f"Due for {rec}: " + group_by_time(due) + ".")
        if missed:
            parts.append("Not logged yet: " + join_words([f"{r['med']} from {r['spoken_time']}" for r in missed]) + ".")
        if not parts:
            nxt = next((r for r in sched if r["state"] == "upcoming"), None)
            parts.append(f"Nothing due right now. Next is {nxt['med']} at {nxt['spoken_time']}." if nxt
                         else f"All of {rec}'s doses for today are done.")
        return {"now": now.isoformat(), "due": due, "missed": missed, "today": sched, "speech": " ".join(parts)}

    def dose_log(self, actor: str, medication: str, status: str = "taken", at: str | None = None,
                 confirm_extra_dose: bool = False) -> dict:
        who = self.member(actor)
        med = self.med(medication)
        if status not in ("taken", "skipped"):
            raise CareError("Status must be taken or skipped.")
        when = self.clock.parse(at) if at else self.now()
        if when > self.now() + timedelta(minutes=5):
            raise CareError("That time is in the future. Log a dose after it's taken.")
        rec = self.recipient()["name"]
        if status == "taken":
            last = max(self._taken(med["id"]), key=lambda x: x["at"], default=None)
            if last:
                gap = when - datetime.fromisoformat(last["at"])
                if timedelta(0) <= gap < timedelta(hours=med["min_interval_hours"]) and not confirm_extra_dose:
                    by = self.member(last["logged_by"])["name"]
                    t = spoken_time(datetime.fromisoformat(last["at"]))
                    return {"logged": False, "blocked": "possible_double_dose", "previous": last,
                            "speech": (f"Hold on: {by} already logged {rec}'s {med['name']} at {t}. "
                                       f"I did not log another dose. If a second dose was really taken, say "
                                       f"'confirm extra dose' and I'll alert the family, and please check with "
                                       f"a pharmacist or doctor.")}
        # match to the nearest open slot (yesterday or today)
        slots = self._slots(med, when) + self._slots(med, when - timedelta(days=1))
        open_slots = [s for s in slots if not self._slot_status(med, s) and abs(when - s) <= SLOT_MATCH]
        slot = min(open_slots, key=lambda s: abs(when - s), default=None)
        with self.store.lock:
            entry = {"id": self.store.next_id("dose"), "med_id": med["id"], "status": status,
                     "at": when.isoformat(), "slot": slot.isoformat() if slot else None, "logged_by": who["id"],
                     "extra": bool(confirm_extra_dose)}
            self.d["doses"].append(entry)
            alert = None
            if status == "taken":
                med["pills_on_hand"] = max(0, med["pills_on_hand"] - med["pills_per_dose"])
                if confirm_extra_dose:
                    alert = self._raise_alert("extra_dose", "high",
                                              f"{who['name']} confirmed an extra dose of {med['name']} for {rec} at "
                                              f"{spoken_time(when)}, inside the {med['min_interval_hours']:g}-hour minimum gap.",
                                              who["id"])
            self._audit(who["id"], "dose_log", med=med["id"], status=status, extra=bool(confirm_extra_dose))
            self.store.save()
        verb = "took" if status == "taken" else "skipped"
        speech = f"Got it: {rec} {verb} {med['name']} at {spoken_time(when)}."
        if alert:
            speech += " I've alerted the family about the extra dose. Please check with a pharmacist or doctor."
        low = self._refill_row(med)
        if status == "taken" and low["needs_refill"]:
            speech += f" Heads up, about {low['days_left']} days of {med['name']} left. Want me to get a refill quote?"
        return {"logged": True, "dose": entry, "alert": alert, "refill": low, "speech": speech}

    def adherence(self, days: int = 7) -> dict:
        days = max(1, min(int(days), 31))
        now = self.now()
        out_days = []
        per_med: dict[str, list[int]] = {}
        for i in range(days - 1, -1, -1):
            day = now - timedelta(days=i)
            sched = taken = 0
            for med in self.d["meds"]:
                if not med.get("active", True):
                    continue
                for slot in self._slots(med, day):
                    if slot + MISSED_AFTER > now:
                        continue  # not judged yet
                    sched += 1
                    log = self._slot_status(med, slot)
                    ok = 1 if log and log["status"] == "taken" else 0
                    taken += ok
                    per_med.setdefault(med["name"], [0, 0])
                    per_med[med["name"]][0] += ok
                    per_med[med["name"]][1] += 1
            out_days.append({"date": day.date().isoformat(), "weekday": day.strftime("%a"), "scheduled": sched,
                             "taken": taken, "pct": round(100 * taken / sched) if sched else None})
        tot_s = sum(d["scheduled"] for d in out_days)
        tot_t = sum(d["taken"] for d in out_days)
        pct = round(100 * tot_t / tot_s) if tot_s else None
        meds = [{"med": k, "taken": v[0], "scheduled": v[1], "pct": round(100 * v[0] / v[1]) if v[1] else None}
                for k, v in per_med.items()]
        worst = min(meds, key=lambda m: m["pct"] if m["pct"] is not None else 101, default=None)
        rec = self.recipient()["name"]
        speech = (f"Over the last {days} days {rec} took {tot_t} of {tot_s} scheduled doses, {pct} percent."
                  if tot_s else "There isn't enough history yet.")
        if worst and worst["pct"] is not None and worst["pct"] < 100:
            speech += f" Most missed: {worst['med']}, at {worst['pct']} percent."
        return {"days": out_days, "medications": meds, "overall_pct": pct, "speech": speech}

    # ------------------------------------------------------------------ refills & purchasing
    def _refill_row(self, med: dict) -> dict:
        per_day = len(med["times"]) * med["pills_per_dose"]
        days_left = med["pills_on_hand"] // per_day if per_day else 999
        pending = [o for o in self.d["orders"] if o["med_id"] == med["id"] and o["status"] == "placed"]
        return {"med_id": med["id"], "med": med["name"], "pills_on_hand": med["pills_on_hand"],
                "per_day": per_day, "days_left": days_left,
                "runs_out": (self.now() + timedelta(days=days_left)).date().isoformat(),
                "needs_refill": days_left <= med["refill_threshold_days"] and not pending,
                "order_pending": pending[-1]["id"] if pending else None}

    def refill_status(self) -> dict:
        rows = [self._refill_row(m) for m in self.d["meds"] if m.get("active", True)]
        low = [r for r in rows if r["needs_refill"]]
        if low:
            speech = "Running low: " + join_words([f"{r['med']}, about {r['days_left']} days left" for r in low]) + \
                     ". I can get a refill quote."
        else:
            speech = "Every medication has more than a week's supply or a refill on the way."
        return {"medications": rows, "low": low, "speech": speech}

    def _month_spend(self) -> int:
        ym = self.now().strftime("%Y-%m")
        return sum(o["total_cents"] for o in self.d["orders"] if o["placed_at"].startswith(ym) and o["status"] != "cancelled")

    def refill_quote(self, actor: str, medication: str, days_supply: int = 30) -> dict:
        who = self.member(actor)
        med = self.med(medication)
        days_supply = max(7, min(int(days_supply), 90))
        qty = days_supply * len(med["times"]) * med["pills_per_dose"]
        price = self.pharmacy.price(med["item_id"])
        subtotal = qty * price.unit_cents
        total = subtotal + price.delivery_cents
        cap = self.d["settings"]["monthly_spend_cap_cents"]
        spent = self._month_spend()
        over = spent + total > cap
        now = self.now()
        with self.store.lock:
            # one live quote per medication; a new quote replaces the old one
            for code, q in list(self.d["quotes"].items()):
                if q["med_id"] == med["id"] and q["status"] == "open":
                    q["status"] = "replaced"
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
            while code in self.d["quotes"]:
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(4))
            quote = {"code": code, "med_id": med["id"], "med": med["name"], "strength": med.get("strength", ""),
                     "quantity": qty, "days_supply": days_supply, "unit_cents": price.unit_cents,
                     "subtotal_cents": subtotal, "delivery_cents": price.delivery_cents, "total_cents": total,
                     "total": money(total), "pharmacy": self.pharmacy.name, "requested_by": who["id"],
                     "created": now.isoformat(),
                     "expires": (now + timedelta(minutes=self.d["settings"]["quote_ttl_minutes"])).isoformat(),
                     "status": "blocked_over_cap" if over else "open",
                     "month_spent_cents": spent, "cap_cents": cap}
            self.d["quotes"][code] = quote
            self._audit(who["id"], "refill_quote", code=code, total=total)
            self.store.save()
        spelled = " ".join(code)
        if over:
            speech = (f"A {days_supply}-day refill of {med['name']} is {money(total)}, but that would pass the "
                      f"monthly limit of {money(cap)} ({money(spent)} already spent). I can't order it unless "
                      f"a caregiver raises the limit.")
        else:
            speech = (f"A {days_supply}-day refill of {med['name']}, {qty} tablets, is {money(total)} including "
                      f"delivery. To order, say: confirm order {spelled}.")
        return {"quote": quote, "speech": speech}

    def refill_confirm(self, actor: str, code: str) -> dict:
        who = self.member(actor)
        code = "".join(ch for ch in code.upper() if ch.isalnum())
        q = self.d["quotes"].get(code)
        if not q:
            raise CareError("I don't have a quote with that code. Ask me for a new refill quote.")
        if q["status"] == "ordered":  # idempotent: confirming twice returns the same order
            order = next(o for o in self.d["orders"] if o["quote"] == code)
            return {"order": order, "duplicate": True,
                    "speech": f"That order is already placed. Confirmation {order['confirmation']}, arriving {spoken_day(datetime.fromisoformat(order['eta']), self.now())}."}
        if not who.get("can_purchase"):
            raise CareError(f"{who['name']} isn't allowed to place orders. Ask a caregiver to confirm.")
        if q["status"] == "blocked_over_cap":
            raise CareError("That quote is over the monthly spending limit, so I can't place it.")
        if q["status"] != "open":
            raise CareError("That quote was replaced by a newer one. Use the latest code.")
        if self.now() > datetime.fromisoformat(q["expires"]):
            q["status"] = "expired"
            self.store.save()
            raise CareError("That quote expired. Ask me for a fresh refill quote; prices can change.")
        if self._month_spend() + q["total_cents"] > self.d["settings"]["monthly_spend_cap_cents"]:
            raise CareError("Placing this would pass the monthly spending limit.")
        med = self.med(q["med_id"])
        placed = self.pharmacy.place_order(med["item_id"], q["quantity"], q["total_cents"], self.now())
        with self.store.lock:
            order = {"id": self.store.next_id("order"), "quote": code, "med_id": med["id"], "med": med["name"],
                     "quantity": q["quantity"], "total_cents": q["total_cents"], "total": q["total"],
                     "placed_by": who["id"], "placed_at": self.now().isoformat(), "status": "placed", **placed}
            self.d["orders"].append(order)
            q["status"] = "ordered"
            self._audit(who["id"], "refill_confirm", code=code, order=order["id"], total=q["total_cents"])
            self.store.save()
        eta = datetime.fromisoformat(order["eta"])
        return {"order": order, "duplicate": False,
                "speech": f"Ordered. {q['quantity']} tablets of {med['name']} for {q['total']}, arriving "
                          f"{spoken_day(eta, self.now())} around {spoken_time(eta)}. Confirmation {order['confirmation']}."}

    # ------------------------------------------------------------------ handoff, check-ins, alerts
    def _raise_alert(self, kind: str, severity: str, message: str, by: str) -> dict:
        alert = {"id": self.store.next_id("alert"), "at": self.now().isoformat(), "kind": kind,
                 "severity": severity, "message": message, "raised_by": by, "acknowledged_by": None,
                 "notified": [m["id"] for m in self.d["household"]["members"] if m.get("role") == "caregiver" and m["id"] != by]}
        self.d["alerts"].append(alert)
        return alert

    def note_add(self, actor: str, text: str) -> dict:
        who = self.member(actor)
        text = text.strip()
        if not text:
            raise CareError("What should the note say?")
        with self.store.lock:
            note = {"id": self.store.next_id("note"), "author": who["id"], "text": text[:1000], "at": self.now().isoformat()}
            self.d["notes"].append(note)
            self._audit(who["id"], "note_add", note=note["id"])
            self.store.save()
        others = [m["name"] for m in self.d["household"]["members"] if m["id"] != who["id"] and m.get("role") == "caregiver"]
        return {"note": note, "speech": f"Saved. {join_words(others) or 'The family'} will hear it in their next update."}

    def whats_new(self, actor: str, mark_seen: bool = True) -> dict:
        who = self.member(actor)
        since = self.d["seen"].get(who["id"]) or (self.now() - timedelta(hours=24)).isoformat()
        since_dt = datetime.fromisoformat(since)
        def fresh(x): return datetime.fromisoformat(x["at"]) > since_dt
        notes = [n | {"author_name": self.member(n["author"])["name"]} for n in self.d["notes"] if fresh(n) and n["author"] != who["id"]]
        doses = [x for x in self.d["doses"] if fresh(x) and x["logged_by"] != who["id"]]
        checkins = [c for c in self.d["checkins"] if fresh(c) and c["by"] != who["id"]]
        alerts = [a for a in self.d["alerts"] if fresh(a) and not a["acknowledged_by"]]
        orders = [o for o in self.d["orders"] if datetime.fromisoformat(o["placed_at"]) > since_dt and o["placed_by"] != who["id"]]
        parts = []
        for n in notes[-3:]:
            prefix = f"For {who['name']}:"
            if n["text"].lower().startswith(prefix.lower()):
                parts.append(f"{n['author_name']} left you a note: {n['text'][len(prefix):].strip()}")
            else:
                parts.append(f"{n['author_name']} said: {n['text']}")
        if doses:
            by: dict[str, int] = {}
            for x in doses:
                name = self.member(x["logged_by"])["name"]
                by[name] = by.get(name, 0) + 1
            parts.append(join_words([f"{k} logged {v} dose{'s' if v != 1 else ''}" for k, v in by.items()]))
        if orders:
            parts.append(join_words([f"{self.member(o['placed_by'])['name']} ordered {o['med']}" for o in orders]))
        if alerts:
            parts.append(f"{len(alerts)} open alert{'s' if len(alerts) != 1 else ''}")
        speech = ("Since you last checked: " + ". ".join(parts) + ".") if parts else "Nothing new since you last checked."
        if mark_seen:
            self.d["seen"][who["id"]] = self.now().isoformat()
            self.store.save()
        return {"since": since, "notes": notes, "doses": doses, "checkins": checkins, "alerts": alerts,
                "orders": orders, "speech": speech}

    def checkin(self, actor: str, text: str, mood: int | None = None) -> dict:
        who = self.member(actor)
        if mood is not None and not 1 <= mood <= 5:
            raise CareError("Mood is from 1, very low, to 5, great.")
        low = f" {text.lower()} "
        flags = [f for f in RED_FLAGS if f in low]
        with self.store.lock:
            entry = {"id": self.store.next_id("checkin"), "by": who["id"], "at": self.now().isoformat(),
                     "text": text[:1000], "mood": mood, "flags": flags}
            self.d["checkins"].append(entry)
            alert = None
            if flags:
                alert = self._raise_alert("red_flag", "urgent", f"{who['name']} said \"{text[:200]}\"", who["id"])
            elif mood is not None and mood <= 2:
                alert = self._raise_alert("low_mood", "normal", f"{who['name']} reported a low mood ({mood}/5): \"{text[:200]}\"", who["id"])
            self._audit(who["id"], "checkin", flags=flags, mood=mood)
            self.store.save()
        if flags:
            names = join_words([self.member(i)["name"] for i in alert["notified"]]) or "the family"
            speech = (f"I've alerted {names} right away. If this is an emergency, call your local emergency "
                      f"number now.")
        elif alert:
            speech = "Thank you for telling me. I've let the family know so someone can check in with you."
        else:
            speech = "Thanks, I've noted that for the family."
        return {"checkin": entry, "alert": alert, "speech": speech}

    def alerts(self, include_acknowledged: bool = False) -> dict:
        rows = [a for a in self.d["alerts"] if include_acknowledged or not a["acknowledged_by"]]
        rows = sorted(rows, key=lambda a: ({"urgent": 0, "high": 1}.get(a["severity"], 2), a["at"]))
        rows = [a | {"notified_names": [self.member(i)["name"] for i in a["notified"]],
                     "acknowledged_by_name": self.member(a["acknowledged_by"])["name"] if a["acknowledged_by"] else None}
                for a in rows]
        if rows:
            top = rows[0]
            told = join_words(top["notified_names"])
            when = spoken_time(datetime.fromisoformat(top["at"]))
            head = (f"1 open alert, from {when}: " if len(rows) == 1
                    else f"{len(rows)} open alerts. Most urgent, from {when}: ")
            speech = (head + top["message"] + "."
                      + (f" {told} {'were' if len(top['notified']) > 1 else 'was'} notified." if told else "")
                      + " Say 'I'm on it' to let the others know.")
        else:
            speech = "No open alerts."
        return {"alerts": rows, "speech": speech}

    def alert_ack(self, actor: str, alert_id: str) -> dict:
        who = self.member(actor)
        a = next((a for a in self.d["alerts"] if a["id"] == alert_id), None)
        if not a:
            raise CareError(f"No alert with id {alert_id}.")
        if a["acknowledged_by"]:
            return {"alert": a, "speech": f"{self.member(a['acknowledged_by'])['name']} already handled that one."}
        with self.store.lock:
            a["acknowledged_by"] = who["id"]
            a["acknowledged_at"] = self.now().isoformat()
            self._audit(who["id"], "alert_ack", alert=alert_id)
            self.store.save()
        return {"alert": a, "speech": f"Marked as handled by {who['name']}. The others will see that."}

    # ------------------------------------------------------------------ appointments
    def appointment_add(self, actor: str, title: str, when: str, where: str = "", driver: str | None = None,
                        notes: str = "") -> dict:
        who = self.member(actor)
        dt = self.clock.parse(when)
        drv = self.member(driver)["id"] if driver else None
        with self.store.lock:
            appt = {"id": self.store.next_id("appt"), "title": title.strip(), "when": dt.isoformat(), "where": where,
                    "driver": drv, "notes": notes, "created_by": who["id"]}
            self.d["appointments"].append(appt)
            self._audit(who["id"], "appointment_add", appt=appt["id"])
            self.store.save()
        speech = f"Added {appt['title']} {spoken_day(dt, self.now())} at {spoken_time(dt)}."
        if drv:
            speech += f" {self.member(drv)['name']} is driving."
        return {"appointment": appt, "speech": speech}

    def agenda(self, day: str = "today") -> dict:
        now = self.now()
        key = day.strip().lower()
        if key in ("today", ""):
            target = now.date()
        elif key == "tomorrow":
            target = (now + timedelta(days=1)).date()
        elif key in ("week", "this week", "next 7 days"):
            target = None
        else:
            target = self.clock.parse(day).date()
        def ok(a):
            d = datetime.fromisoformat(a["when"])
            return d.date() == target if target else now.date() <= d.date() <= (now + timedelta(days=7)).date()
        rows = sorted([a for a in self.d["appointments"] if ok(a)], key=lambda a: a["when"])
        rows = [a | {"spoken": f"{a['title']} {spoken_day(datetime.fromisoformat(a['when']), now)} at "
                               f"{spoken_time(datetime.fromisoformat(a['when']))}",
                     "spoken_short": f"{a['title']} at {spoken_time(datetime.fromisoformat(a['when']))}",
                     "when_label": f"{spoken_day(datetime.fromisoformat(a['when']), now).capitalize()} · {spoken_time(datetime.fromisoformat(a['when']))}",
                     "driver_name": self.member(a["driver"])["name"] if a.get("driver") else None} for a in rows]
        label = "this week" if target is None else spoken_day(datetime.combine(target, now.timetz()), now)
        if rows:
            key_name = "spoken" if target is None else "spoken_short"
            speech = f"{label[0].upper() + label[1:]}: " + join_words(
                [r[key_name] + (f", {r['driver_name']} driving" if r["driver_name"] else "") for r in rows]) + "."
        else:
            speech = f"No appointments {label}."
        return {"appointments": rows, "speech": speech}

    # ------------------------------------------------------------------ orchestration
    def briefing(self, actor: str) -> dict:
        """One call that orchestrates everything a caregiver needs: doses, refills, agenda, alerts, handoffs."""
        who = self.member(actor)
        now = self.now()
        due = self.meds_due(window_hours=3)
        refills = self.refill_status()
        today = self.agenda("today")
        tomorrow = self.agenda("tomorrow")
        al = self.alerts()
        new = self.whats_new(actor, mark_seen=True)
        rec = self.recipient()["name"]
        hello = "Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18 else "Good evening"
        parts = [f"{hello}, {who['name']}."]
        urgent = [a for a in al["alerts"] if a["severity"] in ("urgent", "high")]
        if urgent:
            parts.append(f"First, an alert: {urgent[0]['message']}")
        if due["missed"]:
            parts.append(f"{rec}'s {join_words([r['med'] for r in due['missed']])} from earlier isn't logged.")
        if due["due"]:
            parts.append(f"Due for {rec}: {group_by_time(due['due'])}.")
        if today["appointments"]:
            parts.append(today["speech"])
        elif tomorrow["appointments"]:
            parts.append(tomorrow["speech"])
        if refills["low"]:
            parts.append(f"{join_words([r['med'] for r in refills['low']])} is running low.")
        if new["notes"]:
            n = new["notes"][-1]
            parts.append(f"Latest note from {n['author_name']}: {n['text']}")
        if len(parts) == 1:
            parts.append(f"All quiet. {rec} is on track today.")
        return {"member": who["name"], "recipient": rec, "generated": now.isoformat(),
                "greeting": f"{hello}, {who['name']}", "doses": due["today"], "due": due["due"],
                "missed": due["missed"], "refills": refills["medications"], "low": refills["low"],
                "appointments": today["appointments"] + tomorrow["appointments"], "alerts": al["alerts"],
                "new_notes": new["notes"], "speech": " ".join(parts)}
