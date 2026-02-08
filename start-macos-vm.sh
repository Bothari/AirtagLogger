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
    -e GENERATE_UNIQUE=true \
    -e MASTER_PLIST_URL='https://raw.githubusercontent.com/sickcodes/osx-serial-generator/master/config-custom.plist' \
    -e SHORTNAME=monterey \
    -e "DISPLAY=${DISPLAY:-:0.0}" \
    -e EXTRA="-display none -vnc 0.0.0.0:99" \
    sickcodes/docker-osx:latest
fi

echo "VM running. Connect via VNC at localhost:5999"
echo "Stop with: docker stop $CONTAINER_NAME"
