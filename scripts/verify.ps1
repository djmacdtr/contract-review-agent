$ErrorActionPreference = 'Stop'

docker compose config | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'docker compose config failed' }
docker compose build --no-cache api test
if ($LASTEXITCODE -ne 0) { throw 'docker compose build failed' }
docker compose up -d postgres
if ($LASTEXITCODE -ne 0) { throw 'postgres startup failed' }
docker compose run --rm api alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw 'alembic upgrade failed' }
docker compose --profile tools run --rm test
if ($LASTEXITCODE -ne 0) { throw 'test suite failed' }
$previousAllowHttp = $env:ALLOW_HTTP_DOWNLOADS
$previousAllowlist = $env:DOWNLOAD_HOST_ALLOWLIST
$previousLlmEnabled = $env:LLM_ENABLED
$previousOcrEnabled = $env:OCR_ENABLED
try {
    $env:ALLOW_HTTP_DOWNLOADS = 'true'
    $env:DOWNLOAD_HOST_ALLOWLIST = 'api'
    $env:LLM_ENABLED = 'false'
    $env:OCR_ENABLED = 'false'
    docker compose up -d --force-recreate --wait api worker
    if ($LASTEXITCODE -ne 0) { throw 'API/Worker startup failed' }
    docker compose exec -T api python scripts/e2e_smoke.py
    if ($LASTEXITCODE -ne 0) { throw 'API/Worker smoke test failed' }
}
finally {
    if ($null -eq $previousAllowHttp) {
        Remove-Item Env:ALLOW_HTTP_DOWNLOADS -ErrorAction SilentlyContinue
    }
    else {
        $env:ALLOW_HTTP_DOWNLOADS = $previousAllowHttp
    }
    if ($null -eq $previousAllowlist) {
        Remove-Item Env:DOWNLOAD_HOST_ALLOWLIST -ErrorAction SilentlyContinue
    }
    else {
        $env:DOWNLOAD_HOST_ALLOWLIST = $previousAllowlist
    }
    if ($null -eq $previousLlmEnabled) {
        Remove-Item Env:LLM_ENABLED -ErrorAction SilentlyContinue
    }
    else {
        $env:LLM_ENABLED = $previousLlmEnabled
    }
    if ($null -eq $previousOcrEnabled) {
        Remove-Item Env:OCR_ENABLED -ErrorAction SilentlyContinue
    }
    else {
        $env:OCR_ENABLED = $previousOcrEnabled
    }
    docker compose up -d --force-recreate --wait api worker
    if ($LASTEXITCODE -ne 0) { throw 'API/Worker restore failed' }
}

Write-Host 'Verification completed. Services remain running on http://localhost:8000/'
