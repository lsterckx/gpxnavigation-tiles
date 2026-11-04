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
3. GitHub Actions (`.github/workflows/tiles.yml`) doet dit de 1e van elke maand (gesplitst per gebied, zie onderaan) en laadt de tegels op naar
   Cloudflare R2, bereikbaar op `https://tiles.data-tech-up.com/14/{x}/{y}.json.gz`. Zie hieronder.

Een tegel heeft `"complete": true` als hij volledig binnen de gebieden ligt. Aan een gebiedsrand
(`false`) ontbreken wegen van over de grens; daar gebruikt de app nog Overpass. Een tegel die niet
bestaat (404) ligt buiten de gebieden, of ver van elke weg (op zee): lege tegels worden alleen gemaakt
naast een tegel met wegen.

## Op de Mac proberen (fase 1)

```sh
brew install osmium-tool            # aangeraden: veel sneller en minder geheugen
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 tests/test_tiles.py         # test zonder internet
python3 scripts/fetch_regions.py    # de gebieden met enabled: true in regions.yml (enkele GB)
python3 scripts/build_tiles.py      # tegels in out/
```

Tegels vergelijken met Overpass voor een route. **Zet nooit eigen routes of navigatielogboeken in
deze repository** (ze is openbaar); bewaar ze erbuiten, bijvoorbeeld in `~/gpx-test/`:

```sh
python3 scripts/compare_overpass.py ~/gpx-test/route.gpx --tiles out
```

## Automatisch bijwerken (fase 2)

De werkstroom **Tegels maken en opladen** draait de 1e van elke maand om 02:00 UTC, en met de hand via
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

## Omgevingsgegevens (gemeenten, deelgemeenten, plaatsen)

`scripts/build_areas.py` maakt per gebied `out/areas/<gebied>.json.gz` en `out/areas/index.json`, voor
*Extra → Omgeving* in de app: in welke (deel)gemeente ligt een punt, welke gemeenten grenzen eraan, welke
deelgemeenten en steden liggen in de buurt. Uit hetzelfde Geofabrik-bestand: grenzen met
`boundary=administrative` op `municipality_level` (standaard 8) en `section_level` (standaard 9; per gebied in
`regions.yml`, bv. Portugal 7 en 8), vereenvoudigd tot ±40 m, buren (grenzen die elkaar raken), en plaatsen
(`place=city/town/village` met vertalingen en inwonersaantal). De werkstroom maakt ze na de tegels en laadt ze op
onder `areas/`. Test: `python3 tests/test_areas.py`.

## Water (waterlopen en watervlakken)

`scripts/build_water.py` maakt een aparte laag tegels `out/water/14/{x}/{y}.json.gz` (zelfde raster, alleen tegels
met water) voor *Extra → Water* in de app: waterlopen (`waterway=river/stream/canal/ditch/drain/tidal_channel`, met
en zonder naam, in de stroomrichting) en watervlakken (`natural=water`, `landuse=reservoir/basin`; geen
rivierbeddingen, geen zwembaden, geen naamloze vlakken onder 200 m²). Test: `python3 tests/test_water.py`.

## Een gebied toevoegen

Een regel bijzetten in `regions.yml` (of `enabled: true` zetten). Alle gebieden worden samen
verwerkt, zodat een tegel op de grens van twee gebieden de wegen van beide bevat.

## Gesplitste run (sinds 04/11/2026)

Eén job voor alle gebieden paste niet meer (6 uur, 14 GB schijf, 16 GB geheugen). De werkstroom draait nu
**de 1e van elke maand** en bestaat uit drie delen:

1. **plan**: de tests en de lijst van gebieden met `enabled: true`.
2. **part**: één job per gebied, hoogstens 4 tegelijk. Downloadt het gebied en de omtrek van alle gebieden
   (`fetch_regions.py --only <id>` en `--polys-only`), maakt de wegtegels (`build_tiles.py --part <id>`), de
   omgevingsgegevens en het water (`build_water.py --part <id>`). De **eigen tegels** (verder dan
   `border_margin_degrees` van elk ander gebied) worden meteen opgeladen. De **randtegels** gaan als artefact naar:
3. **merge**: `merge_parts.py` voegt de randtegels van alle gebieden samen (elementen van beide kanten, `complete`
   opnieuw berekend met alle gebieden samen), laadt ze op, wist verdwenen tegels (nooit meer dan 5% zonder de keuze
   *allow_many_deletes*), en schrijft `regions.json`, `status.json` en `areas/index.json`.

`merge_parts.py` controleert dat geen eigen tegel van een gebied geraakt wordt door een ander gebied; anders was de
marge te klein en stopt het met een fout. `tests/test_parts.py` bouwt twee verzonnen gebieden op beide manieren en
controleert dat de tegels byte voor byte gelijk zijn. Lokaal kan `build_tiles.py` zonder `--part` nog altijd alles in
één keer bouwen.
