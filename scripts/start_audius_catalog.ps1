$WorkspacePath = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $WorkspacePath

python -m uvicorn track_catalog.api:app `
    --host 127.0.0.1 `
    --port 8011
