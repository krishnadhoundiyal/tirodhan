# Explicit, finite QA qualification. Dependencies are mandatory; no skip fallback.
param([switch]$Setup,[string[]]$Campaigns=@('Control','A','B','C','D','E'),[int]$Budget=600,[int]$MixedBudget=180)
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$database='tirodhan-geographical-qa-db'
$osrm='tirodhan-geographical-qa-osrm'
$volume='tirodhan-geographical-qa-graphs'
$road='ghcr.io/project-osrm/osrm-backend@sha256:729461bcc9ae9e6aafa92c0f93db9b060a32e85d5e72092c01ae4a4a9f1eb564'
$image='tirodhan-routing-tests:increment-8'
function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Mandatory Docker command failed: $($args[0])" }
}
function Road-Usage {
    $value=& docker exec $osrm sh -c 'cat /sys/fs/cgroup/cpuacct/cpuacct.usage /sys/fs/cgroup/memory/memory.max_usage_in_bytes /sys/fs/cgroup/memory/memory.usage_in_bytes'
    if ($LASTEXITCODE -ne 0) { throw 'Mandatory OSRM cgroup v1 counters missing; adapt counters for this host before qualification' }
    return @{cpu_nanoseconds=[long]$value[0];lifetime_peak_memory_bytes=[long]$value[1];current_memory_bytes=[long]$value[2]}
}
Push-Location $repo
try {
    if ($Setup) {
        Invoke-Docker volume create --label tirodhan.task=geographical-qa $volume
        $fixture=Join-Path $repo 'tests/routing/data/delhi-road.osm.pbf'
        if ((Get-FileHash -LiteralPath $fixture).Hash.ToLowerInvariant() -ne '4f36f7e749aef6f21128288ab8ca6447666a506a440d74db778a079974a71b49') { throw 'Wrong dataset' }
        Invoke-Docker run --rm --cpus 2 --memory 2g -v "${volume}:/data" -v "${fixture}:/input/map.osm.pbf:ro" $road sh -ec 'mkdir -p /data/car /data/bicycle; for profile in car bicycle; do cp /input/map.osm.pbf /data/$profile/map.osm.pbf; osrm-extract -t 2 -p /opt/$profile.lua /data/$profile/map.osm.pbf; osrm-partition -t 2 /data/$profile/map.osrm; osrm-customize -t 2 /data/$profile/map.osrm; done'
        $topology=Join-Path $repo 'tests/routing/data/qa-topology.osm'
        Invoke-Docker run --rm --cpus 2 --memory 2g -v "${volume}:/data" -v "${topology}:/input/map.osm:ro" $road sh -ec 'mkdir -p /data/topology-car /data/topology-bicycle; for profile in car bicycle; do cp /input/map.osm /data/topology-$profile/map.osm; osrm-extract -t 2 -p /opt/$profile.lua /data/topology-$profile/map.osm; osrm-partition -t 2 /data/topology-$profile/map.osrm; osrm-customize -t 2 /data/topology-$profile/map.osrm; done'
        Invoke-Docker run -d --name $database --label tirodhan.task=geographical-qa --cpus 2 --memory 1g -e POSTGRES_PASSWORD=local-test-only -e POSTGRES_DB=geographical_qa postgis/postgis@sha256:01a6a70e41e6c4467c8f55f6063555ed72db2d6662cd0d571040d42eadaeb6f6
        $ready=$false
        for ($i=0;$i -lt 30;$i++) {
            & docker exec $database pg_isready -U postgres -d geographical_qa | Out-Null
            if ($LASTEXITCODE -eq 0) { $ready=$true;break }
            Start-Sleep -Seconds 1
        }
        if (-not $ready) { throw 'PostgreSQL unavailable' }
        Invoke-Docker exec $database psql -U postgres -c 'CREATE DATABASE geographical_custody'
        Invoke-Docker exec $database psql -U postgres -d geographical_custody -c 'CREATE EXTENSION postgis'
        Invoke-Docker run -d --name $osrm --label tirodhan.task=geographical-qa --cpus 2 --memory 512m --network "container:$database" -v "${volume}:/graphs:ro" $road osrm-routed --algorithm mld --threads 1 --max-table-size 1024 --verbosity WARNING /graphs/car/map.osrm
        foreach ($spec in @(@('bicycle',5001),@('topology-car',5031),@('topology-bicycle',5032))) {
            $profile=$spec[0]; $port=$spec[1]
            Invoke-Docker run -d --name "tirodhan-geographical-qa-$profile" --label tirodhan.task=geographical-qa --cpus 2 --memory 512m --network "container:$database" -v "${volume}:/graphs:ro" $road osrm-routed --algorithm mld --threads 1 --port $port --max-table-size 1024 --verbosity WARNING "/graphs/$profile/map.osrm"
        }
    }
    foreach ($name in @($database,$osrm)) {
        $labels=(& docker inspect --format '{{json .Config.Labels}}' $name | ConvertFrom-Json)
        if ($LASTEXITCODE -ne 0 -or $labels.'tirodhan.task' -ne 'geographical-qa') { throw "Not a QA-owned resource: $name" }
    }
    New-Item -ItemType Directory -Force .docker-tmp | Out-Null
    Invoke-Docker run --rm --cpus 2 --memory 4g --network "container:$database" -v "${repo}:/repo" $image python tests/routing/geographical_preflight.py --output docs/benchmarks/GEOGRAPHICAL_QA_FIXTURE.json
    $exits=@{}
    foreach ($campaign in $Campaigns) {
        if ($campaign -notin @('Control','A','B','C','D','E')) { throw 'Invalid campaign' }
        $before=Road-Usage
        $effectiveBudget=if ($campaign -eq 'E') {$MixedBudget} else {$Budget}
        $started=[DateTimeOffset]::UtcNow.ToString('o')
        $command=@('run','--rm','--name',"tirodhan-geographical-qa-$campaign",'--label','tirodhan.task=geographical-qa','--cpus','2','--memory','4g','--network',"container:$database",'-v',"${repo}:/repo",'-e','TIRODHAN_HYPOTHESIS_DATABASE=/tmp/hypothesis','-e','TIRODHAN_GEO_QA=1','-e','TIRODHAN_TEST_DATABASE_URL=postgresql+asyncpg://postgres:local-test-only@127.0.0.1:5432/geographical_qa','-e','TIRODHAN_OSRM_TEST_URL=http://127.0.0.1:5000','-e',"TIRODHAN_GEO_CAMPAIGN=$campaign",'-e',"TIRODHAN_ADMISSION_BUDGET=$effectiveBudget",'-e',"TIRODHAN_GEO_OUTPUT=docs/benchmarks/GEOGRAPHICAL_QA_$campaign.json",$image,'python','-m','pytest','-q','-s','tests/integration/test_geographical_qa.py','-p','no:cacheprovider')
        # Windows PowerShell 5 treats native stderr (including Alembic INFO)
        # as NativeCommandError; qualify by the actual process exit status.
        $ErrorActionPreference='Continue'
        & docker @command *> ".docker-tmp/geo-$campaign.log"
        $exits[$campaign]=$LASTEXITCODE
        $ErrorActionPreference='Stop'
        $after=Road-Usage
        @{campaign=$campaign;started_at=$started;docker_arguments=$command;exit_code=$exits[$campaign];osrm_before=$before;osrm_after=$after;osrm_cpu_seconds=($after.cpu_nanoseconds-$before.cpu_nanoseconds)/1e9} | ConvertTo-Json -Depth 6 | Set-Content "docs/benchmarks/GEOGRAPHICAL_QA_$campaign.runtime.json"
        Write-Output "Campaign $campaign exit $($exits[$campaign]); evidence written"
    }
    if (@($exits.Values | Where-Object {$_ -ne 0}).Count -gt 0) { throw 'QA qualification failed; inspect campaign evidence and logs' }
} finally { Pop-Location }
# Resources intentionally remain available for additional QA. Remove only verified task-owned names.
