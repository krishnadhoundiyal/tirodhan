# Finite local-only tests. No host ports, production services or credentials.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$fixture = (Resolve-Path (Join-Path $PSScriptRoot 'data/delhi-road.osm.pbf')).Path
$graphHash = (Get-FileHash -LiteralPath $fixture -Algorithm SHA256).Hash.ToLowerInvariant()
if ($graphHash -ne '4f36f7e749aef6f21128288ab8ca6447666a506a440d74db778a079974a71b49') { throw 'Unexpected road fixture hash' }
$osrm = 'ghcr.io/project-osrm/osrm-backend@sha256:729461bcc9ae9e6aafa92c0f93db9b060a32e85d5e72092c01ae4a4a9f1eb564'
$suffix = [Guid]::NewGuid().ToString('N')
$volume = "tirodhan-routing-$suffix"
$server = "tirodhan-routing-osrm-$suffix"
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker exited with $LASTEXITCODE" }
}
Push-Location $repo
try {
    Invoke-Docker build --target routing-tests -f tests/routing/Dockerfile -t tirodhan-routing-tests:increment-2 .
    Invoke-Docker volume create $volume
    Invoke-Docker run --rm --cpus 2 --memory 2g -v "${volume}:/data" -v "${fixture}:/input/map.osm.pbf:ro" $osrm sh -ec 'cp /input/map.osm.pbf /data/map.osm.pbf && osrm-extract -t 2 -p /opt/car.lua /data/map.osm.pbf && osrm-partition -t 2 /data/map.osrm && osrm-customize -t 2 /data/map.osrm'
    Invoke-Docker run -d --name $server --cpus 2 --memory 2g -v "${volume}:/data:ro" $osrm osrm-routed -t 2 --algorithm mld /data/map.osrm
    # Bounded readiness polling in the test image's shared network namespace.
    Invoke-Docker run --rm --network "container:$server" --entrypoint python tirodhan-routing-tests:increment-2 tests/routing/wait_osrm.py
    Invoke-Docker run --rm --cpus 2 --memory 4g --network "container:$server" -e TIRODHAN_ROUTING_ENGINE_TESTS=1 -e TIRODHAN_OSRM_TEST_URL=http://localhost:5000 -e "TIRODHAN_OSRM_GRAPH_VERSION=fixture-$graphHash" tirodhan-routing-tests:increment-2
} finally {
    # A missing server after a build/graph failure must not skip volume cleanup.
    $cleanupPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker rm -f $server 2>$null | Out-Null
        & docker volume rm $volume 2>$null | Out-Null
    } finally {
        $ErrorActionPreference = $cleanupPreference
        Pop-Location
    }
}
