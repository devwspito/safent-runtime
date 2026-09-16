# Diagnóstico de red — el puerto publicado no responde desde macOS (MAC2-05)

**Qué**: causa raíz de MAC2-05 (`verificacion-mac-2.md`): con la máquina
podman de Safent viva y el contenedor del motor sano por dentro, el puerto
publicado no responde desde el host macOS (`curl` → `000`), sólo desde el
loopback de la VM (`307`). **Dónde**: MacBook Air del dueño
(`macbook-air-2-1`), por SSH, sin sesión gráfica. **Cuándo**: 11-sep-2026.
**Quién**: debug-engineer.

**Alcance y aislamiento**: todo bajo `/tmp/safent-net-diag/`, con
`XDG_CONFIG_HOME`/`XDG_DATA_HOME`/`TMPDIR` apuntando ahí (`TMPDIR` en su forma
canónica `/private/tmp/...`, lección de MAC2-07), máquina propia
`netdiag-engine`, podman **empaquetado** extraído del artefacto
`Safent-macOS-arm64` (run 34549128789, `gh api .../artifacts/10180322412/zip`,
mismo sha256 de DMG que `verificacion-mac-2.md`:
`b6aa36c793179d93eaeb05651dd8861a24b85609ce31b2e2054d8933ac1de45e`; mismo
digest de motor, `sha256:62d459e3…953549`). Nunca se tocó `tailscale`,
`podman-machine-default`, `/opt/podman`, `~/.config/containers`,
`~/.local/share/containers` ni `/Applications`. Verificado ANTES y DESPUÉS:
`podman-machine-default` (libkrun) seguía `Currently running`, y `krunkit`/
`gvproxy` del dueño vivos con los mismos PID. Todo lo creado por esta prueba
— 4 contenedores, 1 volumen, 2 imágenes, 1 máquina, el sandbox entero — quedó
retirado (ver «Desmontaje»).

## Veredicto

**No es un fallo de podman/gvproxy/netavark.** Un contenedor mínimo, publicado
exactamente igual (`-p 127.0.0.1:PUERTO:80`, sin capacidades ni seccomp ni
`--systemd`), responde `200` desde el host en la MISMA máquina, con el MISMO
`gvproxy`. El motor real de Safent, con las banderas **exactas** de
`run-safent.sh` sobre la MISMA máquina, reproduce el `000`/timeout de forma
determinista. La causa vive **dentro** del contenedor: el firewall interno de
hermes-runtime (`ops/agents-os-edition/netns/host-input.nft`, cargado por
`hermes-host-firewall.service`) es un `INPUT` *default-deny* cuya lista de
orígenes permitidos al puerto 7517 cubre el hipervisor de Lumen (QEMU
`10.0.2.0/24`, Apple VZ `192.168.64.0/24`) y el caso «podman nativo en Linux»
(el gateway del puente, derivado en caliente por
`hermes-host-firewall-engine-port`) — pero **no** cubre la subred que usa
`gvproxy` en `podman machine` de macOS (`192.168.127.0/24` por defecto), que
es exactamente la vía por la que entra la conexión reenviada desde el host
cuando el motor corre en el Mac del dueño. `netavark` hace bien su DNAT,
hermes-runtime está sano y escuchando — y el firewall interno del propio
contenedor tira el paquete antes de que hermes-runtime lo vea.

## Hipótesis (antes de medir)

`run-safent.sh` da NET_ADMIN + SYS_ADMIN al contenedor para que hermes-runtime
construya su propia jaula de red (veth + netns + nftables) por dentro. Esa
jaula podría estar interfiriendo con el propio tráfico de publicación del
contenedor. Alternativa a descartar primero: que sea un límite genérico de
`podman machine` rootful + `gvproxy` en macOS (en cuyo caso un contenedor
CUALQUIERA, sin esas banderas, fallaría igual).

## Experimentos

### 0. Máquina de diagnóstico, aislada

```
$ source env.sh   # XDG_CONFIG_HOME/XDG_DATA_HOME/TMPDIR → /private/tmp/safent-net-diag
$ $RT machine list                                    # (vacío antes de crear)
$ $RT machine init netdiag-engine \
    --image .../runtime/podman-machine.aarch64.applehv.raw.zst \
    --provider applehv --rootful --cpus 4 --memory 8192 --disk-size 60
Machine init complete                                 (7.9 s)
$ $RT machine start netdiag-engine
Machine "netdiag-engine" started successfully
$ $RT machine list --format json
  ... "VMType":"applehv","CPUs":4,"Memory":"8589934592", ... "UserModeNetworking":true
$ ps aux | grep gvproxy   # el de NUESTRA máquina, sin -listen tcp:// alguno:
gvproxy -mtu 1500 -ssh-port 64443 \
  -listen-vfkit unixgram:///private/tmp/safent-net-diag/tmp/podman/netdiag-engine-gvproxy.sock \
  -forward-sock .../netdiag-engine-api.sock -forward-dest /run/podman/podman.sock \
  -forward-user root -pid-file .../gvproxy.pid -log-file .../gvproxy.log
```

Máquina propia, `rootful=true`, `applehv`, aislada del dueño (confirmado igual
que en `verificacion-mac-2.md`: `podman-machine-default` del dueño siguió
`Currently running` todo el rato, mismos PID de `krunkit`/`gvproxy`).

### 1. Línea base — contenedor mínimo, misma publicación

```
$ $RT pull docker.io/library/python:3-alpine
$ $RT run -d --name netdiag-probe0 -p 127.0.0.1:18111:80 \
    docker.io/library/python:3-alpine python3 -m http.server 80
$ $RT port netdiag-probe0
80/tcp -> 127.0.0.1:18111
$ curl -sS -o /dev/null -m 5 -w '%{http_code}\n' http://127.0.0.1:18111/   (x3)
200
200
200
$ lsof -nP -iTCP:18111
gvproxy 62248 luiscorrea 31u IPv4 ... TCP 127.0.0.1:18111 (LISTEN)
```

**Publicación de puertos de `podman machine` (rootful, applehv, gvproxy)
funciona bien en general.** Descarta el límite genérico.

### 2. Banderas exactas de `run-safent.sh`, sobre la imagen trivial (sin `--systemd`)

Copiadas literalmente de `run-safent.sh:247-261` (caps, seccomp real de
`ops/container/seccomp/safent.json`, `unmask`, `label=disable`, mount de
`/sys/kernel/security`, `--shm-size=1g`), con el mismo `python:3-alpine`:

```
$ $RT run -d --name netdiag-probe1 -p 127.0.0.1:18113:80 \
    --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \
    --security-opt seccomp=/private/tmp/safent-net-diag/safent-seccomp.json \
    --security-opt unmask=/sys/kernel/security --security-opt label=disable \
    -v /sys/kernel/security:/sys/kernel/security:ro --shm-size=1g \
    docker.io/library/python:3-alpine python3 -m http.server 80
$ curl ... http://127.0.0.1:18113/   (x3)  -> 200 200 200
```

(Primer intento con la ruta `/tmp/...` sin canonizar reprodujo MAC2-07 al
vuelo — `Error: opening seccomp profile failed: ... no such file or
directory` — corregido usando `/private/tmp/...`, igual que hizo la
verificación 2.)

Repetido añadiendo también `--systemd=always` (`netdiag-probe2`, puerto
18114): **sigue en `200`**, y el log de `python -m http.server` deja ver el
origen real que usa `gvproxy` para inyectar una conexión del host dentro de
la VM:

```
192.168.127.1 - - [11/Sep/2026 02:58:53] "GET / HTTP/1.1" 200 -
```

**Ni las capacidades, ni el seccomp, ni `unmask`/`label=disable`, ni
`--systemd=always` por sí solos rompen nada.** El origen real de una conexión
reenviada por `gvproxy` es `192.168.127.1` (la subred de usuario de gvproxy,
`192.168.127.0/24`), no `127.0.0.1` ni la subred del puente de contenedores.

### 3. Imagen real del motor, banderas EXACTAS de `run-safent.sh` (sin compañero)

```
$ $RT pull ghcr.io/devwspito/safent:v0.9.0-rc1     # digest 62d459e3…953549, igual que MAC2
$ $RT run -d --name netdiag-engine-c --systemd=always \
    -p 127.0.0.1:18120:7517 \
    --stop-signal=SIGRTMIN+3 --stop-timeout=30 \
    --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \
    --security-opt seccomp=/private/tmp/safent-net-diag/safent-seccomp.json \
    --security-opt unmask=/sys/kernel/security --security-opt label=disable \
    -v /sys/kernel/security:/sys/kernel/security:ro \
    -v netdiag-engine-data:/var/lib/hermes --shm-size=1g \
    ghcr.io/devwspito/safent:v0.9.0-rc1
$ $RT exec netdiag-engine-c systemctl is-active hermes-runtime
active                                              (inmediato, imagen ya en caché)
$ curl -sS -o /dev/null -m 5 -w '%{http_code}\n' http://127.0.0.1:18120/   (x3)
000  (curl: (28) Operation timed out after 5006 ms)
000
000
$ lsof -nP -iTCP:18120
gvproxy 62248 ... TCP 127.0.0.1:18120 (LISTEN)
gvproxy 62248 ... TCP 127.0.0.1:18120->127.0.0.1:49977 (CLOSE_WAIT)
```

**Reproducido, determinista, con la misma firma exacta que
`verificacion-mac-2.md` (000 desde host, `gvproxy` en `CLOSE_WAIT`)** — con la
imagen real, las banderas reales, en la MISMA máquina donde el paso 1 y el
paso 2 respondieron bien. El único cambio entre «funciona» y «no funciona» es
la imagen `ghcr.io/devwspito/safent` arrancando de verdad (con systemd, con
sus propias unidades).

### 4. Dentro del contenedor: ¿quién escucha y qué firewall hay?

```
$ $RT exec netdiag-engine-c ss -ltnp
LISTEN  10.200.0.1:3128  0.0.0.0:*
LISTEN     0.0.0.0:7517  0.0.0.0:*        <- hermes-runtime, en TODAS las interfaces, no sólo loopback
LISTEN  10.200.1.1:3128  0.0.0.0:*

$ $RT exec netdiag-engine-c nft list ruleset
table inet hermes_host {
    chain input_dynamic {
        ip saddr 10.88.0.1 tcp dport 7517 accept comment "container engine published control plane"
        ip saddr 10.200.1.2 ip daddr 10.200.1.1 tcp dport 3128 accept comment "mcp netns -> egress proxy"
    }
    chain input {
        type filter hook input priority filter - 10; policy drop;
        ct state established,related accept
        ct state invalid drop
        iif "lo" accept
        meta l4proto ipv6-icmp accept
        ip protocol icmp accept
        ip saddr 10.0.2.0/24 tcp dport 7517 accept comment "Lumen UI: host hipervisor vía QEMU user-net"
        ip saddr 192.168.64.0/24 tcp dport 7517 accept comment "Lumen UI: host hipervisor vía Apple VZ NAT"
        ip saddr 10.200.0.2 ip daddr 10.200.0.1 tcp dport 3128 accept
        jump input_dynamic
        log prefix "hermes_host INPUT DROP: " drop
    }
}
```

`hermes-runtime` escucha bien en `0.0.0.0:7517` (no es un bind a loopback).
Pero el propio contenedor trae un `INPUT` **default-deny** (`policy drop` +
`drop` explícito al final) cuya lista de orígenes permitidos para el 7517 es:
`10.0.2.0/24` (QEMU user-net, el otro producto — Lumen appliance), `192.168.64.0/24`
(Apple VZ NAT, ídem), y — vía `hermes-host-firewall-engine-port`, que corre en
el arranque y deriva el gateway por defecto del contenedor
(`ip route show default`) — `10.88.0.1`, el gateway del puente de podman.
**Ninguna entrada cubre `192.168.127.0/24`**, la subred de `gvproxy` vista en
el paso 2 y ya apuntada en `verificacion-mac-2.md` (`curl
http://192.168.127.2:40643/ -> 000`, la propia VM auto-consultándose por esa
misma subred).

Esto también explica por qué el loopback de la VM SÍ funciona (`307` en
`verificacion-mac-2.md`): `netavark` sólo enmascara (SNAT) el origen cuando es
`127.0.0.1` o la subred del puente — en ese caso, dentro del contenedor la
conexión aparece viniendo de `10.88.0.1`, que SÍ está en la lista dinámica. Una
conexión que entra por la NIC de `gvproxy` (host → VM) no matchea ese SNAT
(su origen real, `192.168.127.x`, no es ni loopback ni la subred del puente),
así que llega al contenedor con su dirección real — y esa dirección no está
en ninguna lista.

### 5. Confirmación causal — una regla, ida y vuelta

Regla de prueba, insertada en caliente en el propio `input_dynamic` (chain de
extensión que el propio fichero documenta como el sitio correcto para esto —
nunca en `input`, que termina en `drop` fijo):

```
$ $RT exec netdiag-engine-c nft insert rule inet hermes_host input_dynamic \
    ip saddr 192.168.127.0/24 tcp dport 7517 accept comment "netdiag-temp-test"
$ curl -sS -o /dev/null -m 5 -w '%{http_code}\n' http://127.0.0.1:18120/   (x3)
307
307
307
```

**El mismo código (`307`) que `verificacion-mac-2.md` vio desde el loopback de
la VM.** Con esa única regla, el host alcanza el motor. Confirma la causa sin
ambigüedad.

## Causa identificada

`hermes-runtime`, al arrancar dentro del contenedor con las capacidades que
`run-safent.sh` le da (NET_ADMIN/SYS_ADMIN), carga su propio firewall interno
`INPUT` default-deny —
`ops/agents-os-edition/netns/host-input.nft:32-75`, vía
`hermes-host-firewall.service:26` — con una lista **cerrada** de orígenes que
pueden alcanzar el puerto 7517: dos subredes fijas para el otro producto
(Lumen sobre QEMU/Apple VZ, `host-input.nft:53,57`) y, dinámicamente, el
gateway del propio puente del contenedor
(`ops/agents-os-edition/scripts/hermes-host-firewall-engine-port:32-42`,
`ExecStartPost` en `hermes-host-firewall.service:34`). El comentario de ese
script (líneas 6-9) es explícito sobre su propio modelo: «the engine DNATs on
the HOST side, so inside this netns the packet arrives from the bridge
GATEWAY» — un modelo que asume «podman nativo en Linux», donde `netavark`
enmascara SIEMPRE el origen del host hacia el gateway del puente antes de que
el contenedor lo vea.

Ese modelo es **incompleto para `podman machine` en macOS**: ahí la conexión
publicada no llega vía el puente enmascarada como el gateway del puente, sino
vía la NIC de usuario de `gvproxy` (subred `192.168.127.0/24` por defecto, sin
enmascarar), un tercer camino que ni la lista estática (pensada para el otro
producto) ni la regla dinámica (pensada para el gateway del puente) cubren.
El paquete llega bien DNATeado por `netavark`, `hermes-runtime` está sano y
escuchando en `0.0.0.0:7517` — y el propio firewall del contenedor lo
descarta con su `policy drop` antes de que el proceso lo vea. El comentario
que ya existe en `safent:1991-1999` («verified NOT to be an
`_ensure_machine`/`_run` invocation bug») acertaba: el punto de fallo está
más abajo, dentro de la imagen del motor, no en el envoltorio ni en el CLI.

**Archetype**: default-deny allow-list incompleta para un tercer entorno de
red (el mismo patrón que MAC2-05 ya intuía, una capa más abajo de lo que la
verificación 2 pudo ver sin acceso root dentro del contenedor).

## Arreglo mínimo propuesto (no aplicado — sólo diagnóstico)

Añadir una entrada estática más en `ops/agents-os-edition/netns/host-input.nft`,
junto a las dos ya existentes para QEMU/Apple VZ (mismo formato, mismo
razonamiento de seguridad — «el NIC del guest sólo es alcanzable por su
hipervisor, nunca por la LAN»):

```
ip saddr 192.168.127.0/24 tcp dport 7517 accept comment "podman machine (macOS, applehv/gvproxy) NAT"
```

**Por qué es el arreglo mínimo**: una línea, mismo patrón ya aceptado y
auditado para los otros dos modelos de VM, cero cambios de comportamiento
fuera de abrir exactamente ese origen a ese puerto — el mismo perímetro de
seguridad que ya protege el 7517 hoy (default-deny, sólo el hipervisor local,
nunca la LAN) sigue intacto: `192.168.127.0/24` de `gvproxy` es, igual que las
otras dos, una subred NAT de un proceso de usuario en el HOST local, nunca
alcanzable desde la LAN.

**Contrapartidas**:
- `192.168.127.0/24` es el valor **por defecto** de `gvproxy`
  (`UserModeNetworking`), no un contrato fijado por Safent: `podman machine
  init` no recibe hoy un `--subnet` explícito (ni en `safent:1878-1884` ni en
  `run-safent.sh`), así que una subred distinta en una versión futura de
  podman/gvproxy, o un `podman machine init --subnet` manual del dueño, volvería
  a abrir esta misma brecha en silencio — exactamente el mismo riesgo, ya
  aceptado, que las entradas de QEMU/Apple VZ.
- Alternativa más robusta (no mínima): que `run-safent.sh`/`cmd_ensure_machine`
  fijen `--subnet` explícitamente al crear la máquina, y que
  `hermes-host-firewall-engine-port` reciba esa subred por variable de entorno
  en vez de una constante horneada en `host-input.nft` — cierra el riesgo de
  arriba de raíz, pero toca el flujo de creación de la máquina y el paso de
  variables al contenedor, más superficie que este diagnóstico no considera
  «mínimo». Queda como seguimiento, no como parte de este arreglo.
- No se puede resolver por `ct status dnat` dentro de este netns: el propio
  script ya lo señala (línea 8) — el DNAT ocurre en el netns del HOST/puente,
  no en el del contenedor, así que el contenedor nunca ve ese estado de
  conntrack para decidir con él.

## Desmontaje

```
$RT ps -a                        -> netdiag-probe0/1/2, netdiag-engine-c (nombres verificados antes de borrar)
$RT rm -f netdiag-probe0 netdiag-probe1 netdiag-probe2 netdiag-engine-c
$RT volume rm -f netdiag-engine-data
$RT rmi -f ghcr.io/devwspito/safent:v0.9.0-rc1 docker.io/library/python:3-alpine
$RT machine stop netdiag-engine  -> Machine "netdiag-engine" stopped successfully
$RT machine rm -f netdiag-engine
$RT machine list                 -> (vacío, sandbox)
rm -rf /tmp/safent-net-diag       -> (fichero no existe tras borrar)
podman machine list (dueño)       -> podman-machine-default  libkrun  Currently running
pgrep -fl krunkit/gvproxy         -> los dos procesos del dueño, mismos PID que al empezar
~/.config/containers, ~/.local/share/containers, /opt/podman -> mtimes sin cambio (jul-2026)
```

Todo lo creado por este diagnóstico quedó retirado; la máquina del dueño
siguió arrancada e intacta todo el tiempo. Ningún fichero de producto fue
modificado — este documento es la única escritura.
