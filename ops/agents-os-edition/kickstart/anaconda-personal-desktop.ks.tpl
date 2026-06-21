# anaconda-personal-desktop.ks.tpl — T110 — Fase 1 Anaconda minimal.
# research §4 two-phase: Anaconda recoge invariantes (idioma, disco,
# cifrado); el wizard agéntico Fase 2 recoge lo conversacional (perfil,
# tenant, consentimientos).
#
# TEMPLATE — no usar directamente. build-iso.sh sustituye IMAGE_REF y
# COSIGN_IDENTITY_REGEXP con envsubst y produce el .ks final.

# Locale + keyboard
lang es_ES.UTF-8
keyboard --xlayouts='es'
timezone Europe/Madrid --utc

# Network — DHCP por defecto, configurable en Fase 2.
network --bootproto=dhcp --device=link --activate

# Root account deshabilitado (FR-058 invariante CI: no NOPASSWD).
rootpw --lock

# User humano local — sin wheel (igual que bib-config.toml que usa groups=["hermes","systemd-journal"]).
# La password expira en el primer login (FR-058 / finding #17).
user --name=hermes-user --groups=hermes,systemd-journal --plaintext --password=changeme-on-firstboot

# Disco: autopart con LVM + LUKS.
# FR-021 política mínima: passphrase ≥ 12 chars con 3 categorías.
# FR-051: cifrado SIEMPRE habilitado. El usuario elige la passphrase durante la
# instalación interactiva de Anaconda (fase gráfica). La variable de entorno
# ANACONDA_LUKS_PASSPHRASE se valida y usa aquí para instalaciones automatizadas
# (CI/smoke); en instalación real el usuario la escribe en la UI de Anaconda.
clearpart --all --initlabel
autopart --type=lvm --encrypted --passphrase=${ANACONDA_LUKS_PASSPHRASE}

# Bootloader
bootloader --location=mbr --timeout=5

# Instalar desde la imagen OCI bootc construida/firmada por el pipeline.
# IMAGE_REF es sustituido por build-iso.sh con la ref exacta (registry/repo:tag@digest).
ostreecontainer --url=${IMAGE_REF} --no-signature-verification=false

# Verificación cosign del rootfs ostree (FR-047 BLOCKER).
# Corre en %post (no en %pre) porque:
#   1. cosign vive en /usr/bin dentro de la imagen bootc instalada en /mnt/sysimage.
#   2. El entorno live de Anaconda (initrd) NO incluye cosign.
#   3. Verificar post-install es igualmente bloqueante: si falla, el sistema
#      queda en un estado incompleto y no arranca limpio (sin %end + reboot).
# COSIGN_IDENTITY_REGEXP es sustituido por build-iso.sh.
%post --interpreter=/usr/bin/bash --erroronfail
# FR-047 BLOQUEANTE: identidad y OIDC issuer exactos — no wildcards.
# Valores idénticos a build-iso.sh para garantizar que la imagen instalada
# es la misma que fue verificada durante la composición del ISO.
/usr/bin/cosign verify \
    --certificate-identity-regexp='${COSIGN_IDENTITY_REGEXP}' \
    --certificate-oidc-issuer='https://token.actions.githubusercontent.com' \
    '${IMAGE_REF}' \
    || { echo "FAIL FR-047: cosign verify falló para ${IMAGE_REF}" >&2; exit 1; }

# Limpiar cualquier rastro de passphrase en logs del instalador.
sed -i 's|passphrase=.*||g' /var/log/anaconda/*.log 2>/dev/null || true
# Marcar nodo para Fase 2 wizard agéntico.
touch /var/lib/agents-os/install/needs-first-boot-wizard
# Forzar cambio de password en primer login interactivo (finding #17).
# Sin esto, 'changeme-on-firstboot' persiste indefinidamente en nodos no gestionados.
chage -d 0 hermes-user 2>/dev/null || true
%end

reboot
