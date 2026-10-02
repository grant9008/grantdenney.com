"""Pull GA4 and Search Console numbers into data/analytics.json.

Runs from .github/workflows/track-analytics.yml, once a day. It reads the
service-account JSON from the GOOGLE_SERVICE_ACCOUNT environment variable,
which comes from a GitHub secret and is never written to disk or logged.

Which properties get read is set in data/analytics-sources.json, where the IDs
live because they are not secret. Anything the credential cannot read is
skipped with a note rather than failing the run, so adding a property before
granting access is harmless.

Nothing here is estimated. If a number cannot be fetched it is absent, and the
site renders nothing for it rather than something wrong.
"""
import datetime
import json
import os
import sys

import requests
from google.oauth2 import service_account
from google.auth.transport.requests import Request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = os.path.join(ROOT, "data", "analytics-sources.json")
OUT = os.path.join(ROOT, "data", "analytics.json")

SCOPES = [
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
]


def token():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "").strip()
    if not raw:
        print("no credential in the environment")
        sys.exit(0)
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    creds.refresh(Request())
    return creds.token


def ga4(prop_id, access_token):
    """Last 28 days and the 28 before it, so the site can show a trend."""
    url = "https://analyticsdata.googleapis.com/v1beta/properties/%s:runReport" % prop_id
    body = {
        "dateRanges": [
            {"startDate": "28daysAgo", "endDate": "yesterday", "name": "current"},
            {"startDate": "56daysAgo", "endDate": "29daysAgo", "name": "previous"},
        ],
        "metrics": [
            {"name": "totalUsers"},
            {"name": "sessions"},
            {"name": "screenPageViews"},
        ],
    }
    r = requests.post(url, json=body,
                      headers={"Authorization": "Bearer " + access_token}, timeout=60)
    if r.status_code != 200:
        print("  GA4 %s: %s %s" % (prop_id, r.status_code, r.text[:160]))
        return None
    rows = r.json().get("rows", [])
    out = {}
    for row in rows:
        name = row.get("dimensionValues", [{}])[0].get("value") if row.get("dimensionValues") else None
        vals = [v.get("value") for v in row.get("metricValues", [])]
        key = name or ("current" if "current" not in out else "previous")
        out[key] = {"users": int(vals[0]), "sessions": int(vals[1]), "views": int(vals[2])}
    # runReport labels multiple ranges by order when names are given
    names = [rr.get("name") for rr in body["dateRanges"]]
    if set(out) == {"current", "previous"} or not out:
        return out or None
    ordered = list(out.values())
    return dict(zip(names, ordered)) if len(ordered) == len(names) else out


def search_console(site_url, access_token):
    """Totals plus a daily series, so the position chart has real points."""
    end = datetime.date.today() - datetime.timedelta(days=3)   # GSC lags ~2 days
    start = end - datetime.timedelta(days=89)
    base = "https://searchconsole.googleapis.com/webmasters/v3/sites/%s/searchAnalytics/query" % requests.utils.quote(site_url, safe="")
    head = {"Authorization": "Bearer " + access_token}

    totals = requests.post(base, json={
        "startDate": start.isoformat(), "endDate": end.isoformat(),
    }, headers=head, timeout=60)
    if totals.status_code != 200:
        print("  GSC %s: %s %s" % (site_url, totals.status_code, totals.text[:160]))
        return None

    trow = (totals.json().get("rows") or [{}])[0]
    daily = requests.post(base, json={
        "startDate": start.isoformat(), "endDate": end.isoformat(),
        "dimensions": ["date"], "rowLimit": 500,
    }, headers=head, timeout=60)

    series = []
    if daily.status_code == 200:
        for row in daily.json().get("rows", []):
            series.append({
                "date": row["keys"][0],
                "clicks": int(row.get("clicks", 0)),
                "impressions": int(row.get("impressions", 0)),
                "position": round(row.get("position", 0), 1),
            })

    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "clicks": int(trow.get("clicks", 0)),
        "impressions": int(trow.get("impressions", 0)),
        "ctr": round(trow.get("ctr", 0) * 100, 2),
        "position": round(trow.get("position", 0), 1),
        "daily": series,
    }


def main():
    with open(SOURCES) as fh:
        sources = json.load(fh)

    access_token = token()
    stamp = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    out = {"updated": stamp, "ga4": {}, "searchConsole": {}}

    for entry in sources.get("ga4", []):
        pid = str(entry.get("propertyId", ""))
        if not pid.isdigit():
            print("  skipping GA4 %s: property id not set" % entry.get("label"))
            continue
        got = ga4(pid, access_token)
        if got:
            out["ga4"][entry["label"]] = got
            print("  GA4 %s ok" % entry["label"])

    for entry in sources.get("searchConsole", []):
        got = search_console(entry["siteUrl"], access_token)
        if got:
            out["searchConsole"][entry["label"]] = got
            print("  GSC %s ok (%d daily rows)" % (entry["label"], len(got["daily"])))

    if not out["ga4"] and not out["searchConsole"]:
        print("nothing readable; leaving the existing file alone")
        return 0

    with open(OUT, "w", newline="\n") as fh:
        json.dump(out, fh, indent=2)
        fh.write("\n")
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
