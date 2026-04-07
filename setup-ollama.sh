#!/bin/bash
# Setup Ollama with a lightweight model for CATAI
set -e

echo "CATAI - Ollama Setup"
echo "===================="

# Check if ollama is installed
if command -v ollama &>/dev/null; then
    echo "Ollama is already installed."
else
    echo "Installing Ollama..."
    curl -fsSL https://ollama.ai/install.sh | sh
    echo "Ollama installed."
fi

# Start ollama if not running
if ! curl -s http://localhost:11434/api/tags &>/dev/null; then
    echo "Starting Ollama service..."
    ollama serve &>/dev/null &
    sleep 3
fi

# Check if a lightweight model is available
MODELS=$(curl -s http://localhost:11434/api/tags 2>/dev/null | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for m in data.get('models', []):
        print(m['name'])
except: pass
" 2>/dev/null)

if [ -n "$MODELS" ]; then
    echo ""
    echo "Models already installed:"
    echo "$MODELS" | while read m; do echo "  - $m"; done
    echo ""
    read -p "Pull a lightweight model anyway? (gemma3:1b ~815MB) [y/N] " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "All set! Run: python3 catai.py"
        exit 0
    fi
fi

echo ""
echo "Pulling gemma3:1b (lightweight, ~815MB)..."
echo "This is a small, fast model that works well for cat chat."
ollama pull gemma3:1b

echo ""
echo "Done! Ollama is ready."
echo "Run CATAI: python3 catai.py"
echo ""
echo "Other lightweight models you can try:"
echo "  ollama pull gemma3:4b     # Better quality, ~3GB"
echo "  ollama pull phi4-mini     # Microsoft, ~2.5GB"
echo "  ollama pull qwen3:1.7b   # Alibaba, ~1.3GB"
