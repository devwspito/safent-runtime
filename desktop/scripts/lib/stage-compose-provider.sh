#!/usr/bin/env bash
# Uses the existing verified downloader; no search of the host's Compose tools.
stage_compose_provider() {
  local lock="$1" target="$2" cache="$3" destination="$4"
  local component url digest size relative cached
  for component in download license provenance sbom; do
    url="$(jq -er --arg t "$target" --arg c "$component" '.targets[$t].compose_provider[$c].url' "$lock")" || return 4
    digest="$(jq -er --arg t "$target" --arg c "$component" '.targets[$t].compose_provider[$c].sha256' "$lock")" || return 4
    size="$(jq -er --arg t "$target" --arg c "$component" '.targets[$t].compose_provider[$c].size_bytes' "$lock")" || return 4
    relative="$(jq -er --arg t "$target" --arg c "$component" '.targets[$t].compose_provider[$c].path' "$lock")" || return 4
    case "$component:$relative" in download:bin/docker-compose|license:docker-compose-LICENSE|provenance:docker-compose-provenance.json|sbom:docker-compose-sbom.json) ;; *) return 4 ;; esac
    cached="$cache/docker-compose-$component"
    fetch_verified "$url" "$cached" "$size" "$digest" || return $?
    mkdir -p "$(dirname "$destination/$relative")" || return 4
    cp -p "$cached" "$destination/$relative" || return 4
    if [ "$component" = download ]; then chmod 0755 "$destination/$relative" || return 4; else chmod 0644 "$destination/$relative" || return 4; fi
  done
}
