#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Check for API key ──────────────────────────────────────────────────────
if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo ""
  echo "  ⚠️  ANTHROPIC_API_KEY is not set."
  echo "  Export it before running:"
  echo "    export ANTHROPIC_API_KEY=sk-ant-..."
  echo ""
  exit 1
fi

# ── Create / activate virtual environment ─────────────────────────────────
if [ ! -d "venv" ]; then
  echo "  Creating virtual environment..."
  python3 -m venv venv
fi

source venv/bin/activate

# ── Install / upgrade dependencies ────────────────────────────────────────
echo "  Installing dependencies..."
pip install -q -r requirements.txt

# ── Create uploads directory ──────────────────────────────────────────────
mkdir -p uploads

# ── Start server ──────────────────────────────────────────────────────────
echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   ALTA Survey Analyzer                   ║"
echo "  ║   Open  →  http://localhost:5000         ║"
echo "  ║   Stop  →  Ctrl+C                        ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

python app.py
