#!/bin/bash
# Runs ONLY inside the disposable Ubuntu preparatory VM, never on the host.
set -euo pipefail
exec > >(tee /dev/ttyAMA0) 2>&1
trap 'printf "SAFENT_PREPARE_FAILED line=%s\n" "$LINENO"; systemctl poweroff' ERR
set -x
test "$(systemd-detect-virt)" = kvm
test "$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)" != "DGX Spark"
disk=/dev/disk/by-id/virtio-SAFENT_FIXTURE
test -b "$disk"
test "$(blockdev --getsize64 "$disk")" = 21474836480
test "$(findmnt -n -o SOURCE /)" != "$(readlink -f "$disk")"
test -z "$(lsblk -n -o MOUNTPOINTS "$disk" | tr -d '[:space:]')"
mkdir -p /mnt/safent-data /mnt/safent-runtime
mount -o ro /dev/disk/by-label/SAFENT_DATA /mnt/safent-data
test -f /mnt/safent-data/rc2-rootfs.tar
mkfs.ext4 -F -L SAFENT_RUNTIME "$disk"
mount "$disk" /mnt/safent-runtime
tar --numeric-owner -xpf /mnt/safent-data/rc2-rootfs.tar -C /mnt/safent-runtime
cp -a /boot/vmlinuz-"$(uname -r)" /mnt/safent-runtime/boot/vmlinuz-fixture
if test -f /boot/initrd.img-"$(uname -r)"; then
  cp -a /boot/initrd.img-"$(uname -r)" /mnt/safent-runtime/boot/initrd-fixture
else
  # Minimal cloud images may boot their built-in virtio/ext4 drivers without
  # an initrd. Preserve that official image property rather than fabricating one.
  printf 'SAFENT_KERNEL_WITHOUT_INITRD\n'
fi
mkdir -p /mnt/safent-runtime/lib/modules
cp -a /lib/modules/"$(uname -r)" /mnt/safent-runtime/lib/modules/
# A container uses the host's /sbin/modprobe for kernel autoload. A native
# guest instead needs this executable locally; use the verified Ubuntu guest's
# own kmod binary, not any host binary. Its libkmod dependencies are already in RC2.
install -m0755 /usr/bin/kmod /mnt/safent-runtime/usr/bin/kmod
ln -s ../bin/kmod /mnt/safent-runtime/usr/sbin/modprobe
ln -s ../bin/kmod /mnt/safent-runtime/usr/sbin/depmod
mkdir -p /mnt/safent-runtime/opt/safent-guest-fixture
cp /mnt/safent-data/*.py /mnt/safent-runtime/opt/safent-guest-fixture/
cp /mnt/safent-data/*.whl /mnt/safent-runtime/opt/safent-guest-fixture/
cp /mnt/safent-data/hermes-runtime.service /mnt/safent-runtime/etc/systemd/system/
cp /mnt/safent-data/org.hermes.Runtime1.conf /mnt/safent-runtime/etc/dbus-1/system.d/
cp /mnt/safent-data/safent-guest-proof.service /mnt/safent-runtime/etc/systemd/system/
chroot /mnt/safent-runtime /usr/bin/python3 -m pip install --no-deps --no-index --break-system-packages --force-reinstall /opt/safent-guest-fixture/hermes_runtime-0.9.0-py3-none-any.whl
# These files describe the exported container, not the new guest machine.
# Their removal is limited to this freshly formatted fixture disk.
truncate -s 0 /mnt/safent-runtime/etc/machine-id
if test -f /mnt/safent-runtime/run/systemd/container; then
  unlink /mnt/safent-runtime/run/systemd/container
fi
if test -f /mnt/safent-runtime/.dockerenv; then unlink /mnt/safent-runtime/.dockerenv; fi
if test -f /mnt/safent-runtime/run/.containerenv; then unlink /mnt/safent-runtime/run/.containerenv; fi
ln -s /etc/systemd/system/safent-guest-proof.service /mnt/safent-runtime/etc/systemd/system/multi-user.target.wants/safent-guest-proof.service
sync
umount /mnt/safent-runtime
umount /mnt/safent-data
printf 'SAFENT_PREPARE_PASS kernel=%s\n' "$(uname -r)"
systemctl poweroff
