<#
  Release a new version: tags the current commit (vX.Y.Z, with release notes) and pushes it to GitHub.
  The Pis see it within 6 hours (or at once with "Check now"), and Home Assistant shows it under Settings → Updates.

  From the repo, in PowerShell:
      .\tools\release.ps1                     asks for the version and the notes
      .\tools\release.ps1 1.6.0 "Lyrics in JPlay Records"
  Without notes, the commit messages since the last release are used.
#>
param([string]$Version, [string]$Notes)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

if (git status --porcelain --untracked-files=no) {
    Write-Host "Commit your changes first:" -ForegroundColor Yellow
    git status --short --untracked-files=no
    exit 1
}
$branch = git rev-parse --abbrev-ref HEAD
if ($branch -ne 'main') { Write-Host "Release from the main branch (now on $branch)." -ForegroundColor Yellow; exit 1 }
git fetch --tags --quiet origin

$last = git tag -l 'v*.*.*' --sort=-v:refname | Select-Object -First 1
if ($last) {
    $lv = [version]$last.Substring(1)
    $suggest = "$($lv.Major).$($lv.Minor).$($lv.Build + 1)"
    $changes = @(git log --format='%s' "$last..HEAD")
    if ($changes.Count -eq 0) { Write-Host "Nothing new since $last." -ForegroundColor Yellow; exit 1 }
    Write-Host "Last release: $last. Since then:"
} else {
    $suggest = '1.0.0'
    $changes = @(git log --format='%s' -n 15)
    Write-Host "First release. Recent commits:"
}
$changes | Select-Object -First 15 | ForEach-Object { Write-Host "  - $_" }

if (-not $Version) {
    $Version = Read-Host "Version (Enter for $suggest)"
    if (-not $Version) { $Version = $suggest }
}
$Version = $Version.TrimStart('v')
if ($Version -notmatch '^\d+\.\d+\.\d+$') { Write-Host "Use a version like 1.6.0" -ForegroundColor Yellow; exit 1 }
if ($last -and [version]$Version -le [version]$last.Substring(1)) {
    Write-Host "$Version isn't newer than $last" -ForegroundColor Yellow; exit 1
}
if (-not $Notes) {
    Write-Host "Release notes, one line at a time; an empty line ends (nothing: use the commits above)."
    $lines = @()
    while ($true) { $l = Read-Host ' '; if (-not $l) { break }; $lines += $l }
    $Notes = if ($lines.Count) { $lines -join "`n" } else { ($changes | ForEach-Object { "- $_" }) -join "`n" }
}

$tag = "v$Version"
$file = [IO.Path]::GetTempFileName()
[IO.File]::WriteAllText($file, $Notes + "`n", (New-Object Text.UTF8Encoding $false))
git tag -a $tag -F $file
Remove-Item $file
git push origin main --follow-tags
if ($LASTEXITCODE -ne 0) { Write-Host "Push failed; the tag $tag exists locally (git push --follow-tags to retry)." -ForegroundColor Yellow; exit 1 }
Write-Host "Released $tag." -ForegroundColor Green
Write-Host "The Pis see it within 6 hours, or press Check now in their settings (or Settings -> Updates in Home Assistant)."
