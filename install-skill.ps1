# Install the GenFeed AI skill into your coding assistant(s).
#
# Usage:
#   irm https://raw.githubusercontent.com/AbstrusImad/genfeed/main/install-skill.ps1 | iex
#   .\install-skill.ps1          # from a local clone
#
# Supported tools (auto-detected):
#   Claude Code    ->  .claude\skills\genfeed.md
#   Cursor         ->  .cursor\rules\genfeed.md
#   Codex CLI      ->  AGENTS.md                        (appended with header)
#   Gemini CLI     ->  GEMINI.md                        (appended with header)
#   GitHub Copilot ->  .github\copilot-instructions.md  (appended with header)
#   Windsurf       ->  .windsurfrules                   (appended with header)
#   Cline / Roo    ->  .clinerules                      (appended with header)

$ErrorActionPreference = "Stop"

$SKILL_URL  = "https://raw.githubusercontent.com/AbstrusImad/genfeed/main/genfeed.skill.md"
$SEPARATOR  = "`n---`n<!-- GenFeed skill (https://github.com/AbstrusImad/genfeed) -->`n"
$TMPFILE    = [System.IO.Path]::GetTempFileName() + ".md"
$INSTALLED  = 0

# ---- download skill content -------------------------------------------------

if (Test-Path "genfeed.skill.md") {
    Write-Host "GenFeed skill installer (local clone)"
    Copy-Item "genfeed.skill.md" $TMPFILE
} else {
    Write-Host "GenFeed skill installer"
    Write-Host "  Fetching skill from GitHub..."
    Invoke-WebRequest -Uri $SKILL_URL -OutFile $TMPFILE -UseBasicParsing
}

$SKILL_CONTENT = Get-Content $TMPFILE -Raw -Encoding UTF8

# ---- Claude Code (always installed) ----------------------------------------

$claudeDir = ".claude\skills"
if (-not (Test-Path $claudeDir)) { New-Item -ItemType Directory -Force $claudeDir | Out-Null }
Copy-Item $TMPFILE "$claudeDir\genfeed.md" -Force
Write-Host "  [+] Claude Code    ->  $claudeDir\genfeed.md"
$INSTALLED++

# ---- Cursor -----------------------------------------------------------------

if (Test-Path ".cursor") {
    $cursorDir = ".cursor\rules"
    if (-not (Test-Path $cursorDir)) { New-Item -ItemType Directory -Force $cursorDir | Out-Null }
    Copy-Item $TMPFILE "$cursorDir\genfeed.md" -Force
    Write-Host "  [+] Cursor         ->  $cursorDir\genfeed.md"
    $INSTALLED++
}

# ---- GitHub Copilot ---------------------------------------------------------

if (Test-Path ".github") {
    $copilot = ".github\copilot-instructions.md"
    if (Test-Path $copilot) {
        Add-Content -Path $copilot -Value ($SEPARATOR + $SKILL_CONTENT) -Encoding UTF8
        Write-Host "  [+] Copilot        ->  $copilot  (appended)"
    } else {
        Copy-Item $TMPFILE $copilot -Force
        Write-Host "  [+] Copilot        ->  $copilot  (created)"
    }
    $INSTALLED++
}

# ---- Windsurf ---------------------------------------------------------------

if (Test-Path ".windsurfrules") {
    Add-Content -Path ".windsurfrules" -Value ($SEPARATOR + $SKILL_CONTENT) -Encoding UTF8
    Write-Host "  [+] Windsurf       ->  .windsurfrules  (appended)"
    $INSTALLED++
}

# ---- Codex CLI (OpenAI) -----------------------------------------------------

$codexInstalled = (Test-Path "AGENTS.md") -or ($null -ne (Get-Command codex -ErrorAction SilentlyContinue))
if ($codexInstalled) {
    if (Test-Path "AGENTS.md") {
        Add-Content -Path "AGENTS.md" -Value ($SEPARATOR + $SKILL_CONTENT) -Encoding UTF8
        Write-Host "  [+] Codex CLI      ->  AGENTS.md  (appended)"
    } else {
        Copy-Item $TMPFILE "AGENTS.md" -Force
        Write-Host "  [+] Codex CLI      ->  AGENTS.md  (created)"
    }
    $INSTALLED++
}

# ---- Gemini CLI (Google) ----------------------------------------------------

$geminiInstalled = (Test-Path "GEMINI.md") -or ($null -ne (Get-Command gemini -ErrorAction SilentlyContinue))
if ($geminiInstalled) {
    if (Test-Path "GEMINI.md") {
        Add-Content -Path "GEMINI.md" -Value ($SEPARATOR + $SKILL_CONTENT) -Encoding UTF8
        Write-Host "  [+] Gemini CLI     ->  GEMINI.md  (appended)"
    } else {
        Copy-Item $TMPFILE "GEMINI.md" -Force
        Write-Host "  [+] Gemini CLI     ->  GEMINI.md  (created)"
    }
    $INSTALLED++
}

# ---- Cline / Roo ------------------------------------------------------------

if (Test-Path ".clinerules") {
    Add-Content -Path ".clinerules" -Value ($SEPARATOR + $SKILL_CONTENT) -Encoding UTF8
    Write-Host "  [+] Cline/Roo      ->  .clinerules  (appended)"
    $INSTALLED++
}

# ---- cleanup & done ---------------------------------------------------------

Remove-Item $TMPFILE -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done. GenFeed skill installed to $INSTALLED tool(s)."
Write-Host "Your AI assistant now knows the full GenFeed API without reading the README."
Write-Host ""
Write-Host "Claude Code: type /genfeed (or the skill name) in your next prompt."
Write-Host "Docs: https://github.com/AbstrusImad/genfeed"
