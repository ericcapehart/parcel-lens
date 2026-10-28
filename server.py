#!/usr/bin/env python3
"""Utah property lookup server (Python 3.9+, standard library only).

GET /api/lookup?lat=..&lon=..   -> property near a GPS fix
GET /api/lookup?address=..      -> property by typed address (Utah only)
Everything else is served from ./static.

Data sources, merged in priority order:
  1. UtahRealEstate.com (WFRMLS) RESO Web API  - needs WFRMLS_TOKEN (licensed feed)
  2. RentCast API                              - needs RENTCAST_API_KEY
  3. UGRC county parcel records (LIR)          - free, public, always on
"""
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8000"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
USER_AGENT = os.environ.get("USER_AGENT", "UtahPropertyLookup/0.1 (prototype)")
WFRMLS_TOKEN = os.environ.get("WFRMLS_TOKEN")
WFRMLS_BASE = os.environ.get("WFRMLS_BASE", "https://resoapi.utahrealestate.com/reso/odata")
RENTCAST_API_KEY = os.environ.get("RENTCAST_API_KEY")

UGRC_BASE = "https://services1.arcgis.com/99lidPhWCzftIe9K/ArcGIS/rest/services"
PARCEL_FIELDS = ("PARCEL_ID,PARCEL_ADD,PARCEL_CITY,COUNTY_NAME,TOTAL_MKT_VALUE,LAND_MKT_VALUE,"
                 "PARCEL_ACRES,PROP_CLASS,PRIMARY_RES,BLDG_SQFT,FLOORS_CNT,BUILT_YR,"
                 "EFFBUILT_YR,CONST_MATERIAL,SUBDIV_NAME,CURRENT_ASOF")
SEARCH_RADIUS_M = 60  # GPS on a sidewalk is often outside the lot itself

_cache = {}
CACHE_TTL = 15 * 60


def http_json(url, headers=None, timeout=12):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    _cache[url] = (time.time(), data)
    return data


# ---------- geocoding (UGRC boundary layers + U.S. Census geocoder, no keys) ----------

def _point_attrs(service, lat, lon, fields):
    q = urllib.parse.urlencode({
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects", "outFields": fields,
        "returnGeometry": "false", "f": "json",
    })
    feats = http_json(f"{UGRC_BASE}/{service}/FeatureServer/0/query?{q}").get("features", [])
    return feats[0]["attributes"] if feats else {}


def reverse_geocode(lat, lon):
    """County, ZIP and city for a point; empty when the point is outside Utah."""
    county = _point_attrs("UtahCountyBoundaries", lat, lon, "NAME")
    if not county:
        return {}
    zip_area = _point_attrs("UtahZipCodeAreas", lat, lon, "ZIP5,NAME")
    return {
        "state": "Utah",
        "county": county["NAME"].title() + " County",
        "postcode": zip_area.get("ZIP5", ""),
        "city": (zip_area.get("NAME") or "").title() or None,
    }


def forward_geocode(address):
    q = urllib.parse.urlencode({"address": address, "benchmark": "Public_AR_Current", "format": "json"})
    matches = http_json("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?" + q)
    for m in matches.get("result", {}).get("addressMatches", []):
        if m.get("addressComponents", {}).get("state") == "UT":
            lat, lon = m["coordinates"]["y"], m["coordinates"]["x"]
            geo = reverse_geocode(lat, lon)
            if geo:
                return lat, lon, geo
    return None


def county_service(county_name):
    # "Salt Lake County" -> "Parcels_SaltLake_LIR"; "Utah County" -> "Parcels_Utah_LIR"
    name = re.sub(r"\s+County$", "", county_name or "").replace(" ", "")
    return f"Parcels_{name}_LIR" if name else None


# ---------- UGRC parcels ----------

def _dist_point_to_rings(lat, lon, rings):
    """Approx. meters from a point to a polygon (0 when inside)."""
    kx = 111320 * math.cos(math.radians(lat))
    ky = 110540
    inside = False
    best = float("inf")
    for ring in rings:
        pts = [((x - lon) * kx, (y - lat) * ky) for x, y in ring]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if (y1 > 0) != (y2 > 0) and 0 < (x2 - x1) * (0 - y1) / (y2 - y1) + x1:
                inside = not inside
            dx, dy = x2 - x1, y2 - y1
            t = max(0, min(1, -(x1 * dx + y1 * dy) / (dx * dx + dy * dy or 1)))
            best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return 0.0 if inside else best


def find_parcels(lat, lon, county_name):
    svc = county_service(county_name)
    if not svc:
        return []
    q = urllib.parse.urlencode({
        "geometry": f"{lon},{lat}", "geometryType": "esriGeometryPoint", "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects", "distance": SEARCH_RADIUS_M,
        "units": "esriSRUnit_Meter", "outFields": PARCEL_FIELDS, "returnGeometry": "true",
        "outSR": 4326, "geometryPrecision": 6, "f": "json",
    })
    data = http_json(f"{UGRC_BASE}/{svc}/FeatureServer/0/query?{q}")
    out = []
    for f in data.get("features", []):
        a = f["attributes"]
        if not a.get("PARCEL_ADD"):
            continue  # roads, common areas, unaddressed land
        a["_distance_m"] = round(_dist_point_to_rings(lat, lon, f["geometry"]["rings"]), 1)
        out.append(a)
    # Prefer residential parcels when distances are similar
    out.sort(key=lambda a: (a["_distance_m"] + (0 if a.get("PRIMARY_RES") == "Y" else 8)))
    return out


ABBREV = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W", "STREET": "ST", "AVENUE": "AVE",
          "DRIVE": "DR", "ROAD": "RD", "LANE": "LN", "COURT": "CT", "CIRCLE": "CIR", "PLACE": "PL",
          "BOULEVARD": "BLVD", "WAY": "WAY", "PARKWAY": "PKWY", "TERRACE": "TER"}


def normalize_street(typed):
    """'1330 East 2100 South, Salt Lake City' -> '1330 E 2100 S' (assessor style)."""
    street = typed.split(",")[0].upper().replace(".", "")
    return " ".join(ABBREV.get(w, w) for w in street.split())


def find_parcel_by_address(typed, county_name):
    svc = county_service(county_name)
    street = normalize_street(typed)
    if not svc or not re.match(r"^\d+\s", street):
        return []
    q = urllib.parse.urlencode({
        "where": f"PARCEL_ADD LIKE '{street.replace(chr(39), chr(39) * 2)}%'",
        "outFields": PARCEL_FIELDS, "returnGeometry": "false", "resultRecordCount": 6, "f": "json",
    })
    rows = [f["attributes"] for f in http_json(f"{UGRC_BASE}/{svc}/FeatureServer/0/query?{q}").get("features", [])]
    city_hint = typed.upper()
    rows.sort(key=lambda a: (a.get("PARCEL_ADD") != street, (a.get("PARCEL_CITY") or "").upper() not in city_hint,
                             not a.get("BLDG_SQFT")))
    for a in rows:
        a["_distance_m"] = 0.0
    return rows


# ---------- listing providers ----------

STATUS_MAP = {
    "active": "For Sale", "coming soon": "Coming Soon",
    "active under contract": "Pending", "pending": "Pending", "under contract": "Pending",
    "closed": "Sold", "sold": "Sold",
    "expired": "Off Market", "withdrawn": "Off Market", "canceled": "Off Market",
    "cancelled": "Off Market", "inactive": "Off Market",
}


def wfrmls_lookup(street, zipcode):
    if not WFRMLS_TOKEN or not street:
        return None
    flt = f"startswith(UnparsedAddress,'{street.replace(chr(39), chr(39) * 2)}')"
    if zipcode:
        flt += f" and PostalCode eq '{zipcode}'"
    q = urllib.parse.urlencode({
        "$filter": flt, "$orderby": "ModificationTimestamp desc", "$top": 1,
        "$select": "ListingId,StandardStatus,ListPrice,ClosePrice,CloseDate,BedroomsTotal,"
                   "BathroomsTotalInteger,LivingArea,YearBuilt,UnparsedAddress,ListingContractDate,"
                   "DaysOnMarket,PublicRemarks",
    }, quote_via=urllib.parse.quote)
    data = http_json(f"{WFRMLS_BASE}/Property?{q}", {"Authorization": f"Bearer {WFRMLS_TOKEN}"})
    rows = data.get("value", [])
    if not rows:
        return None
    r = rows[0]
    return {
        "source": "UtahRealEstate.com (WFRMLS)",
        "status": STATUS_MAP.get((r.get("StandardStatus") or "").lower(), r.get("StandardStatus")),
        "mls_number": r.get("ListingId"),
        "list_price": r.get("ListPrice"), "sold_price": r.get("ClosePrice"),
        "sold_date": r.get("CloseDate"), "listed_date": r.get("ListingContractDate"),
        "days_on_market": r.get("DaysOnMarket"),
        "bedrooms": r.get("BedroomsTotal"), "bathrooms": r.get("BathroomsTotalInteger"),
        "sqft": r.get("LivingArea"), "year_built": r.get("YearBuilt"),
        "remarks": r.get("PublicRemarks"),
    }


def rentcast_lookup(full_address):
    if not RENTCAST_API_KEY or not full_address:
        return None
    hdr = {"X-Api-Key": RENTCAST_API_KEY}
    out = {"source": "RentCast"}
    enc = urllib.parse.quote(full_address)
    for status in ("Active", "Inactive"):
        try:
            rows = http_json(f"https://api.rentcast.io/v1/listings/sale?address={enc}&status={status}&limit=1", hdr)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        if rows:
            r = rows[0]
            out.update({
                "status": STATUS_MAP.get((r.get("status") or "").lower(), r.get("status")),
                "list_price": r.get("price"), "listed_date": r.get("listedDate"),
                "days_on_market": r.get("daysOnMarket"), "bedrooms": r.get("bedrooms"),
                "bathrooms": r.get("bathrooms"), "sqft": r.get("squareFootage"),
                "year_built": r.get("yearBuilt"),
            })
            break
    try:
        props = http_json(f"https://api.rentcast.io/v1/properties?address={enc}", hdr)
        if props:
            p = props[0]
            for k_out, k_in in (("bedrooms", "bedrooms"), ("bathrooms", "bathrooms"),
                                ("sqft", "squareFootage"), ("year_built", "yearBuilt"),
                                ("last_sale_price", "lastSalePrice"), ("last_sale_date", "lastSaleDate")):
                if out.get(k_out) is None and p.get(k_in) is not None:
                    out[k_out] = p[k_in]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    return out if len(out) > 1 else None


# ---------- merge ----------

def build_property(parcel, geo_addr):
    street = parcel.get("PARCEL_ADD") if parcel else None
    city = (parcel or {}).get("PARCEL_CITY") or geo_addr.get("city") or geo_addr.get("town") or geo_addr.get("village")
    zipcode = geo_addr.get("postcode", "")[:5]
    full = ", ".join(x for x in (street, city, "UT " + zipcode if zipcode else "UT") if x)

    result = {"address": full, "street": street, "city": city, "zip": zipcode,
              "county": geo_addr.get("county") or (parcel or {}).get("COUNTY_NAME"),
              "sources": [], "errors": []}

    listing = None
    for name, fn, arg in (("UtahRealEstate.com", wfrmls_lookup, None), ("RentCast", rentcast_lookup, full)):
        try:
            listing = fn(street, zipcode) if arg is None else fn(arg)
        except Exception as e:  # keep going with the next source
            result["errors"].append(f"{name}: {e}")
            listing = None
        if listing:
            break

    configured = bool(WFRMLS_TOKEN or RENTCAST_API_KEY)
    if listing:
        result["sources"].append(listing.pop("source"))
        result.update({k: v for k, v in listing.items() if v is not None})
    result.setdefault("status", "Off Market" if configured and not result["errors"] else "Status Unknown")

    if parcel:
        result["sources"].append(f"{parcel.get('COUNTY_NAME')} Assessor via UGRC")
        result.setdefault("sqft", parcel.get("BLDG_SQFT"))
        result.setdefault("year_built", parcel.get("BUILT_YR"))
        result.update({
            "parcel_id": parcel.get("PARCEL_ID"),
            "assessed_value": parcel.get("TOTAL_MKT_VALUE"),
            "land_value": parcel.get("LAND_MKT_VALUE"),
            "acres": parcel.get("PARCEL_ACRES"),
            "property_class": parcel.get("PROP_CLASS"),
            "floors": parcel.get("FLOORS_CNT"),
            "construction": parcel.get("CONST_MATERIAL"),
            "subdivision": parcel.get("SUBDIV_NAME"),
            "assessor_as_of": time.strftime("%Y-%m-%d", time.gmtime(parcel["CURRENT_ASOF"] / 1000))
            if parcel.get("CURRENT_ASOF") else None,
        })
    return {k: v for k, v in result.items() if v not in (None, "", [])} | {"errors": result["errors"]}


def lookup(params):
    if "address" in params:
        hit = forward_geocode(params["address"])
        if not hit:
            return 404, {"error": "Couldn't find that address in Utah."}
        lat, lon, geo_addr = hit
    else:
        try:
            lat, lon = float(params["lat"]), float(params["lon"])
        except (KeyError, ValueError):
            return 400, {"error": "Pass lat & lon, or address."}
        geo_addr = reverse_geocode(lat, lon)
    if geo_addr.get("state") != "Utah":
        return 422, {"error": "This version only covers Utah properties."}

    parcels = []
    if "address" in params:
        parcels = find_parcel_by_address(params["address"], geo_addr.get("county"))
    if not parcels:
        parcels = find_parcels(lat, lon, geo_addr.get("county"))
    if params.get("parcel_id"):
        parcels.sort(key=lambda a: a.get("PARCEL_ID") != params["parcel_id"])
    best = parcels[0] if parcels else None
    prop = build_property(best, geo_addr)
    prop["location"] = {"lat": lat, "lon": lon}
    prop["nearby"] = [{"parcel_id": p.get("PARCEL_ID"), "address": p.get("PARCEL_ADD"),
                       "distance_m": p["_distance_m"]} for p in parcels[1:6]]
    return 200, prop


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC_DIR, **kw)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/api/lookup":
            return super().do_GET()
        params = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            code, body = lookup(params)
        except Exception as e:
            code, body = 502, {"error": f"Lookup failed: {e}"}
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"Listing feeds: WFRMLS={'on' if WFRMLS_TOKEN else 'off'}  RentCast={'on' if RENTCAST_API_KEY else 'off'}")
    print(f"Open http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
