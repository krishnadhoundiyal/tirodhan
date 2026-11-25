# Adds the two web-serving APIs to the existing NONPROD service.
# Run without -Apply to check the target, API identities, and prerequisites.
param(
    [switch]$Apply,
    [string]$CustomerImage,
    [string]$ManagerImage,
    [switch]$AddV1Operations
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$subscription = '362d539c-c919-481e-97f5-1a9cda362747'
$tenant = '13e1ca63-9c4d-4e19-addb-49c235109a5e'
$scope = "/subscriptions/$subscription/resourceGroups/tirodhan-np-tdnp01/providers"
$gateway = "$scope/Microsoft.ApiManagement/service/tirodhan-np-tdnp01-apim"
$namedValues = @('tirodhan-web-edge-key', 'tirodhan-web-origin-key')
$vault = 'https://td-np-tdnp01-kv.vault.azure.net'
$policyXml = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'apim-origin-policy.xml')
$armToken = az account get-access-token --subscription $subscription --resource https://management.azure.com/ --query accessToken -o tsv
if ($LASTEXITCODE -ne 0 -or -not $armToken) { throw 'Azure Resource Manager login unavailable' }
$encoded = $armToken.Split('.')[1].Replace('-','+').Replace('_','/')
$claims = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($encoded.PadRight($encoded.Length + ((4 - $encoded.Length % 4) % 4), '='))
) | ConvertFrom-Json
if ($claims.tid -ne $tenant) { throw 'Unexpected Azure login tenant' }

function Invoke-Arm([string]$Method, [string]$Path, [string]$Version, [hashtable]$Body = @{}, [string]$Etag = '') {
    $options = @('-4','-sS','--connect-timeout','7','--max-time','25','--config','-',
        '--request',$Method,'--header','Content-Type: application/json',
        '--header','Accept: application/json','--write-out','\nARM_HTTP|%{http_code}|%header{etag}')
    if ($Method -eq 'PUT') {
        $condition = if ($Etag) { "If-Match: $Etag" } else { 'If-None-Match: *' }
        $options += @('--header',$condition,'--data-binary',($Body | ConvertTo-Json -Depth 8 -Compress))
    }
    $url = "https://management.azure.com${Path}?api-version=$Version"
    if ($Method -eq 'GET' -and $Path.EndsWith('/policies/policy')) { $url += '&format=xml' }
    $lines = @(('header = "Authorization: Bearer '+$armToken+'"') | curl.exe @options $url)
    if ($LASTEXITCODE -ne 0 -or $lines.Count -eq 0 -or $lines[-1] -notmatch '^ARM_HTTP\|(\d{3})\|([^\r\n]*)$') {
        throw "Azure control-plane transport failed ($Method)"
    }
    $status = [int]$Matches[1]
    $observedEtag = $Matches[2]
    $raw = if ($lines.Count -gt 1) { ($lines[0..($lines.Count-2)] -join [Environment]::NewLine).TrimStart([char]0xFEFF) } else { '' }
    $data = if ($Method -eq 'GET' -and $status -eq 200 -and $Path.EndsWith('/policies/policy') -and $raw.StartsWith('<policies>')) {
        @{id=$Path;properties=@{value=$raw}}
    } else {
        $raw | ConvertFrom-Json
    }
    [pscustomobject]@{Status=$status;Data=$data;Etag=$observedEtag}
}

function Confirm-Resource($Response, [string]$Id) {
    $actual = [string]$Response.Data.id -replace '(/apis/tirodhan-(?:(?:customer|manager)-web|backend-v1));rev=1(?=/|$)', '$1'
    if ($Response.Status -ne 200 -or $actual -ne $Id) {
        throw "Resource identity unavailable at $Id (HTTP $($Response.Status))"
    }
}

function Ensure-Resource([string]$Id, [hashtable]$Properties, [scriptblock]$MatchesResource, [scriptblock]$MatchesPrior) {
    $live = Invoke-Arm GET $Id '2024-05-01'
    if ($live.Status -eq 200) {
        Confirm-Resource $live $Id
        if (& $MatchesResource $live.Data.properties) { return }
        if (-not $MatchesPrior -or -not (& $MatchesPrior $live.Data.properties)) {
            throw "Conflicting resource at $Id"
        }
        $verb = 'UPDATE'
        $etag = $live.Etag
        if (-not $etag) { $etag = (Invoke-Arm HEAD $Id '2024-05-01').Etag }
        if (-not $etag) { throw "Missing ETag for $Id" }
    } elseif ($live.Status -eq 404 -and $live.Data.error.code -eq 'ResourceNotFound') {
        $verb = 'CREATE'
        $etag = ''
    } else {
        throw "Azure did not confirm absence at $Id (HTTP $($live.Status))"
    }
    Write-Output "$verb $Id"
    if (-not $Apply) { return }
    $created = Invoke-Arm PUT $Id '2024-05-01' @{properties=$Properties} $etag
    if ($created.Status -notin 200,201,202) {
        $reason = $created.Data.error.code
        if ($reason -notmatch '^[A-Za-z][A-Za-z0-9_.]+$') { $reason = 'unclassified' }
        throw "Azure creation failed at $Id (HTTP $($created.Status), $reason)"
    }
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        $verified = Invoke-Arm GET $Id '2024-05-01'
        if ($verified.Status -eq 200) {
            Confirm-Resource $verified $Id
            if (& $MatchesResource $verified.Data.properties) { return }
        } elseif ($verified.Status -ne 404) {
            Confirm-Resource $verified $Id
        }
        Start-Sleep -Seconds 2
    }
    throw "Creation unverified at $Id"
}

$server = Invoke-Arm GET $gateway '2024-05-01'
Confirm-Resource $server $gateway
if ($server.Data.sku.name -ne 'Consumption' -or $server.Data.identity.type -ne 'SystemAssigned' -or
    $server.Data.properties.provisioningState -ne 'Succeeded') {
    throw 'The existing NONPROD gateway is not ready'
}
foreach ($name in $namedValues) {
    $named = Invoke-Arm GET "$gateway/namedValues/$name" '2024-05-01'
    if ($named.Status -eq 200 -and $named.Data.properties.secret -eq $true -and
        $named.Data.properties.keyVault.secretIdentifier -eq "$vault/secrets/$name") { continue }
    if ($Apply) { throw "APIM requires a verified Key Vault named value for $name" }
    Write-Output "PREREQUISITE $name"
}
foreach ($app in @('customer','manager')) {
    $appName = "tirodhan-np-tdnp01-$app-web"
    $appId = "$scope/Microsoft.App/containerApps/$appName"
    $origin = Invoke-Arm GET $appId '2025-07-01'
    Confirm-Resource $origin $appId
    $hostName = $origin.Data.properties.configuration.ingress.fqdn
    if ($hostName -notmatch ('^'+[regex]::Escape($appName)+'\.[a-z0-9-]+\.centralindia\.azurecontainerapps\.io$')) {
        throw "Unexpected ACA origin hostname for $app"
    }
    if ($Apply) {
        $image = if ($app -eq 'customer') { $CustomerImage } else { $ManagerImage }
        $package = if ($app -eq 'customer') { 'customer' } else { 'operations' }
        $container = $origin.Data.properties.template.containers | Where-Object name -eq 'web'
        if ($image -notmatch "^ghcr\.io/krishnadhoundiyal/tirodhan-$package-web@sha256:[a-f0-9]{64}$" -or
            $container.image -ne $image -or $origin.Data.properties.provisioningState -ne 'Succeeded') {
            throw "The verified candidate digest must be live on the $app origin before adding its APIM route"
        }
    }
    $api = "$gateway/apis/tirodhan-$app-web"
    $path = "web/$app"
    $backend = "https://$hostName"
    Ensure-Resource $api @{
        apiRevision='1'; displayName="Tirodhan $app web"; path=$path; serviceUrl=$backend
        protocols=@('https'); subscriptionRequired=$false
    } {
        param($properties)
        $properties.path -eq $path -and $properties.serviceUrl.TrimEnd('/') -eq $backend -and
            @($properties.protocols).Count -eq 1 -and $properties.protocols[0] -eq 'https' -and
            $properties.subscriptionRequired -eq $false
    }
    # Install the authentication policy first so a partially applied run cannot open web routes.
    $prefixLength = $path.Length + 1
    $originHeader = '    <set-header name="X-Tirodhan-Web-Origin" exists-action="override">'
    $rewrite = '    <rewrite-uri template="@(context.Request.OriginalUrl.Path.Substring(Math.Min(context.Request.OriginalUrl.Path.Length, PREFIX_LENGTH)).PadLeft(1, (char)47))" copy-unmatched-params="true" />'
    $priorRewrite = '    <rewrite-uri template="@(context.Request.OriginalUrl.Path.Length > PREFIX_LENGTH ? context.Request.OriginalUrl.Path.Substring(PREFIX_LENGTH) : &amp;quot;/&amp;quot;)" copy-unmatched-params="true" />'
    $rewrittenPolicy = $policyXml.Replace($originHeader, $rewrite.Replace('PREFIX_LENGTH',[string]$prefixLength) + "`n" + $originHeader)
    $priorPolicy = $policyXml.Replace($originHeader, $priorRewrite.Replace('PREFIX_LENGTH',[string]$prefixLength) + "`n" + $originHeader)
    Ensure-Resource "$api/policies/policy" @{format='rawxml'; value=$rewrittenPolicy} {
        param($properties)
        ([xml]$properties.value).DocumentElement.OuterXml -eq ([xml]$rewrittenPolicy).DocumentElement.OuterXml
    } {
        param($properties)
        $registered = ([xml]$properties.value).DocumentElement.OuterXml
        $registered -in @(
            ([xml]$policyXml).DocumentElement.OuterXml,
            ([xml]$priorPolicy).DocumentElement.OuterXml
        )
    }
    foreach ($method in @('GET','HEAD')) {
        foreach ($route in @{root='/'; path='/*'}.GetEnumerator()) {
            $operation = $method.ToLowerInvariant() + '-' + $route.Key
            Ensure-Resource "$api/operations/$operation" @{
                displayName="$method $($route.Value)"; method=$method; urlTemplate=$route.Value
            } {
                param($properties)
                $properties.method -eq $method -and $properties.urlTemplate -eq $route.Value
            }
        }
    }
}

if ($AddV1Operations) {
    $backendApi = "$gateway/apis/tirodhan-backend-v1"
    $v1 = Invoke-Arm GET $backendApi '2024-05-01'
    if ($v1.Status -ne 200 -or $v1.Data.properties.path -ne 'v1' -or
        $v1.Data.properties.serviceUrl -notmatch '^https://tirodhan-np-tdnp01-api\.[a-z0-9-]+\.centralindia\.azurecontainerapps\.io/v1/?$') {
        throw 'The existing v1 FastAPI target is unexpected'
    }
    $operations = [ordered]@{
        'manager-business-command' = @('POST', '/manager/business/commands')
        'customer-payment-attempt-create' = @('POST', '/payments/collection-requests/{request_id}/attempts')
        'customer-checkout-parameters' = @('GET', '/customer/payment-attempts/{payment_attempt_id}/checkout')
    }
    foreach ($operation in $operations.Keys) {
        $method, $path = $operations[$operation]
        $parameters = @([regex]::Matches($path,'\{([a-z_]+)\}') | ForEach-Object {
            @{name=$_.Groups[1].Value;type='string';required=$true}
        })
        Ensure-Resource "$backendApi/operations/$operation" @{
            displayName=$operation; method=$method; urlTemplate=$path; templateParameters=$parameters
        } {
            param($properties)
            $properties.method -eq $method -and $properties.urlTemplate -eq $path
        }
    }
}
