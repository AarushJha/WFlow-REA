#!/bin/bash
cd "$(dirname "$0")"

if [ "$1" == "--cli" ]; then
    echo "Starting WFlow CLI prototype..."
    if [ -f ".venv/bin/python" ]; then
        exec .venv/bin/python flow.py
    else
        uv run --with sounddevice --with soundfile --with pynput --with requests --with pyobjc-framework-Quartz python3 flow.py
    fi
else
    echo "Starting WFlow Graphical Interface..."
    if [ -f ".venv/bin/python" ]; then
        exec .venv/bin/python app_gui.py
    else
        uv run --with sounddevice --with soundfile --with numpy --with pynput --with requests --with pyobjc-framework-Quartz --with customtkinter --with pillow python3 app_gui.py
    fi
fi
