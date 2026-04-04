# start-live.ps1 — Windows operator helper for live trading sessions.
#
# Run from the project root in PowerShell:
#   .\start-live.ps1
#
# WHAT THIS DOES
#   Sets LIVE_TRADING_CONFIRMED=true for THIS session only.
#   After the bot exits the variable is cleared immediately.
#   It is NEVER persisted to User or Machine environment.
#
# ONE-TIME CREDENTIAL SETUP (run manually in PowerShell, once per machine):
#   # 1. Set the API key ID (not sensitive enough to hide, but still treat as secret):
#   [System.Environment]::SetEnvironmentVariable('COINBASE_API_KEY', '<your-key-id>', 'User')
#
#   # 2. Set the PEM secret — preserve real newlines from the file:
#   $pem = Get-Content "C:\path\to\your_coinbase_key.pem" -Raw
#   [System.Environment]::SetEnvironmentVariable('COINBASE_API_SECRET', $pem, 'User')
#
#   # Restart your terminal after setting User env vars so they are inherited.
#
# NEVER put actual key values in this file.
# NEVER commit this file with credentials in it.
# NEVER set LIVE_TRADING_CONFIRMED as a User/Machine environment variable.
#
# RECOMMENDED WORKFLOW
#   1. python verify_coinbase.py          # prove credentials work
#   2. python verify_live_ready.py        # confirm all gates green
#   3. .\start-live.ps1                   # launch (sets LIVE_TRADING_CONFIRMED for session)

# Verify prerequisites before committing to live mode
Write-Host ""
Write-Host "=== Live Session Pre-Launch ===" -ForegroundColor Yellow

if (-not $env:COINBASE_API_KEY) {
    Write-Host "ERROR: COINBASE_API_KEY is not set. See setup instructions above." -ForegroundColor Red
    exit 1
}
if (-not $env:COINBASE_API_SECRET) {
    Write-Host "ERROR: COINBASE_API_SECRET is not set. See setup instructions above." -ForegroundColor Red
    exit 1
}

Write-Host "  COINBASE_API_KEY    : SET" -ForegroundColor Green
Write-Host "  COINBASE_API_SECRET : SET" -ForegroundColor Green
Write-Host ""

# Set LIVE_TRADING_CONFIRMED for this session only
$env:LIVE_TRADING_CONFIRMED = "true"
Write-Host "  LIVE_TRADING_CONFIRMED = true (session only)" -ForegroundColor Yellow
Write-Host ""
Write-Host "Starting bot ..."
Write-Host ""

try {
    python main.py
} finally {
    # Always clear — even if bot crashes or is Ctrl+C'd
    Remove-Item Env:\LIVE_TRADING_CONFIRMED -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "LIVE_TRADING_CONFIRMED cleared." -ForegroundColor Green
}
