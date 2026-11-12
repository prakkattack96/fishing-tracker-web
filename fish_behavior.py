"""Cautious, source-cited bathymetry adjustments for shore-pin ranking."""

KNOWLEDGE_BASE_VERSION = "1.0"
KNOWLEDGE_ACCESSED_ON = "2026-11-11"

SOURCES = {
    "massgis_bathymetry": {
        "title": "MassGIS Data: MassWildlife Inland Water Bathymetry",
        "publisher": "MassGIS and Massachusetts Division of Fisheries and Wildlife",
        "url": "https://www.mass.gov/info-details/massgis-data-masswildlife-inland-water-bathymetry",
        "finding": (
            "MassGIS documents DEPTH in feet, SHORE=1 for shoreline/island lines, "
            "and contours generally at 2-, 5-, or 10-foot intervals. Sounding "
            "density and age vary by waterbody."
        ),
    },
    "masswildlife_summer_trout": {
        "title": "Finding summer trout",
        "publisher": "Massachusetts Division of Fisheries and Wildlife",
        "url": "https://www.mass.gov/info-details/finding-summer-trout",
        "finding": (
            "Lake trout need cool, oxygenated water. In summer, suitable habitat "
            "can be squeezed between warm shallows and oxygen-poor depths; its "
            "depth varies by pond and date."
        ),
    },
    "masswildlife_trout_tips": {
        "title": "Trout identification and fishing tips",
        "publisher": "Massachusetts Division of Fisheries and Wildlife",
        "url": "https://www.mass.gov/info-details/trout-identification-and-fishing-tips",
        "finding": (
            "MassWildlife says brown trout have more nocturnal habits; the page "
            "does not generalize that behavior to all trout species."
        ),
    },
    "smallmouth_maine": {
        "title": "Relation of adult size to movements and distribution of smallmouth bass in a central Maine Lake",
        "publisher": "Cole and Moring, Transactions of the American Fisheries Society",
        "url": "https://www.usgs.gov/publications/relation-adult-size-movements-and-distribution-smallmouth-bass-a-central-maine-lake",
        "finding": (
            "In one central Maine lake, larger smallmouth used deeper water more "
            "often in late summer; cover use varied with size and season. This "
            "does not establish an exact Massachusetts shore depth."
        ),
    },
    "smallmouth_stream": {
        "title": "Smallmouth Bass Summer Habitat Use, Movement, and Survival in Response to Low Flow in the Illinois Bayou, Arkansas",
        "publisher": "North American Journal of Fisheries Management",
        "url": "https://doi.org/10.1577/M08-183.1",
        "finding": (
            "In a summer stream study, fish used a median depth of 0.80 m and "
            "preferred boulder habitat when available. It is stream evidence, "
            "not a lake-depth rule."
        ),
    },
    "walleye_lake_erie": {
        "title": "Does behavioural thermoregulation underlie seasonal movements in Lake Erie walleye?",
        "publisher": "Canadian Journal of Fisheries and Aquatic Sciences",
        "url": "https://doi.org/10.1139/cjfas-2017-0145",
        "finding": (
            "Telemetry found some adult Lake Erie walleye moved from warm, shallow "
            "water toward deeper/cooler basins seasonally; temperature alone did "
            "not explain all movements. This large-lake result transfers weakly "
            "to small ponds."
        ),
    },
    "northern_pike_depth_oxygen": {
        "title": "Depths and Thermal Habitat Used by Large versus Small Northern Pike in Three Minnesota Lakes",
        "publisher": "Transactions of the American Fisheries Society",
        "url": "https://doi.org/10.1080/00028487.2013.822422",
        "finding": (
            "Depth use varied with season and fish size in three lakes; dissolved "
            "oxygen can constrain depth use. No oxygen profile is available for "
            "the selected waterbody."
        ),
    },
}

BASELINE = {
    "seasonal_adjustments": {},
    "source_ids": ["massgis_bathymetry"],
    "confidence": "Low",
    "behavior_note": (
        "No reliable local species-depth or day/night tracking data are available. "
        "Nearby mapped contours are treated as relative structure only."
    ),
    "limitations": [
        "The contour layer does not confirm that the selected species occurs in this waterbody.",
        "Substrate, vegetation, forage, dissolved oxygen, and thermocline are not measured by the contour layer.",
        "A contour near a pin is not a measured depth at the shoreline or proof of a fish-holding hotspot.",
    ],
}

PROFILES = {
    "smallmouth bass": {
        "seasonal_adjustments": {
            "summer": {
                "mid_to_deep_transition": 0.35,
                "deep_edge": 0.15,
            }
        },
        "source_ids": [
            "smallmouth_maine",
            "smallmouth_stream",
            "massgis_bathymetry",
        ],
        "confidence": "Low",
        "behavior_note": (
            "A small summer weighting favors mapped mid/deep transitions, informed "
            "by one central Maine lake study. Fish size, local lake shape, cover, "
            "and temperature/oxygen can change the pattern."
        ),
    },
    "walleye": {
        "seasonal_adjustments": {
            "summer": {
                "mid_to_deep_transition": 0.2,
                "deep_edge": 0.1,
            }
        },
        "source_ids": ["walleye_lake_erie", "massgis_bathymetry"],
        "confidence": "Low",
        "behavior_note": (
            "A very small summer weighting favors deeper transitions, informed by "
            "large-lake Lake Erie telemetry; this is weak evidence for a pond."
        ),
    },
    "trout": {
        "seasonal_adjustments": {
            "summer": {
                "mid_to_deep_transition": 0.1,
                "deep_edge": -0.15,
            }
        },
        "source_ids": [
            "masswildlife_summer_trout",
            "masswildlife_trout_tips",
            "massgis_bathymetry",
        ],
        "confidence": "Low",
        "behavior_note": (
            "In summer, the ranking avoids assuming the deepest contour is suitable: "
            "MassWildlife says trout need both cool water and oxygen, and the map "
            "does not measure either. The selector groups trout species, so no "
            "species-wide night shift is applied."
        ),
        "limitations": [
            "A contour map cannot show whether deep water has suitable temperature or oxygen.",
            "The selector groups brook, brown, rainbow, and stocked trout; their behavior differs.",
            *BASELINE["limitations"],
        ],
    },
    "northern pike": {
        "seasonal_adjustments": {},
        "source_ids": ["northern_pike_depth_oxygen", "massgis_bathymetry"],
        "confidence": "Low",
        "behavior_note": (
            "No deep-water preference is assumed: seasonal pike depth use can be "
            "limited by dissolved oxygen, which is not measured here."
        ),
        "limitations": [
            "No local dissolved-oxygen profile is available to identify usable deep water.",
            *BASELINE["limitations"],
        ],
    },
}


def profile_for_species(species: str) -> dict:
    """Return species-specific evidence when available, otherwise a neutral profile."""
    profile = PROFILES.get(species.casefold(), {})
    return {
        **BASELINE,
        **profile,
        "limitations": profile.get("limitations", BASELINE["limitations"]),
    }
