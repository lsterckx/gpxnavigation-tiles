# gpxnavigation-tiles

Voorbereide OpenStreetMap-tegels voor de iOS-app **gpxnavigation** (wandelnavigatie voor blinde
gebruikers). De app haalt hiermee straten, zijstraten, paden, trappen en oversteekplaatsen langs een
route in enkele seconden op, in plaats van 25–45 s per vraag bij de publieke Overpass-servers.

Gegevens © OpenStreetMap-bijdragers, beschikbaar onder de Open Database License (ODbL):
<https://www.openstreetmap.org/copyright>. Zie [ATTRIBUTION.txt](ATTRIBUTION.txt).

## Hoe het werkt

1. `scripts/fetch_regions.py` downloadt per gebied uit `regions.yml` het `.osm.pbf`-bestand en de
   omtrek (`.poly`) bij [Geofabrik](https://download.geofabrik.de).
2. `scripts/build_tiles.py` maakt tegels op zoomniveau 14 (ongeveer 1,6 × 1,6 km in België):
   `out/14/{x}/{y}.json.gz`, in het JSON-formaat van Overpass. Daarnaast `out/regions.json`
   (welke gebieden, datum van de gegevens) en `out/status.json` (aantallen, grootte, duur).
3. GitHub Actions (`.github/workflows/tiles.yml`) doet dit elke zondagnacht en laadt de tegels op naar
   Cloudflare R2, bereikbaar op `https://tiles.data-tech-up.com/14/{x}/{y}.json.gz`. Zie hieronder.

Een tegel heeft `"complete": true` als hij volledig binnen de gebieden ligt. Aan een gebiedsrand
(`false`) ontbreken wegen van over de grens; daar gebruikt de app nog Overpass. Een tegel die niet
bestaat (404) ligt buiten de gebieden.

## Op de Mac proberen (fase 1)

```sh
brew install osmium-tool            # aangeraden: veel sneller en minder geheugen
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 tests/test_tiles.py         # test zonder internet
python3 scripts/fetch_regions.py    # België, ongeveer 650 MB
python3 scripts/build_tiles.py      # tegels in out/
```

Tegels vergelijken met Overpass voor een route. **Zet nooit eigen routes of navigatielogboeken in
deze repository** (ze is openbaar); bewaar ze erbuiten, bijvoorbeeld in `~/gpx-test/`:

```sh
python3 scripts/compare_overpass.py ~/gpx-test/route.gpx --tiles out
```

## Automatisch bijwerken (fase 2)

De werkstroom **Tegels maken en opladen** draait elke zondag om 02:00 UTC, en met de hand via
*Actions → Tegels maken en opladen → Run workflow* (vink *upload* uit om alleen te testen). Hij:

1. draait `tests/test_tiles.py`, downloadt de gebieden en maakt de tegels (mislukt boven 8 GB);
2. laadt met `rclone sync --checksum` alleen de gewijzigde tegels op naar de bucket `gpxnavigation-tiles`
   (onder `14/`; andere bestanden in de bucket blijven staan), plus `regions.json`, `status.json` en
   `ATTRIBUTION.txt`. Tegels krijgen `Content-Type: application/gzip`: de app pakt ze zelf uit;
3. controleert via het publieke adres dat de grootste tegel en `regions.json` bereikbaar zijn;
4. schrijft `status.json` terug in de repository (aantallen, grootte, duur). Dat houdt de geplande run
   actief: GitHub zet die anders stil na 60 dagen zonder activiteit.

Nodige secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. Mislukt een run, dan mailt
GitHub en blijven de vorige tegels in R2 gewoon werken.

## Een gebied toevoegen

Een regel bijzetten in `regions.yml` (of `enabled: true` zetten). Alle gebieden worden samen
verwerkt, zodat een tegel op de grens van twee gebieden de wegen van beide bevat.
