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
RECOMMENDATION_SPECIES = (
    "Largemouth Bass", "Smallmouth Bass", "Crappie", "Walleye",
    "Northern Pike", "Catfish", "Trout",
)
MA_WATER_FEATURES_URL = (
    "https://services1.arcgis.com/hGdibHYSPO59RG1h/arcgis/rest/services/"
    "Massachusetts_Water_Features/FeatureServer/8/query"
)
MA_FISHING_ACCESS_URL = (
    "https://services1.arcgis.com/hGdibHYSPO59RG1h/arcgis/rest/services/"
    "Office_of_Fishing_and_Boating_Access_Sites/FeatureServer/0/query"
)
NH_FISHERY_WATER_URL = (
    "https://nhgeodata.unh.edu/hosting/rest/services/Hosted/EC_NHWAP/"
    "FeatureServer/6/query"
)
NH_WATER_FEATURES_URL = (
    "https://nhgeodata.unh.edu/hosting/rest/services/Hosted/IWR_WaterResources/"
    "FeatureServer/9/query"
)
NH_FISHING_ACCESS_URL = (
    "https://services8.arcgis.com/hg1B9Egwk1I5p300/arcgis/rest/services/"
    "nh_access_sites/FeatureServer/0/query"
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


def _waterbody_tokens(water_body: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", water_body.casefold())
        if token not in {
            "lake", "river", "stream", "creek", "brook", "pond",
            "reservoir", "water", "the", "in", "near", "at", "by",
        }
    }


def _waterbody_name_tokens(water_body: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", water_body.casefold())
        if token not in {"the", "in", "near", "at", "by"}
    }


def _waterbody_query_parts(water_body: str) -> tuple[set[str], set[str]]:
    match = re.search(r",|\b(?:in|near|at)\b", water_body, re.IGNORECASE)
    if not match:
        return _waterbody_name_tokens(water_body), set()
    return (
        _waterbody_name_tokens(water_body[:match.start()]),
        _waterbody_name_tokens(water_body[match.end():]),
    )


def _infer_water_type(name: str, provider_type: str = "") -> str:
    searchable = f"{name} {provider_type}".casefold()
    if "reservoir" in searchable:
        return "reservoir"
    if any(token in searchable for token in ("stream", "brook", "creek")):
        return "stream"
    if "river" in searchable:
        return "river"
    if "pond" in searchable:
        return "pond"
    if "lake" in searchable or provider_type.casefold() == "water":
        return "lake"
    if "canal" in searchable:
        return "river"
    return "waterbody"


def _state_waterbody_matches(water_body: str, state: str) -> tuple[list[dict], str | None]:
    waterbody_tokens, _ = _waterbody_query_parts(water_body)
    if not waterbody_tokens or state not in {"MA", "NH"}:
        return [], None
    search_tokens = sorted(waterbody_tokens, key=lambda token: (-len(token), token))
    if state == "MA":
        endpoint = MA_WATER_FEATURES_URL
        name_field = "NAME"
        out_fields = "NAME,MINOR_TOT,POLY_CODE,PALIS_ID,SOURCE"
        source = "MassGIS"
        queries = [(endpoint, name_field, out_fields, source)]
    else:
        queries = [
            (
                NH_FISHERY_WATER_URL,
                "gnis_name",
                "gnis_name,nhfishery,nhfishnote,acres,maxdepth_m",
                "New Hampshire Fish and Game",
            ),
            (
                NH_WATER_FEATURES_URL,
                "gnis_name",
                "gnis_name,ftype,fcode",
                "NH GRANIT",
            ),
        ]
    for endpoint, name_field, out_fields, source in queries:
        for search_token in search_tokens:
            try:
                result = _external_json(
                    endpoint
                    + "?"
                    + urlencode(
                        {
                            "where": f"UPPER({name_field}) LIKE '%{search_token.upper()}%'",
                            "outFields": out_fields,
                            "returnGeometry": "false",
                            "returnCentroid": "true",
                            "outSR": "4326",
                            "resultRecordCount": 100,
                            "f": "json",
                        }
                    ),
                    source,
                )
            except DataSourceError:
                break
            features = result.get("features")
            if not isinstance(features, list):
                continue
            matches = []
            for feature in features:
                if not isinstance(feature, dict):
                    continue
                attributes = feature.get("attributes")
                centroid = feature.get("centroid")
                if not isinstance(attributes, dict) or not isinstance(centroid, dict):
                    continue
                name = str(attributes.get(name_field, "")).strip()
                name_tokens = _waterbody_name_tokens(name)
                if (
                    not name
                    or not name_tokens
                    or not name_tokens.issubset(waterbody_tokens)
                ):
                    continue
                try:
                    latitude = float(centroid["y"])
                    longitude = float(centroid["x"])
                except (KeyError, TypeError, ValueError):
                    continue
                matches.append(
                    {
                        "latitude": latitude,
                        "longitude": longitude,
                        "place": name,
                        "reference": f"{source} waterbody inventory: {name}",
                        "provider": source,
                        "site": None,
                        "water_type": _infer_water_type(
                            name, str(attributes.get("ftype", ""))
                        ),
                        "fishery": {
                            "category": attributes.get("nhfishery"),
                            "note": attributes.get("nhfishnote"),
                        }
                        if state == "NH" and attributes.get("nhfishery")
                        else None,
                    }
                )
            if matches:
                return matches, None
    return [], None


def _disambiguate_state_waterbody_matches(
    matches: list[dict], water_body: str, state: str
) -> dict | None:
    if not matches:
        return None

    unique_matches = []
    for match in matches:
        if not any(
            _distance_miles(
                match["latitude"],
                match["longitude"],
                existing["latitude"],
                existing["longitude"],
            )
            < 0.1
            for existing in unique_matches
        ):
            unique_matches.append(match)
    if len(unique_matches) == 1:
        return unique_matches[0]

    query_tokens = _waterbody_name_tokens(water_body)
    name_tokens = _waterbody_name_tokens(unique_matches[0]["place"])
    _, explicit_locality_tokens = _waterbody_query_parts(water_body)
    locality_tokens = explicit_locality_tokens or query_tokens - name_tokens
    if not locality_tokens:
        return None

    state_name = STATES[state]
    query = f"{water_body}, {state_name}, United States"
    try:
        candidates = _nominatim_locations(query)
    except DataSourceError:
        candidates = []

    water_types = {
        "bay", "canal", "coastline", "creek", "lake", "lagoon", "pond",
        "reservoir", "river", "riverbank", "stream", "water", "wetland",
    }
    located_candidates = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        address = candidate.get("address")
        if not isinstance(address, dict):
            continue
        candidate_state = str(address.get("state", "")).casefold()
        state_code = str(address.get("ISO3166-2-lvl4", "")).split("-")[-1].upper()
        if candidate_state != state_name.casefold() and state_code != state:
            continue
        searchable = (
            f"{candidate.get('name', '')} {candidate.get('display_name', '')}"
        ).casefold()
        candidate_tokens = set(re.findall(r"[a-z0-9]+", searchable))
        if not name_tokens.issubset(candidate_tokens):
            continue
        address_tokens = set(
            re.findall(
                r"[a-z0-9]+",
                " ".join(str(value) for value in address.values()).casefold(),
            )
        )
        if not locality_tokens.issubset(candidate_tokens | address_tokens):
            continue
        if str(candidate.get("type", "")).casefold() not in water_types:
            continue
        try:
            latitude = float(candidate["lat"])
            longitude = float(candidate["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        located_candidates.append((latitude, longitude, candidate))

    if located_candidates:
        ranked_matches = []
        for match in unique_matches:
            nearest_distance = min(
                _distance_miles(
                    match["latitude"],
                    match["longitude"],
                    latitude,
                    longitude,
                )
                for latitude, longitude, _ in located_candidates
            )
            ranked_matches.append((nearest_distance, match))
        ranked_matches.sort(key=lambda result: result[0])
        if (
            ranked_matches[0][0] <= 5
            and (
                len(ranked_matches) == 1
                or ranked_matches[1][0] - ranked_matches[0][0] >= 0.1
            )
        ):
            return ranked_matches[0][1]

    locality = " ".join(
        token for token in re.findall(r"[a-z0-9]+", water_body.casefold())
        if token in locality_tokens
    )
    try:
        localities = _nominatim_locations(
            f"{locality}, {state_name}, United States"
        )
    except DataSourceError:
        return None
    for candidate in localities:
        if not isinstance(candidate, dict):
            continue
        address = candidate.get("address")
        if not isinstance(address, dict):
            continue
        candidate_state = str(address.get("state", "")).casefold()
        state_code = str(address.get("ISO3166-2-lvl4", "")).split("-")[-1].upper()
        if candidate_state != state_name.casefold() and state_code != state:
            continue
        try:
            latitude = float(candidate["lat"])
            longitude = float(candidate["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        ranked_matches = sorted(
            [
                (
                    _distance_miles(
                        match["latitude"],
                        match["longitude"],
                        latitude,
                        longitude,
                    ),
                    match,
                )
                for match in unique_matches
            ],
            key=lambda result: result[0],
        )
        if (
            ranked_matches[0][0] <= 25
            and (
                len(ranked_matches) == 1
                or ranked_matches[1][0] - ranked_matches[0][0] >= 1
            )
        ):
            return ranked_matches[0][1]
    return None


def _resolve_named_waterbody(water_body: str, state: str) -> dict:
    state_matches, _ = _state_waterbody_matches(water_body, state)
    if state_matches:
        location = _disambiguate_state_waterbody_matches(
            state_matches, water_body, state
        )
        if location:
            return location
        if len(state_matches) > 1 and not (
            _waterbody_name_tokens(water_body)
            - _waterbody_name_tokens(state_matches[0]["place"])
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"More than one place named “{water_body}” was found in {STATES[state]}. "
                    "Please include a nearby town or county in the waterbody name."
                ),
            )
        if len(state_matches) == 1:
            return state_matches[0]

    site, site_error = _waterbody_site(water_body, state)
    if site:
        return {
            "latitude": site["latitude"],
            "longitude": site["longitude"],
            "place": site["station_nm"],
            "reference": f"USGS site {site['site_no']} ({site['station_nm']})",
            "provider": "USGS",
            "water_type": _infer_water_type(
                site["station_nm"],
                "lake" if site["site_tp_cd"] == "LK" else "river",
            ),
            "site": {
                "name": site["station_nm"],
                "site_type": site["site_tp_cd"],
                "site_number": site["site_no"],
            },
            "fishery": None,
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

    query_tokens = _waterbody_tokens(water_body)
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
                "water_type": _infer_water_type(
                    water_body, str(candidate.get("type", ""))
                ),
                "fishery": None,
                "_candidate_name": name,
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
    unique_matches = []
    for match in matches:
        if not any(
            _distance_miles(
                match["latitude"],
                match["longitude"],
                existing["latitude"],
                existing["longitude"],
            )
            < 0.1
            for existing in unique_matches
        ):
            unique_matches.append(match)

    locality_tokens = _waterbody_query_parts(water_body)[1]
    if unique_matches:
        locality_tokens = locality_tokens or (
            _waterbody_name_tokens(water_body)
            - _waterbody_name_tokens(unique_matches[0]["_candidate_name"])
        )
    locality = ""
    if len(unique_matches) > 1 and locality_tokens:
        locality = " ".join(
            token for token in re.findall(r"[a-z0-9]+", water_body.casefold())
            if token in locality_tokens
        )
        try:
            localities = _nominatim_locations(
                f"{locality}, {STATES[state]}, United States"
            )
        except DataSourceError:
            localities = []
        for candidate in localities:
            if not isinstance(candidate, dict):
                continue
            address = candidate.get("address")
            if not isinstance(address, dict):
                continue
            candidate_state = str(address.get("state", "")).casefold()
            state_code = str(address.get("ISO3166-2-lvl4", "")).split("-")[-1].upper()
            if candidate_state != STATES[state].casefold() and state_code != state:
                continue
            try:
                latitude = float(candidate["lat"])
                longitude = float(candidate["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            ranked_matches = sorted(
                [
                    (
                        _distance_miles(
                            match["latitude"],
                            match["longitude"],
                            latitude,
                            longitude,
                        ),
                        match,
                    )
                    for match in unique_matches
                ],
                key=lambda result: result[0],
            )
            if (
                ranked_matches[0][0] <= 25
                and ranked_matches[1][0] - ranked_matches[0][0] >= 1
            ):
                return {
                    key: value
                    for key, value in ranked_matches[0][1].items()
                    if not key.startswith("_")
                }

    if len(unique_matches) > 1:
        raise HTTPException(
            status_code=422,
            detail=(
                f"More than one place named “{water_body}” was found in {STATES[state]}. "
                + (
                    f"“{locality}” did not identify a single location. "
                    "Try adding a county or nearby landmark."
                    if locality_tokens
                    else "Please include a nearby town or county in the waterbody name."
                )
            ),
        )
    return {
        key: value
        for key, value in unique_matches[0].items()
        if not key.startswith("_")
    }


def _local_time_and_wind(location: dict) -> tuple[str, datetime, dict]:
    latitude = location["latitude"]
    longitude = location["longitude"]
    try:
        point = _external_json(
            f"https://api.weather.gov/points/{latitude:.4f},{longitude:.4f}",
            "National Weather Service",
        )
        properties = point.get("properties", {})
        if not isinstance(properties, dict):
            raise TypeError("The location response has no properties.")
        timezone_name = str(properties.get("timeZone", ""))
        zone = ZoneInfo(timezone_name)
        local_now = datetime.now(zone)
        forecast_url = properties.get("forecastHourly")
        if not isinstance(forecast_url, str) or not forecast_url.startswith(
            "https://api.weather.gov/"
        ):
            raise TypeError("The location response has no hourly forecast URL.")
        forecast = _external_json(forecast_url, "National Weather Service")
    except DataSourceError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except (KeyError, TypeError, ZoneInfoNotFoundError) as error:
        raise HTTPException(
            status_code=502,
            detail="Could not determine local conditions for the waterbody.",
        ) from error
    forecast_properties = forecast.get("properties")
    periods = forecast_properties.get("periods") if isinstance(forecast_properties, dict) else None
    if not isinstance(periods, list):
        raise HTTPException(
            status_code=502,
            detail="The hourly forecast returned an unexpected response.",
        )

    next_day = local_now + timedelta(hours=24)
    wind_speeds: dict[str, list[float]] = {"daytime": [], "nighttime": []}
    for period in periods:
        if not isinstance(period, dict) or not isinstance(period.get("isDaytime"), bool):
            continue
        try:
            start = datetime.fromisoformat(str(period["startTime"])).astimezone(zone)
        except (KeyError, TypeError, ValueError):
            continue
        if start < local_now or start > next_day:
            continue
        category = "daytime" if period["isDaytime"] else "nighttime"
        wind_values = re.findall(r"\d+(?:\.\d+)?", str(period.get("windSpeed", "")))
        wind_speeds[category].extend(float(value) for value in wind_values)

    wind = {
        category: {
            "maximum_wind_mph": max(values) if values else None,
        }
        for category, values in wind_speeds.items()
    }
    return timezone_name, local_now, wind


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


def _arcgis_nearby_features(
    endpoint: str,
    latitude: float,
    longitude: float,
    radius_miles: float,
    where: str,
    out_fields: str,
    source: str,
) -> list[dict]:
    latitude_delta = radius_miles / 69
    longitude_delta = radius_miles / max(
        69 * math.cos(math.radians(latitude)), 1
    )
    geometry = json.dumps(
        {
            "xmin": longitude - longitude_delta,
            "ymin": latitude - latitude_delta,
            "xmax": longitude + longitude_delta,
            "ymax": latitude + latitude_delta,
            "spatialReference": {"wkid": 4326},
        },
        separators=(",", ":"),
    )
    result = _external_json(
        endpoint
        + "?"
        + urlencode(
            {
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "true",
                "outSR": "4326",
                "geometry": geometry,
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "resultRecordCount": 100,
                "f": "json",
            }
        ),
        source,
    )
    features = result.get("features")
    if not isinstance(features, list):
        raise DataSourceError(source, f"{source} returned unexpected access data.")
    return features


def _nearby_fishing_access(location: dict, state: str = "") -> dict:
    latitude = location["latitude"]
    longitude = location["longitude"]
    radius_meters = 8000
    radius_miles = radius_meters / 1609.344
    query = (
        "[out:json][timeout:12];("
        f'nwr(around:{radius_meters},{latitude},{longitude})["leisure"="fishing"];'
        f'nwr(around:{radius_meters},{latitude},{longitude})["sport"="fishing"];'
        f'nwr(around:{radius_meters},{latitude},{longitude})["fishing"="yes"];'
        f'nwr(around:{radius_meters},{latitude},{longitude})["amenity"="parking"];'
        ");out center tags 80;"
    )
    fishing_spots = []
    parking = []
    if state == "MA":
        try:
            features = _arcgis_nearby_features(
                MA_FISHING_ACCESS_URL,
                latitude,
                longitude,
                radius_miles,
                "1=1",
                "FACNAME,TOWN,RESTRICTED,FEE,PARKING,STREET_PERMITED,PENDING,COMMENTS",
                "Massachusetts Office of Fishing and Boating Access",
            )
        except DataSourceError as error:
            raise HTTPException(
                status_code=502,
                detail="Massachusetts public fishing-access data is unavailable. Please try again later.",
            ) from error
        for feature in features:
            if not isinstance(feature, dict):
                continue
            attributes = feature.get("attributes")
            geometry = feature.get("geometry")
            if not isinstance(attributes, dict) or not isinstance(geometry, dict):
                continue
            if str(attributes.get("PENDING", "")).casefold() == "yes":
                continue
            if str(attributes.get("RESTRICTED", "")).casefold() == "yes":
                continue
            try:
                point_latitude = float(geometry["y"])
                point_longitude = float(geometry["x"])
            except (KeyError, TypeError, ValueError):
                continue
            distance = _distance_miles(
                latitude, longitude, point_latitude, point_longitude
            )
            if distance > radius_miles:
                continue
            permit_required = str(
                attributes.get("STREET_PERMITED", "")
            ).casefold() == "yes"
            fee = str(attributes.get("FEE", "")).strip()
            status = "State fishing access"
            rank = 0
            if permit_required:
                status = "Street permit required"
                rank = 1
            elif fee and fee.casefold() not in {"no", "none"}:
                status = f"Fee listed: {fee}"
                rank = 1
            item = {
                "name": str(attributes.get("FACNAME") or "Massachusetts fishing access"),
                "latitude": point_latitude,
                "longitude": point_longitude,
                "distance_miles": round(distance, 1),
                "access_status": status,
                "source": "Massachusetts Office of Fishing and Boating Access",
                "_access_rank": rank,
            }
            fishing_spots.append(item)
            if attributes.get("PARKING"):
                parking.append({**item, "name": f"{item['name']} parking"})
    elif state == "NH":
        try:
            features = _arcgis_nearby_features(
                NH_FISHING_ACCESS_URL,
                latitude,
                longitude,
                radius_miles,
                "UPPER(FISH) = 'YES'",
                "WATER_BODY,FACILITY,OWNERSHIP,ACCESS_TYP,FISH,PARKING_TY,"
                "SHOREBANK_,FEE,LATITUDE,LONGITUDE",
                "New Hampshire public-water access dataset",
            )
        except DataSourceError as error:
            raise HTTPException(
                status_code=502,
                detail="New Hampshire public fishing-access data is unavailable. Please try again later.",
            ) from error
        for feature in features:
            if not isinstance(feature, dict):
                continue
            attributes = feature.get("attributes")
            geometry = feature.get("geometry")
            if not isinstance(attributes, dict):
                continue
            try:
                point_latitude = float(
                    geometry["y"] if isinstance(geometry, dict) else attributes["LATITUDE"]
                )
                point_longitude = float(
                    geometry["x"] if isinstance(geometry, dict) else attributes["LONGITUDE"]
                )
            except (KeyError, TypeError, ValueError):
                continue
            distance = _distance_miles(
                latitude, longitude, point_latitude, point_longitude
            )
            if distance > radius_miles:
                continue
            fee = str(attributes.get("FEE", "")).strip()
            status = "Mapped fishing access"
            rank = 0
            if fee and fee.casefold() not in {"no", "none"}:
                status = f"Fee listed: {fee}"
                rank = 1
            item = {
                "name": str(
                    attributes.get("FACILITY")
                    or attributes.get("WATER_BODY")
                    or "New Hampshire fishing access"
                ),
                "latitude": point_latitude,
                "longitude": point_longitude,
                "distance_miles": round(distance, 1),
                "access_status": status,
                "source": "NH Fish and Game / NH GRANIT",
                "_access_rank": rank,
            }
            if str(attributes.get("SHOREBANK_", "")).casefold() in {"y", "yes"}:
                fishing_spots.append(item)
            if attributes.get("PARKING_TY"):
                parking.append({**item, "name": f"{item['name']} parking"})

    try:
        result = _external_json(
            "https://overpass-api.de/api/interpreter?" + urlencode({"data": query}),
            "OpenStreetMap",
        )
    except DataSourceError as error:
        if not fishing_spots and not parking:
            raise HTTPException(
                status_code=502,
                detail="Could not load mapped fishing spots and parking. Please try again later.",
            ) from error
        result = {"elements": []}

    elements = result.get("elements")
    if not isinstance(elements, list):
        raise HTTPException(
            status_code=502,
            detail="Mapped fishing access returned an unexpected response.",
        )

    supported_access = {"public", "yes", "permissive", "permit"}
    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags")
        if not isinstance(tags, dict):
            continue
        is_fishing = (
            tags.get("leisure") == "fishing"
            or tags.get("sport") == "fishing"
            or tags.get("fishing") == "yes"
        )
        is_parking = tags.get("amenity") == "parking"
        if not is_fishing and not is_parking:
            continue
        access = str(tags.get("access", "")).casefold()
        if access not in supported_access:
            continue

        coordinates = element.get("center", element)
        if not isinstance(coordinates, dict):
            continue
        try:
            point_latitude = float(coordinates["lat"])
            point_longitude = float(coordinates["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = _distance_miles(
            latitude, longitude, point_latitude, point_longitude
        )
        if distance > radius_miles:
            continue

        if access == "public":
            access_status = "Public access mapped"
            access_rank = 0
        elif access == "permissive":
            access_status = "Public access permitted"
            access_rank = 0
        elif access == "yes":
            access_status = "Access marked unrestricted"
            access_rank = 0
        elif access == "permit":
            access_status = "Permit required"
            access_rank = 1
        else:
            access_status = "Permit required"
            access_rank = 1
        point = {
            "name": (
                tags.get("name")
                or tags.get("description")
                or ("Fishing spot" if is_fishing else "Parking")
            ),
            "latitude": point_latitude,
            "longitude": point_longitude,
            "distance_miles": round(distance, 1),
            "access_status": access_status,
            "source": "OpenStreetMap",
            "_access_rank": access_rank,
        }
        (fishing_spots if is_fishing else parking).append(point)

    def nearest_first(points: list[dict]) -> list[dict]:
        points.sort(
            key=lambda point: (
                point["_access_rank"],
                point["distance_miles"],
                point["name"].casefold(),
            )
        )
        return [
            {key: value for key, value in point.items() if key != "_access_rank"}
            for point in points[:3]
        ]

    return {
        "search_radius_miles": round(radius_miles, 1),
        "fishing_spots": nearest_first(fishing_spots),
        "parking": nearest_first(parking),
        "attribution": (
            "MassGIS, Massachusetts Office of Fishing and Boating Access, and OpenStreetMap contributors"
            if state == "MA"
            else "NH Fish and Game, NH GRANIT, and OpenStreetMap contributors"
            if state == "NH"
            else "OpenStreetMap contributors"
        ),
    }


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


def _lure_instruction(lure: str, species: str, water_type: str = "waterbody") -> str:
    name = lure.casefold()
    cast_areas = {
        "pond": "the bank-side weed edge",
        "reservoir": "a point, cove mouth, or inflow",
        "lake": "a point or windblown shoreline",
        "river": "an eddy or current seam",
        "stream": "a riffle-to-current-seam transition",
    }
    cast_area = cast_areas.get(water_type, "visible shoreline cover")
    if "slip-sinker" in name:
        return f"Cast near {cast_area}; let the bait rest on bottom and keep the line slightly slack."
    if "bobber" in name or "under a float" in name:
        return f"Cast beside brush near {cast_area}; let the float drift naturally at the fish's depth."
    if "jerkbait" in name or "minnow-style" in name or "minnow bait" in name:
        return f"Cast parallel to {cast_area}; retrieve with two twitches, pause, then repeat."
    if "frog" in name or "topwater" in name or "buzzbait" in name:
        return f"Cast toward {cast_area}; work it steadily, pausing at openings."
    if "spinnerbait" in name:
        return f"Cast across {cast_area}; retrieve steadily and slow briefly beside cover."
    if "crankbait" in name:
        return f"Cast across {cast_area}; retrieve steadily, then pause after it bumps cover."
    if "spoon" in name or "blade bait" in name:
        return f"Cast across {cast_area}; lift briefly and let it flutter down between lifts."
    if "swimbait" in name or "paddletail" in name or "swim jig" in name:
        return f"Cast parallel to {cast_area}; swim it steadily just above the cover."
    if "ned rig" in name or "tube jig" in name or "hair jig" in name or "jig" in name:
        return f"Cast near {cast_area}; let it reach bottom, then drag or hop it slowly."
    if "stick worm" in name or "worm" in name:
        return f"Cast beside shade or cover near {cast_area}; let it sink, then lift gently and pause."
    if "spinner" in name:
        return f"Cast across {cast_area}; retrieve just fast enough to keep the blade turning."
    if species.casefold() == "catfish":
        return f"Cast beyond {cast_area}; keep bait on bottom and reel in slack without moving it."
    return f"Cast near {cast_area}; retrieve slowly with short pauses."


def _lure_identity(lure: str) -> str:
    normalized = re.sub(
        r"\b\d+(?:/\d+)?-oz\b|\b\d+(?:\.\d+)?-inch\b",
        "",
        lure.casefold(),
    )
    normalized = re.sub(r"\bfinesse\b", "", normalized)
    return re.sub(r"[\s-]+", " ", normalized).strip()


def _recommend_lures(
    target_species: str,
    water_temperature_f: float | None,
    season: str,
    weather: dict,
    water_type: str = "waterbody",
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
            "cold": ["Suspending shad-pattern jerkbait", "Green-pumpkin Ned rig", "Black-and-blue jig with craw trailer"],
            "cool": ["Suspending shad-pattern jerkbait", "White-and-chartreuse spinnerbait", "Green-pumpkin Ned rig"],
            "warming": ["White spinnerbait", "Green-pumpkin Ned rig", "Shallow shad-pattern crankbait"],
            "warm": ["Walking topwater bait", "White swim jig", "Green-pumpkin Texas-rigged worm"],
            "hot": ["Green-pumpkin Texas-rigged worm", "Black-and-blue jig with craw trailer", "Hollow-body frog"],
            "unknown": ["White spinnerbait", "Green-pumpkin Ned rig", "Green-pumpkin Texas-rigged worm"],
            "night": ["Black Colorado-blade spinnerbait", "Black wacky-rigged stick worm", "Black buzzbait"],
            "cold_night": ["Black-and-blue jig with craw trailer", "Black Colorado-blade spinnerbait", "Suspending shad-pattern jerkbait"],
        },
        "smallmouth bass": {
            "cold": ["Suspending jerkbait", "Hair jig", "Small tube jig"],
            "cool": ["Suspending jerkbait", "Tube jig", "Ned rig"],
            "warming": ["Jerkbait", "Small swimbait", "Ned rig"],
            "warm": ["Topwater walking bait", "Small swimbait", "Tube jig"],
            "hot": ["Ned rig", "Small tube jig", "Deep-diving crankbait"],
            "unknown": ["Suspending jerkbait", "Tube jig", "Ned rig"],
            "night": ["Black 1/4-oz finesse jig with craw trailer", "Black 3/8-oz spinnerbait", "3-inch smoke paddletail on 1/8-oz jig head"],
            "cold_night": ["Black hair jig", "Green-pumpkin 2.75-inch tube on 1/8-oz jig head", "Suspending shad-pattern jerkbait"],
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
            "night": ["Black-and-silver minnow jerkbait", "Jig with 3-inch paddletail", "Slow-rolled spinnerbait"],
            "cold_night": ["Jig and minnow", "Suspending jerkbait", "Slow blade bait"],
        },
        "northern pike": {
            "cold": ["Slow spoon", "Suspending jerkbait", "Spinnerbait with wire leader"],
            "cool": ["Spinnerbait with wire leader", "Suspending jerkbait", "Spoon"],
            "warming": ["Spinnerbait with wire leader", "Paddletail swimbait", "Spoon"],
            "warm": ["Spinnerbait with wire leader", "Paddletail swimbait", "Topwater frog"],
            "hot": ["Paddletail swimbait", "Weedless spoon", "Spinnerbait along weed edge"],
            "unknown": ["Spinnerbait with wire leader", "Spoon", "Paddletail swimbait"],
            "night": ["Black 3/8-oz spinnerbait with wire leader", "Black 5-inch paddletail with wire leader", "1/2-oz spoon with wire leader"],
            "cold_night": ["Slow 1/2-oz spoon with wire leader", "Suspending jerkbait with wire leader", "1/2-oz spinnerbait with wire leader"],
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
            "night": ["1/8-oz black spoon", "Small inline spinner", "Suspending minnow bait"],
            "cold_night": ["Small spoon", "Inline spinner", "Jig under a float"],
        },
    }
    key = species.casefold()
    profile = profiles.get(key, profiles["largemouth bass"])
    day_lures = list(profile[temp_band])
    cold_water = (temp is not None and temp < 58) or (temp is None and season == "winter")
    night_lures = list(profile["cold_night"] if cold_water else profile["night"])
    waterbody_picks = {
        "pond": {
            "largemouth bass": ("4-inch green-pumpkin wacky-rigged stick worm", "5-inch black wacky-rigged Senko"),
            "smallmouth bass": ("2.75-inch green-pumpkin tube on a 1/8-oz jig head", "Black 2.75-inch tube on a 1/8-oz jig head"),
            "crappie": ("1/16-oz chartreuse jig under a slip bobber", "1/16-oz glow jig under a slip bobber"),
            "walleye": ("1/8-oz jig tipped with a 3-inch minnow", "Black-and-silver minnow jerkbait"),
            "northern pike": ("1/4-oz silver spoon with wire leader", "Black 3/8-oz spinnerbait with wire leader"),
            "catfish": ("Nightcrawler on a 1/4-oz slip-sinker rig", "Cut bait on a 1/2-oz slip-sinker rig"),
            "trout": ("1/8-oz black-and-gold inline spinner", "1/8-oz black spoon"),
        },
        "reservoir": {
            "largemouth bass": ("3/8-oz white-and-chartreuse spinnerbait", "1/2-oz black Colorado-blade spinnerbait"),
            "smallmouth bass": ("3-inch shad paddletail on a 1/4-oz jig head", "Black 1/4-oz finesse jig with craw trailer"),
            "crappie": ("Minnow under a 1/16-oz slip bobber", "Minnow under a lighted slip bobber"),
            "walleye": ("3/8-oz shad-pattern crankbait", "Black-and-silver minnow jerkbait"),
            "northern pike": ("3/8-oz spinnerbait with wire leader", "Black 3/8-oz spinnerbait with wire leader"),
            "catfish": ("Cut bait on a 1/2-oz slip-sinker rig", "Nightcrawler on a 1/2-oz slip-sinker rig"),
            "trout": ("1/4-oz silver spoon", "1/8-oz black spoon"),
        },
        "lake": {
            "largemouth bass": ("3/8-oz suspending shad-pattern jerkbait", "Black-and-blue 3/8-oz jig with craw trailer"),
            "smallmouth bass": ("2.75-inch green-pumpkin tube on a 1/8-oz jig head", "Black 1/4-oz finesse jig with craw trailer"),
            "crappie": ("1/16-oz marabou jig", "Minnow under a lighted slip bobber"),
            "walleye": ("1/4-oz jig tipped with a minnow", "Black-and-silver minnow jerkbait"),
            "northern pike": ("1/2-oz silver spoon with wire leader", "Black 3/8-oz spinnerbait with wire leader"),
            "catfish": ("Cut bait on a 1/2-oz slip-sinker rig", "Nightcrawler on a 1/2-oz slip-sinker rig"),
            "trout": ("1/4-oz silver spoon", "1/8-oz black spoon"),
        },
        "river": {
            "largemouth bass": ("3/8-oz white spinnerbait", "Black 1/4-oz spinnerbait"),
            "smallmouth bass": ("1/8-oz green-pumpkin tube jig", "Black 1/8-oz hair jig"),
            "crappie": ("1/16-oz jig under a slip bobber", "1/16-oz glow jig under a slip bobber"),
            "walleye": ("1/4-oz jig tipped with a minnow", "Black-and-silver minnow jerkbait"),
            "northern pike": ("1/2-oz spoon with wire leader", "Black 3/8-oz spinnerbait with wire leader"),
            "catfish": ("Cut bait on a 1/2-oz slip-sinker rig", "Nightcrawler on a 1/2-oz slip-sinker rig"),
            "trout": ("1/8-oz copper inline spinner", "1/8-oz black inline spinner"),
        },
        "stream": {
            "largemouth bass": ("1/4-oz white spinnerbait", "Black 1/8-oz tube jig"),
            "smallmouth bass": ("1/8-oz green-pumpkin tube jig", "Black 1/8-oz hair jig"),
            "crappie": ("1/32-oz jig under a slip bobber", "1/32-oz glow jig under a slip bobber"),
            "walleye": ("1/8-oz jig tipped with a minnow", "Black-and-silver minnow jerkbait"),
            "northern pike": ("1/4-oz spoon with wire leader", "Black 1/4-oz spinnerbait with wire leader"),
            "catfish": ("Nightcrawler on a 1/4-oz slip-sinker rig", "Cut bait on a 1/4-oz slip-sinker rig"),
            "trout": ("1/8-oz copper inline spinner", "1/8-oz black inline spinner"),
        },
    }
    water_picks = waterbody_picks.get(water_type, {}).get(key)
    if water_picks:
        day_pick, night_pick = water_picks
        if cold_water and key == "largemouth bass":
            day_pick = {
                "pond": "1/8-oz green-pumpkin Ned rig",
                "reservoir": "3/8-oz suspending shad-pattern jerkbait",
                "lake": "3/8-oz suspending shad-pattern jerkbait",
                "river": "1/4-oz black-and-blue jig with craw trailer",
                "stream": "1/8-oz green-pumpkin Ned rig",
            }.get(water_type, day_pick)
            night_pick = {
                "pond": "Black-and-blue 3/8-oz finesse jig with craw trailer",
                "reservoir": "Black-and-blue 3/8-oz finesse jig with craw trailer",
                "lake": "Black-and-blue 3/8-oz jig with craw trailer",
                "river": "Black 1/4-oz finesse jig with craw trailer",
                "stream": "Black 1/8-oz tube jig",
            }.get(water_type, night_pick)
        day_identity = _lure_identity(day_pick)
        night_identity = _lure_identity(night_pick)
        day_lures = [
            day_pick
        ] + [lure for lure in day_lures if _lure_identity(lure) != day_identity]
        night_lures = [
            night_pick
        ] + [lure for lure in night_lures if _lure_identity(lure) != night_identity]
    daytime = weather.get("daytime") or {}
    day_wind = daytime.get("maximum_wind_mph")
    if isinstance(day_wind, (int, float)) and day_wind >= 10:
        moving_lure = next(
            (
                index
                for index, lure in enumerate(day_lures)
                if any(
                    keyword in lure.casefold()
                    for keyword in (
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
        if moving_lure is not None:
            day_lures.insert(0, day_lures.pop(moving_lure))
    day_lures = day_lures[:3]
    night_lures = night_lures[:3]
    if temp is None:
        basis = f"Seasonal picks for {species}; no recent nearby water-temperature reading."
    else:
        basis = f"Picks for {species} using nearby water temperature ({temp:.1f}°F)."
    return {
        "species": species,
        "basis": basis,
        "day": [
            {
                "rank": index + 1,
                "lure": lure,
                "how": _lure_instruction(lure, species, water_type),
            }
            for index, lure in enumerate(day_lures)
        ],
        "night": [
            {
                "rank": index + 1,
                "lure": lure,
                "how": _lure_instruction(lure, species, water_type),
            }
            for index, lure in enumerate(night_lures)
        ],
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
    timezone_name, local_now, weather = _local_time_and_wind(location)
    reading = _water_temperature(location)
    water_temperature = {"available": bool(reading.get("available"))}
    if water_temperature["available"]:
        water_temperature["temperature_f"] = reading["temperature_f"]
        water_temperature["distance_miles"] = reading["distance_miles"]
    temp = water_temperature.get("temperature_f")
    season = _season_for(local_now.month)
    moon = _moon_phase(local_now)
    water_type = location.get("water_type", "waterbody")
    recommendations = _recommend_lures(
        target_species, temp, season, weather, water_type
    )
    access = _nearby_fishing_access(location, state)
    return {
        "water_body": water_body,
        "state": {"code": state, "name": STATES[state]},
        "water_type": water_type,
        "fishery": location.get("fishery"),
        "local_date": local_now.date().isoformat(),
        "season": season,
        "moon": moon,
        "water_temperature": water_temperature,
        "recommendations": recommendations,
        "access": access,
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
