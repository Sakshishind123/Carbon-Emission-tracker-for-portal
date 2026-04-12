"""
website_carbon.py  (v2 — full schema)
──────────────────────────────────────
Estimates the carbon footprint of any URL and outputs a CSV row that
matches the codecarbon schema exactly (same fields as carbon_tracker.py).

Extra website-specific columns appended:
  page_size_bytes, page_size_kb, resource_count, rating, green_hosting,
  monthly_views, monthly_co2_kg, monthly_energy_kwh, comparable_to

Methodology — Sustainable Web Design model v4:
  energy_kWh = page_size_MB × (0.000072 + 0.000152 + 0.000059)
                               data-centre   network    device
  co2_g = energy_kWh × carbon_intensity_g_per_kWh

No external libraries required — uses only Python stdlib.

Optional:  pip install requests    (enables geo-IP lookup for location fields)

Usage:
  python website_carbon.py --url https://example.com
  python website_carbon.py --url https://example.com --views 50000 --green-host
  python website_carbon.py --url https://example.com --output results.csv
"""

import argparse
import csv
import datetime
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, asdict, field
from urllib.parse import urljoin, urlparse

try:
    import requests as _req
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# ─── Energy model constants ───────────────────────────────────────────────────

ENERGY_KWH_PER_MB = {
    "data_centre": 0.000072,
    "network":     0.000152,
    "end_device":  0.000059,
}
TOTAL_ENERGY_KWH_PER_MB = sum(ENERGY_KWH_PER_MB.values())

CARBON_INTENSITY_GRID  = 442.0   # gCO₂/kWh  global avg (IEA 2022)
CARBON_INTENSITY_GREEN =  50.0   # gCO₂/kWh  renewable-heavy hosting

TOOL_VERSION = "2.0.0"
USER_AGENT   = "Mozilla/5.0 (compatible; WebCarbonBot/2.0)"
TIMEOUT      = 15
MB           = 1024 * 1024

# CSV schema — codecarbon core fields + website-specific extras
CSV_FIELDS = [
    # ── codecarbon core ──────────────────────────────────────────────────────
    "timestamp", "project_name", "run_id", "experiment_id",
    "duration", "emissions", "emissions_rate",
    "cpu_power", "gpu_power", "ram_power",
    "cpu_energy", "gpu_energy", "ram_energy", "energy_consumed",
    "country_name", "country_iso_code", "region",
    "cloud_provider", "cloud_region",
    "os", "python_version", "codecarbon_version",
    "cpu_count", "cpu_model",
    "gpu_count", "gpu_model",
    "longitude", "latitude",
    "ram_total_size", "tracking_mode", "on_cloud", "pue",
    # ── website-specific extras ───────────────────────────────────────────────
    "url", "page_size_bytes", "page_size_kb", "resource_count",
    "green_hosting", "rating",
    "monthly_views", "monthly_co2_kg", "monthly_energy_kwh",
    "comparable_to",
]


# ─── Geo lookup (reused from carbon_tracker pattern) ─────────────────────────

def geo_lookup() -> dict:
    if not REQUESTS_AVAILABLE:
        return {}
    try:
        r = _req.get("http://ip-api.com/json/", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def fetch(url: str) -> tuple:
    """Return (bytes, headers_dict). Raises RuntimeError on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} — {url}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach {url}: {e.reason}")


def head_size(url: str) -> int:
    """Try HEAD for Content-Length, fall back to GET byte count."""
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            cl = r.headers.get("Content-Length", "")
            if cl.isdigit():
                return int(cl)
    except Exception:
        pass
    try:
        body, _ = fetch(url)
        return len(body)
    except Exception:
        return 0


def extract_resources(html: bytes, base_url: str) -> list:
    """Parse HTML for CSS/JS/image/font sub-resource URLs."""
    text = html.decode("utf-8", errors="replace")
    patterns = [
        r'src=["\']([^"\']+)["\']',
        r'href=["\']([^"\']+\.(?:css|js|woff2?|ttf|eot))["\']',
        r'url\(["\']?([^"\'()]+\.(?:png|jpg|jpeg|gif|webp|svg|avif))["\']?\)',
    ]
    raw: set = set()
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw.add(m.group(1))

    parsed = urlparse(base_url)
    scheme_host = f"{parsed.scheme}://{parsed.netloc}"
    out = []
    for u in raw:
        if u.startswith("data:"):
            continue
        if u.startswith("//"):
            u = parsed.scheme + ":" + u
        elif u.startswith("/"):
            u = scheme_host + u
        elif not u.startswith("http"):
            u = urljoin(base_url, u)
        out.append(u)
    return list(set(out))


# ─── Rating scale ─────────────────────────────────────────────────────────────

def rate(co2_g: float) -> str:
    thresholds = [
        (0.095, "A+"), (0.186, "A"), (0.341, "B"),
        (0.493, "C"), (0.656, "D"), (1.000, "E"),
    ]
    for limit, grade in thresholds:
        if co2_g <= limit:
            return grade
    return "F"


def equivalence(monthly_co2_kg: float) -> str:
    km = monthly_co2_kg / 0.21
    if km < 0.01:
        return f"{km * 1000:.2f} metres driven in an average car/month"
    return f"{km:.2f} km driven in an average car/month"


# ─── Core analyser ────────────────────────────────────────────────────────────

def analyse(
    url:              str,
    monthly_views:    int  = 10_000,
    green_hosting:    bool = False,
    follow_resources: bool = True,
    project_name:     str  = "website-carbon",
    experiment_id:    str  = "",
) -> dict:
    """
    Fetch the URL, measure bytes, compute energy + CO₂,
    and return a dict with all CSV_FIELDS populated.
    """
    t0 = time.time()

    # ── Fetch HTML ────────────────────────────────────────────────────────────
    print(f"  ↳  Fetching {url} …", end=" ", flush=True)
    html, _headers = fetch(url)
    html_bytes = len(html)
    print(f"{html_bytes / 1024:.1f} KB")

    total_bytes    = html_bytes
    resource_count = 0

    if follow_resources:
        resources = extract_resources(html, url)
        resource_count = len(resources)
        print(f"  ↳  Measuring {resource_count} sub-resources …")
        for r_url in resources[:50]:
            total_bytes += head_size(r_url)

    duration_s = time.time() - t0

    # ── Energy & emissions ────────────────────────────────────────────────────
    mb              = total_bytes / MB
    dc_energy       = mb * ENERGY_KWH_PER_MB["data_centre"]
    net_energy      = mb * ENERGY_KWH_PER_MB["network"]
    dev_energy      = mb * ENERGY_KWH_PER_MB["end_device"]
    total_energy    = dc_energy + net_energy + dev_energy

    intensity       = CARBON_INTENSITY_GREEN if green_hosting else CARBON_INTENSITY_GRID
    co2_g           = total_energy * intensity       # grams per page view
    co2_kg          = co2_g / 1000                   # kg per page view
    rate_kg_s       = co2_kg / max(duration_s, 1)

    monthly_energy  = total_energy * monthly_views
    monthly_co2_kg  = co2_kg * monthly_views

    # ── System / geo metadata ─────────────────────────────────────────────────
    geo = geo_lookup()

    return {
        # ── codecarbon core ──────────────────────────────────────────────────
        "timestamp":          datetime.datetime.now().isoformat(timespec="seconds"),
        "project_name":       project_name,
        "run_id":             str(uuid.uuid4()),
        "experiment_id":      experiment_id or str(uuid.uuid4()),
        "duration":           round(duration_s, 4),
        "emissions":          round(co2_kg, 12),
        "emissions_rate":     round(rate_kg_s, 12),
        "cpu_power":          0.0,   # website scan — no local CPU tracking
        "gpu_power":          0.0,
        "ram_power":          0.0,
        "cpu_energy":         round(dc_energy, 12),   # map DC → cpu_energy slot
        "gpu_energy":         0.0,
        "ram_energy":         round(net_energy, 12),  # map network → ram_energy slot
        "energy_consumed":    round(total_energy, 12),
        "country_name":       geo.get("country", "Unknown"),
        "country_iso_code":   geo.get("countryCode", "N/A"),
        "region":             geo.get("regionName", "Unknown"),
        "cloud_provider":     "N/A",
        "cloud_region":       "N/A",
        "os":                 f"{platform.system()} {platform.release()}",
        "python_version":     platform.python_version(),
        "codecarbon_version": TOOL_VERSION,
        "cpu_count":          0,
        "cpu_model":          "N/A (website scan)",
        "gpu_count":          0,
        "gpu_model":          "N/A",
        "longitude":          geo.get("lon", 0.0),
        "latitude":           geo.get("lat", 0.0),
        "ram_total_size":     0.0,
        "tracking_mode":      "website",
        "on_cloud":           "N",
        "pue":                1.0,
        # ── website extras ───────────────────────────────────────────────────
        "url":                url,
        "page_size_bytes":    total_bytes,
        "page_size_kb":       round(total_bytes / 1024, 2),
        "resource_count":     resource_count,
        "green_hosting":      green_hosting,
        "rating":             rate(co2_g),
        "monthly_views":      monthly_views,
        "monthly_co2_kg":     round(monthly_co2_kg, 8),
        "monthly_energy_kwh": round(monthly_energy, 8),
        "comparable_to":      equivalence(monthly_co2_kg),
    }


# ─── Reporting ────────────────────────────────────────────────────────────────

RATING_ICON = {"A+": "🌿", "A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴", "E": "🔴", "F": "⛔"}

def print_result(r: dict) -> None:
    icon = RATING_ICON.get(r["rating"], "❓")
    print()
    print("=" * 65)
    print("  🌍  Website Carbon Report")
    print("=" * 65)
    print(f"  URL              : {r['url']}")
    print(f"  Page size        : {r['page_size_kb']:.1f} KB  ({r['page_size_bytes']:,} bytes)")
    print(f"  Sub-resources    : {r['resource_count']}")
    print()
    print(f"  ⚡  Energy per page view:")
    print(f"       Data centre : {r['cpu_energy']*1e6:.4f} µWh")
    print(f"       Network     : {r['ram_energy']*1e6:.4f} µWh")
    print(f"       End device  : {(r['energy_consumed']-r['cpu_energy']-r['ram_energy'])*1e6:.4f} µWh")
    print(f"       Total       : {r['energy_consumed']*1e6:.4f} µWh")
    print()
    print(f"  💨  CO₂/page view : {r['emissions']*1e6:.4f} µg  ({r['emissions']*1000:.6f} g)")
    print(f"  📅  Monthly ({r['monthly_views']:,} views):")
    print(f"       Energy      : {r['monthly_energy_kwh']:.6f} kWh")
    print(f"       CO₂         : {r['monthly_co2_kg']*1000:.4f} g  ({r['monthly_co2_kg']:.8f} kg)")
    print()
    print(f"  {icon}  Rating : {r['rating']}")
    print(f"  📍  Location : {r['country_name']} ({r['country_iso_code']}) — {r['region']}")
    print(f"       Lat {r['latitude']}, Lon {r['longitude']}")
    print(f"  🌱  {r['comparable_to'].capitalize()}")
    print("=" * 65)
    print()


def write_csv(result: dict, path: str) -> None:
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(result)
    print(f"  💾  Saved → {path}")


def write_json(result: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"  💾  JSON → {path}")


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Website carbon — full codecarbon-compatible schema")
    p.add_argument("--url",          required=True, help="Full URL (include https://)")
    p.add_argument("--views",        type=int,   default=10_000)
    p.add_argument("--green-host",   action="store_true")
    p.add_argument("--no-resources", action="store_true", help="Skip sub-resource measurement")
    p.add_argument("--project",      default="website-carbon")
    p.add_argument("--experiment-id", default="")
    p.add_argument("--output",       default="website_emissions.csv")
    p.add_argument("--output-json",  default=None)
    args = p.parse_args()

    print(f"\n🔍  Analysing: {args.url}\n")
    try:
        result = analyse(
            url              = args.url,
            monthly_views    = args.views,
            green_hosting    = args.green_host,
            follow_resources = not args.no_resources,
            project_name     = args.project,
            experiment_id    = args.experiment_id,
        )
    except RuntimeError as e:
        print(f"\n❌  {e}", file=sys.stderr)
        sys.exit(1)

    print_result(result)
    write_csv(result, args.output)
    if args.output_json:
        write_json(result, args.output_json)
