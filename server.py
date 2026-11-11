"""FastAPI server for the Fishing Tracker progressive web app."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


APP_DIRECTORY = Path(__file__).resolve().parent
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_SPECIES = 150
STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_FISHING_SOURCES = {
    "AL": "https://www.outdooralabama.com/fishing/freshwater-fishing",
    "AK": "https://www.adfg.alaska.gov/index.cfm?adfg=fishingSport.main",
    "AZ": "https://www.azgfd.com/fishing/",
    "AR": "https://www.agfc.com/fishing/",
    "CA": "https://wildlife.ca.gov/Fishing/Inland",
    "CO": "https://cpw.state.co.us/thingstodo/Pages/Fishing.aspx",
    "CT": "https://portal.ct.gov/deep/fishing",
    "DE": "https://dnrec.delaware.gov/fish-wildlife/fishing/",
    "FL": "https://myfwc.com/fishing/freshwater/",
    "GA": "https://georgiawildlife.com/fishing",
    "HI": "https://dlnr.hawaii.gov/dofaw/",
    "ID": "https://idfg.idaho.gov/fish",
    "IL": "https://dnr.illinois.gov/fishing.html",
    "IN": "https://www.in.gov/dnr/fish-and-wildlife/fishing/",
    "IA": "https://www.iowadnr.gov/fishing",
    "KS": "https://ksoutdoors.com/Fishing",
    "KY": "https://fw.ky.gov/Fish/Pages/default.aspx",
    "LA": "https://www.wlf.louisiana.gov/page/fishing",
    "ME": "https://www.maine.gov/ifw/fishing-boating/fishing/",
    "MD": "https://dnr.maryland.gov/fisheries/Pages/default.aspx",
    "MA": "https://www.mass.gov/orgs/division-of-fisheries-and-wildlife",
    "MI": "https://www.michigan.gov/dnr/things-to-do/fishing",
    "MN": "https://www.dnr.state.mn.us/fishing/index.html",
    "MS": "https://www.mdwfp.com/fishing-boating",
    "MO": "https://mdc.mo.gov/fishing",
    "MT": "https://fwp.mt.gov/fish",
    "NE": "https://outdoornebraska.gov/fishing/",
    "NV": "https://www.ndow.org/fish/",
    "NH": "https://www.wildlife.nh.gov/fishing",
    "NJ": "https://dep.nj.gov/njfw/fishing/freshwater/",
    "NM": "https://www.wildlife.state.nm.us/fishing/",
    "NY": "https://dec.ny.gov/things-to-do/freshwater-fishing",
    "NC": "https://www.ncwildlife.org/Fishing",
    "ND": "https://gf.nd.gov/fishing",
    "OH": "https://www.fishwildlife.org/landing/membership/member-list",
    "OK": "https://www.wildlifedepartment.com/fishing",
    "OR": "https://myodfw.com/fishing",
    "PA": "https://www.pa.gov/agencies/fishandboat/fishing.html",
    "RI": "https://dem.ri.gov/natural-resources-bureau/fish-wildlife",
    "SC": "https://www.dnr.sc.gov/fishing.html",
    "SD": "https://gfp.sd.gov/fishing-boating/",
    "TN": "https://www.tn.gov/twra/fishing.html",
    "TX": "https://tpwd.texas.gov/fishboat/",
    "UT": "https://wildlife.utah.gov/fishing",
    "VT": "https://www.vtfishandwildlife.com/fish",
    "VA": "https://dwr.virginia.gov/fishing/",
    "WA": "https://wdfw.wa.gov/fishing",
    "WV": "https://wvdnr.gov/fishing/",
    "WI": "https://dnr.wisconsin.gov/topic/Fishing",
    "WY": "https://wgfd.wyo.gov/fishing-boating",
}
RECOMMENDATION_SPECIES = (
    "Largemouth Bass", "Smallmouth Bass", "Crappie", "Walleye",
    "Northern Pike", "Catfish", "Trout",
)
_geocoder_lock = threading.Lock()
_last_geocoder_request = 0.0

app = FastAPI(title="Fishing Tracker API", version="0.1.0")
allowed_origins = [
    origin.strip()
    for origin in os.environ.get("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )


class IdentifyRequest(BaseModel):
    image_data: str = Field(max_length=MAX_IMAGE_BYTES * 2)
    content_type: str
    species_options: list[str] = Field(min_length=1, max_length=MAX_SPECIES)


class SpeciesSuggestion(BaseModel):
    species: str
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=240)


class IdentifyResponse(BaseModel):
    suggestions: list[SpeciesSuggestion]
    needs_better_photo: bool


class RecommendationRequest(BaseModel):
    water_body: str = Field(min_length=2, max_length=100)
    state: str = Field(min_length=2, max_length=2)
    target_species: str = Field(default="", max_length=40)


class DataSourceError(Exception):
    def __init__(self, source: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.source = source
        self.status = status


def _external_json(url: str, source: str) -> dict:
    request = Request(
        url,
        headers={
            "Accept": "application/geo+json, application/json",
            "User-Agent": "FishingTracker/1.0 (shore fishing recommendation report)",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        raise DataSourceError(source, f"{source} returned HTTP {error.code}.", error.code) from error
    except (URLError, TimeoutError, OSError) as error:
        raise DataSourceError(source, f"Could not reach {source}.") from error
    except json.JSONDecodeError as error:
        raise DataSourceError(source, f"{source} returned invalid data.") from error
    if not isinstance(result, dict):
        raise DataSourceError(source, f"{source} returned an unexpected response.")
    return result


def _external_text(url: str, source: str) -> str:
    request = Request(
        url,
        headers={
            "Accept": "text/plain",
            "User-Agent": "FishingTracker/1.0 (shore fishing recommendation report)",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = response.read(2_000_001).decode("utf-8", errors="replace")
    except HTTPError as error:
        raise DataSourceError(source, f"{source} returned HTTP {error.code}.", error.code) from error
    except (URLError, TimeoutError, OSError) as error:
        raise DataSourceError(source, f"Could not reach {source}.") from error
    if len(result) > 2_000_000:
        raise DataSourceError(source, f"{source} returned too much data to process.")
    return result


def _nominatim_locations(query: str) -> list:
    global _last_geocoder_request
    with _geocoder_lock:
        wait = 1.0 - (time.monotonic() - _last_geocoder_request)
        if wait > 0:
            time.sleep(wait)
        request = Request(
            "https://nominatim.openstreetmap.org/search?"
            + urlencode(
                {
                    "q": query,
                    "format": "jsonv2",
                    "addressdetails": 1,
                    "limit": 5,
                    "countrycodes": "us",
                }
            ),
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "FishingTracker/1.0 "
                    "(https://github.com/prakkattack96/fishing-tracker-web)"
                ),
            },
        )
        _last_geocoder_request = time.monotonic()
        try:
            with urlopen(request, timeout=15) as response:
                result = json.loads(response.read())
        except HTTPError as error:
            raise DataSourceError(
                "OpenStreetMap geocoder",
                f"OpenStreetMap geocoder returned HTTP {error.code}.",
                error.code,
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise DataSourceError(
                "OpenStreetMap geocoder", "Could not reach OpenStreetMap geocoder."
            ) from error
        except json.JSONDecodeError as error:
            raise DataSourceError(
                "OpenStreetMap geocoder", "OpenStreetMap geocoder returned invalid data."
            ) from error
    if not isinstance(result, list):
        raise DataSourceError(
            "OpenStreetMap geocoder", "OpenStreetMap geocoder returned unexpected data."
        )
    return result


def _resolve_named_waterbody(water_body: str, state: str) -> dict:
    site, site_error = _waterbody_site(water_body, state)
    if site:
        return {
            "latitude": site["latitude"],
            "longitude": site["longitude"],
            "place": site["station_nm"],
            "reference": f"USGS site {site['site_no']} ({site['station_nm']})",
            "provider": "USGS",
            "site": {
                "name": site["station_nm"],
                "site_type": site["site_tp_cd"],
                "site_number": site["site_no"],
            },
        }

    query = f"{water_body}, {STATES[state]}, United States"
    try:
        candidates = _nominatim_locations(query)
    except DataSourceError as error:
        raise HTTPException(
            status_code=502,
            detail=(
                "Could not look up that waterbody by name. "
                "Please try again in a moment."
                + (f" {site_error}" if site_error else "")
            ),
        ) from error

    query_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", water_body.casefold())
        if token not in {"lake", "river", "stream", "creek", "pond", "reservoir", "water"}
    }
    matches = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        address = candidate.get("address")
        if not isinstance(address, dict):
            continue
        candidate_state = str(address.get("state", "")).casefold()
        state_code = str(address.get("ISO3166-2-lvl4", "")).split("-")[-1].upper()
        if candidate_state != STATES[state].casefold() and state_code != state:
            continue
        name = str(candidate.get("name", ""))
        display_name = str(candidate.get("display_name", ""))
        searchable_name = f"{name} {display_name}".casefold()
        candidate_tokens = set(re.findall(r"[a-z0-9]+", searchable_name))
        feature_class = str(candidate.get("class", "")).casefold()
        feature_type = str(candidate.get("type", "")).casefold()
        if (
            feature_class and feature_class not in {"natural", "water", "waterway"}
        ) or feature_type not in {
            "bay",
            "canal",
            "coastline",
            "creek",
            "lake",
            "lagoon",
            "pond",
            "reservoir",
            "river",
            "riverbank",
            "stream",
            "water",
            "wetland",
        }:
            continue
        if query_tokens and not query_tokens.issubset(candidate_tokens):
            continue
        if not query_tokens and water_body.casefold() not in searchable_name:
            continue
        try:
            latitude = float(candidate["lat"])
            longitude = float(candidate["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        matches.append(
            {
                "latitude": latitude,
                "longitude": longitude,
                "place": display_name or name or water_body,
                "reference": f"OpenStreetMap result: {display_name or name or water_body}",
                "provider": "OpenStreetMap",
                "site": None,
            }
        )
    if not matches:
        detail = (
            f"USGS could not verify a named site ({site_error}). "
            if site_error
            else ""
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Could not confidently locate “{water_body}” in {STATES[state]}. "
                f"{detail}Try a more specific waterbody name."
            ),
        )
    if len(matches) > 1:
        coordinates = {
            (match["latitude"], match["longitude"]) for match in matches
        }
        if len(coordinates) > 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"More than one place named “{water_body}” was found in {STATES[state]}. "
                    "Please include a nearby town or county in the waterbody name."
                ),
            )
    return matches[0]


def _nws_forecast(location: dict) -> tuple[dict, str, datetime]:
    latitude = location["latitude"]
    longitude = location["longitude"]
    try:
        point = _external_json(
            f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}",
            "National Weather Service",
        )
        properties = point.get("properties", {})
        if not isinstance(properties, dict):
            raise TypeError("The forecast point has no properties.")
        forecast_url = properties.get("forecastHourly")
        timezone_name = str(properties.get("timeZone", ""))
        zone = ZoneInfo(timezone_name)
        local_now = datetime.now(zone)
    except DataSourceError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except (KeyError, TypeError, ZoneInfoNotFoundError) as error:
        raise HTTPException(
            status_code=502,
            detail="The National Weather Service could not identify a forecast for that waterbody location.",
        ) from error
    if not isinstance(forecast_url, str) or not forecast_url.startswith("https://api.weather.gov/"):
        raise HTTPException(
            status_code=502, detail="The National Weather Service did not provide an hourly forecast."
        )
    try:
        forecast = _external_json(forecast_url, "National Weather Service")
    except DataSourceError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    forecast_properties = forecast.get("properties")
    if not isinstance(forecast_properties, dict):
        raise HTTPException(
            status_code=502, detail="The hourly forecast returned an unexpected response."
        )
    periods = forecast_properties.get("periods")
    if not isinstance(periods, list) or not periods:
        raise HTTPException(status_code=502, detail="The hourly forecast is currently unavailable.")
    today_periods = []
    for period in periods:
        try:
            start = datetime.fromisoformat(str(period["startTime"]))
        except (KeyError, TypeError, ValueError):
            continue
        if start.astimezone(local_now.tzinfo).date() == local_now.date():
            today_periods.append(period)
    if not today_periods:
        today_periods = periods[:12]
    daytime = [
        item for item in today_periods
        if isinstance(item, dict) and item.get("isDaytime") is True
    ]
    nighttime = [
        item for item in today_periods
        if isinstance(item, dict) and item.get("isDaytime") is False
    ]
    if not daytime:
        daytime = [
            item for item in periods
            if isinstance(item, dict) and item.get("isDaytime") is True
        ][:8]
    if not nighttime:
        nighttime = [
            item for item in periods
            if isinstance(item, dict) and item.get("isDaytime") is False
        ][:8]
    current_period = next(
        (item for item in periods if isinstance(item, dict)), {}
    )

    def summarize(items: list[dict]) -> dict | None:
        temperatures = [
            item.get("temperature")
            for item in items
            if isinstance(item.get("temperature"), (int, float))
        ]
        precip = [
            item.get("probabilityOfPrecipitation", {}).get("value")
            for item in items
            if isinstance(item.get("probabilityOfPrecipitation"), dict)
            and isinstance(item["probabilityOfPrecipitation"].get("value"), (int, float))
        ]
        winds = []
        for item in items:
            match = re.search(r"\d+(?:\.\d+)?", str(item.get("windSpeed", "")))
            if match:
                winds.append(float(match.group()))
        first = items[0] if items else {}
        return {
            "temperature_low_f": min(temperatures) if temperatures else None,
            "temperature_high_f": max(temperatures) if temperatures else None,
            "conditions": first.get("shortForecast"),
            "wind": first.get("windSpeed"),
            "wind_direction": first.get("windDirection"),
            "maximum_precipitation_chance_percent": max(precip) if precip else None,
            "maximum_wind_mph": max(winds) if winds else None,
            "hours_forecast": len(items),
        } if items else None

    return (
        {
            "provider": "National Weather Service",
            "forecast_date": local_now.date().isoformat(),
            "local_time_zone": timezone_name,
            "current_hour": {
                "temperature_f": current_period.get("temperature"),
                "conditions": current_period.get("shortForecast"),
                "wind": current_period.get("windSpeed"),
                "wind_direction": current_period.get("windDirection"),
            },
            "daytime": summarize(daytime),
            "nighttime": summarize(nighttime),
        },
        timezone_name,
        local_now,
    )


def _parse_usgs_sites(text: str) -> list[dict]:
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    if len(lines) < 3:
        return []
    headers = lines[0].split("\t")
    rows = []
    for line in lines[2:]:
        values = line.split("\t")
        if len(values) < len(headers):
            continue
        row = dict(zip(headers, values))
        try:
            row["latitude"] = float(row["dec_lat_va"])
            row["longitude"] = float(row["dec_long_va"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(row)
    return rows


def _waterbody_site(water_body: str, state: str) -> tuple[dict | None, str | None]:
    query_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", water_body.casefold())
        if token not in {"lake", "river", "stream", "creek", "pond", "reservoir", "water"}
    }
    if not query_tokens:
        return None, None
    search_term = max(query_tokens, key=len)
    query = urlencode(
        {
            "format": "rdb",
            "stateCd": state,
            "siteName": search_term,
            "siteStatus": "all",
            "siteType": "LK,ST",
        }
    )
    try:
        sites = _parse_usgs_sites(
            _external_text(f"https://waterservices.usgs.gov/nwis/site/?{query}", "USGS water data")
        )
    except DataSourceError as error:
        if error.status == 404:
            return None, None
        return None, str(error)
    if not sites:
        return None, None
    query_phrase = " ".join(re.findall(r"[a-z0-9]+", water_body.casefold()))
    matches = []
    for site in sites:
        name = str(site.get("station_nm", "")).casefold()
        name_tokens = set(re.findall(r"[a-z0-9]+", name))
        if not query_tokens.issubset(name_tokens):
            continue
        normalized_name = " ".join(re.findall(r"[a-z0-9]+", name))
        matches.append(
            (
                int(query_phrase in normalized_name),
                int(site.get("site_tp_cd") == "LK"),
                site,
            )
        )
    if not matches:
        return None, None
    return max(matches, key=lambda match: (match[0], match[1]))[2], None


def _distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    return 2 * radius * math.asin(min(1, math.sqrt(value)))


def _water_temperature(center: dict) -> dict:
    latitude = center["latitude"]
    longitude = center["longitude"]
    radius_miles = 75
    latitude_delta = radius_miles / 69
    longitude_delta = radius_miles / max(69 * math.cos(math.radians(latitude)), 1)
    bbox = (
        f"{longitude - longitude_delta:.4f},{latitude - latitude_delta:.4f},"
        f"{longitude + longitude_delta:.4f},{latitude + latitude_delta:.4f}"
    )
    query = urlencode(
        {
            "f": "json",
            "parameter_code": "00010",
            "bbox": bbox,
            "limit": 1000,
        }
    )
    try:
        result = _external_json(
            f"https://api.waterdata.usgs.gov/ogcapi/v1/collections/latest-continuous/items?{query}",
            "USGS water data",
        )
    except DataSourceError as error:
        return {
            "available": False,
            "message": f"USGS temperature data could not be reached ({error}).",
            "source": "https://api.waterdata.usgs.gov/ogcapi/v1/collections/latest-continuous",
        }
    now = datetime.now(timezone.utc)
    by_site: dict[str, dict] = {}
    features = result.get("features")
    if not isinstance(features, list):
        return {
            "available": False,
            "message": "USGS returned an unexpected response for water temperature.",
            "source": "https://api.waterdata.usgs.gov/ogcapi/v1/collections/latest-continuous",
        }
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        geometry = feature.get("geometry")
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            continue
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        try:
            site_longitude, site_latitude = float(coordinates[0]), float(coordinates[1])
            value = float(properties["value"])
            observed = datetime.fromisoformat(str(properties["time"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        age = now - observed.astimezone(timezone.utc)
        distance = _distance_miles(latitude, longitude, site_latitude, site_longitude)
        if age < timedelta(minutes=-15) or age > timedelta(days=7) or distance > radius_miles:
            continue
        unit = str(properties.get("unit_of_measure", "")).casefold()
        if "c" in unit or "cel" in unit:
            value = value * 9 / 5 + 32
        elif "f" not in unit:
            continue
        site_id = str(properties.get("monitoring_location_id", ""))
        if not site_id:
            continue
        reading = {
            "site_id": site_id,
            "temperature_f": round(value, 1),
            "observed_at": observed.isoformat(),
            "distance_miles": round(distance, 1),
            "approval_status": properties.get("approval_status"),
            "latitude": site_latitude,
            "longitude": site_longitude,
            "unit": properties.get("unit_of_measure"),
        }
        old = by_site.get(site_id)
        if not old or reading["observed_at"] > old["observed_at"]:
            by_site[site_id] = reading
    if not by_site:
        return {
            "available": False,
            "message": f"No recent USGS water-temperature reading was found within 75 miles of {center['reference']}.",
            "search_radius_miles": radius_miles,
            "source": "https://api.waterdata.usgs.gov/ogcapi/v1/collections/latest-continuous",
        }
    reading = min(by_site.values(), key=lambda item: item["distance_miles"])
    site_query = urlencode({"f": "json", "id": reading["site_id"], "limit": 1})
    try:
        details = _external_json(
            "https://api.waterdata.usgs.gov/ogcapi/v1/collections/monitoring-locations/items?"
            + site_query,
            "USGS station details",
        )
    except DataSourceError:
        details = {}
    detail_features = details.get("features")
    if isinstance(detail_features, list) and detail_features:
        props = detail_features[0].get("properties", {})
        if isinstance(props, dict):
            reading["station_name"] = props.get("monitoring_location_name")
    reading["available"] = True
    reading["source"] = (
        "https://waterdata.usgs.gov/monitoring-location/"
        + reading["site_id"].removeprefix("USGS-")
    )
    reading["distance_reference"] = center.get("reference", "waterbody location")
    reading["message"] = (
        "This is the nearest recent monitored reading, not a measurement taken at the fishing spot."
    )
    return reading


def _moon_phase(local_now: datetime) -> dict:
    known_new_moon = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    instant = local_now.astimezone(timezone.utc)
    cycle = 29.530588853
    phase = ((instant - known_new_moon).total_seconds() / 86400 / cycle) % 1
    illumination = (1 - math.cos(2 * math.pi * phase)) / 2 * 100
    names = (
        "New Moon", "Waxing Crescent", "First Quarter", "Waxing Gibbous",
        "Full Moon", "Waning Gibbous", "Last Quarter", "Waning Crescent",
    )
    index = int((phase * 8 + 0.5) % 8)
    return {
        "phase": names[index],
        "illumination_percent": round(illumination),
        "local_date": local_now.date().isoformat(),
        "note": "The phase is global; the waterbody location sets the local date and time used in this estimate.",
    }


def _season_for(month: int) -> str:
    if month in (12, 1, 2):
        return "winter"
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    return "fall"


def _behavior_context(
    species: str, water_temperature_f: float | None, season: str
) -> str:
    if water_temperature_f is None:
        return (
            f"No recent water-temperature reading is available for {species}. "
            f"The {season} calendar season is used only as a broad timing cue; "
            "air temperature is not used as a substitute and no local fish activity is known."
        )
    if water_temperature_f < 48:
        cue = "cold-water fish activity is often slower, so deliberate retrieves and pauses are a reasonable starting point"
    elif water_temperature_f < 58:
        cue = "cool-water fish may respond better to controlled retrieves and nearby cover or depth changes"
    elif water_temperature_f < 68:
        cue = "warming-water conditions support alternating a search lure with a slower follow-up"
    elif water_temperature_f < 80:
        cue = "warm-water fish may use shade, vegetation, and shallow-to-deep edges as conditions change"
    else:
        cue = "hot-water conditions make shade, cooler inflows, and deeper edges sensible places to check"
    return (
        f"For {species}, {cue}. This is a general temperature-based fishing heuristic, "
        "not a verified behavior report for this waterbody; actual cover and fish location were not measured."
    )


def _recommend_lures(
    target_species: str,
    water_temperature_f: float | None,
    season: str,
    weather: dict,
) -> dict:
    species = target_species or "Largemouth Bass"
    temp = water_temperature_f
    if temp is None:
        temp_band = {
            "winter": "cold",
            "spring": "warming",
            "summer": "warm",
            "fall": "cool",
        }[season]
    elif temp < 48:
        temp_band = "cold"
    elif temp < 58:
        temp_band = "cool"
    elif temp < 68:
        temp_band = "warming"
    elif temp < 80:
        temp_band = "warm"
    else:
        temp_band = "hot"

    profiles = {
        "largemouth bass": {
            "cold": ["Suspending jerkbait", "Compact jig and craw", "Small Colorado-blade spinnerbait"],
            "cool": ["Suspending jerkbait", "Lipless crankbait", "Compact spinnerbait"],
            "warming": ["Spinnerbait", "Ned rig", "Shallow crankbait"],
            "warm": ["Walking topwater", "Swim jig", "Texas-rigged soft-plastic worm"],
            "hot": ["Texas-rigged soft-plastic worm", "Compact jig and craw", "Hollow-body frog"],
            "unknown": ["Spinnerbait", "Ned rig", "Texas-rigged soft-plastic worm"],
            "night": ["Black Colorado-blade spinnerbait", "Dark jig and craw", "Buzzbait"],
            "cold_night": ["Dark compact jig", "Slow-rolled Colorado-blade spinnerbait", "Suspending jerkbait"],
        },
        "smallmouth bass": {
            "cold": ["Suspending jerkbait", "Hair jig", "Small tube jig"],
            "cool": ["Suspending jerkbait", "Tube jig", "Ned rig"],
            "warming": ["Jerkbait", "Small swimbait", "Ned rig"],
            "warm": ["Topwater walking bait", "Small swimbait", "Tube jig"],
            "hot": ["Ned rig", "Small tube jig", "Deep-diving crankbait"],
            "unknown": ["Suspending jerkbait", "Tube jig", "Ned rig"],
            "night": ["Dark finesse jig", "Black spinnerbait", "Slow-swimming swimbait"],
            "cold_night": ["Dark hair jig", "Small tube jig", "Slow suspending jerkbait"],
        },
        "crappie": {
            "cold": ["1/16-oz marabou jig", "Small minnow under a slip bobber", "Tiny tube jig"],
            "cool": ["Small minnow under a slip bobber", "1/16-oz jig", "Small paddletail"],
            "warming": ["1/16-oz jig", "Minnow under a slip bobber", "Small curly-tail grub"],
            "warm": ["Small paddletail jig", "Minnow under a slip bobber", "Small curly-tail grub"],
            "hot": ["Small jig beside deep cover", "Minnow under a slip bobber", "Small paddletail"],
            "unknown": ["1/16-oz jig", "Minnow under a slip bobber", "Small paddletail"],
            "night": ["Minnow under a lighted slip bobber", "Glow 1/16-oz jig", "Small paddletail jig"],
            "cold_night": ["Small minnow under a slip bobber", "Glow 1/16-oz jig", "Tiny tube jig"],
        },
        "walleye": {
            "cold": ["Jig tipped with a minnow", "Suspending minnow jerkbait", "Blade bait"],
            "cool": ["Jig and minnow", "Suspending jerkbait", "Shallow-running crankbait"],
            "warming": ["Jig and minnow", "Small swimbait on a jig head", "Shallow crankbait"],
            "warm": ["Jig and paddletail", "Shallow crankbait", "Night minnow-style jerkbait"],
            "hot": ["Jig and paddletail along a drop-off", "Deep-diving crankbait", "Night minnow-style jerkbait"],
            "unknown": ["Jig and minnow", "Suspending jerkbait", "Small paddletail"],
            "night": ["Dark minnow-style jerkbait", "Jig and paddletail", "Slow-rolled spinnerbait"],
            "cold_night": ["Jig and minnow", "Suspending jerkbait", "Slow blade bait"],
        },
        "northern pike": {
            "cold": ["Slow spoon", "Suspending jerkbait", "Spinnerbait with wire leader"],
            "cool": ["Spinnerbait with wire leader", "Suspending jerkbait", "Spoon"],
            "warming": ["Spinnerbait with wire leader", "Paddletail swimbait", "Spoon"],
            "warm": ["Spinnerbait with wire leader", "Paddletail swimbait", "Topwater frog"],
            "hot": ["Paddletail swimbait", "Weedless spoon", "Spinnerbait along weed edge"],
            "unknown": ["Spinnerbait with wire leader", "Spoon", "Paddletail swimbait"],
            "night": ["Black spinnerbait with wire leader", "Large dark swimbait", "Slow-rolled spoon"],
            "cold_night": ["Slow spoon with wire leader", "Suspending jerkbait", "Slow spinnerbait"],
        },
        "catfish": {
            "cold": ["Cut bait on a slip-sinker rig", "Nightcrawler on a slip-sinker rig", "Small jig near bottom"],
            "cool": ["Cut bait on a slip-sinker rig", "Nightcrawler on a slip-sinker rig", "Live minnow under a float"],
            "warming": ["Cut bait on a slip-sinker rig", "Nightcrawler on a slip-sinker rig", "Live bait under a float"],
            "warm": ["Cut bait on a slip-sinker rig", "Chicken liver on a slip-sinker rig", "Nightcrawler on a slip-sinker rig"],
            "hot": ["Cut bait on a slip-sinker rig", "Nightcrawler near deeper current", "Chicken liver on a slip-sinker rig"],
            "unknown": ["Cut bait on a slip-sinker rig", "Nightcrawler on a slip-sinker rig", "Live bait under a float"],
            "night": ["Cut bait on a slip-sinker rig", "Nightcrawler on a slip-sinker rig", "Chicken liver on a slip-sinker rig"],
            "cold_night": ["Cut bait on a slip-sinker rig", "Nightcrawler on a slip-sinker rig", "Small jig near bottom"],
        },
        "trout": {
            "cold": ["Small inline spinner", "Small spoon", "Jig under a float"],
            "cool": ["Small inline spinner", "Small spoon", "Suspending minnow bait"],
            "warming": ["Small inline spinner", "Small paddletail jig", "Small spoon"],
            "warm": ["Small spoon in moving water", "Small inline spinner", "Jig under a float"],
            "hot": ["Small spoon in cooler inflow", "Small inline spinner", "Jig under a float"],
            "unknown": ["Small inline spinner", "Small spoon", "Jig under a float"],
            "night": ["Small dark spoon", "Inline spinner", "Suspending minnow bait"],
            "cold_night": ["Small spoon", "Inline spinner", "Jig under a float"],
        },
    }
    key = species.casefold()
    profile = profiles.get(key, profiles["largemouth bass"])
    day_lures = list(profile[temp_band])
    cold_water = (temp is not None and temp < 58) or (temp is None and season == "winter")
    night_lures = list(profile["cold_night"] if cold_water else profile["night"])

    daytime = weather.get("daytime") or {}
    night_weather = weather.get("nighttime") or {}
    day_wind = daytime.get("maximum_wind_mph")
    day_rain = daytime.get("maximum_precipitation_chance_percent")
    night_wind = night_weather.get("maximum_wind_mph")
    day_note = (
        "Wind above 10 mph can make a moving, vibration-producing lure easier to find and cast from shore."
        if isinstance(day_wind, (int, float)) and day_wind >= 10
        else "With lighter forecast wind, cover water with a search bait, then slow down with a finesse option."
    )
    if isinstance(day_rain, (int, float)) and day_rain >= 50:
        day_note += " Rain is forecast; check water clarity and avoid unsafe rising water."
    if isinstance(day_wind, (int, float)) and day_wind >= 10:
        search_lure = next(
            (
                index
                for index, lure in enumerate(day_lures)
                if any(
                    keyword in lure.casefold()
                    for keyword in (
                        "spinnerbait",
                        "spinner",
                        "crankbait",
                        "spoon",
                        "swimbait",
                        "topwater",
                        "buzzbait",
                    )
                )
            ),
            None,
        )
        if search_lure is not None:
            day_lures.insert(0, day_lures.pop(search_lure))
    night_note = (
        "A darker silhouette can be easier for fish to track against the night sky; keep retrieves controlled near shore."
    )
    if isinstance(night_wind, (int, float)) and night_wind >= 15:
        night_note += " Forecast wind is strong; choose a protected bank or skip night casting if footing is unsafe."
    if species.casefold() == "catfish":
        day_lures = profile[temp_band]
        night_lures = profile["night"] if not cold_water else profile["cold_night"]
        day_note = "These are bait rigs rather than artificial lures; keep the rig simple for a shore setup."
        night_note = "Use a secure rod holder, keep bait and hooks organized, and follow local night-fishing rules."
    if temp is None:
        basis = (
            f"No recent nearby water-temperature reading was available. Picks use {season} timing and "
            "the forecast as context; air temperature is not treated as water temperature."
        )
    else:
        basis = f"Picks are keyed to the nearest recent measured water temperature ({temp:.1f}°F)."
    return {
        "species": species,
        "basis": basis,
        "day": [
            {"rank": index + 1, "lure": lure, "why": basis + " " + day_note}
            for index, lure in enumerate(day_lures)
        ],
        "night": [
            {"rank": index + 1, "lure": lure, "why": basis + " " + night_note}
            for index, lure in enumerate(night_lures)
        ],
        "shore_tips": [
            "Fan-cast from the bank, then work parallel to the shoreline; this keeps the lure in the near-shore strike zone longer.",
            "Start with a natural forage color in clear water. If the water is stained, try a darker or higher-contrast color; actual visibility was not measured.",
            "Look for accessible points, weed edges, riprap, inflows, docks, and windblown banks. These are suggestions to inspect, not features confirmed at this waterbody.",
            "At night, use a headlamp, tell someone where you are, and avoid casting near other people.",
        ],
        "conditions_note": day_note,
    }


def build_recommendation_report(payload: RecommendationRequest) -> dict:
    state = payload.state.strip().upper()
    if state not in STATES:
        raise HTTPException(status_code=422, detail="Choose a U.S. state from the list.")
    water_body = payload.water_body.strip()
    if len(water_body) < 2:
        raise HTTPException(status_code=422, detail="Enter the name of the lake, river, or other waterbody.")
    target_species = payload.target_species.strip()
    if target_species and target_species.casefold() not in {
        item.casefold() for item in RECOMMENDATION_SPECIES
    }:
        raise HTTPException(
            status_code=422,
            detail="Choose a target from the species list, or leave it blank for bass-first picks.",
        )
    target_species = next(
        (item for item in RECOMMENDATION_SPECIES if item.casefold() == target_species.casefold()),
        "Largemouth Bass",
    )
    location = _resolve_named_waterbody(water_body, state)
    weather, timezone_name, local_now = _nws_forecast(location)
    water_temperature = _water_temperature(location)
    if location["site"]:
        water_temperature["waterbody_site"] = location["site"]
    else:
        water_temperature["waterbody_site"] = None
        water_temperature["waterbody_site_message"] = (
            "The waterbody location was identified from OpenStreetMap; USGS may or may not have "
            "a temperature sensor at this exact water."
        )
    temp = water_temperature.get("temperature_f") if water_temperature.get("available") else None
    season = _season_for(local_now.month)
    moon = _moon_phase(local_now)
    recommendations = _recommend_lures(target_species, temp, season, weather)
    if state == "OH":
        state_source = {
            "name": "Association of Fish & Wildlife Agencies member list",
            "url": STATE_FISHING_SOURCES[state],
            "description": (
                "Use this directory to locate Ohio's state fish and wildlife agency; "
                "verify current rules and advisories there."
            ),
        }
    else:
        state_source = {
            "name": f"{STATES[state]} official fish and wildlife agency",
            "url": STATE_FISHING_SOURCES[state],
            "description": (
                "Verify this water's current fishing regulations, access, advisories, "
                "and local seasonal guidance."
            ),
        }
    notes = [
        "Lure picks are transparent seasonal/temperature heuristics, not an AI-generated catch guarantee or a live fish-activity report.",
        "The behavior context is a general heuristic; NWS and USGS do not provide real-time fish activity or verified bite reports for this waterbody.",
        "Water temperature is measured at the named waterbody only when a matching USGS site and recent sensor reading are available; otherwise the nearest recent USGS sensor is used and its distance is shown.",
        "Weather comes from the National Weather Service forecast grid at the resolved waterbody coordinates; a forecast grid may not exactly match the bank you will fish.",
        "Waterbody coordinates are resolved from a matching USGS site when possible, otherwise from OpenStreetMap results verified against the selected state. Ambiguous names are rejected instead of silently choosing a location.",
        "The report cannot observe local water clarity, structure, access, fishing pressure, or current fish location. Treat each as a factor to verify at the bank.",
        "Check the linked state agency for current seasons, size/bag limits, access rules, and any waterbody-specific advisories before fishing.",
    ]
    return {
        "water_body": water_body,
        "state": {"code": state, "name": STATES[state]},
        "location": location,
        "local_date": local_now.date().isoformat(),
        "season": season,
        "moon": moon,
        "weather": weather,
        "location_resolution": {
            "place": location["place"],
            "provider": location["provider"],
            "reference": location["reference"],
            "latitude": location["latitude"],
            "longitude": location["longitude"],
        },
        "water_temperature": water_temperature,
        "recommendations": recommendations,
        "behavior_context": _behavior_context(target_species, temp, season),
        "decision_factors": [
            {
                "factor": "Water temperature",
                "use": (
                    "Primary lure-selection signal when a recent USGS reading is available."
                    if temp is not None
                    else "Unavailable; the report does not substitute air temperature."
                ),
            },
            {
                "factor": "Season and target fish",
                "use": (
                    "Used for the fallback seasonal pattern when water temperature is unavailable; "
                    "the selected fish determines the lure profiles."
                ),
            },
            {
                "factor": "Weather",
                "use": (
                    "Wind can move a shore-search lure higher in the daytime list; precipitation "
                    "and wind add safety/clarity cautions."
                ),
            },
            {
                "factor": "Moon phase",
                "use": (
                    "Shown as local context, but not used to rank lures because it does not establish "
                    "a reliable bite prediction for this specific water."
                ),
            },
            {
                "factor": "State",
                "use": (
                    "Links to the state fish and wildlife agency for current rules and advisories; "
                    "the report does not infer a season or regulation for an unverified waterbody."
                ),
            },
        ],
        "data_notes": notes,
        "sources": [
            {
                "name": "National Weather Service hourly forecast",
                "url": "https://www.weather.gov/documentation/services-web-api",
                "description": "Government forecast and weather conditions near the resolved waterbody coordinates.",
            },
            {
                "name": "USGS measured water temperature",
                "url": water_temperature.get("source", "https://waterdata.usgs.gov/"),
                "description": "Recent temperature readings and monitoring-site details; a nearby sensor may differ from the fishing water.",
            },
            state_source,
            {
                "name": "Waterbody location",
                "url": (
                    "https://waterdata.usgs.gov/"
                    if location["provider"] == "USGS"
                    else "https://www.openstreetmap.org/copyright"
                ),
                "description": (
                    f"Location resolved by {location['provider']}: {location['place']}. "
                    + (
                        "© OpenStreetMap contributors."
                        if location["provider"] == "OpenStreetMap"
                        else ""
                    )
                ),
            },
        ],
        "timezone": timezone_name,
        "generated_at": local_now.isoformat(),
    }


@app.post("/api/recommendations")
def recommendations(payload: RecommendationRequest) -> dict:
    return build_recommendation_report(payload)


def _read_json(url: str, headers: dict[str, str], body: bytes) -> dict:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as error:
        raise HTTPException(
            status_code=502, detail="The species recognition service rejected the request."
        ) from error
    except (URLError, TimeoutError) as error:
        raise HTTPException(
            status_code=502, detail="The species recognition service is unavailable."
        ) from error
    except (json.JSONDecodeError, OSError) as error:
        raise HTTPException(
            status_code=502, detail="The species recognition service returned an invalid response."
        ) from error


def _verify_supabase_user(access_token: str, project_url: str, anon_key: str) -> None:
    request = Request(
        f"{project_url.rstrip('/')}/auth/v1/user",
        headers={
            "apikey": anon_key,
            "Authorization": f"Bearer {access_token}",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise HTTPException(status_code=401, detail="Sign in to identify a fish.")
    except HTTPError as error:
        if error.code in (401, 403):
            raise HTTPException(
                status_code=401, detail="Your session expired. Sign in again."
            ) from error
        raise HTTPException(
            status_code=502, detail="Could not verify your account with the sync service."
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise HTTPException(
            status_code=502, detail="Could not verify your account with the sync service."
        ) from error


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/identify", response_model=IdentifyResponse)
def identify_species(
    payload: IdentifyRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> IdentifyResponse:
    project_url = os.environ.get("SUPABASE_URL", "").strip()
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not project_url or not anon_key:
        raise HTTPException(
            status_code=503, detail="Cloud sign-in is not configured on the server."
        )
    if not api_key:
        raise HTTPException(
            status_code=503, detail="AI species suggestions are not configured yet."
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in before requesting an AI suggestion.")

    content_type = payload.content_type.lower().strip()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=415, detail="Choose a JPEG, PNG, or WebP photo."
        )
    try:
        image_bytes = base64.b64decode(payload.image_data, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=400, detail="The photo data is invalid.") from error
    if not image_bytes or len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413, detail="Choose a photo smaller than 8 MB."
        )

    access_token = authorization.removeprefix("Bearer ").strip()
    _verify_supabase_user(access_token, project_url, anon_key)

    species_options = list(dict.fromkeys(name.strip() for name in payload.species_options))
    species_options = [name for name in species_options if name][:MAX_SPECIES]
    if not species_options:
        raise HTTPException(status_code=400, detail="No species choices were provided.")

    image_url = (
        f"data:{content_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    )
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip()
    request_body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a cautious freshwater fish identification assistant. "
                        "Only suggest names from the user's allowed species list. "
                        "Do not guess confidently when the photo is unclear. "
                        "Return JSON with keys suggestions and needs_better_photo. "
                        "suggestions must be a list of up to 3 objects with species, "
                        "confidence (a rough model estimate between 0 and 1), and "
                        "reason (brief visual evidence). Set needs_better_photo true "
                        "when the fish is not visible or identification is too uncertain. "
                        "A suggested species must still be confirmed by the user."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Allowed species: "
                                + ", ".join(species_options)
                                + ". Identify the likely freshwater fish in this photo."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url, "detail": "low"},
                        },
                    ],
                },
            ],
        }
    ).encode("utf-8")
    upstream = _read_json(
        "https://api.openai.com/v1/chat/completions",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        request_body,
    )
    try:
        content = upstream["choices"][0]["message"]["content"]
        result = json.loads(content)
        allowed_by_casefold = {name.casefold(): name for name in species_options}
        suggestions: list[SpeciesSuggestion] = []
        for item in result.get("suggestions", [])[:3]:
            matched_name = allowed_by_casefold.get(str(item.get("species", "")).casefold())
            if matched_name is None:
                continue
            suggestions.append(
                SpeciesSuggestion(
                    species=matched_name,
                    confidence=max(0.0, min(1.0, float(item.get("confidence", 0)))),
                    reason=str(item.get("reason", "Visual estimate"))[:240],
                )
            )
        return IdentifyResponse(
            suggestions=suggestions,
            needs_better_photo=bool(result.get("needs_better_photo", not suggestions)),
        )
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(
            status_code=502, detail="The AI service returned an invalid species suggestion."
        ) from error


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(APP_DIRECTORY / "index.html")


@app.get("/config.js", include_in_schema=False)
def config() -> Response:
    config_path = APP_DIRECTORY / "config.js"
    if config_path.is_file():
        return FileResponse(config_path, media_type="application/javascript")
    return PlainTextResponse(
        (APP_DIRECTORY / "config.example.js").read_text(encoding="utf-8"),
        media_type="application/javascript",
    )


app.mount("/", StaticFiles(directory=APP_DIRECTORY), name="web")
