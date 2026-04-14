#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERMES_HOME="${HOME}/.hermes"

echo "=== Installing Hermes Finance Agent config ==="

mkdir -p "$HERMES_HOME"

if [ -f "$HERMES_HOME/config.yaml" ]; then
    echo "Backing up existing config.yaml → config.yaml.bak"
    cp "$HERMES_HOME/config.yaml" "$HERMES_HOME/config.yaml.bak"
fi
cp "$SCRIPT_DIR/cli-config.yaml" "$HERMES_HOME/config.yaml"
echo "Copied cli-config.yaml → ~/.hermes/config.yaml"

if [ -f "$HERMES_HOME/.env" ]; then
    echo "~/.hermes/.env already exists — merging missing keys only"
    while IFS= read -r line; do
        key=$(echo "$line" | grep -oP '^[A-Z_]+(?==)' || true)
        if [ -n "$key" ] && ! grep -q "^${key}=" "$HERMES_HOME/.env" 2>/dev/null; then
            echo "$line" >> "$HERMES_HOME/.env"
            echo "  Added missing key: $key"
        fi
    done < "$SCRIPT_DIR/.env"
else
    cp "$SCRIPT_DIR/.env" "$HERMES_HOME/.env"
    echo "Copied .env → ~/.hermes/.env"
fi

mkdir -p "$HERMES_HOME/skills"
for skill_dir in "$SCRIPT_DIR/../skills"/*/; do
    skill_name=$(basename "$skill_dir")
    target="$HERMES_HOME/skills/$skill_name"
    if [ -d "$target" ]; then
        echo "Skill '$skill_name' already exists — skipping"
    else
        cp -r "$skill_dir" "$target"
        echo "Installed skill: $skill_name"
    fi
done

echo ""
echo "=== Done ==="
echo "Next steps:"
echo "  1. Edit ~/.hermes/.env with your actual API keys"
echo "  2. Run: bash scripts/link-tools.sh"
echo "  3. Run: hermes gateway setup"
