"""Fetch every client's Meta ads numbers and write them encrypted for the page.

Runs in the GitHub Action every 15 minutes. Two secrets drive it:
  META_TOKEN  a user token with ads_read
  CLIENTS     {"masterKey": "...", "clients": [{"name", "accountId", "slug", "key", "dailyLimit"}]}

Writes out/all.enc (every client, for the owner, opened with masterKey) and
out/<slug>.enc (one client, opened with that client's key). Each file is
base64(iv || AES-256-GCM ciphertext) of a JSON document, so the public repo
holds no client names or numbers.
"""

import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

GRAPH = "https://graph.facebook.com/v21.0"
IST = timezone(timedelta(hours=5, minutes=30))
TOKEN = os.environ["META_TOKEN"].strip()
CONFIG = json.loads(os.environ["CLIENTS"])

LABELS = {
    "click_to_call_native_call_placed": "Phone calls placed",
    "app_custom_event.fb_mobile_complete_registration": "In-app registrations completed",
    "offsite_conversion.fb_pixel_purchase": "Website purchases",
    "omni_purchase": "Purchases",
    "onsite_conversion.messaging_conversation_started_7d": "Messaging conversations started",
    "offsite_conversion.fb_pixel_lead": "Website leads",
    "lead": "Leads",
    "onsite_conversion.lead_grouped": "Leads",
    "link_click": "Link clicks",
    "landing_page_view": "Landing page views",
    "offsite_conversion.fb_pixel_add_to_cart": "Adds to cart",
    "offsite_conversion.fb_pixel_complete_registration": "Registrations",
}
_custom_names: dict[str, str] = {}


def get(path: str, **params) -> dict:
    """One Graph GET, with the token in a header (never the URL); retries twice."""
    query = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
    )
    req = urllib.request.Request(
        f"{GRAPH}/{path}?{query}", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.load(res)
        except urllib.error.HTTPError as err:
            body = err.read().decode()[:300]
            if attempt == 2 or err.code < 500:
                raise RuntimeError(f"Graph {path}: {err.code} {body}") from None
        except urllib.error.URLError:
            if attempt == 2:
                raise
        time.sleep(2 * (attempt + 1))
    return {}


def get_all(path: str, **params) -> list[dict]:
    out, after = [], None
    while True:
        page = get(path, limit=500, **params, **({"after": after} if after else {}))
        out += page.get("data", [])
        after = page.get("paging", {}).get("cursors", {}).get("after")
        if not after or not page.get("paging", {}).get("next"):
            return out


def label_of(indicator: str | None) -> str | None:
    if not indicator:
        return None
    kind = indicator.split(":", 1)[-1]
    if kind.startswith("offsite_conversion.custom."):
        cid = kind.rsplit(".", 1)[-1]
        if cid not in _custom_names:
            try:
                _custom_names[cid] = get(cid, fields="name").get("name") or "Custom conversion"
            except Exception:
                _custom_names[cid] = "Custom conversion"
        return _custom_names[cid]
    return LABELS.get(kind, kind.replace("_", " ").replace(".", " ").capitalize())


def count_of(row: dict) -> float:
    for r in row.get("results") or []:
        for v in r.get("values") or []:
            try:
                return float(v.get("value") or 0)
            except ValueError:
                return 0
    return 0


def day(account: str, preset: str, active: set[str]) -> dict:
    rows = get_all(
        f"act_{account}/insights",
        level="campaign",
        date_preset=preset,
        fields="campaign_id,campaign_name,spend,impressions,inline_link_clicks,results",
    )
    campaigns, by_label = [], {}
    for r in rows:
        spend = float(r.get("spend") or 0)
        if spend <= 0:
            continue
        indicator = ((r.get("results") or [{}])[0]).get("indicator")
        label = label_of(indicator)
        count = count_of(r)
        line = {
            "id": r["campaign_id"],
            "name": r.get("campaign_name", ""),
            "status": "ACTIVE" if r["campaign_id"] in active else "OFF",
            "spend": spend,
            "impressions": int(r.get("impressions") or 0),
            "clicks": int(r.get("inline_link_clicks") or 0),
            "results": count,
            "label": label,
        }
        campaigns.append(line)
        if count and label:
            agg = by_label.setdefault(label, {"label": label, "count": 0, "spend": 0})
            agg["count"] += count
            agg["spend"] += spend
    campaigns.sort(key=lambda c: -c["spend"])
    return {
        "spend": sum(c["spend"] for c in campaigns),
        "impressions": sum(c["impressions"] for c in campaigns),
        "clicks": sum(c["clicks"] for c in campaigns),
        "results": sorted(by_label.values(), key=lambda l: -l["count"]),
        "campaigns": campaigns,
    }


def money(v) -> float:
    """Budgets and caps come in paise."""
    return float(v or 0) / 100


def anomalies(today: dict, yesterday: dict, week: dict, active: set[str], rejected: list[dict], hour: int) -> list[dict]:
    """Same rules as the Claude Code pane (hooks/watch.ts), minus the stall check."""
    out = []
    week_by = {c["id"]: c for c in week["campaigns"]}
    for when, d in (("Yesterday", yesterday), ("Today", today)):
        for c in d["campaigns"]:
            w = week_by.get(c["id"])
            if not w or w["results"] < 3:
                continue
            avg = w["spend"] / w["results"]
            if c["spend"] < 2 * avg:
                continue
            cpr = c["spend"] / c["results"] if c["results"] else float("inf")
            if cpr <= 1.5 * avg:
                continue
            out.append({
                "tone": "error" if not c["results"] or cpr > 2 * avg else "warning",
                "text": (
                    f"{when}: {c['name'][:40]} ₹{cpr:,.0f} per result vs ₹{avg:,.0f} 7-day avg (+{(cpr / avg - 1) * 100:.0f}%)"
                    if c["results"]
                    else f"{when}: {c['name'][:40]} spent ₹{c['spend']:,.0f}, 0 results (7-day avg ₹{avg:,.0f} each)"
                ),
            })
    today_by = {c["id"]: c for c in today["campaigns"]}
    for c in yesterday["campaigns"]:
        if c["id"] in active and c["id"] not in today_by and hour >= 11:
            out.append({
                "tone": "error",
                "text": f"Not delivering: {c['name'][:40]} has spent ₹0 today (₹{c['spend']:,.0f} yesterday)",
            })
    for ad in rejected:
        bad = ad.get("effective_status") == "DISAPPROVED"
        out.append({
            "tone": "error" if bad else "warning",
            "text": f"{'Ad rejected' if bad else 'Ad has issues'}: {ad.get('name', '')[:30]} ({(ad.get('campaign') or {}).get('name', '')[:24]})",
        })
    return out


def client_report(client: dict, hour: int) -> dict:
    acct = client["accountId"]
    active_filter = [{"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]}]
    camps = get_all(f"act_{acct}/campaigns", fields="id,daily_budget,lifetime_budget", filtering=active_filter)
    adsets = get_all(f"act_{acct}/adsets", fields="id,daily_budget,lifetime_budget", filtering=active_filter)
    active = {c["id"] for c in camps}
    today = day(acct, "today", active)
    yesterday = day(acct, "yesterday", active)
    week = day(acct, "last_7d", active)
    account = get(f"act_{acct}", fields="spend_cap,amount_spent")
    rejected = get_all(
        f"act_{acct}/ads",
        fields="name,effective_status,campaign{name}",
        filtering=[{"field": "effective_status", "operator": "IN", "value": ["DISAPPROVED", "WITH_ISSUES"]}],
    )
    meta_daily = sum(money(x.get("daily_budget")) for x in camps + adsets)
    own = client.get("dailyLimit")
    cap = money(account.get("spend_cap"))
    return {
        "name": client["name"],
        "slug": client["slug"],
        "limit": own or meta_daily or None,
        "isOwnLimit": bool(own),
        "hasLifetime": any(money(x.get("lifetime_budget")) > 0 for x in camps + adsets),
        "today": today,
        "yesterday": yesterday,
        "cap": {"limit": cap, "spent": money(account.get("amount_spent"))} if cap > 0 else None,
        "anomalies": anomalies(today, yesterday, week, active, rejected, hour),
        "error": None,
    }


def seal(doc: dict, key_b64: str) -> str:
    key = base64.urlsafe_b64decode(key_b64 + "=" * (-len(key_b64) % 4))
    iv = os.urandom(12)
    return base64.b64encode(iv + AESGCM(key).encrypt(iv, json.dumps(doc).encode(), None)).decode()


def main() -> None:
    now = datetime.now(IST)
    clients = CONFIG["clients"]

    def safe(c: dict) -> dict:
        try:
            return client_report(c, now.hour)
        except Exception as err:  # one bad account must not blank the rest
            print(f"{c['name']}: {err}", file=sys.stderr)
            return {"name": c["name"], "slug": c["slug"], "error": str(err)[:200], "anomalies": [],
                    "today": None, "yesterday": None, "limit": None, "isOwnLimit": False,
                    "hasLifetime": False, "cap": None}

    with ThreadPoolExecutor(max_workers=4) as pool:
        reports = list(pool.map(safe, clients))

    generated = now.isoformat(timespec="seconds")
    os.makedirs("out", exist_ok=True)
    with open("out/all.enc", "w") as f:
        f.write(seal({"generatedAt": generated, "clients": reports}, CONFIG["masterKey"]))
    for c, r in zip(clients, reports):
        # A client sees its own numbers, never the internal alerts.
        mine = {**r, "anomalies": []}
        with open(f"out/{c['slug']}.enc", "w") as f:
            f.write(seal({"generatedAt": generated, "clients": [mine]}, c["key"]))
    failed = [r["name"] for r in reports if r.get("error")]
    print(f"{generated}: {len(reports)} clients, {len(failed)} failed {failed or ''}")


if __name__ == "__main__":
    main()
