import math
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException

import server


class RecommendationTests(unittest.TestCase):
    def test_api_accepts_waterbody_and_state_without_a_zip(self):
        response_body = {"water_body": "Carnegie Lake", "state": "NJ"}
        payload = server.RecommendationRequest.model_validate(
            {"water_body": "Carnegie Lake", "state": "NJ"}
        )
        with patch.object(server, "build_recommendation_report", return_value=response_body):
            response = server.recommendations(payload)

        self.assertEqual(response, response_body)

    def test_wind_refresh_uses_report_location_and_returns_updated_guidance(self):
        payload = server.WindRefreshRequest(
            latitude=40.35,
            longitude=-74.66,
            timezone="America/New_York",
            water_type="lake",
        )
        current_wind = {
            "available": True,
            "wind_speed_mph": 8,
            "wind_direction_degrees": 225,
            "wind_direction": "SW",
            "source": "National Weather Service",
        }
        with (
            patch.object(
                server,
                "_external_json",
                return_value={
                    "properties": {
                        "observationStations": "https://api.weather.gov/stations"
                    }
                },
            ),
            patch.object(
                server, "_wind_from_observation_stations", return_value=current_wind
            ) as get_wind,
        ):
            result = server.refresh_recommendation_wind(payload)

        self.assertEqual(result["current"], current_wind)
        self.assertIn("downwind shoreline", result["shoreline_guidance"]["message"])
        self.assertEqual(get_wind.call_args.args[1], {
            "latitude": 40.35,
            "longitude": -74.66,
        })
        self.assertTrue(result["updated_at"])

    def test_wind_refresh_rejects_an_invalid_timezone(self):
        with self.assertRaises(HTTPException) as error:
            server.refresh_recommendation_wind(
                server.WindRefreshRequest(
                    latitude=40.35,
                    longitude=-74.66,
                    timezone="Not/A_Timezone",
                )
            )

        self.assertEqual(error.exception.status_code, 422)

    def test_report_has_day_and_night_picks(self):
        now = datetime.now(ZoneInfo("America/New_York"))
        location = {
            "latitude": 40.35,
            "longitude": -74.66,
            "place": "Carnegie Lake",
            "reference": "USGS site 01400000 (Carnegie Lake)",
            "provider": "USGS",
            "water_type": "lake",
            "site": {
                "name": "Carnegie Lake",
                "site_type": "LK",
                "site_number": "01400000",
            },
        }
        temperature = {
            "available": True,
            "temperature_f": 62.6,
            "site_id": "USGS-01400000",
            "distance_miles": 2.1,
            "distance_reference": "USGS site",
        }
        with (
            patch.object(server, "_resolve_named_waterbody", return_value=location),
            patch.object(
                server,
                "_local_time_and_wind",
                return_value=("America/New_York", now, {"daytime": {}, "nighttime": {}}),
            ),
            patch.object(server, "_water_temperature", return_value=temperature),
            patch.object(
                server,
                "_nearby_fishing_access",
                return_value={"shore_access": [], "fishing_spots": [], "parking": []},
            ),
        ):
            report = server.build_recommendation_report(
                server.RecommendationRequest(
                    water_body="Carnegie Lake", state="NJ"
                )
            )

        self.assertEqual(len(report["recommendations"]["day"]), 3)
        self.assertEqual(len(report["recommendations"]["night"]), 3)
        self.assertEqual(
            len({server._lure_identity(pick["lure"]) for pick in report["recommendations"]["day"]}),
            3,
        )
        self.assertEqual(
            len({server._lure_identity(pick["lure"]) for pick in report["recommendations"]["night"]}),
            3,
        )
        self.assertEqual(report["recommendations"]["species"], "Largemouth Bass")
        self.assertEqual(report["water_temperature"]["temperature_f"], 62.6)
        self.assertEqual(report["state"]["code"], "NJ")
        self.assertEqual(report["water_type"], "lake")
        self.assertEqual(
            report["recommendations"]["day"][0]["lure"],
            "3.5- to 4-inch 3/8-oz suspending shad-pattern jerkbait",
        )
        self.assertNotIn("weather", report)
        self.assertNotIn("sources", report)
        self.assertNotIn("behavior_context", report)
        self.assertNotIn("access", report)
        self.assertIn("waterbody_map", report)
        self.assertFalse(report["wind"]["current"]["available"])
        self.assertTrue(all("how" in pick for pick in report["recommendations"]["day"]))

    def test_location_search_accepts_waterbody_in_selected_state(self):
        candidate = {
            "lat": "40.35",
            "lon": "-74.66",
            "name": "Carnegie Lake",
            "display_name": "Carnegie Lake, Princeton, New Jersey, United States",
            "class": None,
            "type": "reservoir",
            "address": {"state": "New Jersey", "ISO3166-2-lvl4": "US-NJ"},
        }
        with (
            patch.object(server, "_waterbody_site", return_value=(None, None)),
            patch.object(server, "_nominatim_locations", return_value=[candidate]),
        ):
            location = server._resolve_named_waterbody("Carnegie Lake", "NJ")

        self.assertEqual(location["provider"], "OpenStreetMap")
        self.assertEqual(location["latitude"], 40.35)
        self.assertIn("Carnegie Lake", location["place"])

    def test_town_qualifier_finds_waterbody_that_spans_an_adjacent_town(self):
        waterbody = {
            "lat": "40.3528",
            "lon": "-74.6268",
            "name": "Lake Carnegie",
            "display_name": (
                "Lake Carnegie, Plainsboro Township, Middlesex County, "
                "New Jersey, United States"
            ),
            "class": None,
            "type": "reservoir",
            "address": {
                "town": "Plainsboro Township",
                "state": "New Jersey",
                "ISO3166-2-lvl4": "US-NJ",
            },
        }
        locality = {
            "lat": "40.3573",
            "lon": "-74.6672",
            "name": "Princeton",
            "display_name": "Princeton, Mercer County, New Jersey, United States",
            "address": {
                "town": "Princeton",
                "state": "New Jersey",
                "ISO3166-2-lvl4": "US-NJ",
            },
        }
        with (
            patch.object(server, "_waterbody_site", return_value=(None, None)),
            patch.object(
                server,
                "_nominatim_locations",
                side_effect=[[waterbody], [locality]],
            ),
        ):
            location = server._resolve_named_waterbody(
                "Carnegie Lake, Princeton", "NJ"
            )

        self.assertEqual(location["provider"], "OpenStreetMap")
        self.assertIn("Lake Carnegie", location["place"])
        self.assertEqual(location["water_type"], "reservoir")

    def test_town_qualifier_resolves_duplicate_openstreetmap_results(self):
        candidates = [
            {
                "lat": "40.35",
                "lon": "-74.66",
                "name": "Lake Example",
                "display_name": "Lake Example, Princeton, New Jersey, United States",
                "type": "lake",
                "address": {
                    "town": "Princeton",
                    "state": "New Jersey",
                    "ISO3166-2-lvl4": "US-NJ",
                },
            },
            {
                "lat": "40.72",
                "lon": "-74.60",
                "name": "Lake Example",
                "display_name": "Lake Example, Princeton, New Jersey, United States",
                "type": "lake",
                "address": {
                    "town": "Princeton",
                    "state": "New Jersey",
                    "ISO3166-2-lvl4": "US-NJ",
                },
            },
        ]
        locality = {
            "lat": "40.35",
            "lon": "-74.66",
            "address": {
                "town": "Princeton",
                "state": "New Jersey",
                "ISO3166-2-lvl4": "US-NJ",
            },
        }
        with (
            patch.object(server, "_waterbody_site", return_value=(None, None)),
            patch.object(
                server,
                "_nominatim_locations",
                side_effect=[candidates, [locality]],
            ),
        ):
            location = server._resolve_named_waterbody(
                "Lake Example, Princeton", "NJ"
            )

        self.assertEqual(location["provider"], "OpenStreetMap")
        self.assertEqual(location["latitude"], 40.35)
        self.assertNotIn("_candidate_name", location)

    def test_massachusetts_inventory_resolves_arlington_reservoir(self):
        response = {
            "features": [
                {
                    "attributes": {"NAME": "Arlington Reservoir"},
                    "centroid": {"x": -71.1894, "y": 42.4291},
                },
                {
                    "attributes": {"NAME": "Arlington Mill Reservoir"},
                    "centroid": {"x": -71.2121, "y": 42.8240},
                },
                {
                    "attributes": {"NAME": "Arlington Pond"},
                    "centroid": {"x": -71.10, "y": 42.50},
                },
            ]
        }
        with patch.object(server, "_external_json", return_value=response):
            matches, error = server._state_waterbody_matches(
                "Arlington Reservoir", "MA"
            )

        self.assertIsNone(error)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["place"], "Arlington Reservoir")
        self.assertEqual(matches[0]["water_type"], "reservoir")
        self.assertEqual(matches[0]["provider"], "MassGIS")

    def test_state_inventory_matches_waterbody_when_town_is_appended(self):
        response = {
            "features": [
                {
                    "attributes": {"NAME": "Arlington Reservoir"},
                    "centroid": {"x": -71.1894, "y": 42.4291},
                }
            ]
        }
        with patch.object(server, "_external_json", return_value=response):
            matches, error = server._state_waterbody_matches(
                "Arlington Reservoir, Lexington", "MA"
            )

        self.assertIsNone(error)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["place"], "Arlington Reservoir")

    def test_town_qualifier_resolves_duplicate_waterbody_names(self):
        matches = [
            {
                "latitude": 42.4291,
                "longitude": -71.1894,
                "place": "Arlington Reservoir",
                "provider": "MassGIS",
                "water_type": "reservoir",
            },
            {
                "latitude": 42.60,
                "longitude": -71.40,
                "place": "Arlington Reservoir",
                "provider": "MassGIS",
                "water_type": "reservoir",
            },
        ]
        candidate = {
            "lat": "42.4291",
            "lon": "-71.1894",
            "name": "Arlington Reservoir",
            "display_name": (
                "Arlington Reservoir, Lexington, Middlesex County, "
                "Massachusetts, United States"
            ),
            "type": "reservoir",
            "address": {
                "town": "Lexington",
                "state": "Massachusetts",
                "ISO3166-2-lvl4": "US-MA",
            },
        }
        with (
            patch.object(server, "_state_waterbody_matches", return_value=(matches, None)),
            patch.object(server, "_nominatim_locations", return_value=[candidate]),
        ):
            location = server._resolve_named_waterbody(
                "Arlington Reservoir, Lexington", "MA"
            )

        self.assertEqual(location["provider"], "MassGIS")
        self.assertAlmostEqual(location["latitude"], 42.4291)

    def test_duplicate_waterbody_without_town_still_requests_clarification(self):
        matches = [
            {"latitude": 42.0, "longitude": -71.0, "place": "Long Pond"},
            {"latitude": 43.0, "longitude": -72.0, "place": "Long Pond"},
        ]
        with patch.object(server, "_state_waterbody_matches", return_value=(matches, None)):
            with self.assertRaises(HTTPException) as raised:
                server._resolve_named_waterbody("Long Pond", "MA")

        self.assertIn("nearby town or county", raised.exception.detail)

    def test_new_hampshire_fishery_inventory_preserves_fishery_metadata(self):
        response = {
            "features": [
                {
                    "attributes": {
                        "gnis_name": "Lake Winnipesaukee",
                        "nhfishery": "Coldwater",
                        "nhfishnote": "Lake Trout",
                    },
                    "centroid": {"x": -71.4, "y": 43.6},
                }
            ]
        }
        with patch.object(server, "_external_json", return_value=response):
            matches, error = server._state_waterbody_matches(
                "Lake Winnipesaukee", "NH"
            )

        self.assertIsNone(error)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["water_type"], "lake")
        self.assertEqual(
            matches[0]["fishery"],
            {"category": "Coldwater", "note": "Lake Trout"},
        )

    def test_location_search_rejects_a_result_in_another_state(self):
        candidate = {
            "lat": "40.35",
            "lon": "-74.66",
            "name": "Carnegie Lake",
            "display_name": "Carnegie Lake, Pennsylvania, United States",
            "class": "natural",
            "type": "water",
            "address": {"state": "Pennsylvania", "ISO3166-2-lvl4": "US-PA"},
        }
        with (
            patch.object(server, "_waterbody_site", return_value=(None, None)),
            patch.object(server, "_nominatim_locations", return_value=[candidate]),
        ):
            with self.assertRaises(HTTPException) as raised:
                server._resolve_named_waterbody("Carnegie Lake", "NJ")

        self.assertEqual(raised.exception.status_code, 422)

    def test_missing_temperature_uses_season_not_air_temperature(self):
        winter = server._recommend_lures("Largemouth Bass", None, "winter", {})
        summer = server._recommend_lures("Largemouth Bass", None, "summer", {})

        self.assertEqual(len(winter["day"]), 3)
        self.assertEqual(len(winter["night"]), 3)
        self.assertNotEqual(winter["day"][0]["lure"], summer["day"][0]["lure"])
        self.assertIn("Seasonal picks", winter["basis"])
        self.assertEqual(
            winter["night"][0]["lure"],
            "1/8-oz Black-and-blue jig with craw trailer",
        )
        self.assertIn("Cast", winter["day"][0]["how"])
        self.assertIn("retrieve", winter["day"][0]["how"])

    def test_lure_picks_are_deterministic_for_same_conditions(self):
        winter = server._recommend_lures("Largemouth Bass", 44, "winter", {})
        same_conditions = server._recommend_lures("Largemouth Bass", 44, "winter", {})

        self.assertEqual(winter, same_conditions)
        self.assertEqual(
            winter["day"][0]["lure"],
            "3.5- to 4-inch, 3/8-oz Suspending shad-pattern jerkbait",
        )

    def test_waterbody_type_changes_shore_lures_and_casting_instructions(self):
        pond = server._recommend_lures("Largemouth Bass", 70, "summer", {}, "pond")
        reservoir = server._recommend_lures(
            "Largemouth Bass", 70, "summer", {}, "reservoir"
        )
        river = server._recommend_lures("Largemouth Bass", 70, "summer", {}, "river")

        self.assertEqual(
            pond["day"][0]["lure"],
            "4-inch green-pumpkin wacky-rigged stick worm (weightless, 1/0 hook)",
        )
        self.assertEqual(reservoir["day"][0]["lure"], "3/8-oz white-and-chartreuse spinnerbait")
        self.assertEqual(river["day"][0]["lure"], "3/8-oz white spinnerbait")
        self.assertNotEqual(pond["day"][0]["lure"], reservoir["day"][0]["lure"])
        self.assertIn("weed edge", pond["day"][0]["how"])
        self.assertIn("point, cove mouth, or inflow", reservoir["day"][0]["how"])
        self.assertIn("eddy or current seam", river["day"][0]["how"])
        self.assertNotEqual(pond["night"][0]["lure"], reservoir["night"][0]["lure"])

    def test_cold_waterbody_type_changes_largemouth_picks(self):
        pond = server._recommend_lures("Largemouth Bass", 44, "winter", {}, "pond")
        lake = server._recommend_lures("Largemouth Bass", 44, "winter", {}, "lake")

        self.assertEqual(
            pond["day"][0]["lure"],
            "3-inch 1/8-oz green-pumpkin Ned rig",
        )
        self.assertEqual(
            lake["day"][0]["lure"],
            "3.5- to 4-inch 3/8-oz suspending shad-pattern jerkbait",
        )

    def test_local_weather_is_reduced_to_day_and_night_wind_signals(self):
        zone = ZoneInfo("America/New_York")
        now = datetime.now(zone)
        point = {
            "properties": {
                "timeZone": "America/New_York",
                "forecastHourly": "https://api.weather.gov/gridpoints/PHI/1,1/forecast/hourly",
            }
        }
        forecast = {
            "properties": {
                "periods": [
                    {
                        "startTime": (now + timedelta(hours=1)).isoformat(),
                        "isDaytime": True,
                        "windSpeed": "10 to 15 mph",
                        "temperature": 65,
                        "skyCover": {"value": 70},
                        "probabilityOfPrecipitation": {"value": 20},
                    },
                    {
                        "startTime": (now + timedelta(hours=2)).isoformat(),
                        "isDaytime": True,
                        "windSpeed": "10 mph",
                        "temperature": 60,
                        "skyCover": {"value": 80},
                        "probabilityOfPrecipitation": {"value": 60},
                    },
                    {
                        "startTime": (now + timedelta(hours=13)).isoformat(),
                        "isDaytime": False,
                        "windSpeed": "5 mph",
                        "temperature": 50,
                        "skyCover": {"value": 40},
                        "probabilityOfPrecipitation": {"value": 10},
                    },
                ]
            }
        }
        with (
            patch.object(server, "_external_json", side_effect=[point, forecast]),
            patch.object(
                server,
                "_open_meteo_current_wind",
                return_value={"available": False},
            ),
        ):
            timezone_name, local_now, wind = server._local_time_and_wind(
                {"latitude": 40.35, "longitude": -74.66}
            )

        self.assertEqual(timezone_name, "America/New_York")
        self.assertEqual(local_now.tzinfo, zone)
        self.assertEqual(wind["daytime"]["maximum_wind_mph"], 15)
        self.assertEqual(wind["nighttime"]["maximum_wind_mph"], 5)
        self.assertEqual(wind["daytime"]["average_air_temperature_f"], 62.5)
        self.assertEqual(wind["daytime"]["average_cloud_cover_percent"], 75)
        self.assertEqual(
            wind["daytime"]["maximum_precipitation_probability_percent"], 60
        )
        self.assertEqual(wind["daytime"]["temperature_change_f"], -5)

    def test_open_meteo_current_wind_is_explicitly_modelled_and_directional(self):
        zone = ZoneInfo("America/New_York")
        local_now = datetime.now(zone).replace(second=0, microsecond=0)
        with patch.object(
            server,
            "_external_json",
            return_value={
                "current": {
                    "time": local_now.strftime("%Y-%m-%dT%H:%M"),
                    "wind_speed_10m": 7.5,
                    "wind_direction_10m": 225,
                }
            },
        ) as request:
            wind = server._open_meteo_current_wind(
                {"latitude": 42.4, "longitude": -71.1},
                local_now,
                "America/New_York",
            )

        self.assertTrue(wind["available"])
        self.assertEqual(wind["wind_speed_mph"], 7.5)
        self.assertEqual(wind["wind_direction"], "SW")
        self.assertTrue(wind["is_modelled"])
        self.assertIn("api.open-meteo.com", request.call_args.args[0])

    def test_current_station_wind_uses_nearest_fresh_observation(self):
        zone = ZoneInfo("America/New_York")
        local_now = datetime.now(zone)
        stations = {
            "features": [
                {
                    "properties": {"stationIdentifier": "KAAA", "name": "Far station"},
                    "geometry": {"coordinates": [-74.8, 40.1]},
                },
                {
                    "properties": {"stationIdentifier": "KBED", "name": "Nearby station"},
                    "geometry": {"coordinates": [-74.66, 40.35]},
                },
            ]
        }
        observation = {
            "properties": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 16.668},
                "windDirection": {"value": 315},
            }
        }
        with patch.object(
            server, "_external_json", side_effect=[stations, observation]
        ):
            wind = server._current_station_wind(
                "https://api.weather.gov/gridpoints/PHI/1,1/stations",
                {"latitude": 40.35, "longitude": -74.66},
                local_now,
            )

        self.assertTrue(wind["available"])
        self.assertEqual(wind["station_id"], "KBED")
        self.assertEqual(wind["wind_speed_mph"], 10.4)
        self.assertEqual(wind["wind_direction"], "NW")

    def test_stale_station_wind_is_not_presented_as_current(self):
        zone = ZoneInfo("America/New_York")
        local_now = datetime.now(zone)
        stations = {
            "features": [
                {
                    "properties": {"stationIdentifier": "KBED", "name": "Nearby station"},
                    "geometry": {"coordinates": [-74.66, 40.35]},
                }
            ]
        }
        observation = {
            "properties": {
                "timestamp": (
                    datetime.now(timezone.utc) - timedelta(hours=4)
                ).isoformat(),
                "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 16},
                "windDirection": {"value": 315},
            }
        }
        with patch.object(
            server, "_external_json", side_effect=[stations, observation]
        ):
            wind = server._current_station_wind(
                "https://api.weather.gov/gridpoints/PHI/1,1/stations",
                {"latitude": 40.35, "longitude": -74.66},
                local_now,
            )

        self.assertFalse(wind["available"])
        self.assertIn("stale", wind["message"])

    def test_distant_station_wind_is_not_used_for_local_conditions(self):
        stations = {
            "features": [
                {
                    "properties": {"stationIdentifier": "KAAA", "name": "Distant station"},
                    "geometry": {"coordinates": [-73.5, 40.35]},
                }
            ]
        }
        with patch.object(server, "_external_json", return_value=stations) as request:
            wind = server._current_station_wind(
                "https://api.weather.gov/stations",
                {"latitude": 40.35, "longitude": -74.66},
                datetime.now(ZoneInfo("America/New_York")),
            )

        self.assertFalse(wind["available"])
        self.assertIn("recent usable wind observation", wind["message"])
        request.assert_called_once()

    def test_station_wind_skips_stale_nearest_station_for_next_fresh_station(self):
        local_now = datetime.now(ZoneInfo("America/New_York"))
        stations = {
            "features": [
                {
                    "properties": {"stationIdentifier": "KAAA", "name": "Nearest stale"},
                    "geometry": {"coordinates": [-74.66, 40.35]},
                },
                {
                    "properties": {"stationIdentifier": "KBBB", "name": "Next fresh"},
                    "geometry": {"coordinates": [-74.7, 40.36]},
                },
            ]
        }

        def observation(timestamp, direction):
            return {
                "properties": {
                    "timestamp": timestamp.isoformat(),
                    "windSpeed": {"unitCode": "wmoUnit:km_h-1", "value": 16},
                    "windDirection": {"value": direction},
                }
            }

        with patch.object(
            server,
            "_external_json",
            side_effect=[
                stations,
                observation(datetime.now(timezone.utc) - timedelta(hours=2), 270),
                observation(datetime.now(timezone.utc), 315),
            ],
        ):
            wind = server._current_station_wind(
                "https://api.weather.gov/stations",
                {"latitude": 40.35, "longitude": -74.66},
                local_now,
            )

        self.assertTrue(wind["available"])
        self.assertEqual(wind["station_id"], "KBBB")
        self.assertEqual(wind["wind_direction"], "NW")

    def test_waterbody_outline_places_downwind_shoreline_pins(self):
        ring = [
            [-71.01, 42.01],
            [-70.99, 42.01],
            [-70.99, 41.99],
            [-71.01, 41.99],
            [-71.01, 42.01],
        ]
        geometry, shoreline = server._arcgis_map_geometry({"rings": [ring]})
        spots = server._shoreline_spots(
            shoreline,
            {
                "current": {
                    "available": True,
                    "wind_speed_mph": 12,
                    "wind_direction_degrees": 315,
                    "wind_direction": "NW",
                }
            },
        )

        self.assertEqual(geometry["type"], "Polygon")
        self.assertEqual(len(spots), 3)
        self.assertGreater(spots[0]["longitude"], -71.0)
        self.assertLess(spots[0]["latitude"], 42.0)
        self.assertIn("NW", spots[0]["reason"])

    def test_waterbody_geometry_preserves_fine_shoreline_points(self):
        ring = [
            [
                -71 + 0.01 * math.cos(index * 2 * math.pi / 240),
                42 + 0.01 * math.sin(index * 2 * math.pi / 240),
            ]
            for index in range(240)
        ]
        ring.append(ring[0])

        geometry, shoreline = server._arcgis_map_geometry({"rings": [ring]})

        self.assertEqual(len(geometry["coordinates"][0]), len(ring))
        self.assertEqual(len(shoreline), len(ring))

    def test_calm_or_missing_wind_pin_reason_explains_the_shoreline_choice(self):
        ring = [
            [-71.01, 42.01],
            [-70.99, 42.01],
            [-70.99, 41.99],
            [-71.01, 41.99],
            [-71.01, 42.01],
        ]
        spots = server._shoreline_spots(
            ring,
            {"current": {"available": False, "message": "Station report stale."}},
        )

        self.assertEqual(len(spots), 3)
        for spot in spots:
            self.assertIn("Selected on the", spot["reason"])
            self.assertIn("one of three spaced checks", spot["reason"])
            self.assertIn("No validated current wind direction", spot["reason"])
            self.assertNotIn("No usable current wind direction", spot["reason"])

    def test_osm_polygon_fallback_adds_checked_shoreline_pins_outside_ma_and_nh(self):
        polygon = {
            "type": "Polygon",
            "coordinates": [[
                [-74.01, 40.01],
                [-73.99, 40.01],
                [-73.99, 39.99],
                [-74.01, 39.99],
                [-74.01, 40.01],
            ]],
        }
        with patch.object(
            server,
            "_nominatim_locations",
            return_value=[
                {
                    "name": "Carnegie Lake",
                    "display_name": "Carnegie Lake, Princeton, New Jersey",
                    "address": {
                        "state": "New Jersey",
                        "ISO3166-2-lvl4": "US-NJ",
                    },
                    "geojson": polygon,
                }
            ],
        ) as lookup:
            outline = server._waterbody_geometry(
                {
                    "latitude": 40.0,
                    "longitude": -74.0,
                    "place": "Carnegie Lake",
                },
                "NJ",
                "Carnegie Lake",
            )
        spots = server._shoreline_spots(
            outline["shoreline_ring"], {"current": {"available": False}}
        )

        self.assertEqual(outline["source"], "OpenStreetMap")
        self.assertIsNotNone(outline["geometry"])
        self.assertTrue(lookup.call_args.kwargs["include_polygon"])
        self.assertEqual(len(spots), 3)
        self.assertTrue(
            all(
                -74.01 <= spot["longitude"] <= -73.99
                and 39.99 <= spot["latitude"] <= 40.01
                for spot in spots
            )
        )

    def test_bathymetry_contours_are_returned_with_source_units(self):
        response = {
            "features": [
                {
                    "attributes": {"lake": "WINNIPESAUKEE", "depth": 40},
                    "geometry": {"paths": [[[-71.41, 43.60], [-71.40, 43.61]]]},
                }
            ]
        }
        with patch.object(server, "_external_json", return_value=response):
            contours = server._bathymetry_contours(
                {
                    "place": "Lake Winnipesaukee",
                    "latitude": 43.6,
                    "longitude": -71.4,
                },
                "NH",
            )

        self.assertTrue(contours["available"])
        self.assertEqual(contours["unit"], "ft")
        self.assertEqual(contours["contours"][0]["depth"], 40)

    def test_massachusetts_bathymetry_uses_matched_palis_id_and_filters_shore_lines(self):
        def feature(depth, palis_id=71040, shore=0, longitude=-71.16):
            return {
                "attributes": {
                    "NAME": "SPY POND",
                    "DEPTH": depth,
                    "PALIS_ID": palis_id,
                    "SHORE": shore,
                    "DLABEL": f"{depth} ft",
                },
                "geometry": {
                    "paths": [[[longitude, 42.41], [longitude + 0.001, 42.41]]]
                },
            }

        response = {
            "features": [
                feature(5),
                feature(10),
                feature(15),
                feature(0, shore=1),
                feature(40, palis_id=99999, longitude=-71.15),
            ]
        }
        location = {
            "place": "Spy Pond",
            "latitude": 42.41,
            "longitude": -71.16,
        }
        with patch.object(server, "_external_json", return_value=response) as request:
            bathymetry = server._bathymetry_contours(
                location, "MA", "Spy Pond", palis_id=71040
            )

        self.assertIn("PALIS_ID%3D71040", request.call_args.args[0])
        self.assertTrue(bathymetry["available"])
        self.assertEqual(bathymetry["unit"], "ft")
        self.assertEqual(bathymetry["palis_id"], 71040)
        self.assertEqual(
            {contour["depth"] for contour in bathymetry["contours"]},
            {5, 10, 15},
        )
        self.assertEqual(bathymetry["min_depth"], 5)
        self.assertEqual(bathymetry["max_depth"], 15)
        self.assertEqual(bathymetry["depth_interval_ft"], 5)
        self.assertIsNone(bathymetry["survey_date"])
        self.assertIn("MassGIS", bathymetry["attribution"])

    def test_contours_outside_selected_waterbody_outline_are_not_returned(self):
        response = {
            "features": [
                {
                    "attributes": {"lake": "WINNIPESAUKEE", "depth": 15},
                    "geometry": {
                        "paths": [[
                            [-71.41, 43.60],
                            [-71.40, 43.60],
                            [-71.39, 43.60],
                        ]]
                    },
                },
                {
                    "attributes": {"lake": "WINNIPESAUKEE", "depth": 30},
                    "geometry": {
                        "paths": [[
                            [-71.00, 43.60],
                            [-70.99, 43.60],
                            [-70.98, 43.60],
                        ]]
                    },
                },
            ]
        }
        ring = [
            [-71.42, 43.61],
            [-71.38, 43.61],
            [-71.38, 43.59],
            [-71.42, 43.59],
            [-71.42, 43.61],
        ]
        with patch.object(server, "_external_json", return_value=response):
            contours = server._bathymetry_contours(
                {
                    "place": "Lake Winnipesaukee",
                    "latitude": 43.6,
                    "longitude": -71.4,
                },
                "NH",
                shoreline_ring=ring,
            )

        self.assertTrue(contours["available"])
        self.assertEqual(
            {contour["depth"] for contour in contours["contours"]}, {15}
        )

    def test_depth_chart_pdf_contains_actual_contours_and_access_markers(self):
        payload = server.DepthChartPdfRequest(
            water_body="Spy Pond, Arlington",
            state="MA",
            map_data={
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-71.16, 42.41],
                        [-71.15, 42.41],
                        [-71.15, 42.42],
                        [-71.16, 42.42],
                        [-71.16, 42.41],
                    ]],
                },
                "bathymetry": {
                    "available": True,
                    "source": "MassWildlife Inland Bathymetry",
                    "source_url": "https://example.test/contours",
                    "unit": "ft",
                    "contours": [
                        {
                            "depth": 5,
                            "coordinates": [
                                [-71.159, 42.411],
                                [-71.151, 42.419],
                            ],
                        }
                    ],
                    "attribution": "MassGIS",
                },
                "shoreline_spots": [
                    {"longitude": -71.158, "latitude": 42.412}
                ],
            },
        )

        pdf = server._depth_chart_pdf(payload)

        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"Spy Pond, Arlington", pdf)
        self.assertIn(b"MassWildlife Inland Bathymetry", pdf)
        self.assertIn(b"Contours are interpolated depth lines", pdf)

    def test_generated_depth_pdf_rejects_missing_bathymetry(self):
        payload = server.DepthChartPdfRequest(
            water_body="Unknown Pond",
            state="MA",
            map_data={"bathymetry": {"available": False, "contours": []}},
        )

        with self.assertRaises(HTTPException) as error:
            server._depth_chart_pdf(payload)

        self.assertEqual(error.exception.status_code, 422)

    def test_summer_smallmouth_depth_adjustment_is_limited_to_mapped_transitions(self):
        ring = [
            [-71.01, 42.01],
            [-70.99, 42.01],
            [-70.99, 41.99],
            [-71.01, 41.99],
            [-71.01, 42.01],
        ]
        contours = []
        for longitude, latitude in ring[:-1]:
            contours.extend(
                [
                    {
                        "depth": depth,
                        "coordinates": [
                            [longitude, latitude],
                            [longitude + 0.0001, latitude + 0.0001],
                        ],
                    }
                    for depth in (10, 15)
                ]
            )
        bathymetry = {"unit": "ft", "contours": contours}
        wind = {"current": {"available": False}}

        summer = server._shoreline_spots(
            ring, wind, bathymetry, "Smallmouth Bass", season="summer"
        )
        spring = server._shoreline_spots(
            ring, wind, bathymetry, "Smallmouth Bass", season="spring"
        )
        largemouth = server._shoreline_spots(
            ring, wind, bathymetry, "Largemouth Bass", season="summer"
        )

        self.assertEqual(len(summer), 3)
        self.assertTrue(
            all(
                spot["depth_context"]["feature"] == "mid_to_deep_transition"
                for spot in summer
            )
        )
        self.assertTrue(all(spot["depth_rank_adjustment"] == 0.35 for spot in summer))
        self.assertTrue(all(spot["depth_rank_adjustment"] == 0 for spot in spring))
        self.assertTrue(
            all(spot["depth_rank_adjustment"] == 0 for spot in largemouth)
        )

    def test_windy_day_prioritizes_a_moving_shore_lure_without_showing_forecast(self):
        calm = server._recommend_lures("Largemouth Bass", 55, "winter", {})
        windy = server._recommend_lures(
            "Largemouth Bass", 55, "winter", {"daytime": {"maximum_wind_mph": 12}}
        )

        self.assertEqual(
            windy["day"][0]["lure"],
            "3/8-oz White-and-chartreuse spinnerbait",
        )
        self.assertEqual(
            calm["day"][0]["lure"],
            "3.5- to 4-inch, 3/8-oz Suspending shad-pattern jerkbait",
        )
        self.assertNotIn("weather", windy)

    def test_cloud_rain_and_cold_front_change_lure_order_without_overriding_water_temp(self):
        baseline = server._recommend_lures("Walleye", 45, "winter", {})
        low_light = server._recommend_lures(
            "Walleye",
            45,
            "winter",
            {
                "daytime": {
                    "average_cloud_cover_percent": 80,
                    "maximum_precipitation_probability_percent": 60,
                }
            },
        )
        cold_front = server._recommend_lures(
            "Largemouth Bass",
            55,
            "fall",
            {"daytime": {"temperature_change_f": -8}},
        )

        self.assertNotEqual(baseline["day"][0]["lure"], low_light["day"][0]["lure"])
        self.assertIn("clouds or rain", low_light["day"][0]["how"])
        self.assertIn("Ned rig", cold_front["day"][0]["lure"])
        self.assertIn("Cool nearby water", cold_front["day"][0]["how"])

    def test_picks_are_specific_and_include_a_condition_change_backup_for_each_species(self):
        for species in (
            "Largemouth Bass",
            "Smallmouth Bass",
            "Crappie",
            "Walleye",
            "Northern Pike",
            "Catfish",
            "Trout",
        ):
            with self.subTest(species=species):
                result = server._recommend_lures(species, 61, "fall", {}, "pond")
                for period in ("day", "night"):
                    self.assertEqual(len(result[period]), 3)
                    for pick in result[period]:
                        self.assertTrue(pick["lure"])
                        self.assertTrue(pick["backup"])
                        self.assertIn("Cast", pick["how"])
                        self.assertIn("Backup:", pick["how"])
                        self.assertIn("retrieve", pick["how"].casefold())
                        self.assertRegex(pick["lure"], r"\d")
                self.assertEqual(
                    len(
                        {
                            server._lure_identity(pick["lure"])
                            for pick in result["day"]
                        }
                    ),
                    3,
                )

    def test_exact_recent_local_reports_influence_only_matching_water_and_species(self):
        current = server._recommend_lures(
            "Northern Pike",
            50,
            "fall",
            {},
            "lake",
            "MA",
            "Lake Quinsigamond, Worcester",
            date(2026, 11, 11),
        )
        expired = server._recommend_lures(
            "Northern Pike",
            50,
            "fall",
            {},
            "lake",
            "MA",
            "Lake Quinsigamond",
            date(2026, 11, 20),
        )
        wrong_species = server._recommend_lures(
            "Largemouth Bass",
            50,
            "fall",
            {},
            "lake",
            "MA",
            "Lake Quinsigamond",
            date(2026, 11, 11),
        )

        self.assertIn("dated 2026-11-05", current["basis"])
        self.assertIn("spinnerbait", current["day"][0]["lure"].casefold())
        self.assertNotIn("Exact-waterbody report", expired["basis"])
        self.assertNotIn("Exact-waterbody report", wrong_species["basis"])

    def test_verified_shore_access_ranks_above_unverified_shoreline_sections(self):
        ring = [
            [-71.01, 42.01],
            [-70.99, 42.01],
            [-70.99, 41.99],
            [-71.01, 41.99],
            [-71.01, 42.01],
        ]
        spots = server._shoreline_spots(
            ring,
            {"current": {"available": False}},
            access_spots=[
                {
                    "name": "Public fishing pier",
                    "latitude": 42.0,
                    "longitude": -71.01,
                    "access_type": "Fishing pier",
                    "access_status": "Public access mapped",
                    "source": "OpenStreetMap",
                },
                {
                    "name": "Distant pier",
                    "latitude": 42.03,
                    "longitude": -71.01,
                    "access_type": "Fishing pier",
                    "source": "OpenStreetMap",
                },
            ],
        )

        self.assertEqual(spots[0]["label"], "Fishing pier · Public fishing pier")
        self.assertEqual(spots[0]["access_type"], "Fishing pier")
        self.assertNotIn("Distant pier", str(spots))
        self.assertIn("Unverified shoreline section", spots[1]["access_type"])
        self.assertIn("Public shore access is not verified", spots[1]["reason"])

    def test_nearby_access_lists_only_mapped_features_and_ranks_public_access(self):
        response = {
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": 40.36,
                    "lon": -74.66,
                    "tags": {"leisure": "fishing", "name": "Private bank", "access": "private"},
                },
                {
                    "type": "node",
                    "id": 2,
                    "lat": 40.38,
                    "lon": -74.66,
                    "tags": {
                        "leisure": "fishing",
                        "man_made": "pier",
                        "name": "Public pier",
                        "access": "public",
                    },
                },
                {
                    "type": "way",
                    "id": 3,
                    "center": {"lat": 40.37, "lon": -74.66},
                    "tags": {"amenity": "parking", "name": "Lake lot", "access": "public"},
                },
                {
                    "type": "node",
                    "id": 4,
                    "lat": 40.39,
                    "lon": -74.66,
                    "tags": {"amenity": "parking", "name": "Unverified lot"},
                },
            ]
        }
        with patch.object(server, "_external_json", return_value=response):
            access = server._nearby_fishing_access(
                {"latitude": 40.35, "longitude": -74.66}
            )

        self.assertEqual([spot["name"] for spot in access["fishing_spots"]], ["Public pier"])
        self.assertEqual(access["shore_access"][0]["access_type"], "Fishing pier")
        self.assertEqual(access["parking"][0]["name"], "Lake lot")
        self.assertEqual(access["parking"][0]["access_status"], "Public access mapped")

    def test_massachusetts_access_inventory_adds_state_facilities_and_attribution(self):
        state_features = [
            {
                "attributes": {
                    "FACNAME": "Arlington Reservoir",
                    "TOWN": "Lexington",
                    "RESTRICTED": "No",
                    "FEE": "No",
                    "PARKING": "Yes",
                    "STREET_PERMITED": "No",
                    "PENDING": "No",
                },
                "geometry": {"x": -71.1894, "y": 42.4291},
            }
        ]
        with (
            patch.object(server, "_arcgis_nearby_features", return_value=state_features),
            patch.object(server, "_external_json", return_value={"elements": []}),
        ):
            access = server._nearby_fishing_access(
                {"latitude": 42.4291, "longitude": -71.1894}, "MA"
            )

        self.assertEqual(access["fishing_spots"][0]["name"], "Arlington Reservoir")
        self.assertEqual(access["shore_access"], [])
        self.assertEqual(access["parking"][0]["access_status"], "State fishing access")
        self.assertIn("MassGIS", access["attribution"])
        self.assertIn("OpenStreetMap", access["attribution"])

    def test_openstreetmap_access_is_used_when_state_access_lookup_fails(self):
        response = {
            "elements": [
                {
                    "type": "node",
                    "id": 9,
                    "lat": 42.4291,
                    "lon": -71.1894,
                    "tags": {
                        "leisure": "fishing",
                        "man_made": "pier",
                        "access": "public",
                        "name": "Mapped public pier",
                    },
                }
            ]
        }
        with (
            patch.object(
                server,
                "_arcgis_nearby_features",
                side_effect=server.DataSourceError("MassGIS", "service unavailable"),
            ),
            patch.object(server, "_external_json", return_value=response),
        ):
            access = server._nearby_fishing_access(
                {"latitude": 42.4291, "longitude": -71.1894}, "MA"
            )

        self.assertEqual(access["fishing_spots"][0]["name"], "Mapped public pier")
        self.assertEqual(access["shore_access"][0]["access_type"], "Fishing pier")
        self.assertIn(
            "Massachusetts public fishing-access data is unavailable",
            access["message"],
        )

    def test_usgs_rdb_site_parser_skips_type_row(self):
        text = (
            "agency_cd\tsite_no\tstation_nm\tsite_tp_cd\tdec_lat_va\tdec_long_va\n"
            "5s\t15s\t50s\t3s\t16n\t16n\n"
            "USGS\t01400000\tCarnegie Lake\tLK\t40.35\t-74.66\n"
        )

        sites = server._parse_usgs_sites(text)

        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0]["station_nm"], "Carnegie Lake")
        self.assertEqual(sites[0]["latitude"], 40.35)

    def test_water_temperature_converts_celsius_and_reports_station(self):
        observed_at = datetime.now().astimezone().isoformat()
        features = {
            "features": [
                {
                    "properties": {
                        "monitoring_location_id": "USGS-01400000",
                        "time": observed_at,
                        "value": 16,
                        "unit_of_measure": "deg C",
                    },
                    "geometry": {"coordinates": [-74.66, 40.35]},
                }
            ]
        }
        station = {
            "features": [
                {"properties": {"monitoring_location_name": "Carnegie Lake"}}
            ]
        }
        with patch.object(
            server, "_external_json", side_effect=[features, station]
        ):
            report = server._water_temperature(
                {"latitude": 40.35, "longitude": -74.66, "reference": "Carnegie Lake"}
            )

        self.assertTrue(report["available"])
        self.assertEqual(report["temperature_f"], 60.8)
        self.assertEqual(report["station_name"], "Carnegie Lake")

    def test_water_temperature_does_not_report_stale_readings(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        response = {
            "features": [
                {
                    "properties": {
                        "monitoring_location_id": "USGS-01400000",
                        "time": stale,
                        "value": 16,
                        "unit_of_measure": "deg C",
                    },
                    "geometry": {"coordinates": [-74.66, 40.35]},
                }
            ]
        }
        with patch.object(server, "_external_json", return_value=response):
            report = server._water_temperature(
                {"latitude": 40.35, "longitude": -74.66, "reference": "Carnegie Lake"}
            )

        self.assertFalse(report["available"])
        self.assertIn("No recent USGS", report["message"])


class MobilePhotoPickerTests(unittest.TestCase):
    def test_mobile_photo_picker_allows_library_images_and_historical_dates(self):
        html = (server.APP_DIRECTORY / "index.html").read_text(encoding="utf-8")
        photo_input = next(
            line for line in html.splitlines() if 'id="catch-photo"' in line
        )

        self.assertIn('accept="image/jpeg,image/png,image/webp"', photo_input)
        self.assertNotIn("capture=", photo_input)
        self.assertIn('id="catch-date" type="date" required', html)
        self.assertIn('id="catch-time" required', html)


if __name__ == "__main__":
    unittest.main()
