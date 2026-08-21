$ErrorActionPreference = 'Stop'

docker compose config | Out-Null
docker compose build --no-cache api test
docker compose up -d postgres
docker compose run --rm api alembic upgrade head
docker compose --profile tools run --rm test
$previousAllowHttp = $env:ALLOW_HTTP_DOWNLOADS
$previousAllowlist = $env:DOWNLOAD_HOST_ALLOWLIST
try {
    $env:ALLOW_HTTP_DOWNLOADS = 'true'
    $env:DOWNLOAD_HOST_ALLOWLIST = 'api'
    docker compose up -d --force-recreate api worker
    docker compose exec -T api python scripts/e2e_smoke.py
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
    docker compose up -d --force-recreate api worker
}

Write-Host 'Verification completed. Services remain running on http://localhost:8000/'
