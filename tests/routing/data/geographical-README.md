# Broad Delhi operational QA fixture

`geographical-delhi.osm.pbf` is the exact BBBike NewDelhi extract downloaded for this QA:

- Source: https://download.bbbike.org/osm/bbbike/NewDelhi/NewDelhi.osm.pbf
- OSM header timestamp: `2026-10-16T23:00:00Z`.
- Bytes: `37,352,545`.
- SHA256: `10aa4a42d362a8435b2825220ed448e8c3c45f2a44ce2ac3b487a44a07b9d54b`.
- © OpenStreetMap contributors, Open Database License 1.0; see
  https://www.openstreetmap.org/copyright and https://opendatacommons.org/licenses/odbl/1-0/.

The retained PBF is intentional: BBBike's latest download changes, so a URL/hash alone cannot
reproduce this historical graph. It covers a broad Delhi/NCR road network, not the old central
Delhi 2 km fixture. No live customer location or production data is present.

Preparation uses the pinned OSRM image in `run-geographical.ps1`: `osrm-extract -t 2 -p /opt/car.lua`,
`osrm-partition -t 2`, `osrm-customize -t 2`. The shipped `car.lua` SHA256 is
`9809a111a8fc8c465c2d6ef3208938a2a7c35fab0d3cda407ffa7ff187a96477`.
OSRM is 6.0.0; the graph/profile names in the routing contract are the source SHA and `qa-car`.
The pinned image digest fixes the profile dependencies too. This car profile is an engineering
fixture, not a calibration claim about loaded Tirodhan vehicles, traffic or production suitability.

`geographical-delhi-nodes.json` contains deterministic source vertices selected using
`tests/integration/geographical_nodes.py` and `osmium==4.2.0` (fixture regeneration only).
West has interleaved two-dimensional clusters and dispersed vertices; North/East are concentrated;
South is dispersed. Bounding boxes describe fixture selection; configured service areas are H3
resolution-7 cells, recorded in the execution artifacts, not official district boundaries.

`geographical-delhi-eligible-nodes.json` is the graph snap preflight using
`geographical_snap_fixture.py`. It retains 280/272/279/280 vertices in West/North/South/East and
all nine exclusions with actual table snap distances. Only the existing 100 m snap eligibility
rule selects vertices. No matrix distance, negative value, solver outcome or objective is used
for selection. Original coordinates and OSM node identities are retained. The recovery regression
deliberately uses an excluded observation to verify rejection, durable retry and recovery.

Derived graph files live under ignored `.tools/geographical-qa`; they are not deployed.
