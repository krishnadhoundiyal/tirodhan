# Disposable local PostgreSQL and car/bicycle graphs; no cloud changes or host ports.
param([switch]$SkipBuild,[switch]$Benchmark,[switch]$FullSuite,[int]$Repeats=1,[int]$Processes=2,[int]$Admitted=160,[ValidateSet("dense","varied")][string]$Coordinates="varied",[ValidateSet("synthetic_fast","regional_minimum")][string]$ServiceMode="regional_minimum",[switch]$CommercialAdmission,[ValidateRange(1,10000)][int]$AdmissionCount=48,[ValidateRange(1,16)][int]$AdmissionConcurrency=1,[ValidateRange(1,3600)][int]$AdmissionBudget=300,[switch]$InterleavedCheckout,[switch]$AdoptedPolicy)
$ErrorActionPreference='Stop'
if ($CommercialAdmission -and ($Benchmark -or $FullSuite)) { throw 'Select one admission, regional benchmark, or full-suite mode' }
$repo=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$fixture=(Resolve-Path (Join-Path $PSScriptRoot 'data/delhi-road.osm.pbf')).Path
if ((Get-FileHash -LiteralPath $fixture -Algorithm SHA256).Hash.ToLowerInvariant() -ne '4f36f7e749aef6f21128288ab8ca6447666a506a440d74db778a079974a71b49') { throw 'Unexpected road fixture' }
$suffix=[Guid]::NewGuid().ToString('N')
$database="tirodhan-inc8-db-$suffix"
$volume="tirodhan-inc8-graphs-$suffix"
$road='ghcr.io/project-osrm/osrm-backend@sha256:729461bcc9ae9e6aafa92c0f93db9b060a32e85d5e72092c01ae4a4a9f1eb564'
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker exited with $LASTEXITCODE" }
}
Push-Location $repo
try {
    if (-not $SkipBuild) {
        Invoke-Docker build --target operational-tests -f tests/routing/Dockerfile -t tirodhan-routing-tests:increment-6 .
        Invoke-Docker build -f tests/routing/Dockerfile.regional -t tirodhan-routing-tests:increment-8 .
    }
    Invoke-Docker volume create $volume
    Invoke-Docker run --rm --cpus 2 --memory 2g -v "${volume}:/data" -v "${fixture}:/input/map.osm.pbf:ro" $road sh -ec 'mkdir -p /data/car /data/bicycle; for profile in car bicycle; do cp /input/map.osm.pbf /data/$profile/map.osm.pbf; osrm-extract -t 2 -p /opt/$profile.lua /data/$profile/map.osm.pbf; osrm-partition -t 2 /data/$profile/map.osrm; osrm-customize -t 2 /data/$profile/map.osrm; done'
    Invoke-Docker run -d --name $database --cpus 2 --memory 1g -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=tirodhan_test postgis/postgis:17-3.5
    $ready=$false
    for ($attempt=0;$attempt -lt 30;$attempt++) {
        & docker exec $database pg_isready -U postgres -d tirodhan_test | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready=$true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable database did not start' }
    $selection=@('tests/integration/test_regional_parallel.py','tests/integration/test_regional_parallel_migration.py','tests/routing/test_regional_selection.py','tests/routing/test_regional_runtime.py','tests/integration/test_admission_reliability.py','tests/routing/test_admission_insertion.py','tests/routing/test_matrix.py','tests/routing/test_osrm_quantization.py')
    $entry=@('python','tests/routing/regional_entry.py')
    if ($FullSuite) { $selection=@('tests') }
    if ($Benchmark) { $selection=@('tests/integration/test_regional_parallel_benchmark.py');$entry=@('python','-m','pytest') }
    if ($CommercialAdmission) { $selection=@('tests/integration/test_commercial_admission_benchmark.py') }
    Invoke-Docker run --rm --cpus 2 --memory 4g --network "container:$database" -v "${repo}:/repo:ro" -v "${volume}:/graphs:ro" -e TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis -e TIRODHAN_ROUTING_ENGINE_TESTS=1 -e TIRODHAN_REGIONAL_GRAPH_DIR=/graphs -e "TIRODHAN_REGIONAL_BENCHMARK=$([int][bool]$Benchmark)" -e "TIRODHAN_BENCH_REPEATS=$Repeats" -e "TIRODHAN_BENCH_PROCESSES=$Processes" -e "TIRODHAN_BENCH_ADMITTED=$Admitted" -e "TIRODHAN_BENCH_COORDINATES=$Coordinates" -e "TIRODHAN_BENCH_SERVICE=$ServiceMode" -e "TIRODHAN_ADMISSION_BENCHMARK=$([int][bool]$CommercialAdmission)" -e "TIRODHAN_ADMISSION_COUNT=$AdmissionCount" -e "TIRODHAN_ADMISSION_CONCURRENCY=$AdmissionConcurrency" -e "TIRODHAN_ADMISSION_BUDGET=$AdmissionBudget" -e "TIRODHAN_ADMISSION_INTERLEAVED=$([int][bool]$InterleavedCheckout)" -e "TIRODHAN_ADMISSION_ADOPTED=$([int][bool]$AdoptedPolicy)" -e TIRODHAN_TEST_DATABASE_URL=postgresql+asyncpg://postgres:local-test-only@127.0.0.1:5432/tirodhan_test tirodhan-routing-tests:increment-8 @entry -q -s @selection -p no:cacheprovider
} finally {
    & docker rm -fv $database 2>$null | Out-Null
    & docker volume rm $volume 2>$null | Out-Null
    Pop-Location
}
