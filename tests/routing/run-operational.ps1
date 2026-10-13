# Finite local-only database + road + dual-engine tests. No host ports or cloud resources.
param([switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$fixture = (Resolve-Path (Join-Path $PSScriptRoot 'data/delhi-road.osm.pbf')).Path
$hash = (Get-FileHash -LiteralPath $fixture -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hash -ne '4f36f7e749aef6f21128288ab8ca6447666a506a440d74db778a079974a71b49') { throw 'Unexpected fixture hash' }
$osrmImage = 'ghcr.io/project-osrm/osrm-backend@sha256:729461bcc9ae9e6aafa92c0f93db9b060a32e85d5e72092c01ae4a4a9f1eb564'
$suffix = [Guid]::NewGuid().ToString('N')
$database = "tirodhan-v2-db-$suffix"
$server = "tirodhan-v2-road-$suffix"
$volume = "tirodhan-v2-graph-$suffix"
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker exited with $LASTEXITCODE" }
}
Push-Location $repo
try {
    if (-not $SkipBuild) {
        Invoke-Docker build --target operational-tests -f tests/routing/Dockerfile -t tirodhan-routing-tests:increment-3 .
    }
    Invoke-Docker volume create $volume
    Invoke-Docker run --rm --cpus 2 --memory 2g -v "${volume}:/data" -v "${fixture}:/input/map.osm.pbf:ro" $osrmImage sh -ec 'cp /input/map.osm.pbf /data/map.osm.pbf && osrm-extract -t 2 -p /opt/car.lua /data/map.osm.pbf && osrm-partition -t 2 /data/map.osrm && osrm-customize -t 2 /data/map.osrm'
    Invoke-Docker run -d --name $database --cpus 2 --memory 1g -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=tirodhan_test postgis/postgis:17-3.5
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        & docker exec $database pg_isready -U postgres -d tirodhan_test | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable PostgreSQL did not become ready' }
    Invoke-Docker run -d --name $server --network "container:$database" --cpus 2 --memory 2g -v "${volume}:/data:ro" $osrmImage osrm-routed -t 2 --algorithm mld /data/map.osrm
    Invoke-Docker run --rm --network "container:$database" --entrypoint python tirodhan-routing-tests:increment-3 tests/routing/wait_osrm.py
    # Mount the exact reviewed source read-only; native processes write no repository files.
    Invoke-Docker run --rm --cpus 2 --memory 4g --network "container:$database" -v "${repo}:/repo:ro" -e TIRODHAN_ROUTING_ENGINE_TESTS=1 -e TIRODHAN_OSRM_TEST_URL=http://localhost:5000 -e "TIRODHAN_OSRM_GRAPH_VERSION=fixture-$hash" -e TIRODHAN_DATABASE_URL=postgresql+asyncpg://postgres:local-test-only@127.0.0.1:5432/tirodhan_test -e TIRODHAN_TEST_DATABASE_URL=postgresql+asyncpg://postgres:local-test-only@127.0.0.1:5432/tirodhan_test tirodhan-routing-tests:increment-3
} finally {
    $cleanupPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker rm -f $server $database 2>$null | Out-Null
        & docker volume rm $volume 2>$null | Out-Null
    } finally {
        $ErrorActionPreference = $cleanupPreference
        Pop-Location
    }
}
