import unittest
from datetime import datetime, timedelta, timezone
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

    def test_report_has_day_and_night_picks(self):
        now = datetime.now(ZoneInfo("America/New_York"))
        weather = {
            "provider": "NWS",
            "forecast_date": now.date().isoformat(),
            "local_time_zone": "America/New_York",
            "current_hour": {},
            "daytime": {
                "maximum_wind_mph": 12,
                "maximum_precipitation_chance_percent": 20,
            },
            "nighttime": {"maximum_wind_mph": 4},
        }
        location = {
            "latitude": 40.35,
            "longitude": -74.66,
            "place": "Carnegie Lake",
            "reference": "USGS site 01400000 (Carnegie Lake)",
            "provider": "USGS",
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
                "_nws_forecast",
                return_value=(weather, "America/New_York", now),
            ),
            patch.object(server, "_water_temperature", return_value=temperature),
        ):
            report = server.build_recommendation_report(
                server.RecommendationRequest(
                    water_body="Carnegie Lake", state="NJ"
                )
            )

        self.assertEqual(len(report["recommendations"]["day"]), 3)
        self.assertEqual(len(report["recommendations"]["night"]), 3)
        self.assertEqual(report["recommendations"]["species"], "Largemouth Bass")
        self.assertEqual(report["water_temperature"]["temperature_f"], 62.6)
        self.assertEqual(report["state"]["code"], "NJ")
        self.assertEqual(report["location_resolution"]["provider"], "USGS")

    def test_location_search_accepts_waterbody_in_selected_state(self):
        candidate = {
            "lat": "40.35",
            "lon": "-74.66",
            "name": "Carnegie Lake",
            "display_name": "Carnegie Lake, Princeton, New Jersey, United States",
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
        self.assertIn("air temperature", winter["basis"])

    def test_strong_forecast_wind_promotes_a_shore_search_lure(self):
        calm = server._recommend_lures(
            "Largemouth Bass", 44, "winter", {"daytime": {"maximum_wind_mph": 4}}
        )
        windy = server._recommend_lures(
            "Largemouth Bass", 44, "winter", {"daytime": {"maximum_wind_mph": 12}}
        )

        self.assertEqual(calm["day"][0]["lure"], "Suspending jerkbait")
        self.assertEqual(windy["day"][0]["lure"], "Small Colorado-blade spinnerbait")

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


if __name__ == "__main__":
    unittest.main()
