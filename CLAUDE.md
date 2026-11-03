Deze repository maakt OpenStreetMap-tegels voor de iOS-app gpxnavigation (lsterckx/gpxnavigation),
een navigatie-app voor blinde wandelaars. Communicatie, commentaar in code en documentatie: Nederlands.

- De repository is openbaar. Zet er nooit persoonlijke gegevens in: geen GPX-routes, geen
  navigatielogboeken, geen Overpass-antwoorden voor eigen routes, geen sleutels (die staan als secrets).
- Het tegelformaat (zoom, pad, JSON in Overpass-formaat, `complete`, de tags in KEEP_TAG_KEYS) is een
  afspraak met de app. Verander je het, verhoog dan `FORMAT_VERSION` en pas TileStore.swift in de app aan.
  Nieuwe tags die RouteFeatures.swift gaat lezen, moeten ook in KEEP_TAG_KEYS.
- De tegels moeten byte voor byte gelijk blijven als er niets veranderde (geen datum in een tegel,
  gesorteerde elementen, gzip met mtime=0): zo worden alleen gewijzigde tegels opgeladen.
- Na een wijziging: `python3 tests/test_tiles.py` moet slagen.
- Het plan van aanpak: https://claude.ai/artifact/NTzvGRCj3734PmXvqYV7rZ
- Het formaat van `areas/<gebied>.json.gz` (build_areas.py, `FORMAT_VERSION`) is ook een afspraak met de app
  (AreaInfoStore.swift). Na een wijziging: `python3 tests/test_areas.py` moet slagen.
- Het formaat van `water/14/x/y.json.gz` (build_water.py) is een afspraak met de app (WaterStore.swift). Na een
  wijziging: `python3 tests/test_water.py` moet slagen.
