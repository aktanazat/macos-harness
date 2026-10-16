#!/bin/sh
# Copy the skill card and its references into an agent skills directory,
# replacing any references a previous layout left there.
set -eu

source_dir="$(cd "$(dirname "$0")/.." && pwd)/skills/macos-harness"
target_dir="${1:-$HOME/.agents/skills/macos-harness}"

mkdir -p "$target_dir/references"
rm -f "$target_dir"/references/*.md
cp "$source_dir/SKILL.md" "$target_dir/SKILL.md"
cp "$source_dir"/references/*.md "$target_dir/references/"
