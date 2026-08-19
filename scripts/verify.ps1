$ErrorActionPreference = 'Stop'

docker compose config | Out-Null
docker compose build --no-cache api test
docker compose up -d postgres
docker compose run --rm api alembic upgrade head
docker compose --profile tools run --rm test
docker compose up -d api worker
docker compose exec -T api python scripts/e2e_smoke.py

Write-Host 'Verification completed. Services remain running on http://localhost:8000/'

