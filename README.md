# Fishing Tracker Web App

## Shore lure guide

The **Shore lure guide** tab creates separate day and night lure lists for a
named waterbody, state, and optional target fish. No ZIP code is needed.
Leaving the fish blank gives bass-first suggestions. It uses:

- Official Massachusetts (MassGIS) and New Hampshire (Fish and Game / GRANIT)
  waterbody inventories before falling back to USGS and OpenStreetMap name
  matching. The report identifies the inferred type (pond, lake, reservoir,
  river, or stream); Massachusetts hydrography alone does not establish public
  fishing access.
- USGS Water Data for a recent nearby water-temperature reading when available.
- The National Weather Service hourly forecast to determine local time and
  wind influence on lure ranking. Forecast details are not shown in the report.
- Official Massachusetts and New Hampshire public-access datasets, plus
  OpenStreetMap features tagged for fishing and parking, within about five
  miles. Results link to Google Maps and show their mapped access status.
- Seasonal and water-temperature lure profiles with waterbody-specific
  day/night picks and short shore-casting instructions.

Lure picks are rule-based seasonal, temperature, species, and habitat guidance,
not a live scrape of fishing reports or local catch data and not a guarantee.
Map and state inventory listings may be incomplete or out of date; mapped
access does not guarantee current permission or on-site conditions. Follow
posted access and parking restrictions. Ambiguous waterbody names are rejected
rather than silently selecting the wrong location.

The recommendation endpoint uses free public services and does not require the
OpenAI key or a signed-in catch account. It does require the server to have
internet access to MassGIS and NH GRANIT/Fish and Game (where applicable),
OpenStreetMap, Overpass, National Weather Service, and USGS. Access data
attribution and the OpenStreetMap copyright link appear in the report.

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

The initial version accepts JPEG, PNG, and WebP photos up to 8 MB. Stored photos
are private to the signed-in user and are saved in Supabase Storage.

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
