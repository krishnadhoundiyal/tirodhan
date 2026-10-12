# Public road fixture

`delhi-road.osm.pbf` is an OSM extract, **© OpenStreetMap contributors**, distributed
under the Open Database License (ODbL 1.0):
https://www.openstreetmap.org/copyright and https://opendatacommons.org/licenses/odbl/1-0/.
It contains public map data, no Tirodhan customer, rider or operational data.

- Original source: https://download.bbbike.org/osm/bbbike/NewDelhi/NewDelhi.osm.pbf
- Source last-modified: 2026-10-10T16:20:12Z (retained benchmark manifest).
- Source bytes: 37,319,858; SHA-256:
  `bb84a8e614f6cc74c99ded2fc0f831b70ca25c4bbe59be23cdf30a44f546022f`.
- Extraction (osmium 1.18.0): `osmium extract -b 77.20,28.62,77.22,28.64 NewDelhi.osm.pbf -o delhi-road.osm.pbf`.
- Included bytes: 407,402; SHA-256:
  `4f36f7e749aef6f21128288ab8ca6447666a506a440d74db778a079974a71b49`.

The fixture is a small geographic excerpt of that database. The network is deliberately
clipped for fast offline tests; it is not suitable for deployment, city-scale benchmarks
or estimating the quality of routes crossing its boundary. Test facilities, appointments,
cargo and vehicles are synthetic. OSRM uses its shipped **car** profile, which has not been
approved or calibrated for loaded Tirodhan vehicles. No claim of operational suitability
follows from these tests. Routing graph files are generated in a disposable Docker volume
by the pinned OSRM 6.0.0 image; no generated graph binary is committed.
