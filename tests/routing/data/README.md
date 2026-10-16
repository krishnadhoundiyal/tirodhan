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

## Increment 8 regional performance positions

`inc8-road-nodes.json` contains 64 public OSM road vertices sampled from the same
checked-in extract (same attribution and ODbL license above). It contains no household
or rider observations. Each row retains the original public OSM node ID for provenance.
The UTF-8 JSON SHA-256 after LF normalization is `2fe707d09685a1dc8d7f73673e2bb12f0018e6d0b608925da37c9651de3c076b`.

Sampling is deterministic: consider vertices referenced by residential, service,
tertiary, secondary, primary, living_street or unclassified ways inside
77.204–77.214 longitude / 28.627–28.635 latitude; start nearest 77.209/28.632,
then greedily maximize minimum squared distance to selected vertices, scaling longitude
by 0.88 and breaking ties by ascending OSM ID. Take 64 vertices. This is fixture sampling,
not a production clustering or distance algorithm. Raw vertices may snap to another
profile-legal road; the normal maximum-snap and matrix gates still apply.

The varied benchmark repeats these positions across distinct synthetic bookings and uses
canonical resolution-7 H3 membership. Dense mode retains the earlier four-anchor fixture.
Neither mode represents Delhi-wide demand, real traffic, actual customer service durations
or operationally calibrated car/bicycle profiles.

An earlier artificial coordinate-jitter fixture produced negative OSRM distance cells.
The strict provider rejected it; no negative values were clamped or accepted, and no
routing-validator rule was relaxed. See the increment 8 report for this release limitation.
