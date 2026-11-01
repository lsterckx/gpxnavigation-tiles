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
3. Later (fase 2) laadt GitHub Actions dit elke zondagnacht op naar Cloudflare R2, bereikbaar op
   `https://tiles.data-tech-up.com/`.

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

## Een gebied toevoegen

Een regel bijzetten in `regions.yml` (of `enabled: true` zetten). Alle gebieden worden samen
verwerkt, zodat een tegel op de grens van twee gebieden de wegen van beide bevat.
