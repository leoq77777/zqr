#!/usr/bin/env bash
# `zqr` on a deployment host (WSL or Linux with Docker). `zqr release` installs it from the runtime image as
# ~/.local/bin/zqr. The desk is the container zqr-desk running image zqr:<version>; research data lives in the
# Docker volume zqr-data and model credentials in zqr-secrets. No source code on this host.
#
#   zqr up [VERSION]          start, or switch to, a version (default: the current one)
#   zqr down                  stop gracefully (research first, then the paper fleet); stays stopped across reboots
#   zqr restart
#   zqr status                desk status (same as `zqr desk status`)
#   zqr versions              images on this host; * current, - previous
#   zqr rollback              switch back to the previous version
#   zqr prune                 delete images other than the current and previous versions
#   zqr secrets set|import|status
#                             model credentials: hidden input | KEY=VALUE lines on stdin | variable names only
#   zqr setup [ARGS]          `zqr desk setup ARGS` (data, mandates, authorization; credentials if missing)
#   zqr logs [-f]             container output; components: `zqr desk logs research -f`
#   zqr shell                 bash inside the desk container
#   zqr ARGS...               `zqr ARGS` inside the desk container (a one-off container when the desk is stopped)
set -euo pipefail

NAME=zqr-desk
DATA=zqr-data
SECRETS=zqr-secrets
NET=zqr-control
STATE=${ZQR_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/zqr}

die() { echo "zqr: $*" >&2; exit 1; }
current() { cat "$STATE/current" 2>/dev/null || true; }
previous() { cat "$STATE/previous" 2>/dev/null || true; }
exists() { docker container inspect "$NAME" >/dev/null 2>&1; }
running() { [ "$(docker container inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = true ]; }
desk_version() { docker container inspect -f '{{index .Config.Labels "zqr.version"}}' "$NAME" 2>/dev/null || true; }
have_image() { docker image inspect "$1" >/dev/null 2>&1; }
tty_flags() { if [ -t 0 ] && [ -t 1 ]; then echo -it; else echo -i; fi; }

agent_for() {
  local a
  a=$(docker image inspect -f '{{index .Config.Labels "zqr.agent"}}' "$1" 2>/dev/null || true)
  [ -n "$a" ] && [ "$a" != "<no value>" ] && echo "$a" || echo zqr-agent:dev
}

image_of() {
  local v=${1:-$(current)}
  [ -n "$v" ] || v=$(docker image ls zqr --format '{{.CreatedAt}}|{{.Tag}}' | grep -v '|dev$' | sort -r | head -n 1 |
                     cut -d'|' -f2)
  [ -n "$v" ] || die "no version on this host yet; run \`zqr release\` on the development machine"
  have_image "zqr:$v" || die "image zqr:$v is not on this host (see \`zqr versions\`)"
  echo "zqr:$v"
}

# docker run arguments shared by the desk and one-off containers: the Docker socket (the desk starts agent,
# egress-proxy and sandbox containers as siblings), the data volume, and the credentials volume.
desk_args() {
  local img=$1 host=$2 secrets_mode=$3
  DESK_ARGS=(--network "$NET" --hostname "$host"
    -v /var/run/docker.sock:/var/run/docker.sock
    -v "$DATA:/zqr" -v "$SECRETS:/run/zqr$secrets_mode"
    -e ZQR_DATA_VOLUME="$DATA" -e ZQR_CONTROL_NETWORK="$NET"
    -e ZQR_IMAGE="$img" -e ZQR_AGENT_IMAGE="$(agent_for "$img")"
    -e ZQR_CREDENTIALS=/run/zqr/desk.env)
}

ensure_network() { docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null; }

record() {
  mkdir -p "$STATE"
  local cur; cur=$(current)
  if [ -n "$cur" ] && [ "$cur" != "$1" ]; then echo "$cur" > "$STATE/previous"; fi
  echo "$1" > "$STATE/current"
}

one_off() {  # one_off SECRETS_MODE CMD... : run in a fresh container with the desk's mounts
  local mode=$1; shift
  local img; img=$(image_of)
  ensure_network
  desk_args "$img" "zqr-$$" "$mode"
  docker run --rm $(tty_flags) "${DESK_ARGS[@]}" "$img" "$@"
}

cmd_up() {
  local img; img=$(image_of "${1:-}")
  local v=${img#zqr:} agent; agent=$(agent_for "$img")
  have_image "$agent" || die "agent image $agent is not on this host; run \`zqr release\` again"
  if running && [ "$(desk_version)" = "$v" ]; then
    echo "zqr-desk is already running $v"; record "$v"; return
  fi
  if exists; then
    echo "stopping zqr-desk $(desk_version) ..."
    docker stop -t 300 "$NAME" >/dev/null
    docker rm "$NAME" >/dev/null
  fi
  ensure_network
  desk_args "$img" "$NAME" ":ro"
  docker run -d --name "$NAME" --init --restart unless-stopped --stop-timeout 300 --label zqr.version="$v" \
    --log-opt max-size=20m --log-opt max-file=3 "${DESK_ARGS[@]}" "$img" zqr desk supervise >/dev/null
  record "$v"
  echo "zqr-desk started: $v"
  sleep 5
  docker exec "$NAME" zqr desk status || true
}

cmd_down() {
  if running; then
    echo "stopping zqr-desk (research, then the paper fleet; up to 5 minutes) ..."
    docker stop -t 300 "$NAME" >/dev/null
    echo "stopped"
  else
    echo "zqr-desk is not running"
  fi
}

cmd_versions() {
  local cur prev; cur=$(current); prev=$(previous)
  docker image ls zqr --format '{{.Tag}}\t{{.CreatedSince}}\t{{.Size}}' | while IFS=$'\t' read -r tag created size; do
    local mark=" "
    [ "$tag" = "$prev" ] && mark="-"
    [ "$tag" = "$cur" ] && mark="*"
    printf '%s %-32s %-18s %s\n' "$mark" "$tag" "$created" "$size"
  done
  if running; then echo "running: $(desk_version)"; else echo "running: none"; fi
}

cmd_prune() {
  local cur prev keep_agents=" " tag
  cur=$(current); prev=$(previous)
  for tag in "$cur" "$prev"; do
    if [ -n "$tag" ] && have_image "zqr:$tag"; then keep_agents+="$(agent_for "zqr:$tag") "; fi
  done
  for tag in $(docker image ls zqr --format '{{.Tag}}'); do
    if [ "$tag" != "$cur" ] && [ "$tag" != "$prev" ]; then
      docker image rm "zqr:$tag" >/dev/null && echo "removed zqr:$tag"
    fi
  done
  for tag in $(docker image ls zqr-agent --format '{{.Repository}}:{{.Tag}}'); do
    if [[ "$keep_agents" != *" $tag "* ]]; then
      docker image rm "$tag" >/dev/null && echo "removed $tag"
    fi
  done
}

cmd_secrets() {
  local img; img=$(image_of)
  local base=(-v "$SECRETS:/run/zqr" -e ZQR_CREDENTIALS=/run/zqr/desk.env --network none "$img" zqr desk secrets)
  case "${1:-status}" in
    set) docker run --rm -it "${base[@]}" ;;
    import) docker run --rm -i "${base[@]}" --stdin ;;
    status) docker run --rm "${base[@]}" --status ;;
    *) die "usage: zqr secrets set|import|status" ;;
  esac
  if [ "${1:-status}" != status ] && running; then
    echo "a research loop waiting for credentials picks them up by itself; \`zqr restart\` to replace keys in use"
  fi
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  down) cmd_down ;;
  restart) cmd_down; cmd_up ;;
  status) shift; if running; then docker exec "$NAME" zqr desk status "$@"; else one_off ":ro" zqr desk status "$@"; fi ;;
  versions) cmd_versions ;;
  rollback) p=$(previous); [ -n "$p" ] || die "no previous version recorded"; cmd_up "$p" ;;
  prune) cmd_prune ;;
  secrets) shift; cmd_secrets "$@" ;;
  setup) shift; one_off "" zqr desk setup "$@" ;;
  logs) shift; docker logs "$@" "$NAME" ;;
  shell) if running; then docker exec -it "$NAME" bash; else one_off ":ro" bash; fi ;;
  ""|-h|--help|help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//' ;;
  *) if running; then docker exec $(tty_flags) "$NAME" zqr "$@"; else one_off ":ro" zqr "$@"; fi ;;
esac
