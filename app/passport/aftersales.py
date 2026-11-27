"""Read-only OEM projections. Entitlement follows the original produced pack, never its current holder."""

from calendar import monthrange
from collections import Counter
from datetime import date, timedelta

from . import domain
from .aftersales_import import latest_receipt
from .auth import FLEET_ROLES, OEM
from .store import parse_utc

FRESH_FOR = timedelta(days=30)


def organization(person):
    roles = person.roles & FLEET_ROLES.keys() if person and person.has_any((OEM,)) else set()
    return FLEET_ROLES[next(iter(roles))] if len(roles) == 1 else None


def entitled_pack(tx, service, state, party):
    if not party or not state["committed_version"]:
        return None
    origin = service.predecessor(tx, state["battery_id"]) or state["battery_id"]
    pack = tx.get("OEMPhysicalPacks", origin)
    return pack if pack and pack["oem_id"] == party else None


def freshness(receipt, now):
    if not receipt:
        return "unavailable"
    age = parse_utc(now) - parse_utc(receipt["as_of_utc"])
    return "current" if timedelta(0) <= age <= FRESH_FOR else "stale"


def end_date(start, months):
    start = date.fromisoformat(start)
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    return date(year, month + 1, min(start.day, monthrange(year, month + 1)[1]))


def limit_status(remaining, near):
    if remaining is None:
        return "insufficient_data"
    return "exceeded" if remaining <= 0 else "near_limit" if remaining <= near else "below_limit"


def warranty(tx, pack, package_row, receipt, now):
    battery = pack["battery_id"]
    as_of = receipt["as_of_utc"] if receipt else None
    terms = (tx.get("WarrantyTerms", receipt["delivery_id"], battery) if receipt else None) or {}
    observations = sorted((r for r in tx.select("ServiceReadings", battery_id=battery)
                           if as_of and r["measured_utc"] <= as_of), key=lambda r: r["measured_utc"])
    latest = observations[-1] if observations else {}
    start, months, cycles = (terms.get(k) for k in ("in_service_date", "warranty_months", "warranty_cycles"))
    expiry = end_date(start, months) if start and months else None
    days_left = (expiry - date.fromisoformat(as_of[:10])).days if expiry else None
    remaining = cycles - latest["actual_cycles"] if cycles and latest else None
    # Only distinct dated observations of the SAME physical pack in this EFC feed are comparable.
    health = [r for r in observations if r["soh_pct"] is not None]
    trend = None
    if len(health) >= 2:
        first, last = health[-2:]
        trend = {"from_utc": first["measured_utc"], "to_utc": last["measured_utc"],
                 "from_pct": first["soh_pct"], "to_pct": last["soh_pct"],
                 "change_percentage_points": round(last["soh_pct"] - first["soh_pct"], 6)}
    return {"physical_battery_id": battery, "as_of_utc": as_of, "feed_status": freshness(receipt, now),
            "delivery_id": receipt["delivery_id"] if receipt else None,
            "term_version": terms.get("term_version"), "in_service_date": start, "warranty_months": months,
            "warranty_end_date": expiry.isoformat() if expiry else None, "days_remaining": days_left,
            "time_status": limit_status(days_left, 30), "warranty_cycles": cycles, "cycles_remaining": remaining,
            "cycle_status": limit_status(remaining, (cycles or 0) * 0.1),
            "expected_cycles": package_row["expected_cycles"],
            "actual_cycles": latest.get("actual_cycles"), "measured_utc": latest.get("measured_utc"),
            "reading_id": latest.get("reading_id"), "reading_delivery_id": latest.get("delivery_id"),
            "cycle_basis": latest.get("cycle_basis"), "soh_trend": trend}


def campaigns_for(tx, battery, receipt):
    if not receipt:
        return []
    delivery = receipt["delivery_id"]
    return [tx.get("ServiceCampaigns", delivery, link["campaign_id"])
            for link in tx.select("CampaignPacks", delivery_id=delivery, battery_id=battery)]


def active(campaign, receipt):
    return campaign["status"] == "open" and campaign["effective_date"] <= receipt["as_of_utc"][:10]


def recall(tx, pack, receipt, now):
    status = freshness(receipt, now)
    campaigns = campaigns_for(tx, pack["battery_id"], receipt)
    match = any(active(c, receipt) for c in campaigns)
    return {"physical_battery_id": pack["battery_id"], "batch_id": pack["batch_id"],
            "feed_status": status, "as_of_utc": receipt["as_of_utc"] if receipt else None,
            "exposure": ("affected" if match else "no_active_campaign") if status == "current" else "unknown",
            "campaigns": campaigns}


def fleet(service, person, series=None, holder=None, campaign_id=None):
    """First narrow physical membership, then join campaign targets. Count original serials, not passports."""
    party = organization(person)
    if not party:
        return None
    now = service.clock()
    with service.store.read() as tx:
        packs = tx.select("OEMPhysicalPacks", oem_id=party, **({"series_id": series} if series else {}))
        receipts = {s: latest_receipt(tx, s) for s in sorted({p["series_id"] for p in packs})}
        feeds = [{"series_id": s, "as_of_utc": r["as_of_utc"] if r else None, "feed_status": freshness(r, now)}
                 for s, r in receipts.items()]
        rows = []
        for p in packs:
            battery = p["battery_id"]
            current = tx.get("PassportState", service.successor(tx, battery) or battery)
            if not current or not current["committed_version"] or current["lifecycle_status"] in domain.CLOSED:
                continue
            if holder and current["holder_id"] != holder:
                continue
            receipt = receipts[p["series_id"]]
            matched = [c["campaign_id"] for c in campaigns_for(tx, battery, receipt)
                       if active(c, receipt) and (not campaign_id or c["campaign_id"] == campaign_id)]
            if matched:
                rows.append({"physical_battery_id": battery, "passport_id": current["battery_id"],
                             "holder_id": current["holder_id"], "campaign_ids": matched, "series_id": p["series_id"]})
    holders = Counter(r["holder_id"] for r in rows)
    status = "unavailable" if not feeds else ("current" if all(f["feed_status"] == "current" for f in feeds)
                                              else "stale")
    return {"oem_id": party, "feed_status": status, "feeds": feeds,
            "affected_pack_count": None if status == "unavailable" else len(rows),
            "holders": [{"holder_id": h, "affected_pack_count": n} for h, n in sorted(holders.items())],
            "packs": rows, "series_id": series, "holder_id": holder, "campaign_id": campaign_id}
