# QA independiente de topología para la candidata 0.9.13

## Resultado observado

Prueba real en DGX con Podman 4.9.3 / Netavark, sin intervenir en el Mac,
la red `safent-companions` real ni los volúmenes del usuario. La candidata
0.9.12 superó firma/CI, pero su aceptación GUI fue rechazada: un core nuevo
obtuvo dinámicamente `.14` en una red legacy y bloqueó la migración Ads.

Se ejecutaron las funciones productivas `_run`, `_prepare_core_recreation`,
`_core_identity_snapshot`, `_core_address_available`, `_core_network_matches`,
`_container_matches_desired` y `reconcile_reserved_addresses`, extraídas del
CLI en revisión. Snapshot: HEAD `8d99206cb0dac4ceb812cb83ea2b53f1b209993a`
más el cambio CLI pendiente; SHA256 del CLI leído:
`ca45b4a24aa49de1ea3b36210fe939a2fed31a38585c2d2c5abd44f61c0429fb`.

| Caso | Evidencia | Resultado |
| --- | --- | --- |
| Colisión legacy | Red /24 sin rango dinámico reservado, cuatro servicios `.10`–`.13`, core obtiene `.14`; arrancar migración `.14` falla realmente | Reproducido |
| Upgrade | Core con imagen rc2 sustituido por imagen 0.9.1, ambas locales y referenciadas por digest; `_run` productivo asigna `.2` | PASS |
| Conservación | Puerto loopback idéntico, centinelas en volúmenes core/DB intactos, cuatro servicios con mismos IDs, PID, estado, redes y montajes | PASS |
| Migración y reapertura | `.14` queda libre, la migración arranca; dos reaperturas conservan el nuevo core sin recrearlo | PASS |
| Ocupante ajeno `.2` | Preflight devuelve 78 antes de retirar el core; IDs, PID, StartedAt, imagen, red y montajes permanecen iguales | PASS |
| Limpieza | Retirados únicamente 13 contenedores, 2 redes y 6 volúmenes QA identificados por labels/IDs | PASS |

## Reproducción y alcance

Scratch y archivos conservados en DGX:

- `/tmp/safent-core-network-qa.WPGrHY/probe.py`
- `/tmp/safent-core-network-qa.WPGrHY/wrapper.py`
- `/tmp/safent-core-network-qa.WPGrHY/results-final-v2.log`

Comando ejecutado:

```sh
python3 /tmp/safent-core-network-qa.WPGrHY/probe.py /tmp/safent-core-network-qa.WPGrHY
```

El harness usa subredes QA `10.253.231.0/24` y `10.253.232.0/24`, nombres y
volúmenes únicos. El wrapper conserva las operaciones reales de imagen,
inspección, puerto, datos y red, pero sustituye systemd por `/bin/sleep`,
desactiva el healthcheck heredado y omite capacidades y montajes del host.
No ejecuta inferencia, OAuth, migraciones SQL reales ni servicios externos.
Por tanto, prueba topología y recuperación de contenedores, no la jaula
completa ni el arranque funcional de la aplicación en macOS.

Dos intentos de preparación anteriores no se cuentan como PASS: uno chocó
con un nombre duplicado del propio fixture; otro comparó el estado completo
incluyendo un healthcheck periódico heredado. Ambos limpiaron sus recursos.
La pasada final deshabilitó ese healthcheck y comparó explícitamente los
campos de identidad/proceso y todos los montajes y datos de red.

Última comprobación tras limpieza: filtros de contenedores/volúmenes con
label `safent.qa=1896c556c7` vacíos; sólo quedaron las redes anteriores
`podman` y `safent-companions`. Se conservaron logs y scripts, no servicios QA.

El guard adicional de fallo de scaffold (FR3) y los cambios de fases se
verifican en las pruebas focales del agente de implementación; no estaban
en este snapshot. La aceptación GUI de 0.9.13 sigue pendiente y requiere el
artefacto final firmado. Este informe no autoriza promoción a stable/latest.
