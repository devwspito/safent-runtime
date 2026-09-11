import type { FailureCode } from './lifecycle.js'

/**
 * Content-design map: FailureCode → the owner's language (FR-007, NFR-004).
 * No "podman"/"contenedor"/"VM"/"digest" here — those only ever appear inside
 * the failure screen's expandable "Detalles", which shows the CLI's own
 * `detail` string and the raw `code` for support. `headline` MUST be true
 * without the reader knowing what a container is.
 */
export interface FailureCopy {
  readonly headline: string
  readonly hint: string
}

const COPY: Record<FailureCode, FailureCopy> = {
  unsupported_os: {
    headline: 'Este sistema operativo todavía no está preparado.',
    hint: 'Consulta los requisitos en la página de descarga de Safent.',
  },
  unsupported_arch: {
    headline: 'Este equipo todavía no es compatible con Safent.',
    hint: 'Consulta los requisitos en la página de descarga de Safent.',
  },
  insufficient_disk: {
    headline: 'No hay espacio suficiente en el disco.',
    hint: 'Libera espacio en este equipo y vuelve a intentarlo.',
  },
  insufficient_memory: {
    headline: 'Este equipo no tiene memoria suficiente ahora mismo.',
    hint: 'Cierra otras aplicaciones y vuelve a intentarlo.',
  },
  runtime_hash_mismatch: {
    headline: 'No se pudo comprobar lo que Safent trae dentro.',
    hint: 'Vuelve a intentarlo. Si sigue fallando, descarga Safent de nuevo desde la página oficial.',
  },
  machine_create_failed: {
    headline: 'Safent no pudo prepararse en este equipo.',
    hint: 'Vuelve a intentarlo.',
  },
  machine_start_failed: {
    headline: 'Safent no arrancó en este equipo.',
    hint: 'Vuelve a intentarlo.',
  },
  userns_blocked: {
    headline: 'El sistema no dejó continuar la preparación.',
    hint: 'Vuelve a intentarlo; puede que el sistema pida una autorización adicional.',
  },
  helper_denied: {
    headline: 'No se concedió la autorización que Safent pedía.',
    hint: 'Vuelve a intentarlo y concede el permiso cuando el sistema lo pida.',
  },
  registry_unreachable: {
    headline: 'Safent no pudo conectarse para descargar lo que falta.',
    hint: 'Comprueba tu conexión a internet y vuelve a intentarlo.',
  },
  digest_mismatch: {
    headline: 'Lo descargado no coincide con lo esperado.',
    hint: 'Vuelve a intentarlo.',
  },
  pull_interrupted: {
    headline: 'La descarga se interrumpió.',
    hint: 'Vuelve a intentarlo: Safent continúa desde donde se quedó.',
  },
  port_exhausted: {
    headline: 'Safent no encontró un puerto libre en este equipo.',
    hint: 'Cierra aplicaciones que puedan estar ocupando muchos puertos y vuelve a intentarlo.',
  },
  container_start_failed: {
    headline: 'Safent no arrancó.',
    hint: 'Vuelve a intentarlo.',
  },
  daemon_unhealthy: {
    headline: 'Safent no respondió a tiempo.',
    hint: 'Vuelve a intentarlo.',
  },
  companion_network_conflict: {
    headline: 'Anuncios no pudo prepararse por un conflicto en este equipo.',
    hint: 'Vuelve a intentarlo.',
  },
  companion_migration_failed: {
    headline: 'Anuncios no pudo poner al día sus datos.',
    hint: 'Vuelve a intentarlo. Tus datos de Anuncios siguen intactos.',
  },
  companion_unreachable: {
    headline: 'Anuncios no responde ahora mismo.',
    hint: 'Vuelve a intentarlo.',
  },
  backup_failed: {
    headline: 'Safent no pudo hacer la copia de seguridad previa.',
    hint: 'Vuelve a intentarlo.',
  },
  restore_failed: {
    headline: 'Safent no pudo recuperar la versión anterior por sí solo.',
    hint: 'Exporta el diagnóstico y contacta con soporte.',
  },
  clock_skew: {
    headline: 'La fecha y hora de este equipo no están sincronizadas.',
    hint: 'Ajusta la fecha y hora del sistema y vuelve a intentarlo.',
  },
  cancelled_by_owner: {
    headline: 'Cancelaste la preparación.',
    hint: 'Puedes volver a intentarlo cuando quieras.',
  },
  cli_porcelain_unsupported: {
    headline: 'Safent no pudo entenderse con lo que hay instalado en este equipo.',
    hint: 'Vuelve a intentarlo. Si sigue fallando, descarga Safent de nuevo desde la página oficial.',
  },
  repair_ineffective: {
    headline: 'Safent no consiguió avanzar en este equipo.',
    hint: 'Vuelve a intentarlo. Si sigue fallando, exporta el diagnóstico.',
  },
  local_storage_conflict: {
    headline: 'Algo en este equipo ya está usando el almacén local de Safent.',
    hint: 'Cierra otras copias de Safent que puedan estar abiertas y vuelve a intentarlo.',
  },
  engine_digest_missing: {
    headline: 'Esta copia de Safent está incompleta.',
    hint: 'Descarga una copia completa desde la página oficial. Puedes exportar el diagnóstico de arranque para soporte.',
  },
}

const FALLBACK: FailureCopy = {
  headline: 'Algo detuvo la preparación.',
  hint: 'Vuelve a intentarlo. Si el problema sigue, exporta el diagnóstico.',
}

/**
 * Never throws on an unrecognized code: the wire crosses a process boundary
 * (the embedded CLI), so a future/unknown FailureCode must degrade to an
 * honest generic message rather than crash the one failure screen FR-033
 * promises. See UI-STATES.md for the known gap (no `cancelled` code exists
 * yet for the §6 SIGINT path).
 */
export function copyForFailure(code: FailureCode): FailureCopy {
  return COPY[code] ?? FALLBACK
}
