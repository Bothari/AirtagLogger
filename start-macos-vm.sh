#!/bin/bash
# macOS Monterey VM via Docker-OSX (headless + VNC)
# Connect via VNC viewer at localhost:5999
# Or use a browser-based VNC client

docker run -it \
  --device /dev/kvm \
  -p 50922:10022 \
  -p 5999:5999 \
  -e GENERATE_UNIQUE=true \
  -e MASTER_PLIST_URL='https://raw.githubusercontent.com/sickcodes/osx-serial-generator/master/config-custom.plist' \
  -e SHORTNAME=monterey \
  -e "DISPLAY=${DISPLAY:-:0.0}" \
  -e EXTRA="-display none -vnc 0.0.0.0:99" \
  sickcodes/docker-osx:latest
