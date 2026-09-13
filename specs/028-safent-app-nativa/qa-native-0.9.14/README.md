# Evidencia de aceptación nativa 0.9.14

Estos seis probes conservan la comprobación concreta del Mac del 13/09/2026. No son instaladores ni se deben ejecutar automáticamente en otros equipos: contienen rutas, IDs y versiones esperadas de esa instalación. No borran ni modifican contenedores, volúmenes, cuentas, campañas o credenciales. No se imprime ningún token/cookie de autenticación.

- `verify_bundle.py APP ENGINE_DIGEST ADS_DIGEST`: comprobar firma, recursos y pins del DMG montado de sólo lectura.
- `verify_live.py`: ejecutar con Python 3 del host. Comprueba caché, contenido de las imágenes, IDs Ads preservados, puerto, red, volúmenes y acceso denegado a secretos por UID del agente.
- `check_live_http_logging.py`: ejecutar en host. Envía un GET health con canario sintético y comprueba el journal real sin mostrar su contenido.
- `check_mcp.py`, `check_ads_cookie_attributes.py`, `check_business_readiness.py`: pasar su contenido por stdin a `podman exec -i -u 0 safent python3 -`, con el Podman privado de Safent. La credencial se resuelve dentro del core y nunca se copia al host. Sólo hacen initialize/tools-list o GET.

`check_business_readiness.py` espera el negocio ya creado mediante UI: exactamente uno, Friendog Center, EUR, Europe/Madrid. No lo crea. El callback observado por este probe interno usa 7517, mientras la UI nativa muestra el origen publicado 33015: son requests con orígenes distintos.

El informe principal `../ADS-FACTORY-0.9.14.md` conserva el resultado, límites, digests y los recorridos GUI. Estos probes no prueban OAuth ni una operación publicitaria real, la aparición en Launchpad ni instalar una actualización mediante updater.
