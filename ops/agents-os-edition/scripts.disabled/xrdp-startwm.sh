#!/bin/sh
# Sesión para clientes RDP (xrdp + xorgxrdp sobre Xorg).
#
# El escritorio LOCAL del SO es la Hermes Shell (Wayland/mutter, autologin).
# Para acceso REMOTO sobre el Xorg de xrdp servimos XFCE (ligero y estable);
# el startwm.sh por defecto de Fedora no lanza ningún escritorio, así que la
# sesión arrancaba y moría al instante ("abre 1s y se cierra"). El acceso al
# escritorio Hermes real en remoto será vía el control-remoto WebRTC (FR-053).

if [ -r /etc/profile ]; then
    . /etc/profile
fi

export XDG_SESSION_DESKTOP=xfce
export XDG_CURRENT_DESKTOP=XFCE

if command -v startxfce4 >/dev/null 2>&1; then
    exec startxfce4
fi
exec xfce4-session
