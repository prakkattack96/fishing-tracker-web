# Fishing Tracker Web App

## Shore lure guide

The **Shore lure guide** tab creates separate day and night lure lists for a
named waterbody, state, and optional target fish. No ZIP code is needed.
For duplicate waterbody names, add the town after a comma (for example,
`Long Pond, Plymouth`). Leaving the fish blank gives bass-first suggestions.
It uses:

- Official Massachusetts (MassGIS) and New Hampshire (Fish and Game / GRANIT)
  waterbody inventories before falling back to USGS and OpenStreetMap name
  matching. The report identifies the inferred type (pond, lake, reservoir,
  river, or stream); Massachusetts hydrography alone does not establish public
  fishing access.
- USGS Water Data for a recent nearby water-temperature reading when available.
- The nearest available National Weather Service station's latest wind
  observation, including its age and direction. The hourly forecast informs
  day/night lure ranking using wind, cloud cover, precipitation probability,
  and air-temperature changes; those forecasts are not displayed as local
  fishing facts.
- A satellite map from Esri, a state waterbody outline when available, and
  official Massachusetts or New Hampshire bathymetric contours when available.
  New Hampshire contour metadata specifies feet; the Massachusetts service
  does not specify the depth unit in its layer metadata.
- Seasonal and nearby-water-temperature lure profiles with specific sizes,
  rigging, retrieval, shore habitat, rationale, and a backup presentation.
- Public shoreline-access pins only when the state or OpenStreetMap record
  explicitly identifies shore or fishing-pier access and the point is close to
  the matched shoreline. Other pins are approximate shoreline sections, not
  access claims.

Lure picks are rules, not a guarantee or a substitute for a waterbody-specific
fish survey. A dated regional report can influence picks only for an exact
waterbody/species match, and that snapshot expires after 14 days. The report
snapshot is not automatically refreshed. Wind is a cautious search-side
heuristic: it may move surface water and floating forage, but does not establish
where fish are holding. River current seams take priority on moving water.
Where access cannot be confirmed, map pins show approximate shoreline places to
begin looking, not proven hotspots, safe paths, or permission to enter. USGS
water temperature is the nearest recent sensor within 75 miles, not a
measurement at the selected fishing spot. Forage, water clarity, submerged
vegetation, substrate, local species occurrence, and current access conditions
are not consistently available and are not invented. Ambiguous waterbody names
are rejected rather than silently selecting the wrong location.

### Research sources

Sources below were reviewed on **November 11, 2026**. Recent reports are
anecdotal and were used only when they named a waterbody and species; they do
not override seasonal guidance outside the short expiry window. Manufacturer
and tournament-angler material informs presentation technique, not fish
occurrence or guaranteed catch rates.

- MassWildlife, [freshwater fishing](https://www.mass.gov/freshwater-fishing)
  and [2026 trout stocking report](https://www.mass.gov/info-details/trout-stocking-report):
  state fishery context, stocking species, waters, and dates.
- Massachusetts, [saltwater public-access information](https://www.mass.gov/info-details/saltwater-fishing-public-access):
  the Office of Fishing and Boating Access distinguishes ramps, piers, and
  shore-side fishing areas. The app promotes only pins whose access type is
  explicit in its underlying record.
- NOAA, [New England/Mid-Atlantic fisheries](https://www.fisheries.noaa.gov/region/new-england-mid-atlantic),
  [tides and currents](https://tidesandcurrents.noaa.gov/), and
  [Public Access Fishing Site Register](https://www.st.nmfs.noaa.gov/recreational-fisheries/access-data/public-access-fishing-site-register/index):
  regional marine-management context, official coastal conditions, and
  access-site categories.
- USGS, [Water Data](https://waterdata.usgs.gov/nwis): source for nearby
  measured water-temperature readings. Sensor distance and freshness remain
  important limitations.
- On The Water, [Massachusetts Fishing Report, November 5, 2026](https://onthewater.com/fishing-reports/2026/11/massachusetts-fishing-report-november-5-2026):
  exact-water reports included shallow pike catches at Lake Quinsigamond,
  Indian Lake, and Flint Pond (spinnerbaits, inline spinners, and shiners), and
  green dough bait for trout at Pratt's Pond. These matching cases are recorded
  in `server.py` and stop influencing picks after 14 days.
- On The Water, [Cape Cod Fishing Report, November 5, 2026](https://onthewater.com/fishing-reports/2026/11/cape-cod-fishing-report-november-5-2026):
  checked as regional seasonal context; its broad observations are not applied
  to unrelated ponds or asserted as current catch data.
- Bassmaster, [Land of Giants: Early Fall Transition](https://www.bassmaster.com/elite/news/land-of-giants-early-fall-transition/)
  and [Northern Fishing Best in the Fall](https://www.bassmaster.com/column/easton-fothergill/northern-fishing-best-in-the-fall/):
  experienced-angler context on seasonal transitions, forage, location
  uncertainty, and fall presentations.
- Berkley, [bass spawn overview](https://www.berkley-fishing.com/blogs/news/everything-you-need-to-know-about-the-spawn-but-were-afraid-to-ask)
  and [Power vs. Finesse](https://www.berkley-fishing.com/blogs/news/power-vs-finesse):
  technique and seasonal behavior cross-checks. Manufacturer advice is treated
  as technique guidance, not neutral evidence.
- Wind/habitat research already used by the location heuristic:
  [Limnology and Oceanography (2010)](https://doi.org/10.4319/lo.2010.55.4.1590)
  and [Limnology and Oceanography (2005)](https://doi.org/10.4319/lo.2005.50.2.0465).
  These studies do not identify a specific bass hotspot.

The current form covers freshwater target species only; it has no saltwater
target-species or tide-stage selector. Marine recommendations and tide-aware
lure ranking therefore remain out of scope rather than being guessed.

The wind heuristic is intentionally limited. Field research has documented
wind-related patchiness in planktonic predator-prey overlap
([Limnology and Oceanography, 2010](https://doi.org/10.4319/lo.2010.55.4.1590)),
and littoral fish distributions associated with fetch and physical habitat
([Limnology and Oceanography, 2005](https://doi.org/10.4319/lo.2005.50.2.0465)).
Neither study predicts bass locations for a specific shoreline, so the app does
not label its wind-based casting zones as proven hotspots.

The recommendation endpoint uses public data services and does not require the
OpenAI key or a signed-in catch account. It does require the server to have
internet access to MassGIS and NH GRANIT/Fish and Game (where applicable),
National Weather Service, USGS, and OpenStreetMap for fallback waterbody
lookups. The map also loads Esri satellite tiles; its optional street layer
loads OpenStreetMap tiles and displays required map attribution.

This is the phone-friendly, installable web app with a Supabase-backed catch
log. The Tkinter desktop app is a separate client for now and has not been
connected to the Supabase account yet. Do not expect catches to sync between
the web and desktop apps until that client is updated too.

## What is needed

- Python 3.10 or newer
- A Supabase project
- An OpenAI API key to enable photo identification (this is a paid API and usage
  is billed by the provider)

## Set up the database and storage

1. Create a Supabase project.
2. In the Supabase SQL Editor, run `schema.sql`.
3. In Authentication settings, choose whether new accounts require email
   confirmation. If enabled, configure the local and deployed redirect URLs.
4. Copy `config.example.js` to `config.js` and add the project's URL and
   publishable/anon key. This key is intended for browser use; Row Level
   Security policies in `schema.sql` protect each user's data.

## Run locally

Create a local configuration file (it is ignored by git) and fill in the
Supabase project URL and publishable/anon key:

```powershell
Copy-Item config.example.js config.js
notepad config.js
```

Then start the web app from this directory in PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:SUPABASE_URL = "https://YOUR-PROJECT.supabase.co"
$env:SUPABASE_ANON_KEY = "YOUR_SUPABASE_PUBLISHABLE_OR_ANON_KEY"
$env:OPENAI_API_KEY = "YOUR_OPENAI_API_KEY"
$env:ALLOWED_ORIGINS = "http://localhost:8000"
uvicorn server:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` on the computer. For phone testing on the same
Wi-Fi, open `http://COMPUTER-LAN-IP:8000`; Windows Firewall may ask to allow
Python on a private network. Browsers generally require HTTPS to install a PWA
or enable its offline cache on a phone; localhost is the exception.

Never put the OpenAI API key in `config.js` or any browser code. Configure it as
a server environment variable. Do not deploy the local development server
directly to the public internet.

## Photo handling

Photos remain on the device until the user chooses **Identify species** or saves
a catch with a photo. Identification sends the selected photo to the server and
then to the configured AI provider. The AI returns up to three suggestions from
the app's species list; visual identification and the displayed confidence are
estimates, and the user chooses the species before saving.

The initial version accepts JPEG, PNG, and WebP photos up to 8 MB. On mobile,
the photo picker allows choosing an existing library image or taking a new
photo. Set the catch date and time separately, so a historical photo can be
logged later. Stored photos are private to the signed-in user and are saved in
Supabase Storage.

## Data sync

The web app stores catches locally in IndexedDB and syncs them to the signed-in
Supabase account when online. Changes made to the same catch while offline use
the most-recent-edit-wins policy; deletions sync as tombstones. Photos stay
local until the catch syncs. Never sign into an untrusted device, and sign out
when finished on a shared device.

The desktop Tkinter app still uses its separate SQLite database. A follow-up
desktop-client update and one-time local-catch import are required before
desktop and web catches will be shared. Keep the existing desktop database as a
backup until that migration is implemented and verified.
