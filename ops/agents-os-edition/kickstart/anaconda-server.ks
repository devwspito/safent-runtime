# anaconda-server.ks — GENERADO por build-iso.sh desde .ks.tpl
#
# NO EDITAR DIRECTAMENTE. Editar anaconda-server.ks.tpl
# y regenerar con build-iso.sh.

lang es_ES.UTF-8
keyboard --xlayouts='es'
timezone UTC --utc

network --bootproto=static --device=link --activate --hostname=agents-os-server.local

rootpw --lock
user --name=hermes-admin --groups=wheel --plaintext --password=changeme-on-firstboot

clearpart --all --initlabel
# FR-051: cifrado OBLIGATORIO en server — NO opt-out.
autopart --type=lvm --encrypted --passphrase=PLACEHOLDER_SET_BY_BUILD_ISO_SH

bootloader --location=mbr --timeout=5
# IMAGE_REF sustituido por build-iso.sh.
ostreecontainer --url=PLACEHOLDER_SET_BY_BUILD_ISO_SH --no-signature-verification=false

# Verificación cosign en %post (cosign en /usr/bin del rootfs instalado).
%post --interpreter=/usr/bin/bash --erroronfail
/usr/bin/cosign verify \
    --certificate-identity-regexp='^https://github\.com/devwspito/' \
    --certificate-oidc-issuer='https://token.actions.githubusercontent.com' \
    'PLACEHOLDER_SET_BY_BUILD_ISO_SH' \
    || { echo "FAIL FR-047: cosign verify falló" >&2; exit 1; }
sed -i 's|passphrase=.*||g' /var/log/anaconda/*.log 2>/dev/null || true
touch /var/lib/agents-os/install/needs-first-boot-wizard
touch /etc/hermes-control-plane/profile-enabled
chage -d 0 hermes-admin 2>/dev/null || true
%end

reboot
