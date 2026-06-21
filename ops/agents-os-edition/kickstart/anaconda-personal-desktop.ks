# anaconda-personal-desktop.ks — GENERADO por build-iso.sh desde .ks.tpl
#
# NO EDITAR DIRECTAMENTE. Editar anaconda-personal-desktop.ks.tpl
# y regenerar con build-iso.sh.
#
# Este archivo es el kickstart de referencia con valores de ejemplo.
# En producción, build-iso.sh genera el kickstart real con IMAGE_REF
# sustituido por la imagen firmada del pipeline CI.

lang es_ES.UTF-8
keyboard --xlayouts='es'
timezone Europe/Madrid --utc

network --bootproto=dhcp --device=link --activate

rootpw --lock

# Sin wheel: consistente con bib-config.toml (groups=["hermes","systemd-journal"]).
# El usuario gráfico de autologin no necesita sudo.
user --name=hermes-user --groups=hermes,systemd-journal --plaintext --password=changeme-on-firstboot

clearpart --all --initlabel
autopart --type=lvm --encrypted --passphrase=PLACEHOLDER_SET_BY_BUILD_ISO_SH

bootloader --location=mbr --timeout=5

# IMAGE_REF sustituido por build-iso.sh (ghcr.io/devwspito/agents-os-personal-desktop:<version>).
ostreecontainer --url=PLACEHOLDER_SET_BY_BUILD_ISO_SH --no-signature-verification=false

# Verificación cosign en %post (cosign en /usr/bin del rootfs instalado).
# NO en %pre: el entorno live de Anaconda no incluye cosign.
%post --interpreter=/usr/bin/bash --erroronfail
/usr/bin/cosign verify \
    --certificate-identity-regexp='^https://github\.com/devwspito/' \
    --certificate-oidc-issuer='https://token.actions.githubusercontent.com' \
    'PLACEHOLDER_SET_BY_BUILD_ISO_SH' \
    || { echo "FAIL FR-047: cosign verify falló" >&2; exit 1; }
sed -i 's|passphrase=.*||g' /var/log/anaconda/*.log 2>/dev/null || true
touch /var/lib/agents-os/install/needs-first-boot-wizard
chage -d 0 hermes-user 2>/dev/null || true
%end

reboot
