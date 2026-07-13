#!/bin/bash
# Music Manager launcher -- double-click this in Finder to run the app.
# First run sets up a private Python environment and installs everything;
# after that it just launches. You don't need to touch the terminal.

cd "$(dirname "$0")" || exit 1
VENV="$HOME/.venvs/ipad-recovery"
PY="$VENV/bin/python"

echo "================  Music Manager  ================"

# 1. Make sure the private Python environment exists.
if [ ! -x "$PY" ]; then
    echo "First-time setup: creating a private Python environment..."
    PYSYS="$(command -v python3)"
    if [ -z "$PYSYS" ]; then
        echo ""
        echo "Python 3 isn't installed. Get it (free) from:"
        echo "    https://www.python.org/downloads/"
        echo "then double-click this file again."
        read -n 1 -s -r -p "Press any key to close..."
        exit 1
    fi
    mkdir -p "$HOME/.venvs"
    "$PYSYS" -m venv "$VENV" || { echo "Could not create the environment."; \
        read -n 1 -s -r -p "Press any key to close..."; exit 1; }
fi

# 2. Make sure all components are installed (only runs when something's missing).
if ! "$PY" -c "import numpy, musicbrainzngs, mutagen, yt_dlp, requests, PIL, static_ffmpeg, pymobiledevice3" 2>/dev/null; then
    echo "Installing components (first time or after an update -- a minute or two)..."
    "$PY" -m pip install --quiet --upgrade pip
    if ! "$PY" -m pip install --quiet -r requirements.txt; then
        echo "Something went wrong installing components."
        read -n 1 -s -r -p "Press any key to close..."
        exit 1
    fi
fi

# 3. Launch the app.
echo "Launching... (this terminal window can be minimized; closing it quits the app)"
"$PY" music_gui.py
status=$?

if [ $status -ne 0 ]; then
    echo ""
    echo "The app closed with an error (code $status) -- see the messages above."
    read -n 1 -s -r -p "Press any key to close..."
fi
