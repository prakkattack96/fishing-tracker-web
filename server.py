"""FastAPI server for the Fishing Tracker progressive web app."""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fish_behavior import (
    KNOWLEDGE_ACCESSED_ON,
    KNOWLEDGE_BASE_VERSION,
    SOURCES as FISH_BEHAVIOR_SOURCES,
    profile_for_species,
)


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
LOCAL_REPORT_SNAPSHOT = {
    "state": "MA",
    "reported_on": "2026-11-05",
    "source": "On The Water Massachusetts Fishing Report",
    "source_url": (
        "https://onthewater.com/fishing-reports/2026/11/"
        "massachusetts-fishing-report-november-5-2026"
    ),
    "maximum_age_days": 14,
    "waters": {
        "lake quinsigamond": {
            "species": "northern pike",
            "lures": [
                "3/8-oz spinnerbait with a wire leader",
                "1/8-oz inline spinner with a wire leader",
                "Live shiner under a slip float where legal",
            ],
            "note": "The report described recent pike catches as generally shallow.",
            "reported_lure_terms": ["spinnerbait", "inline spinner", "shiner"],
        },
        "indian lake": {
            "species": "northern pike",
            "lures": [
                "3/8-oz spinnerbait with a wire leader",
                "1/8-oz inline spinner with a wire leader",
                "Live shiner under a slip float where legal",
            ],
            "note": "The report described recent pike catches as generally shallow.",
        },
        "flint pond": {
            "species": "northern pike",
            "lures": [
                "3/8-oz spinnerbait with a wire leader",
                "1/8-oz inline spinner with a wire leader",
                "Live shiner under a slip float where legal",
            ],
            "note": "The report described recent pike catches as generally shallow.",
        },
        "pratt's pond": {
            "species": "trout",
            "lures": [
                "Green floating dough bait on a size-10 hook",
                "1/8-oz black-and-gold inline spinner",
                "1/8-oz black spoon",
            ],
            "note": "A local shop report specifically mentioned green dough bait for rainbow trout.",
            "reported_lure_terms": ["dough bait"],
        },
    },
}
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
MA_BATHYMETRY_URL = (
    "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/AGOL/"
    "MassWildlife_Inland_Bathymetry/FeatureServer/0/query"
)
NH_BATHYMETRY_URL = (
    "https://nhgeodata.unh.edu/hosting/rest/services/Hosted/"
    "EDP_Bathymetry_Lakes/FeatureServer/0/query"
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


class WindRefreshRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str = Field(min_length=1, max_length=64)
    water_type: str = Field(default="waterbody", max_length=20)


class DepthChartPdfRequest(BaseModel):
    water_body: str = Field(min_length=2, max_length=100)
    state: str = Field(min_length=2, max_length=2)
    map_data: dict = Field()


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


def _nominatim_locations(query: str, include_polygon: bool = False) -> list:
    global _last_geocoder_request
    with _geocoder_lock:
        wait = 1.0 - (time.monotonic() - _last_geocoder_request)
        if wait > 0:
            time.sleep(wait)
        search_params = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 5,
            "countrycodes": "us",
        }
        if include_polygon:
            search_params.update({"polygon_geojson": 1, "polygon_threshold": 0.0001})
        request = Request(
            "https://nominatim.openstreetmap.org/search?"
            + urlencode(search_params),
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
    waterbody_tokens, explicit_locality_tokens = _waterbody_query_parts(water_body)
    matches = []
    nearby_name_matches = []
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
        raw_class = candidate.get("class")
        feature_class = raw_class.casefold() if isinstance(raw_class, str) else ""
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
        name_matches = (
            waterbody_tokens.issubset(candidate_tokens)
            if waterbody_tokens
            else water_body.casefold() in searchable_name
        )
        query_matches = query_tokens.issubset(candidate_tokens) if query_tokens else name_matches
        if not query_matches and not (explicit_locality_tokens and name_matches):
            continue
        try:
            latitude = float(candidate["lat"])
            longitude = float(candidate["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        location_match = {
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
        if query_matches:
            matches.append(location_match)
        else:
            nearby_name_matches.append(location_match)
    if not matches and nearby_name_matches and explicit_locality_tokens:
        qualifier = re.search(r",|\b(?:in|near|at)\b", water_body, re.IGNORECASE)
        locality = water_body[qualifier.end():].strip() if qualifier else ""
        if locality:
            try:
                localities = _nominatim_locations(
                    f"{locality}, {STATES[state]}, United States"
                )
            except DataSourceError:
                localities = []
            locality_points = []
            for candidate in localities:
                if not isinstance(candidate, dict):
                    continue
                address = candidate.get("address")
                if not isinstance(address, dict):
                    continue
                candidate_state = str(address.get("state", "")).casefold()
                state_code = str(address.get("ISO3166-2-lvl4", "")).split("-")[-1].upper()
                address_text = " ".join(
                    [str(candidate.get("name", "")), str(candidate.get("display_name", ""))]
                    + [str(value) for value in address.values()]
                )
                address_tokens = set(re.findall(r"[a-z0-9]+", address_text.casefold()))
                if (
                    candidate_state != STATES[state].casefold()
                    and state_code != state
                ) or not explicit_locality_tokens.issubset(address_tokens):
                    continue
                try:
                    locality_points.append(
                        (float(candidate["lat"]), float(candidate["lon"]))
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            ranked_nearby = []
            for match in nearby_name_matches:
                distance = min(
                    _distance_miles(
                        match["latitude"],
                        match["longitude"],
                        latitude,
                        longitude,
                    )
                    for latitude, longitude in locality_points
                ) if locality_points else math.inf
                ranked_nearby.append((distance, match))
            ranked_nearby.sort(key=lambda result: result[0])
            if (
                ranked_nearby
                and ranked_nearby[0][0] <= 10
                and (
                    len(ranked_nearby) == 1
                    or ranked_nearby[1][0] - ranked_nearby[0][0] >= 1
                )
            ):
                matches.append(ranked_nearby[0][1])
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
        observation_stations_url = properties.get("observationStations")
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
    forecast_signals: dict[str, dict[str, list[float]]] = {
        category: {
            "wind": [],
            "temperature": [],
            "cloud_cover": [],
            "precipitation_probability": [],
        }
        for category in ("daytime", "nighttime")
    }
    current_period = None
    for period in periods:
        if not isinstance(period, dict) or not isinstance(period.get("isDaytime"), bool):
            continue
        try:
            start = datetime.fromisoformat(str(period["startTime"])).astimezone(zone)
        except (KeyError, TypeError, ValueError):
            continue
        if start <= local_now < start + timedelta(hours=1):
            current_period = "day" if period["isDaytime"] else "night"
        if start < local_now or start > next_day:
            continue
        category = "daytime" if period["isDaytime"] else "nighttime"
        wind_values = re.findall(r"\d+(?:\.\d+)?", str(period.get("windSpeed", "")))
        forecast_signals[category]["wind"].extend(
            float(value) for value in wind_values
        )
        temperature = period.get("temperature")
        if isinstance(temperature, (int, float)):
            forecast_signals[category]["temperature"].append(float(temperature))
        elif isinstance(temperature, dict):
            try:
                temperature_value = float(temperature["value"])
            except (KeyError, TypeError, ValueError):
                temperature_value = None
            if temperature_value is not None:
                unit = str(temperature.get("unitCode", "")).casefold()
                if unit.endswith(":degc"):
                    temperature_value = temperature_value * 9 / 5 + 32
                forecast_signals[category]["temperature"].append(temperature_value)
        for property_name, signal_name in (
            ("skyCover", "cloud_cover"),
            ("probabilityOfPrecipitation", "precipitation_probability"),
        ):
            measurement = period.get(property_name)
            if isinstance(measurement, dict):
                try:
                    value = float(measurement["value"])
                except (KeyError, TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    forecast_signals[category][signal_name].append(value)

    wind = {}
    for category, signals in forecast_signals.items():
        temperatures = signals["temperature"]
        wind[category] = {
            "maximum_wind_mph": max(signals["wind"]) if signals["wind"] else None,
            "average_air_temperature_f": (
                round(sum(temperatures) / len(temperatures), 1)
                if temperatures
                else None
            ),
            "average_cloud_cover_percent": (
                round(sum(signals["cloud_cover"]) / len(signals["cloud_cover"]))
                if signals["cloud_cover"]
                else None
            ),
            "maximum_precipitation_probability_percent": (
                max(signals["precipitation_probability"])
                if signals["precipitation_probability"]
                else None
            ),
            "temperature_change_f": (
                round(temperatures[-1] - temperatures[0], 1)
                if len(temperatures) > 1
                else None
            ),
        }
    current_wind = _wind_from_observation_stations(
        observation_stations_url, location, local_now, timezone_name
    )
    wind["current"] = current_wind
    wind["current_period"] = current_period
    return timezone_name, local_now, wind


def _wind_from_observation_stations(
    stations_url: object,
    location: dict,
    local_now: datetime,
    timezone_name: str,
) -> dict:
    current_wind = _current_station_wind(stations_url, location, local_now)
    if (
        not current_wind.get("available")
        or current_wind.get("wind_direction_degrees") is None
    ):
        model_wind = _open_meteo_current_wind(location, local_now, timezone_name)
        if model_wind.get("available"):
            return model_wind
    return current_wind


def _current_station_wind(
    stations_url: object, location: dict, local_now: datetime
) -> dict:
    unavailable = {
        "available": False,
        "message": "A current NWS station wind reading is unavailable.",
        "source": "National Weather Service",
    }
    if not isinstance(stations_url, str) or not stations_url.startswith(
        "https://api.weather.gov/"
    ):
        return unavailable
    try:
        station_data = _external_json(stations_url, "National Weather Service")
    except DataSourceError as error:
        return {**unavailable, "message": f"Current NWS station wind is unavailable ({error})."}

    features = station_data.get("features")
    if not isinstance(features, list):
        return {**unavailable, "message": "NWS returned an unexpected station list."}
    stations = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            continue
        station_id = str(properties.get("stationIdentifier", ""))
        if not re.fullmatch(r"[A-Z0-9]+", station_id):
            continue
        geometry = feature.get("geometry")
        coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
            continue
        try:
            distance = _distance_miles(
                location["latitude"],
                location["longitude"],
                float(coordinates[1]),
                float(coordinates[0]),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            distance = float("inf")
        if not math.isfinite(distance):
            continue
        stations.append((distance, station_id, str(properties.get("name", station_id))))
    if not stations:
        return {**unavailable, "message": "NWS did not list a nearby observation station."}

    last_message = "No nearby station reported a recent usable wind observation."
    for distance, station_id, station_name in sorted(stations)[:5]:
        if distance > 25:
            break
        try:
            observation = _external_json(
                f"https://api.weather.gov/stations/{station_id}/observations/latest",
                "National Weather Service",
            )
        except DataSourceError as error:
            last_message = f"Current NWS station wind is unavailable ({error})."
            continue
        properties = observation.get("properties")
        if not isinstance(properties, dict):
            last_message = "NWS returned an unexpected station observation."
            continue

        wind_speed = properties.get("windSpeed")
        wind_direction = properties.get("windDirection")
        if not isinstance(wind_speed, dict):
            last_message = "NWS did not provide a usable wind-speed observation."
            continue
        if not isinstance(wind_direction, dict):
            wind_direction = {}
        try:
            speed_value = float(wind_speed["value"])
            speed_unit = str(wind_speed["unitCode"]).rsplit(":", 1)[-1]
            if speed_unit == "km_h-1":
                speed_mph = speed_value * 0.621371
            elif speed_unit == "m_s-1":
                speed_mph = speed_value * 2.23694
            elif speed_unit in {"mi_h-1", "mph"}:
                speed_mph = speed_value
            else:
                raise ValueError(f"Unsupported NWS wind-speed unit: {speed_unit}")
            if not math.isfinite(speed_mph) or speed_mph < 0:
                raise ValueError("NWS returned an invalid wind speed.")
            direction_value = wind_direction.get("value")
            direction_degrees = (
                float(direction_value) % 360 if direction_value is not None else None
            )
            if direction_degrees is not None and not math.isfinite(direction_degrees):
                raise ValueError("NWS returned an invalid wind direction.")
            observed_at = datetime.fromisoformat(
                str(properties["timestamp"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            last_message = "NWS did not provide a usable wind observation."
            continue
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        age_minutes = (
            local_now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)
        ).total_seconds() / 60
        if age_minutes < -15 or age_minutes > 90:
            last_message = "The nearest NWS wind observation is stale."
            continue

        compass = None
        if direction_degrees is not None:
            compass = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[
                round(direction_degrees / 45) % 8
            ]
        return {
            "available": True,
            "wind_speed_mph": round(speed_mph, 1),
            "wind_direction_degrees": direction_degrees,
            "wind_direction": compass,
            "station": station_name,
            "station_id": station_id,
            "distance_miles": round(distance, 1),
            "station_url": f"https://api.weather.gov/stations/{station_id}",
            "observed_at": observed_at.isoformat(),
            "age_minutes": round(max(age_minutes, 0)),
            "source": "National Weather Service",
        }
    return {**unavailable, "message": last_message}


def _open_meteo_current_wind(
    location: dict, local_now: datetime, timezone_name: str
) -> dict:
    unavailable = {
        "available": False,
        "message": "A current modelled wind estimate is unavailable.",
        "source": "Open-Meteo",
    }
    query = urlencode(
        {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current": "wind_speed_10m,wind_direction_10m",
            "wind_speed_unit": "mph",
            "timezone": "auto",
        }
    )
    try:
        result = _external_json(
            f"https://api.open-meteo.com/v1/forecast?{query}", "Open-Meteo"
        )
    except DataSourceError as error:
        return {**unavailable, "message": str(error)}
    current = result.get("current")
    if not isinstance(current, dict):
        return {**unavailable, "message": "Open-Meteo returned no current wind data."}
    try:
        speed = float(current["wind_speed_10m"])
        direction = float(current["wind_direction_10m"]) % 360
        if not math.isfinite(direction):
            raise ValueError("Open-Meteo returned an invalid wind direction.")
        observed_at = datetime.fromisoformat(str(current["time"]))
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=ZoneInfo(timezone_name))
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
        return {
            **unavailable,
            "message": f"Open-Meteo returned invalid current wind data ({error}).",
        }
    age_minutes = (
        local_now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)
    ).total_seconds() / 60
    if not math.isfinite(speed) or speed < 0 or age_minutes < -30 or age_minutes > 90:
        return {
            **unavailable,
            "message": "Open-Meteo's current wind estimate is stale or invalid.",
        }
    compass = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[
        round(direction / 45) % 8
    ]
    return {
        "available": True,
        "wind_speed_mph": round(speed, 1),
        "wind_direction_degrees": direction,
        "wind_direction": compass,
        "observed_at": observed_at.isoformat(),
        "age_minutes": round(max(age_minutes, 0)),
        "source": "Open-Meteo model estimate",
        "station_url": "https://open-meteo.com/",
        "is_modelled": True,
    }


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


def _map_path(path: object, max_points: int = 160) -> list[list[float]]:
    if not isinstance(path, list):
        return []
    points = []
    for point in path:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            longitude, latitude = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            continue
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            continue
        points.append([longitude, latitude])
    if len(points) > max_points:
        stride = math.ceil((len(points) - 1) / (max_points - 1))
        sampled = points[::stride]
        if sampled[-1] != points[-1]:
            sampled.append(points[-1])
        points = sampled
    return points


def _arcgis_map_geometry(geometry: object) -> tuple[dict | None, list[list[float]]]:
    if not isinstance(geometry, dict):
        return None, []
    rings = geometry.get("rings")
    if isinstance(rings, list):
        normalized_rings = [
            ring
            for ring in (_map_path(item, 5000) for item in rings)
            if len(ring) >= 4
        ]
        if not normalized_rings:
            return None, []
        largest_ring = max(normalized_rings, key=lambda ring: abs(_ring_area(ring)))
        return {"type": "Polygon", "coordinates": normalized_rings}, largest_ring
    paths = geometry.get("paths")
    if isinstance(paths, list):
        normalized_paths = [
            path
            for path in (_map_path(item, 5000) for item in paths)
            if len(path) >= 2
        ]
        if not normalized_paths:
            return None, []
        return {"type": "MultiLineString", "coordinates": normalized_paths}, []
    return None, []


def _ring_area(ring: list[list[float]]) -> float:
    return sum(
        ring[index][0] * ring[index + 1][1]
        - ring[index + 1][0] * ring[index][1]
        for index in range(len(ring) - 1)
    ) / 2


def _distance_to_shoreline_miles(
    latitude: float, longitude: float, ring: list[list[float]]
) -> float:
    if len(ring) < 2:
        return float("inf")
    latitude_scale = 69.0
    longitude_scale = 69.0 * math.cos(math.radians(latitude))
    point_x, point_y = longitude * longitude_scale, latitude * latitude_scale
    nearest = float("inf")
    for start, end in zip(ring, ring[1:]):
        if len(start) < 2 or len(end) < 2:
            continue
        start_x, start_y = start[0] * longitude_scale, start[1] * latitude_scale
        end_x, end_y = end[0] * longitude_scale, end[1] * latitude_scale
        delta_x, delta_y = end_x - start_x, end_y - start_y
        length_squared = delta_x * delta_x + delta_y * delta_y
        fraction = (
            max(
                0.0,
                min(
                    1.0,
                    ((point_x - start_x) * delta_x + (point_y - start_y) * delta_y)
                    / length_squared,
                ),
            )
            if length_squared
            else 0.0
        )
        closest_x = start_x + fraction * delta_x
        closest_y = start_y + fraction * delta_y
        nearest = min(
            nearest,
            math.hypot(point_x - closest_x, point_y - closest_y),
        )
    return nearest


def _point_in_ring(longitude: float, latitude: float, ring: list[list[float]]) -> bool:
    inside = False
    for start, end in zip(ring, ring[1:]):
        x1, y1 = start
        x2, y2 = end
        if (y1 > latitude) != (y2 > latitude):
            crossing_longitude = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < crossing_longitude:
                inside = not inside
    return inside


def _shoreline_sample_points(ring: list[list[float]]) -> tuple[list[list[float]], float]:
    segments = []
    perimeter = 0.0
    for start, end in zip(ring, ring[1:]):
        length = _distance_miles(start[1], start[0], end[1], end[0])
        if length <= 0:
            continue
        segments.append((start, end, length))
        perimeter += length
    if not segments or perimeter == 0:
        return [], perimeter
    count = min(240, max(12, math.ceil(perimeter / 0.025)))
    samples = []
    segment_index = 0
    segment_start_miles = 0.0
    for sample_index in range(count):
        target_miles = perimeter * sample_index / count
        while (
            segment_index < len(segments) - 1
            and segment_start_miles + segments[segment_index][2] < target_miles
        ):
            segment_start_miles += segments[segment_index][2]
            segment_index += 1
        start, end, length = segments[segment_index]
        fraction = min(1.0, max(0.0, (target_miles - segment_start_miles) / length))
        samples.append(
            [
                start[0] + (end[0] - start[0]) * fraction,
                start[1] + (end[1] - start[1]) * fraction,
            ]
        )
    return samples, perimeter


def _ring_centroid(ring: list[list[float]]) -> tuple[float, float]:
    area = _ring_area(ring)
    if abs(area) < 1e-12:
        return (
            sum(point[1] for point in ring) / len(ring),
            sum(point[0] for point in ring) / len(ring),
        )
    longitude_sum = latitude_sum = 0.0
    for index in range(len(ring) - 1):
        longitude, latitude = ring[index]
        next_longitude, next_latitude = ring[index + 1]
        cross = longitude * next_latitude - next_longitude * latitude
        longitude_sum += (longitude + next_longitude) * cross
        latitude_sum += (latitude + next_latitude) * cross
    return latitude_sum / (6 * area), longitude_sum / (6 * area)


def _geojson_map_geometry(
    geometry: object, location: dict
) -> tuple[dict | None, list[list[float]]]:
    if not isinstance(geometry, dict):
        return None, []
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        polygons = [coordinates]
    elif geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        polygons = coordinates
    else:
        return None, []
    polygons_rings = []
    for polygon in polygons:
        if not isinstance(polygon, list):
            continue
        rings = [
            ring
            for ring in (_map_path(item, 5000) for item in polygon)
            if len(ring) >= 4
        ]
        if rings:
            polygons_rings.append(rings)
    if not polygons_rings:
        return None, []

    def ring_distance(rings: list[list[list[float]]]) -> float:
        latitude, longitude = _ring_centroid(rings[0])
        return _distance_miles(
            location["latitude"], location["longitude"], latitude, longitude
        )

    nearest_polygon = min(polygons_rings, key=ring_distance)
    selected_ring = nearest_polygon[0]
    if geometry_type == "Polygon":
        return {"type": "Polygon", "coordinates": nearest_polygon}, selected_ring
    return {
        "type": "MultiPolygon",
        "coordinates": [nearest_polygon],
    }, selected_ring


def _map_waterbody_tokens(water_body: str) -> set[str]:
    name_tokens, _ = _waterbody_query_parts(water_body)
    return name_tokens - {
        "lake", "river", "stream", "creek", "brook", "pond", "reservoir",
        "water", "the",
    }


def _osm_waterbody_geometry(
    location: dict, state: str, water_body: str
) -> dict:
    source = "OpenStreetMap"
    query = f"{water_body}, {STATES[state]}, United States"
    try:
        candidates = _nominatim_locations(query, include_polygon=True)
    except DataSourceError as error:
        return {
            "geometry": None,
            "shoreline_ring": [],
            "source": source,
            "palis_id": None,
            "message": f"The OpenStreetMap waterbody outline is unavailable ({error}).",
        }

    tokens = _map_waterbody_tokens(water_body)
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
        searchable = " ".join(
            str(candidate.get(key, "")) for key in ("name", "display_name")
        )
        candidate_tokens = set(re.findall(r"[a-z0-9]+", searchable.casefold()))
        if tokens and not tokens.issubset(candidate_tokens):
            continue
        geometry, shoreline_ring = _geojson_map_geometry(
            candidate.get("geojson"), location
        )
        if not geometry or len(shoreline_ring) < 4:
            continue
        latitude, longitude = _ring_centroid(shoreline_ring)
        distance = _distance_miles(
            location["latitude"], location["longitude"], latitude, longitude
        )
        if distance > 25:
            continue
        matches.append((distance, geometry, shoreline_ring))
    if not matches:
        return {
            "geometry": None,
            "shoreline_ring": [],
            "source": source,
            "palis_id": None,
            "message": (
                "No verified shoreline outline was found in the state map layer "
                "or OpenStreetMap. Pins are omitted rather than placed at guessed "
                "coordinates."
            ),
        }
    _, geometry, shoreline_ring = min(matches, key=lambda match: match[0])
    return {
        "geometry": geometry,
        "shoreline_ring": shoreline_ring,
        "source": source,
        "palis_id": None,
        "message": (
            "Waterbody outline from OpenStreetMap; shore pins are approximate "
            "boundary checkpoints, not verified public access."
        ),
    }


def _waterbody_geometry(
    location: dict, state: str, water_body: str | None = None
) -> dict:
    if state == "MA":
        endpoint, name_field, source = MA_WATER_FEATURES_URL, "NAME", "MassGIS"
    elif state == "NH":
        endpoint, name_field, source = NH_WATER_FEATURES_URL, "gnis_name", "NH GRANIT"
    else:
        name = water_body or str(location.get("place", ""))
        return _osm_waterbody_geometry(location, state, name)

    name = water_body or str(location.get("place", ""))
    tokens = _map_waterbody_tokens(name) or _waterbody_tokens(
        str(location.get("place", ""))
    )
    if not tokens:
        return _osm_waterbody_geometry(location, state, name)
    search_token = sorted(tokens, key=lambda token: (-len(token), token))[0]
    out_fields = f"{name_field},PALIS_ID" if state == "MA" else name_field
    query = endpoint + "?" + urlencode(
        {
            "where": f"UPPER({name_field}) LIKE '%{search_token.upper()}%'",
            "outFields": out_fields,
            "returnGeometry": "true",
            "returnCentroid": "true",
            "outSR": "4326",
            "geometryPrecision": 5,
            "resultRecordCount": 100,
            "f": "json",
        }
    )
    try:
        result = _external_json(query, source)
    except DataSourceError as error:
        fallback = _osm_waterbody_geometry(location, state, name)
        if fallback["geometry"] is None:
            fallback["message"] = (
                f"The {source} waterbody outline is unavailable ({error}). "
                + fallback["message"]
            )
        return fallback
    features = result.get("features")
    if not isinstance(features, list):
        return _osm_waterbody_geometry(location, state, name)

    candidates = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        attributes = feature.get("attributes")
        if not isinstance(attributes, dict):
            continue
        feature_tokens = _waterbody_tokens(str(attributes.get(name_field, "")))
        if not tokens.issubset(feature_tokens):
            continue
        map_geometry, shoreline_ring = _arcgis_map_geometry(feature.get("geometry"))
        if map_geometry is None:
            continue
        centroid = feature.get("centroid")
        if not isinstance(centroid, dict):
            centroid = {}
        try:
            latitude = float(centroid["y"])
            longitude = float(centroid["x"])
        except (KeyError, TypeError, ValueError):
            map_coordinates = (
                shoreline_ring
                or map_geometry.get("coordinates", [[]])[0]
            )
            if not map_coordinates:
                continue
            latitude, longitude = _ring_centroid(map_coordinates)
        distance = _distance_miles(
            location["latitude"], location["longitude"], latitude, longitude
        )
        try:
            palis_id = int(attributes["PALIS_ID"]) if state == "MA" else None
        except (KeyError, TypeError, ValueError):
            palis_id = None
        candidates.append((distance, map_geometry, shoreline_ring, palis_id))
    if not candidates:
        return _osm_waterbody_geometry(location, state, name)
    _, geometry, shoreline_ring, palis_id = min(
        candidates, key=lambda candidate: candidate[0]
    )
    return {
        "geometry": geometry,
        "shoreline_ring": shoreline_ring,
        "source": source,
        "palis_id": palis_id,
        "message": None,
    }


def _bathymetry_contours(
    location: dict,
    state: str,
    water_body: str | None = None,
    palis_id: int | None = None,
    shoreline_ring: list[list[float]] | None = None,
) -> dict:
    if state == "MA":
        endpoint, name_field, depth_field = MA_BATHYMETRY_URL, "NAME", "DEPTH"
        source = "MassWildlife Inland Bathymetry"
        source_url = (
            "https://arcgisserver.digital.mass.gov/arcgisserver/rest/services/"
            "AGOL/MassWildlife_Inland_Bathymetry/FeatureServer"
        )
        source_info_url = (
            "https://www.mass.gov/info-details/"
            "massgis-data-masswildlife-inland-water-bathymetry"
        )
        attribution = (
            "Bureau of Geographic Information (MassGIS), Commonwealth of "
            "Massachusetts, Executive Office of Technology and Security Services."
        )
        unit = "ft"
    elif state == "NH":
        endpoint, name_field, depth_field = NH_BATHYMETRY_URL, "lake", "depth"
        source = "NH DES / NH Fish and Game bathymetry"
        source_url = (
            "https://nhgeodata.unh.edu/hosting/rest/services/Hosted/"
            "EDP_Bathymetry_Lakes/FeatureServer"
        )
        source_info_url = source_url
        attribution = None
        unit = "ft"
    else:
        return {
            "available": False,
            "message": "No state bathymetry layer is connected for this location.",
            "contours": [],
        }

    name = water_body or str(location.get("place", ""))
    tokens = _map_waterbody_tokens(name) or _waterbody_tokens(
        str(location.get("place", ""))
    )
    if not tokens:
        return {
            "available": False,
            "message": "No named bathymetry contours could be matched to this waterbody.",
            "contours": [],
        }
    search_token = sorted(tokens, key=lambda token: (-len(token), token))[0]
    valid_palis_id = (
        palis_id
        if state == "MA" and isinstance(palis_id, int) and not isinstance(palis_id, bool)
        else None
    )
    where = (
        f"PALIS_ID={valid_palis_id}"
        if valid_palis_id is not None
        else f"UPPER({name_field}) LIKE '%{search_token.upper()}%'"
    )
    out_fields = (
        f"{name_field},{depth_field},PALIS_ID,SHORE,DLABEL"
        if state == "MA"
        else f"{name_field},{depth_field}"
    )
    query = endpoint + "?" + urlencode(
        {
            "where": where,
            "outFields": out_fields,
            "returnGeometry": "true",
            "outSR": "4326",
            "geometryPrecision": 5,
            "resultRecordCount": 500,
            "f": "json",
        }
    )
    try:
        result = _external_json(query, source)
    except DataSourceError as error:
        return {
            "available": False,
            "message": f"The {source} layer is unavailable ({error}).",
            "contours": [],
            "source": source,
            "source_url": source_url,
            "source_info_url": source_info_url,
            "unit": unit,
        }
    features = result.get("features")
    if not isinstance(features, list):
        return {
            "available": False,
            "message": f"{source} returned an unexpected contour response.",
            "contours": [],
            "source": source,
            "source_url": source_url,
            "source_info_url": source_info_url,
            "unit": unit,
        }

    matched_features = []
    candidate_ids: dict[int, float] = {}
    for feature in features:
        if not isinstance(feature, dict):
            continue
        attributes = feature.get("attributes")
        geometry = feature.get("geometry")
        if not isinstance(attributes, dict) or not isinstance(geometry, dict):
            continue
        feature_tokens = _waterbody_tokens(str(attributes.get(name_field, "")))
        if not tokens.issubset(feature_tokens):
            continue
        if state == "MA" and str(attributes.get("SHORE", "0")) == "1":
            continue
        try:
            depth = float(attributes[depth_field])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(depth) or depth <= 0:
            continue
        paths = geometry.get("paths")
        if not isinstance(paths, list):
            continue
        try:
            feature_palis_id = int(attributes["PALIS_ID"]) if state == "MA" else None
        except (KeyError, TypeError, ValueError):
            feature_palis_id = None
        nearest_distance = min(
            (
                _distance_miles(
                    location["latitude"],
                    location["longitude"],
                    float(point[1]),
                    float(point[0]),
                )
                for path in paths
                if isinstance(path, list)
                for point in path
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ),
            default=float("inf"),
        )
        if nearest_distance > 8:
            continue
        matched_features.append(
            (feature, attributes, geometry, feature_palis_id, nearest_distance)
        )
        if feature_palis_id is not None:
            candidate_ids[feature_palis_id] = min(
                nearest_distance,
                candidate_ids.get(feature_palis_id, float("inf")),
            )

    selected_palis_id = valid_palis_id
    if state == "MA" and selected_palis_id is None and candidate_ids:
        selected_palis_id = min(
            candidate_ids, key=lambda identifier: candidate_ids[identifier]
        )

    contours = []
    total_points = 0
    for feature, attributes, geometry, feature_palis_id, _ in matched_features:
        if (
            state == "MA"
            and selected_palis_id is not None
            and feature_palis_id != selected_palis_id
        ):
            continue
        depth = float(attributes[depth_field])
        paths = geometry["paths"]
        for path in paths:
            coordinates = _map_path(path, 100)
            if len(coordinates) < 2:
                continue
            if (
                shoreline_ring
                and not any(
                    _point_in_ring(longitude, latitude, shoreline_ring)
                    for longitude, latitude in coordinates
                )
            ):
                continue
            if total_points + len(coordinates) > 1800:
                break
            contours.append(
                {
                    "depth": depth,
                    "coordinates": coordinates,
                }
            )
            total_points += len(coordinates)
        if total_points >= 1800:
            break
    depths = sorted({contour["depth"] for contour in contours})
    intervals = [
        round(second - first, 3)
        for first, second in zip(depths, depths[1:])
        if second > first
    ]
    contour_interval = (
        max(set(intervals), key=lambda interval: (intervals.count(interval), -interval))
        if intervals
        else None
    )
    contour_metadata = {
        "source": source,
        "source_url": source_url,
        "source_info_url": source_info_url,
        "attribution": attribution,
        "unit": unit,
        "contours": contours,
        "palis_id": selected_palis_id,
        "min_depth": depths[0] if depths else None,
        "max_depth": depths[-1] if depths else None,
        "depth_interval_ft": contour_interval if state == "MA" else None,
        "survey_date": None,
        "data_quality": (
            "Depths are interpolated contours, not a sounding at each shoreline "
            "pin. The source does not publish this waterbody's survey date or "
            "sounding density in the queried layer."
            if state == "MA"
            else "Depths are mapped contours, not a sounding at each shoreline pin."
        ),
    }
    return {
        "available": bool(contours),
        "message": None
        if contours
        else f"No published depth contours were found for {location.get('place', 'this waterbody')}.",
        **contour_metadata,
    }


def _bearing_degrees(
    latitude: float, longitude: float, target_latitude: float, target_longitude: float
) -> float:
    phi1, phi2 = math.radians(latitude), math.radians(target_latitude)
    delta_longitude = math.radians(target_longitude - longitude)
    y = math.sin(delta_longitude) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_longitude)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _compass_direction(degrees: float) -> str:
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[
        round(degrees / 45) % 8
    ]


def _shoreline_spots(
    shoreline_ring: list[list[float]],
    wind: dict,
    bathymetry: dict | None = None,
    target_species: str = "",
    access_spots: list[dict] | None = None,
    access_message: str | None = None,
    season: str = "",
) -> list[dict]:
    if len(shoreline_ring) < 4:
        return []
    latitude, longitude = _ring_centroid(shoreline_ring)
    sample_points, shoreline_perimeter = _shoreline_sample_points(shoreline_ring)
    if not sample_points:
        return []
    minimum_spacing = max(0.005, min(0.05, shoreline_perimeter / 9))
    current = wind.get("current", {})
    wind_available = bool(current.get("available"))
    direction = current.get("wind_direction_degrees") if wind_available else None
    speed = current.get("wind_speed_mph") if wind_available else None
    if direction is not None and speed is not None and speed >= 3:
        downwind = (float(direction) + 180) % 360
        targets = [
            (downwind, "Downwind shoreline · first check"),
            ((downwind - 45) % 360, "Adjacent downwind shoreline"),
            ((downwind + 45) % 360, "Adjacent downwind shoreline"),
        ]
    else:
        targets = [
            (0, "North shoreline check"),
            (120, "Southeast shoreline check"),
            (240, "Southwest shoreline check"),
        ]

    profile = profile_for_species(target_species)
    contour_levels: dict[float, list[tuple[float, float]]] = {}
    if bathymetry and bathymetry.get("unit") == "ft":
        for contour in bathymetry.get("contours", []):
            coordinates = contour.get("coordinates", [])
            if not coordinates:
                continue
            stride = max(1, len(coordinates) // 60)
            try:
                depth = float(contour["depth"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(depth) or depth <= 0:
                continue
            points = contour_levels.setdefault(depth, [])
            points.extend(
                (float(coordinate[0]), float(coordinate[1]))
                for coordinate in coordinates[::stride]
                if isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2
            )

    def contour_context(point: list[float]) -> dict:
        nearest_by_depth = []
        for depth, coordinates in contour_levels.items():
            distance = min(
                (
                    _distance_miles(
                        point[1], point[0], contour_latitude, contour_longitude
                    )
                    for contour_longitude, contour_latitude in coordinates
                ),
                default=float("inf"),
            )
            nearest_by_depth.append((distance, depth))
        nearest_by_depth.sort()
        nearby = sorted(
            (depth, distance)
            for distance, depth in nearest_by_depth
            if distance <= 0.05
        )
        if not nearest_by_depth or nearest_by_depth[0][0] > 0.3:
            return {
                "nearest_depth_ft": None,
                "nearest_distance_miles": None,
                "nearby_contours_ft": [],
                "feature": "no_contour",
                "seasonal_adjustment": 0.0,
            }
        nearest_distance, nearest_depth = nearest_by_depth[0]
        nearby_depths = sorted({depth for depth, _ in nearby})
        if len(nearby_depths) >= 2:
            shallowest, deepest = nearby_depths[0], nearby_depths[-1]
            if shallowest <= 5 and deepest <= 10:
                feature = "shallow_transition"
            elif shallowest < 15 and deepest >= 15:
                feature = "mid_to_deep_transition"
            else:
                feature = "deep_transition"
        elif nearest_depth <= 10:
            feature = "shallow_edge"
        elif nearest_depth <= 20:
            feature = "mid_depth_edge"
        else:
            feature = "deep_edge"
        return {
            "nearest_depth_ft": nearest_depth,
            "nearest_distance_miles": nearest_distance,
            "nearby_contours_ft": nearby_depths,
            "feature": feature,
            "seasonal_adjustment": profile["seasonal_adjustments"].get(
                season, {}
            ).get(feature, 0.0),
        }

    candidates = []
    for point in sample_points:
        bearing = _bearing_degrees(latitude, longitude, point[1], point[0])
        context = contour_context(point)
        candidates.append({"bearing": bearing, "point": point, "depth_context": context})

    def depth_guidance(context: dict) -> str:
        nearby_depths = context.get("nearby_contours_ft", [])
        if nearby_depths:
            interval = bathymetry.get("depth_interval_ft") if bathymetry else None
            interval_text = (
                f" The mapped contour interval is approximately {interval:g} ft."
                if isinstance(interval, (int, float))
                else ""
            )
            return (
                " Nearby mapped contours are "
                + ", ".join(f"{depth:g} ft" for depth in nearby_depths)
                + "; this indicates relative bottom structure, not the depth at "
                "the bank or a proven fish-holding spot."
                + interval_text
            )
        if context.get("nearest_depth_ft") is not None:
            return (
                f" The nearest mapped {context['nearest_depth_ft']:g}-ft contour "
                f"is about {context['nearest_distance_miles']:.2f} mi away; it "
                "does not establish the depth at this pin."
            )
        return " No nearby mapped contour supports a depth-specific claim here."

    def point_rationale(point: list[float], context: dict) -> str:
        bearing = _bearing_degrees(latitude, longitude, point[1], point[0])
        shore_side = _compass_direction(bearing)
        reason = (
            f"Selected on the {shore_side} shoreline as one of three spaced "
            "checks around the mapped waterbody perimeter."
        )
        if direction is not None and speed is not None and speed >= 3:
            downwind = (float(direction) + 180) % 360
            downwind_error = min(
                (bearing - downwind) % 360, (downwind - bearing) % 360
            )
            if downwind_error <= 67.5:
                reason += (
                    f" Current {current.get('wind_direction')} wind at "
                    f"{float(speed):g} mph blows toward "
                    f"{_compass_direction(downwind)}; this downwind-facing "
                    "shore is worth checking for wind-pushed surface forage."
                )
            else:
                reason += (
                    f" Current wind is from {current.get('wind_direction')} at "
                    f"{float(speed):g} mph; this is not the downwind side, so "
                    "the pin is included for shoreline coverage."
                )
        elif wind_available and speed is not None and speed < 3:
            reason += (
                f" Current wind is calm ({float(speed):g} mph), so wind did not "
                "drive this location choice."
            )
        else:
            reason += (
                " No validated current wind direction was available, so this "
                "location is based on mapped shoreline coverage, not a wind claim."
            )
        reason += depth_guidance(context)
        if context["seasonal_adjustment"]:
            reason += (
                f" This mapped {context['feature'].replace('_', ' ')} receives "
                "only a small, low-confidence seasonal ranking adjustment; it "
                "is not a measured fish location."
            )
        return reason

    selected = []
    for access in access_spots or []:
        access_type = access.get("access_type")
        if access_type not in {"Shore access", "Fishing pier"}:
            continue
        point_latitude = access.get("latitude")
        point_longitude = access.get("longitude")
        if not isinstance(point_latitude, (int, float)) or not isinstance(
            point_longitude, (int, float)
        ):
            continue
        shore_distance = _distance_to_shoreline_miles(
            point_latitude, point_longitude, shoreline_ring
        )
        if shore_distance > 0.15:
            continue
        if any(
            _distance_miles(
                point_latitude,
                point_longitude,
                existing["latitude"],
                existing["longitude"],
            )
            < 0.05
            for existing in selected
        ):
            continue
        context = contour_context([point_longitude, point_latitude])
        selected.append(
            {
                "latitude": point_latitude,
                "longitude": point_longitude,
                "label": f"{access_type} · {access.get('name', 'public site')}",
                "reason": (
                    f"{access_type} is explicitly identified by "
                    f"{access.get('source', 'the access dataset')} and lies "
                    "beside this waterbody. Confirm current hours, restrictions, "
                    "and conditions before fishing."
                    + depth_guidance(context)
                ),
                "access_type": access_type,
                "access_status": access.get("access_status"),
                "source": access.get("source"),
                "depth_context": context,
            }
        )

    for target_bearing, label in targets:
        def rank(candidate: dict) -> float:
            angular_error = min(
                (candidate["bearing"] - target_bearing) % 360,
                (target_bearing - candidate["bearing"]) % 360,
            )
            context = candidate["depth_context"]
            contour_distance = context["nearest_distance_miles"]
            contour_proximity = (
                max(0.0, 1 - contour_distance / 0.25)
                if contour_distance is not None
                else 0.0
            )
            structural_bonus = (
                0.15 if len(context["nearby_contours_ft"]) >= 2 else 0.0
            )
            seasonal_adjustment = context["seasonal_adjustment"]
            return (
                1 - angular_error / 180
                + 0.2 * contour_proximity
                + structural_bonus
                + seasonal_adjustment
            )

        ordered = sorted(candidates, key=rank, reverse=True)
        point = next(
            (
                candidate["point"]
                for candidate in ordered
                if all(
                    _distance_miles(
                        candidate["point"][1],
                        candidate["point"][0],
                        existing["latitude"],
                        existing["longitude"],
                    )
                    >= minimum_spacing
                    for existing in selected
                )
            ),
            None,
        )
        if point is None:
            if selected:
                break
            continue
        candidate = next(item for item in ordered if item["point"] is point)
        context = candidate["depth_context"]
        reason = point_rationale(point, context)
        if access_message:
            reason += f" {access_message}"
        elif not any(
            _distance_miles(
                point[1],
                point[0],
                access.get("latitude", float("inf")),
                access.get("longitude", float("inf")),
            )
            <= 0.15
            for access in access_spots or []
            if access.get("access_type") in {"Shore access", "Fishing pier"}
        ):
            reason += (
                " Public shore access is not verified for this pin; use only "
                "from a lawful, open access point."
            )
        selected.append(
            {
                "latitude": point[1],
                "longitude": point[0],
                "label": label,
                "reason": reason,
                "access_type": "Unverified shoreline section",
                "depth_context": context,
                "depth_rank_adjustment": context["seasonal_adjustment"],
            }
        )
        if len(selected) >= 3:
            break
    return selected[:3]


def _wind_shoreline_guidance(wind: dict, water_type: str) -> dict:
    current = wind.get("current", {})
    if not current.get("available"):
        return {
            "available": False,
            "message": current.get(
                "message", "Current NWS wind data is unavailable for this area."
            ),
        }
    direction = current.get("wind_direction_degrees")
    speed = current.get("wind_speed_mph")
    if direction is None or speed is None or speed < 3:
        return {
            "available": True,
            "message": (
                f"Current wind is {speed} mph with no useful direction. "
                "There is no clear wind-driven shoreline to prioritize."
            ),
        }
    downwind = (float(direction) + 180) % 360
    downwind_side = _compass_direction(downwind)
    if water_type in {"river", "stream"}:
        message = (
            f"Wind from {current.get('wind_direction')} at {speed} mph blows "
            f"toward the {downwind_side} side. On moving water, prioritize "
            "current seams, eddies, and inflows; use wind only as a secondary "
            "clue on broad, slow stretches."
        )
    else:
        message = (
            f"Wind from {current.get('wind_direction')} at {speed} mph blows "
            f"toward the {downwind_side} side. Start on that downwind shoreline, "
            "where wind may push floating forage; fan-cast parallel to the bank "
            "and around points or inlets. This is a search-side heuristic, not "
            "a confirmed bass location."
        )
    if speed >= 18:
        message += " The wind is strong; avoid exposed or unsafe shorelines."
    return {
        "available": True,
        "downwind_side": downwind_side,
        "message": message,
    }


def _waterbody_map_data(
    location: dict,
    state: str,
    wind: dict,
    water_body: str | None = None,
    target_species: str = "",
    access_data: dict | None = None,
    season: str = "",
    water_temperature: dict | None = None,
) -> dict:
    outline = _waterbody_geometry(location, state, water_body)
    bathymetry = _bathymetry_contours(
        location,
        state,
        water_body,
        outline.get("palis_id"),
        outline.get("shoreline_ring"),
    )
    profile = profile_for_species(target_species)
    behavior_note = profile["behavior_note"]
    if profile["seasonal_adjustments"] and season not in profile["seasonal_adjustments"]:
        behavior_note = (
            f"No species-specific depth adjustment is applied for {season}; "
            "the limited supporting evidence applies only in summer."
        )
    shoreline_spots = _shoreline_spots(
        outline["shoreline_ring"],
        wind,
        bathymetry,
        target_species,
        (access_data or {}).get("shore_access", []),
        (access_data or {}).get("message"),
        season,
    )
    behavior_sources = [
        {
            "id": source_id,
            **FISH_BEHAVIOR_SOURCES[source_id],
            "accessed_on": KNOWLEDGE_ACCESSED_ON,
        }
        for source_id in profile["source_ids"]
        if source_id in FISH_BEHAVIOR_SOURCES
    ]
    access_source_urls = {
        "Massachusetts Office of Fishing and Boating Access": (
            "https://www.mass.gov/info-details/saltwater-fishing-public-access"
        ),
        "NH Fish and Game / NH GRANIT": (
            "https://www.wildlife.nh.gov/fishing/new-hampshire-fishing-map"
        ),
        "OpenStreetMap": "https://www.openstreetmap.org/copyright",
    }
    used_access_sources = {
        spot["source"]
        for spot in shoreline_spots
        if spot.get("access_type") in {"Shore access", "Fishing pier"}
        and isinstance(spot.get("source"), str)
    }
    return {
        "center": {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
        },
        "geometry": outline["geometry"],
        "boundary_source": outline["source"],
        "boundary_message": outline["message"],
        "shoreline_spots": shoreline_spots,
        "access_sources": [
            {"name": source, "url": access_source_urls[source]}
            for source in sorted(used_access_sources)
            if source in access_source_urls
        ],
        "wind_guidance": _wind_shoreline_guidance(
            wind, location.get("water_type", "waterbody")
        ),
        "bathymetry": bathymetry,
        "depth_guidance": {
            "species": target_species,
            "season": season,
            "local_period": wind.get("current_period"),
            "knowledge_base_version": KNOWLEDGE_BASE_VERSION,
            "knowledge_accessed_on": KNOWLEDGE_ACCESSED_ON,
            "confidence": profile["confidence"],
            "behavior_note": behavior_note,
            "limitations": profile["limitations"],
            "sources": behavior_sources,
            "water_temperature": (
                {
                    "temperature_f": water_temperature["temperature_f"],
                    "distance_miles": water_temperature["distance_miles"],
                }
                if water_temperature
                and water_temperature.get("available")
                and isinstance(water_temperature.get("distance_miles"), (int, float))
                and isinstance(water_temperature.get("temperature_f"), (int, float))
                else None
            ),
        },
    }


def _pdf_safe_text(value: object, max_length: int = 180) -> str:
    text = str(value).encode("ascii", "replace").decode("ascii")[:max_length]
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _depth_chart_pdf(payload: DepthChartPdfRequest) -> bytes:
    map_data = payload.map_data
    bathymetry = map_data.get("bathymetry")
    if not isinstance(bathymetry, dict) or not bathymetry.get("available"):
        raise HTTPException(
            status_code=422,
            detail="A PDF chart requires published depth contours for this waterbody.",
        )
    contours = bathymetry.get("contours")
    if not isinstance(contours, list) or not contours or len(contours) > 2000:
        raise HTTPException(
            status_code=422,
            detail="The published contour data is empty or too large to export.",
        )

    def valid_point(value: object) -> tuple[float, float] | None:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return None
        try:
            longitude, latitude = float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return None
        if (
            not math.isfinite(longitude)
            or not math.isfinite(latitude)
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
        ):
            return None
        return longitude, latitude

    line_data = []
    point_count = 0
    for contour in contours:
        if not isinstance(contour, dict):
            continue
        try:
            depth = float(contour["depth"])
        except (KeyError, TypeError, ValueError):
            continue
        coordinates = contour.get("coordinates")
        if not math.isfinite(depth) or not isinstance(coordinates, list):
            continue
        points = [
            point
            for point in (valid_point(coordinate) for coordinate in coordinates)
            if point is not None
        ]
        if len(points) < 2:
            continue
        point_count += len(points)
        if point_count > 50000:
            raise HTTPException(
                status_code=422,
                detail="The contour data is too large to export in one PDF.",
            )
        line_data.append((depth, points))
    if not line_data:
        raise HTTPException(
            status_code=422,
            detail="No valid contour lines were available for the PDF chart.",
        )

    geometry = map_data.get("geometry")
    polygon_rings = []
    if isinstance(geometry, dict):
        geometry_type = geometry.get("type")
        coordinates = geometry.get("coordinates")
        if geometry_type == "Polygon" and isinstance(coordinates, list):
            polygons = [coordinates]
        elif geometry_type == "MultiPolygon" and isinstance(coordinates, list):
            polygons = coordinates
        else:
            polygons = []
        for polygon in polygons:
            if not isinstance(polygon, list):
                continue
            for ring in polygon:
                if isinstance(ring, list):
                    points = [
                        point
                        for point in (valid_point(coordinate) for coordinate in ring)
                        if point is not None
                    ]
                    if len(points) >= 4:
                        polygon_rings.append(points)

    spots = []
    raw_spots = map_data.get("shoreline_spots", [])
    if isinstance(raw_spots, list):
        for index, spot in enumerate(raw_spots[:3], start=1):
            if not isinstance(spot, dict):
                continue
            point = valid_point([spot.get("longitude"), spot.get("latitude")])
            if point is not None:
                spots.append((index, point))

    all_points = [
        point
        for _, points in line_data
        for point in points
    ]
    all_points.extend(point for ring in polygon_rings for point in ring)
    all_points.extend(point for _, point in spots)
    min_longitude = min(point[0] for point in all_points)
    max_longitude = max(point[0] for point in all_points)
    min_latitude = min(point[1] for point in all_points)
    max_latitude = max(point[1] for point in all_points)
    longitude_span = max(max_longitude - min_longitude, 0.0001)
    latitude_span = max(max_latitude - min_latitude, 0.0001)
    longitude_scale = max(
        math.cos(math.radians((min_latitude + max_latitude) / 2)), 0.01
    )
    longitude_span *= longitude_scale
    map_left, map_bottom, map_width, map_height = 42.0, 105.0, 708.0, 420.0
    scale = min(map_width / longitude_span, map_height / latitude_span)
    drawn_width, drawn_height = longitude_span * scale, latitude_span * scale
    offset_x = map_left + (map_width - drawn_width) / 2
    offset_y = map_bottom + (map_height - drawn_height) / 2

    def project(point: tuple[float, float]) -> tuple[float, float]:
        return (
            offset_x
            + (point[0] - min_longitude) * longitude_scale * scale,
            offset_y + (point[1] - min_latitude) * scale,
        )

    content = io.StringIO()
    content.write("0.04 0.15 0.22 rg 0 548 792 64 re f\n")
    content.write("1 1 1 rg BT /F1 18 Tf 42 580 Td ")
    content.write(f"({_pdf_safe_text(payload.water_body)}) Tj ET\n")
    content.write("0.08 0.18 0.24 rg BT /F1 9 Tf 42 562 Td ")
    content.write(
        f"(Depth contours from { _pdf_safe_text(bathymetry.get('source', 'published GIS data'), 90) } "
        f"| {len(line_data)} contour lines | units: {_pdf_safe_text(bathymetry.get('unit', 'unknown'), 12)}) Tj ET\n"
    )
    for ring in polygon_rings:
        x, y = project(ring[0])
        content.write(f"0.22 0.56 0.53 RG 0.82 0.91 0.89 rg 1.2 w {x:.2f} {y:.2f} m\n")
        for point in ring[1:]:
            x, y = project(point)
            content.write(f"{x:.2f} {y:.2f} l\n")
        content.write("h B\n")
    content.write("0.86 0.62 0.16 RG 1.0 w\n")
    for depth, points in line_data:
        x, y = project(points[0])
        content.write(f"{x:.2f} {y:.2f} m\n")
        for point in points[1:]:
            x, y = project(point)
            content.write(f"{x:.2f} {y:.2f} l\n")
        content.write("S\n")
        x, y = project(points[len(points) // 2])
        depth_label = _pdf_safe_text(
            f"{depth:g} {bathymetry.get('unit', 'unknown')}", 20
        )
        content.write(f"1 1 1 rg {x - 3:.2f} {y - 3:.2f} 42 11 re f\n")
        content.write(
            f"0.04 0.15 0.22 rg BT /F1 7 Tf {x:.2f} {y:.2f} Td "
            f"({depth_label}) Tj ET\n"
        )
    for index, point in spots:
        x, y = project(point)
        radius = 6.5
        content.write(
            f"0.73 0.18 0.13 rg {x - radius:.2f} {y - radius:.2f} "
            f"{radius * 2:.2f} {radius * 2:.2f} re f\n"
        )
        content.write(
            f"1 1 1 rg BT /F1 8 Tf {x - 2.2:.2f} {y - 2.8:.2f} Td "
            f"({index}) Tj ET\n"
        )
    content.write("0.04 0.15 0.22 rg BT /F1 10 Tf 42 82 Td ")
    content.write("(Depth contours) Tj ET\n")
    content.write("0.86 0.62 0.16 RG 1.2 w 130 86 m 151 86 l S\n")
    content.write("0.04 0.15 0.22 rg BT /F1 10 Tf 160 82 Td ")
    content.write("(Suggested shore checks) Tj ET\n")
    content.write("0.73 0.18 0.13 rg 323 81 10 10 re f\n")
    content.write("0.28 0.32 0.34 rg BT /F1 8 Tf 42 58 Td ")
    content.write(
        f"(Generated from { _pdf_safe_text(bathymetry.get('source_url', ''), 140) }) Tj ET\n"
    )
    content.write("0.28 0.32 0.34 rg BT /F1 8 Tf 42 42 Td ")
    content.write(
        "(Contours are interpolated depth lines, not depths at shore pins. Verify current access and chart accuracy before fishing.) Tj ET\n"
    )
    attribution = bathymetry.get("attribution")
    if isinstance(attribution, str) and attribution:
        content.write("0.28 0.32 0.34 rg BT /F1 7 Tf 42 28 Td ")
        content.write(f"({_pdf_safe_text(attribution, 180)}) Tj ET\n")

    stream = content.getvalue().encode("ascii", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 792 612] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream
        + b"endstream",
    ]
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, object_data in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{index} 0 obj\n".encode("ascii"))
        output.write(object_data)
        output.write(b"\nendobj\n")
    xref_offset = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.write(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return output.getvalue()


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


def _explicit_shore_access_type(value: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    if any(term in normalized for term in ("boat ramp", "boat launch", "launch ramp")):
        return "Boat ramp"
    if any(term in normalized for term in ("kayak launch", "canoe launch")):
        return "Kayak launch"
    if "pier" in normalized:
        return "Fishing pier"
    if any(term in normalized for term in ("shore access", "shore fishing", "shoreline access", "bank access", "shore bank")):
        return "Shore access"
    return None


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
    shore_access = []
    access_errors = []
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
        except DataSourceError:
            access_errors.append(
                "Massachusetts public fishing-access data is unavailable."
            )
            features = []
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
            access_type = _explicit_shore_access_type(
                " ".join(
                    str(attributes.get(field) or "")
                    for field in ("FACNAME", "COMMENTS")
                )
            )
            if access_type:
                item["access_type"] = access_type
                if access_type in {"Shore access", "Fishing pier"}:
                    shore_access.append(item.copy())
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
        except DataSourceError:
            access_errors.append(
                "New Hampshire public fishing-access data is unavailable."
            )
            features = []
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
                item["access_type"] = "Shore access"
                fishing_spots.append(item)
                shore_access.append(item.copy())
            else:
                access_type = _explicit_shore_access_type(
                    str(attributes.get("ACCESS_TYP") or "")
                )
                if access_type:
                    item["access_type"] = access_type
                    if access_type in {"Shore access", "Fishing pier"}:
                        fishing_spots.append(item)
                        shore_access.append(item.copy())
            if attributes.get("PARKING_TY"):
                parking.append({**item, "name": f"{item['name']} parking"})

    try:
        result = _external_json(
            "https://overpass-api.de/api/interpreter?" + urlencode({"data": query}),
            "OpenStreetMap",
        )
    except DataSourceError as error:
        if not fishing_spots and not parking:
            detail = "Could not load mapped fishing spots and parking."
            if access_errors:
                detail += " " + " ".join(access_errors)
            raise HTTPException(
                status_code=502,
                detail=detail + " Please try again later.",
            ) from error
        access_errors.append("OpenStreetMap fishing-access data is unavailable.")
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
        access_type = None
        if tags.get("man_made") == "pier" or tags.get("leisure") == "pier":
            access_type = "Fishing pier"
        elif str(tags.get("fishing", "")).casefold() == "shore" or str(
            tags.get("fishing_access", "")
        ).casefold() == "shore":
            access_type = "Shore access"
        if is_fishing and access_type in {"Shore access", "Fishing pier"}:
            point["access_type"] = access_type
            shore_access.append(point.copy())
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
        "shore_access": nearest_first(shore_access),
        "message": " ".join(access_errors) if access_errors else None,
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


def _with_lure_pattern(lure: str, species: str) -> str:
    if re.search(
        r"\b(black|white|green|chartreuse|silver|gold|smoke|shad|glow|natural|brown|copper)\b",
        lure.casefold(),
    ) or any(term in lure.casefold() for term in ("minnow", "shiner", "cut bait", "nightcrawler")):
        return lure
    species_key = species.casefold()
    if species_key in {"largemouth bass", "smallmouth bass"}:
        pattern = (
            "green-pumpkin"
            if any(term in lure.casefold() for term in ("jig", "worm", "ned rig", "tube"))
            else "natural-shad-pattern"
        )
    elif species_key == "crappie":
        pattern = "chartreuse"
    elif species_key == "walleye":
        pattern = "natural-shad-pattern"
    elif species_key == "northern pike":
        pattern = "natural-minnow-pattern"
    elif species_key == "trout":
        pattern = "black-and-gold"
    else:
        return lure
    return f"{pattern} {lure}"


def _lure_name_with_details(lure: str, species: str) -> str:
    name = re.sub(r"\bSenko\b", "stick worm", lure, flags=re.IGNORECASE)
    if "live shiner" in name.casefold():
        return "3- to 4-inch live shiner under a slip float where legal"
    if "cut bait" in name.casefold():
        return "2- to 3-inch cut-bait strip on a 1/2-oz slip-sinker rig"
    if "nightcrawler" in name.casefold() and "slip-sinker" in name.casefold():
        return "Whole nightcrawler on a 1/2-oz slip-sinker rig"
    if "chicken liver" in name.casefold():
        return "1-inch chicken-liver piece on a 1/2-oz slip-sinker rig"
    if "dough bait" in name.casefold():
        return "Green floating dough bait on a size-10 hook"

    has_measurement = bool(
        re.search(r"\b\d+(?:/\d+)?(?:\.\d+)?-oz\b|\b\d+(?:\.\d+)?-inch\b", name)
    )
    if has_measurement:
        if "jerkbait" in name.casefold() and "-inch" not in name.casefold():
            name = f"3.5- to 4-inch {name}"
        elif "ned rig" in name.casefold() and "-inch" not in name.casefold():
            name = f"3-inch {name}"
        elif "tube" in name.casefold() and "-inch" not in name.casefold():
            name = f"2.75-inch {name}"
        elif any(term in name.casefold() for term in ("swimbait", "paddletail")) and "-inch" not in name.casefold():
            name = f"3.5-inch {name}"
        if "stick worm" in name.casefold() and "weightless" not in name.casefold():
            name += " (weightless, 1/0 hook)"
        return _with_lure_pattern(name, species)

    species_key = species.casefold()
    if (
        "jerkbait" in name.casefold()
        or "minnow-style" in name.casefold()
        or "minnow bait" in name.casefold()
    ):
        size = "3.5- to 4-inch, 3/8-oz" if "bass" in species_key else "3-inch, 1/4-oz"
        return _with_lure_pattern(f"{size} {name}", species)
    if "ned rig" in name.casefold():
        return _with_lure_pattern(f"3-inch {name} on a 1/10-oz mushroom jig", species)
    if "tube" in name.casefold():
        return _with_lure_pattern(f"2.75-inch {name} on a 1/8-oz jig head", species)
    if "spinnerbait" in name.casefold():
        weight = "1/2-oz" if species_key == "northern pike" else "3/8-oz"
        return _with_lure_pattern(f"{weight} {name}", species)
    if "inline spinner" in name.casefold() or name.casefold().endswith(" spinner"):
        weight = "1/8-oz" if species_key == "trout" else "1/4-oz"
        return _with_lure_pattern(f"{weight} {name}", species)
    if "spoon" in name.casefold() or "blade bait" in name.casefold():
        weight = "1/2-oz" if species_key == "northern pike" else "1/4-oz"
        return _with_lure_pattern(f"{weight} {name}", species)
    if "crankbait" in name.casefold():
        return _with_lure_pattern(f"2.5-inch, 3/8-oz {name}", species)
    if "swimbait" in name.casefold() or "paddletail" in name.casefold():
        return _with_lure_pattern(f"3.5-inch {name} on a 1/4-oz jig head", species)
    if "topwater" in name.casefold() or "walking bait" in name.casefold():
        return _with_lure_pattern(f"3.5-inch {name}", species)
    if "stick worm" in name.casefold():
        return _with_lure_pattern(f"4-inch {name} (weightless, 1/0 hook)", species)
    if "frog" in name.casefold() or "buzzbait" in name.casefold():
        titled = (
            f"1/2-oz weedless {name}"
            if "frog" in name.casefold()
            else f"3/8-oz {name}"
        )
        return _with_lure_pattern(titled, species)
    if "jig" in name.casefold():
        return _with_lure_pattern(f"1/8-oz {name}", species)
    if (
        "slip bobber" in name.casefold()
        or "under a float" in name.casefold()
        or "under slip float" in name.casefold()
    ):
        return f"2-inch minnow under a slip float"
    return _with_lure_pattern(name, species)


def _backup_lure(lure: str, species: str) -> str:
    name = lure.casefold()
    species_key = species.casefold()
    if species_key == "largemouth bass":
        return (
            "3-inch green-pumpkin Ned rig on a 1/10-oz mushroom jig"
            if any(term in name for term in ("spinner", "crankbait", "jerkbait", "topwater", "swimbait"))
            else "3/8-oz white spinnerbait"
        )
    if species_key == "smallmouth bass":
        return (
            "2.75-inch green-pumpkin tube on a 1/8-oz jig head"
            if any(term in name for term in ("spinner", "crankbait", "jerkbait", "topwater", "swimbait"))
            else "3-inch shad paddletail on a 1/4-oz jig head"
        )
    if species_key == "crappie":
        return (
            "1/16-oz chartreuse jig under a slip float"
            if "minnow" in name or "bobber" in name or "float" in name
            else "2-inch minnow under a slip float"
        )
    if species_key == "walleye":
        return (
            "1/4-oz jig tipped with a 3-inch minnow"
            if any(term in name for term in ("crankbait", "jerkbait", "spinner", "spoon"))
            else "3/8-oz shad-pattern crankbait"
        )
    if species_key == "northern pike":
        return (
            "1/2-oz silver spoon with a wire leader"
            if any(term in name for term in ("spinner", "swimbait", "topwater"))
            else "3/8-oz spinnerbait with a wire leader"
        )
    if species_key == "catfish":
        return (
            "Whole nightcrawler on a 1/2-oz slip-sinker rig"
            if "cut bait" in name or "liver" in name
            else "2- to 3-inch cut-bait strip on a 1/2-oz slip-sinker rig"
        )
    return (
        "1/8-oz black-and-gold inline spinner"
        if any(term in name for term in ("spoon", "jig", "float", "dough"))
        else "1/8-oz black spoon"
    )


def _lure_reason(
    species: str, temperature_band: str, water_type: str, report_note: str | None
) -> str:
    if report_note:
        return report_note
    season_note = {
        "cold": "The nearby water reading is cold, so a slow presentation with pauses is the conservative choice.",
        "cool": "Cool nearby water favors a controlled retrieve and time for the bait to stay in the strike zone.",
        "warming": "The nearby water reading is in a transition range; cover both shallow edges and the first nearby break.",
        "warm": "Warm-water fish may use shade, weeds, and nearby depth changes; the exact cover is not mapped at this bank.",
        "hot": "Warm conditions make shade and cooler adjacent water worth checking; avoid assuming the whole lake is uniformly warm.",
        "unknown": "No nearby water-temperature reading is available, so this is a seasonal starting pattern rather than a local measurement.",
    }.get(temperature_band, "This is a seasonal starting pattern, not a confirmed local fish location.")
    if species.casefold() == "smallmouth bass":
        species_note = "For smallmouth, use the presentation around rock or gravel only where that bottom is actually visible or mapped."
    elif species.casefold() == "largemouth bass":
        species_note = "For largemouth, check the accessible weed edge, shade, or point rather than assuming a specific bed or dock."
    elif species.casefold() == "crappie":
        species_note = "For crappie, vary the bait depth beside visible cover; brush and fish depth are not confirmed by the map."
    elif species.casefold() == "walleye":
        species_note = "For walleye, prioritize low-light edges and mapped depth breaks where present; no local forage survey is available."
    elif species.casefold() == "northern pike":
        species_note = "For pike, work weed edges or open pockets and use a wire leader; vegetation is not mapped at this pin."
    elif species.casefold() == "catfish":
        species_note = "For catfish, keep the bait near bottom and let scent disperse; use only bait legal in this state."
    else:
        species_note = "For trout, fish the cooler or flowing water available from shore; this system does not confirm a recent stocking at this water."
    return f"{season_note} {species_note}"


def _recent_local_report(
    state: str,
    water_body: str,
    species: str,
    local_date: date,
) -> dict | None:
    if state != LOCAL_REPORT_SNAPSHOT["state"]:
        return None
    report_date = date.fromisoformat(LOCAL_REPORT_SNAPSHOT["reported_on"])
    age = (local_date - report_date).days
    if age < 0 or age > LOCAL_REPORT_SNAPSHOT["maximum_age_days"]:
        return None
    unqualified_name = water_body.split(",", 1)[0]
    normalized_name = re.sub(r"[^a-z0-9]+", " ", unqualified_name.casefold()).strip()
    for known_water, report in LOCAL_REPORT_SNAPSHOT["waters"].items():
        normalized_known = re.sub(r"[^a-z0-9]+", " ", known_water.casefold()).strip()
        if normalized_known != normalized_name:
            continue
        if report["species"] != species.casefold():
            return None
        return {
            **report,
            "reported_on": LOCAL_REPORT_SNAPSHOT["reported_on"],
            "source": LOCAL_REPORT_SNAPSHOT["source"],
            "source_url": LOCAL_REPORT_SNAPSHOT["source_url"],
        }
    return None


def _lure_instruction(
    lure: str,
    species: str,
    water_type: str = "waterbody",
    temperature_band: str = "unknown",
    period_weather: dict | None = None,
    night: bool = False,
    backup: str | None = None,
    report_note: str | None = None,
) -> str:
    name = lure.casefold()
    cast_areas = {
        "pond": "the accessible outer weed edge, shade, or a point",
        "reservoir": "an accessible point, cove mouth, or inflow",
        "lake": "a point or windblown shoreline",
        "river": "an eddy or current seam from the bank",
        "stream": "a riffle-to-current-seam transition from the bank",
    }
    cast_area = cast_areas.get(water_type, "visible shoreline cover")
    if "slip-sinker" in name:
        technique = f"Cast beyond {cast_area}; keep the bait on bottom and retrieve only to remove slack or reset it, without dragging it."
    elif "bobber" in name or "under a float" in name or "slip float" in name:
        technique = f"Cast beside visible cover near {cast_area}; set the float just above the bottom or suspended fish and retrieve by letting it drift naturally."
    elif "jerkbait" in name or "minnow-style" in name or "minnow bait" in name:
        pause = "5-8 seconds in cold water" if temperature_band in {"cold", "cool"} else "2-4 seconds"
        technique = f"Cast parallel to {cast_area}; retrieve with two twitches, pause {pause}, then repeat."
    elif "frog" in name or "topwater" in name or "buzzbait" in name:
        technique = f"Cast along {cast_area}; retrieve steadily, pausing a frog at vegetation openings."
    elif "spinnerbait" in name:
        technique = f"Cast across {cast_area}; retrieve steadily just fast enough to keep the blades turning, then slow beside cover."
    elif "crankbait" in name:
        technique = f"Cast across {cast_area}; retrieve until it reaches its running depth and pause briefly after contact with cover."
    elif "spoon" in name or "blade bait" in name:
        technique = f"Cast across {cast_area}; retrieve with a controlled lift-and-fall and keep the lure above snags."
    elif "swimbait" in name or "paddletail" in name or "swim jig" in name:
        technique = f"Cast parallel to {cast_area}; retrieve steadily just above the visible or mapped cover."
    elif "ned rig" in name or "tube jig" in name or "hair jig" in name or "jig" in name:
        technique = f"Cast near {cast_area}; retrieve by letting it touch bottom, then drag it a few inches, pause, and repeat."
    elif "stick worm" in name or "worm" in name:
        technique = f"Cast beside shade or cover near {cast_area}; retrieve by letting it sink on a slack line, then lift gently and pause."
    elif "spinner" in name:
        technique = f"Cast across {cast_area}; retrieve only fast enough to keep the blade rotating."
    elif "dough bait" in name:
        technique = f"Cast to a clean bottom near {cast_area}; leave the bait still and retrieve periodically to check it."
    else:
        technique = f"Cast near {cast_area}; retrieve slowly with short pauses."

    title = _lure_name_with_details(lure, species)
    reason = _lure_reason(species, temperature_band, water_type, report_note)
    conditions = period_weather or {}
    cloud_cover = conditions.get("average_cloud_cover_percent")
    precipitation = conditions.get("maximum_precipitation_probability_percent")
    forecast_low_light = (
        isinstance(cloud_cover, (int, float))
        and cloud_cover >= 60
        or isinstance(precipitation, (int, float))
        and precipitation >= 50
    )
    moving_presentation = any(
        term in name
        for term in (
            "spinner",
            "crankbait",
            "spoon",
            "swimbait",
            "topwater",
            "buzzbait",
            "blade bait",
        )
    )
    if forecast_low_light and moving_presentation:
        reason += " Forecast clouds or rain favor a visible, moving presentation; switch to a high-contrast color if the water is stained."
    elif night:
        reason += " At night, make slow, accurate casts along the bank rather than covering open water blindly."
    backup_text = (
        f" Backup: {backup} if fish ignore this presentation or the conditions change."
        if backup
        else ""
    )
    return f"{title}. {technique} {reason}{backup_text}"


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
    state: str = "",
    water_body: str = "",
    local_date: date | None = None,
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
        if temp_band not in {"cold", "cool"} or cold_water and key == "largemouth bass":
            day_identity = _lure_identity(day_pick)
            night_identity = _lure_identity(night_pick)
            day_lures = [
                day_pick
            ] + [lure for lure in day_lures if _lure_identity(lure) != day_identity]
            night_lures = [
                night_pick
            ] + [lure for lure in night_lures if _lure_identity(lure) != night_identity]

    daytime = weather.get("daytime") or {}
    nighttime = weather.get("nighttime") or {}

    def prioritize_conditions(lures: list[str], conditions: dict) -> list[str]:
        wind_speed = conditions.get("maximum_wind_mph")
        cloud_cover = conditions.get("average_cloud_cover_percent")
        rain_probability = conditions.get("maximum_precipitation_probability_percent")
        air_temperature_change = conditions.get("temperature_change_f")
        average_air_temperature = conditions.get("average_air_temperature_f")
        cold_front = (
            isinstance(air_temperature_change, (int, float))
            and air_temperature_change <= -6
            and (temp is None or temp < 68)
        )
        cold_air = (
            isinstance(average_air_temperature, (int, float))
            and average_air_temperature <= 40
            and (temp is None or temp < 58)
        )
        if cold_front or cold_air:
            slow_lure = next(
                (
                    index
                    for index, lure in enumerate(lures)
                    if any(term in lure.casefold() for term in ("ned", "tube", "jig", "worm", "minnow under"))
                ),
                None,
            )
            if slow_lure is not None:
                lures.insert(0, lures.pop(slow_lure))
            return lures
        low_light_or_wind = (
            isinstance(wind_speed, (int, float))
            and wind_speed >= 10
            or isinstance(cloud_cover, (int, float))
            and cloud_cover >= 60
            or isinstance(rain_probability, (int, float))
            and rain_probability >= 50
        )
        if low_light_or_wind:
            moving_lure = next(
                (
                    index
                    for index, lure in enumerate(lures)
                    if any(
                        keyword in lure.casefold()
                        for keyword in (
                            "spinner",
                            "crankbait",
                            "spoon",
                            "swimbait",
                            "topwater",
                            "buzzbait",
                            "blade bait",
                        )
                    )
                ),
                None,
            )
            if moving_lure is not None:
                lures.insert(0, lures.pop(moving_lure))
        return lures

    day_lures = prioritize_conditions(day_lures, daytime)
    night_lures = prioritize_conditions(night_lures, nighttime)
    local_report = (
        _recent_local_report(state, water_body, species, local_date)
        if local_date is not None and water_body
        else None
    )
    if local_report:
        day_lures = local_report["lures"] + [
            lure
            for lure in day_lures
            if all(
                _lure_identity(lure) != _lure_identity(reported_lure)
                for reported_lure in local_report["lures"]
            )
        ]
    day_lures = day_lures[:3]
    night_lures = night_lures[:3]
    if temp is None:
        basis = f"Seasonal picks for {species}; no recent nearby water-temperature reading."
    else:
        basis = f"Picks for {species} using nearby water temperature ({temp:.1f}°F)."
    if local_report:
        basis += (
            f" Exact-waterbody report dated {local_report['reported_on']} "
            f"({local_report['source']}); short-term influence expires after "
            f"{LOCAL_REPORT_SNAPSHOT['maximum_age_days']} days."
        )

    def build_picks(lures: list[str], night: bool, conditions: dict) -> list[dict]:
        picks = []
        for index, lure in enumerate(lures):
            backup = _backup_lure(lure, species)
            detailed_lure = _lure_name_with_details(lure, species)
            if (
                species.casefold() == "northern pike"
                and "wire leader" not in detailed_lure.casefold()
            ):
                detailed_lure += " with a 12-inch wire leader"
            report_note = None
            if local_report and not night and any(
                term in lure.casefold()
                for term in local_report["reported_lure_terms"]
            ):
                report_note = local_report["note"]
            picks.append(
                {
                    "rank": index + 1,
                    "lure": detailed_lure,
                    "how": _lure_instruction(
                        detailed_lure,
                        species,
                        water_type,
                        temp_band,
                        conditions,
                        night,
                        backup,
                        report_note,
                    ),
                    "backup": backup,
                }
            )
        return picks

    return {
        "species": species,
        "basis": basis,
        "day": build_picks(day_lures, False, daytime),
        "night": build_picks(night_lures, True, nighttime),
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
        target_species,
        temp,
        season,
        weather,
        water_type,
        state,
        water_body,
        local_now.date(),
    )
    access_message = None
    access_data: dict[str, Any]
    try:
        access_data = _nearby_fishing_access(location, state)
    except HTTPException as error:
        access_data = {"shore_access": []}
        access_message = (
            "The public-access lookup failed, so access at these shoreline pins "
            f"remains unverified ({error.detail})."
        )
    if access_message:
        access_data["message"] = access_message
    waterbody_map = _waterbody_map_data(
        location,
        state,
        weather,
        water_body,
        target_species,
        access_data,
        season,
        water_temperature,
    )
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
        "wind": {
            "current": weather.get("current", {
                "available": False,
                "message": "Current NWS station wind is unavailable.",
            }),
            "shoreline_guidance": waterbody_map["wind_guidance"],
        },
        "waterbody_map": waterbody_map,
        "timezone": timezone_name,
        "generated_at": local_now.isoformat(),
    }


@app.post("/api/recommendations")
def recommendations(payload: RecommendationRequest) -> dict:
    return build_recommendation_report(payload)


@app.post("/api/recommendations/wind")
def refresh_recommendation_wind(payload: WindRefreshRequest) -> dict:
    try:
        zone = ZoneInfo(payload.timezone)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(status_code=422, detail="A valid waterbody timezone is required.") from error
    location = {"latitude": payload.latitude, "longitude": payload.longitude}
    local_now = datetime.now(zone)
    try:
        point = _external_json(
            f"https://api.weather.gov/points/{payload.latitude:.4f},{payload.longitude:.4f}",
            "National Weather Service",
        )
    except DataSourceError:
        current_wind = _open_meteo_current_wind(
            location, local_now, payload.timezone
        )
    else:
        properties = point.get("properties")
        stations_url = (
            properties.get("observationStations")
            if isinstance(properties, dict)
            else None
        )
        current_wind = _wind_from_observation_stations(
            stations_url, location, local_now, payload.timezone
        )
    return {
        "current": current_wind,
        "shoreline_guidance": _wind_shoreline_guidance(
            {"current": current_wind}, payload.water_type
        ),
        "updated_at": local_now.isoformat(),
    }


@app.post("/api/recommendations/depth-chart.pdf")
def download_depth_chart(payload: DepthChartPdfRequest) -> Response:
    pdf = _depth_chart_pdf(payload)
    safe_name = re.sub(
        r"[^a-z0-9]+", "-", payload.water_body.casefold()
    ).strip("-") or "waterbody"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="depth-chart-{safe_name}.pdf"'
        },
    )


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
