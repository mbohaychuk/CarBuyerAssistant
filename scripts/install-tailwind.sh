#!/usr/bin/env bash
# Download the Tailwind v4 standalone CLI binary into bin/tailwindcss.
#
# Standalone binary (single file, no Node.js or npm required) — chosen so the
# Python/uv project keeps a single package manager. Pinned to "latest" rather
# than a specific version; bump intentionally by editing this script when a
# breaking change ships. For reproducibility you can swap in a tagged release
# URL once you're past Tailwind v4.x churn.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "${REPO_DIR}/bin"

case "$(uname -s)-$(uname -m)" in
    Linux-x86_64) ARCH="tailwindcss-linux-x64" ;;
    Linux-aarch64) ARCH="tailwindcss-linux-arm64" ;;
    Darwin-x86_64) ARCH="tailwindcss-macos-x64" ;;
    Darwin-arm64) ARCH="tailwindcss-macos-arm64" ;;
    *) echo "Unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

URL="https://github.com/tailwindlabs/tailwindcss/releases/latest/download/${ARCH}"
echo "Downloading ${ARCH} → bin/tailwindcss"
curl -sL "${URL}" -o "${REPO_DIR}/bin/tailwindcss"
chmod +x "${REPO_DIR}/bin/tailwindcss"
"${REPO_DIR}/bin/tailwindcss" --help | head -1
