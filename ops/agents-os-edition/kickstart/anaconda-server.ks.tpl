# anaconda-server.ks.tpl — T310 — FR-051 cifrado OBLIGATORIO en server self-hosted.
#
# TEMPLATE — no usar directamente. build-iso.sh sustituye IMAGE_REF y
# COSIGN_IDENTITY_REGEXP con envsubst y produce el .ks final.

lang es_ES.UTF-8
keyboard --xlayouts='es'
timezone UTC --utc

network --bootproto=static --device=link --activate --hostname=agents-os-server.local

rootpw --lock
user --name=hermes-admin --groups=wheel --plaintext --password=changeme-on-firstboot

clearpart --all --initlabel
# FR-051: cifrado OBLIGATORIO en server — NO opt-out.
autopart --type=lvm --encrypted --passphrase=${ANACONDA_LUKS_PASSPHRASE}

bootloader --location=mbr --timeout=5
# IMAGE_REF es sustituido por build-iso.sh con la ref exacta.
ostreecontainer --url=${IMAGE_REF} --no-signature-verification=false

# Verificación cosign en %post (cosign está en /usr/bin del rootfs instalado).
# Ver nota en anaconda-personal-desktop.ks.tpl para la justificación.
%post --interpreter=/usr/bin/bash --erroronfail
# FR-047 BLOQUEANTE: identidad y OIDC issuer exactos — no wildcards.
/usr/bin/cosign verify \
    --certificate-identity-regexp='${COSIGN_IDENTITY_REGEXP}' \
    --certificate-oidc-issuer='https://token.actions.githubusercontent.com' \
    '${IMAGE_REF}' \
    || { echo "FAIL FR-047: cosign verify falló para ${IMAGE_REF}" >&2; exit 1; }

sed -i 's|passphrase=.*||g' /var/log/anaconda/*.log 2>/dev/null || true
touch /var/lib/agents-os/install/needs-first-boot-wizard
touch /etc/hermes-control-plane/profile-enabled
# Forzar cambio de password en primer login interactivo (finding #17).
chage -d 0 hermes-admin 2>/dev/null || true
%end

reboot
