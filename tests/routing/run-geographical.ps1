# Explicit opt-in acceptance. Own disposable resources only; no production flags or paid services.
param([switch]$Extended, [switch]$SkipBuild, [string]$Selection='tests/integration/test_geographical_rolling.py', [string]$ArtifactsDirectory='')
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$road='ghcr.io/project-osrm/osrm-backend@sha256:729461bcc9ae9e6aafa92c0f93db9b060a32e85d5e72092c01ae4a4a9f1eb564'
$suffix=[Guid]::NewGuid().ToString('N')
$database="tirodhan-geo-qa-$suffix"
$runtime="tirodhan-geo-run-$suffix"
$relativeOutput=if ($ArtifactsDirectory) {$ArtifactsDirectory} else {".tools/geographical-qa/runs/$suffix"}
$outputPath=[IO.Path]::GetFullPath((Join-Path $repo $relativeOutput))
if (-not $outputPath.StartsWith($repo+[IO.Path]::DirectorySeparatorChar)) { throw 'ArtifactsDirectory must be inside the mounted repository' }
$containerOutput='/repo/'+$outputPath.Substring($repo.Length+1).Replace('\','/')
if ((Test-Path -LiteralPath $outputPath) -and (Get-ChildItem -LiteralPath $outputPath -Force | Select-Object -First 1)) {
    throw 'ArtifactsDirectory is not empty; choose a fresh directory to avoid mixing runtime traces'
}
Write-Host "QA evidence: $outputPath"
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker exited with $LASTEXITCODE" }
}
Push-Location $repo
try {
    $data=Join-Path $repo '.tools/geographical-qa'
    New-Item -ItemType Directory -Force $data | Out-Null
    $fixture=Get-Content tests/routing/data/geographical-delhi-nodes.json -Raw | ConvertFrom-Json
    $source=Join-Path $data 'NewDelhi.osm.pbf'
    if (-not (Test-Path -LiteralPath $source)) {
        Copy-Item -LiteralPath 'tests/routing/data/geographical-delhi.osm.pbf' -Destination $source
    }
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $fixture.source_sha256) {
        throw 'OSM snapshot changed: obtain the exact SHA256 in geographical-delhi-nodes.json. Never relabel a newer graph.'
    }
    if (-not $SkipBuild) {
        Invoke-Docker build --target operational-tests -f tests/routing/Dockerfile -t tirodhan-routing-tests:increment-6 .
        Invoke-Docker build -f tests/routing/Dockerfile.regional -t tirodhan-routing-tests:increment-8 .
    }
    if (-not (Test-Path (Join-Path $data 'NewDelhi.osrm.cells'))) {
        Invoke-Docker run --rm --cpus 2 --memory 2g -v "${data}:/data" $road sh -ec 'osrm-extract -t 2 -p /opt/car.lua /data/NewDelhi.osm.pbf; osrm-partition -t 2 /data/NewDelhi.osrm; osrm-customize -t 2 /data/NewDelhi.osrm'
    }
    Invoke-Docker run -d --name $database --cpus 2 --memory 1g -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=tirodhan_test postgis/postgis:17-3.5
    $ready=$false
    for ($attempt=0;$attempt -lt 30;$attempt++) {
        & docker exec $database pg_isready -U postgres -d tirodhan_test | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready=$true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw 'Disposable PostgreSQL did not start' }
    $extendedValue=if ($Extended) {'1'} else {'0'}
    Invoke-Docker run --rm --name $runtime --network "container:$database" --cpus 4 --memory 5g -v "${repo}:/repo" -e TIRODHAN_TEST_DATABASE_URL=postgresql+asyncpg://postgres:local-test-only@127.0.0.1:5432/tirodhan_test -e TIRODHAN_GEOGRAPHICAL_QA=1 -e "TIRODHAN_GEOGRAPHICAL_EXTENDED=$extendedValue" -e "TIRODHAN_GEO_ARTIFACTS=$containerOutput" -e "TIRODHAN_HYPOTHESIS_DATABASE=$containerOutput/hypothesis" -e PYTHONPATH=/repo/src:/repo/tests/integration --entrypoint python tirodhan-routing-tests:increment-8 tests/routing/geographical_entry.py -q $Selection -p no:cacheprovider --tb=short --hypothesis-seed=1729
} finally {
    & docker rm -f $runtime 2>$null | Out-Null
    & docker rm -f $database 2>$null | Out-Null
    Pop-Location
}
