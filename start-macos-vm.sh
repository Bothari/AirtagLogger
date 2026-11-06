#!/bin/bash
# macOS Monterey VM via Docker-OSX (headless + VNC)
# Connect via VNC viewer at localhost:5999
#
# Uses a fixed-size 64GB virtual disk stored at ./macos-vm/mac_hdd_ng.img
# On first run, the disk image is created automatically.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VM_DIR="$SCRIPT_DIR/macos-vm"
DISK_IMG="$VM_DIR/mac_hdd_ng.img"
DISK_SIZE="64G"

mkdir -p "$VM_DIR"

# Create a fixed-size sparse disk image if it doesn't exist
if [ ! -f "$DISK_IMG" ]; then
  echo "Creating ${DISK_SIZE} virtual disk at $DISK_IMG ..."
  qemu-img create -f qcow2 "$DISK_IMG" "$DISK_SIZE"
fi

CONTAINER_NAME="macos-monterey"

# Fixed Mac identity. GENERATE_UNIQUE=true made a new serial every boot, which
# signed iCloud out each time. This set is the one iCloud was signed in with on
# 2026-11-04 -- changing it will force a fresh iCloud sign-in.
DEVICE_MODEL="iMacPro1,1"
SERIAL="REDACTED_SERIAL"
BOARD_SERIAL="REDACTED_BOARD_SERIAL"
UUID="REDACTED-UUID"
MAC_ADDRESS="00:00:00:00:00:00"

# Screen resolution (baked into the OpenCore plist at container creation)
WIDTH=1280
HEIGHT=800

# If container already exists, just start it
if docker container inspect "$CONTAINER_NAME" &>/dev/null; then
  echo "Starting existing VM container..."
  docker start "$CONTAINER_NAME"
else
  echo "Creating new VM container..."
  docker run -d \
    --name "$CONTAINER_NAME" \
    --device /dev/kvm \
    -p 50922:10022 \
    -p 5999:5999 \
    -v "$DISK_IMG:/home/arch/OSX-KVM/mac_hdd_ng.img" \
    -e GENERATE_SPECIFIC=true \
    -e DEVICE_MODEL="$DEVICE_MODEL" \
    -e SERIAL="$SERIAL" \
    -e BOARD_SERIAL="$BOARD_SERIAL" \
    -e UUID="$UUID" \
    -e MAC_ADDRESS="$MAC_ADDRESS" \
    -e WIDTH="$WIDTH" \
    -e HEIGHT="$HEIGHT" \
    -e MASTER_PLIST_URL='https://raw.githubusercontent.com/sickcodes/osx-serial-generator/master/config-custom.plist' \
    -e SHORTNAME=monterey \
    -e "DISPLAY=${DISPLAY:-:0.0}" \
    -e EXTRA="-display none -vnc 0.0.0.0:99" \
    sickcodes/docker-osx:latest
fi

echo "VM running. Connect via VNC at localhost:5999"
echo "Stop with: docker stop $CONTAINER_NAME"
