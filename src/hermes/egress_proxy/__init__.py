"""hermes-egress-proxy — filtrado de egress por dominio (SNI/Host).

Paquete de confinamiento de red del navegador del agente.  El proxy se
interpone entre el netns hermes-browser y la WAN; decide por dominio
(no por IP) en dos modos por sesión:

  - open-logged   : cualquier dominio permitido, cada destino auditado.
  - default-deny  : solo dominios en la whitelist; lo demás → 403 + audit.

NO se intercepta ni descifra el TLS del cliente.

Entry point: ``python3 -m hermes.egress_proxy``
"""
