#!/bin/bash
# Stop ComfyUI so background removal / TRELLIS get the whole GPU.
# The [m] keeps pkill from matching this script's own command line.
pkill -f "[m]ain.py --listen" ; sleep 3
if pgrep -f "[m]ain.py --listen" > /dev/null; then echo "COMFY_STILL_RUNNING"; exit 1; fi
echo "COMFY_STOPPED"
