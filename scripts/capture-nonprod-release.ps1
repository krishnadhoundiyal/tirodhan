# Run from the clean, reviewed main checkout on the operator workstation.
$captureArguments = $args
$releaseProfiles = @('release', 'runtime-release', 'apim-release', 'database', 'apply')
if (
    $captureArguments.Count -gt 0 -and
    $captureArguments[0] -in $releaseProfiles -and
    -not ($captureArguments -match '^--expires-at(=|$)')
) {
    $expiresAt = [DateTimeOffset]::UtcNow.AddHours(8).ToString('yyyy-MM-ddTHH:mm:ssZ')
    $captureArguments += @('--expires-at', $expiresAt)
}

$checkoutPython = Join-Path $PWD.Path '.venv/Scripts/python.exe'
$python = if (Test-Path -LiteralPath $checkoutPython -PathType Leaf) {
    $checkoutPython
} else {
    'python'
}
& $python -m tirodhan.deployment.release_capture @captureArguments
exit $LASTEXITCODE
