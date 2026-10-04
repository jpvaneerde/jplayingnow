# Define your moOde Pi IP address
$moodeIP = "192.168.1.223"

function Get-MoodeCover {
    # Attempt 1: Standard API Call
    $url1 = "http://$moodeIP/command/?cmd=get_currentsong"
    try {
        $response = Invoke-RestMethod -Uri $url1 -Method Get -UseBasicParsing
        if ($response.coverurl) { return $response }
    } catch {}

    # Attempt 2: Fallback to the live engine payload if coverurl was missing
    $url2 = "http://$moodeIP/engine-mpd.php"
    try {
        # This endpoint outputs continuous events; we take the first line of JSON
        $engineData = Invoke-WebRequest -Uri $url2 -Method Get -TimeoutSec 2 -UseBasicParsing
        $cleanJson = $engineData.Content -split "`n" | Where-Object { $_ -match '{.*}' } | Select-Object -First 1
        if ($cleanJson) {
            return $cleanJson | ConvertFrom-Json
        }
    } catch {}
    
    return $null
}

# Execute the search
$playerState = Get-MoodeCover

if ($playerState) {
    # Compatible property extraction for older PowerShell versions
    $coverUrl = $null
    if ($playerState.coverurl) { $coverUrl = $playerState.coverurl }
    elseif ($playerState.cover) { $coverUrl = $playerState.cover }

    $artist = "Unknown Station"
    if ($playerState.artist) { $artist = $playerState.artist }

    $title = "Live Stream"
    if ($playerState.title) { $title = $playerState.title }

    if ($coverUrl) {
        # Format relative paths vs absolute links
        if ($coverUrl -like "http://*" -or $coverUrl -like "https://*") {
            $finalImage = $coverUrl
        } else {
            $cleanPath = $coverUrl.TrimStart('/')
            $finalImage = "http://$moodeIP/$cleanPath"
        }

        Write-Host "--- moOde Current Status ---" -ForegroundColor Cyan
        Write-Host "Station/Artist: $artist"
        Write-Host "Track/Title:    $title"
        Write-Host "Cover Image:    $finalImage" -ForegroundColor Green
    } else {
        Write-Host "Player data found, but the radio stream did not provide any cover image path." -ForegroundColor Yellow
    }
} else {
    Write-Host "Could not retrieve live metadata from moOde at $moodeIP." -ForegroundColor Red
}
