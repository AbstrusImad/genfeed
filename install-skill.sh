#!/usr/bin/env bash
# Install the GenFeed AI skill into your coding assistant(s).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/AbstrusImad/genfeed/main/install-skill.sh | bash
#   bash install-skill.sh          # from a local clone
#
# Supported tools (auto-detected):
#   Claude Code  ->  .claude/skills/genfeed.md
#   Cursor       ->  .cursor/rules/genfeed.md
#   GitHub Copilot ->  .github/copilot-instructions.md  (appended with header)
#   Windsurf     ->  .windsurfrules  (appended with header)
#   Cline / Roo  ->  .clinerules    (appended with header)

set -euo pipefail

SKILL_URL="https://raw.githubusercontent.com/AbstrusImad/genfeed/main/genfeed.skill.md"
SEPARATOR=$'\n---\n<!-- GenFeed skill (https://github.com/AbstrusImad/genfeed) -->\n'

# ---- download skill content -------------------------------------------------

TMPFILE="$(mktemp /tmp/genfeed-skill.XXXXXX.md)"
trap 'rm -f "$TMPFILE"' EXIT

if [ -f "genfeed.skill.md" ]; then
    # running from a local clone — no download needed
    cp "genfeed.skill.md" "$TMPFILE"
    echo "GenFeed skill installer (local clone)"
else
    echo "GenFeed skill installer"
    echo "  Fetching skill from GitHub..."
    if command -v curl &>/dev/null; then
        curl -fsSL "$SKILL_URL" -o "$TMPFILE"
    elif command -v wget &>/dev/null; then
        wget -qO "$TMPFILE" "$SKILL_URL"
    else
        echo "Error: curl or wget required." >&2
        exit 1
    fi
fi

INSTALLED=0

# ---- Claude Code (always installed — creates directory if needed) -----------

mkdir -p ".claude/skills"
cp "$TMPFILE" ".claude/skills/genfeed.md"
echo "  [+] Claude Code    ->  .claude/skills/genfeed.md"
INSTALLED=$((INSTALLED + 1))

# ---- Cursor -----------------------------------------------------------------

if [ -d ".cursor" ]; then
    mkdir -p ".cursor/rules"
    cp "$TMPFILE" ".cursor/rules/genfeed.md"
    echo "  [+] Cursor         ->  .cursor/rules/genfeed.md"
    INSTALLED=$((INSTALLED + 1))
fi

# ---- GitHub Copilot ---------------------------------------------------------

if [ -d ".github" ]; then
    COPILOT=".github/copilot-instructions.md"
    if [ -f "$COPILOT" ]; then
        printf '%s' "$SEPARATOR" >> "$COPILOT"
        cat "$TMPFILE" >> "$COPILOT"
        echo "  [+] Copilot        ->  $COPILOT  (appended)"
    else
        cp "$TMPFILE" "$COPILOT"
        echo "  [+] Copilot        ->  $COPILOT  (created)"
    fi
    INSTALLED=$((INSTALLED + 1))
fi

# ---- Windsurf ---------------------------------------------------------------

if [ -f ".windsurfrules" ]; then
    printf '%s' "$SEPARATOR" >> ".windsurfrules"
    cat "$TMPFILE" >> ".windsurfrules"
    echo "  [+] Windsurf       ->  .windsurfrules  (appended)"
    INSTALLED=$((INSTALLED + 1))
fi

# ---- Cline / Roo ------------------------------------------------------------

if [ -f ".clinerules" ]; then
    printf '%s' "$SEPARATOR" >> ".clinerules"
    cat "$TMPFILE" >> ".clinerules"
    echo "  [+] Cline/Roo      ->  .clinerules  (appended)"
    INSTALLED=$((INSTALLED + 1))
fi

# ---- done -------------------------------------------------------------------

echo ""
echo "Done. GenFeed skill installed to $INSTALLED tool(s)."
echo "Your AI assistant now knows the full GenFeed API without reading the README."
echo ""
echo "Claude Code: type /genfeed (or the skill name) in your next prompt."
echo "Docs: https://github.com/AbstrusImad/genfeed"
