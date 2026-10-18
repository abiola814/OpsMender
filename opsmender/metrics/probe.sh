#!/bin/sh
# OpsMender probe v1. Read-only.
#
# Invoked by sshd as the forced command for the opsmender user. It accepts exactly two requests:
#   snapshot                  print one JSON snapshot of host + container resource counters
#   logs <container> <lines>  print the most recent log lines of one container
# Anything else is refused. Nothing is written to disk and nothing keeps running afterwards.
set -u
set -f
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
SELF=/usr/local/lib/opsmender/probe

valid_name() {
  case "$1" in ''|-*|*[!A-Za-z0-9_.-]*) return 1 ;; esac
  return 0
}

valid_lines() {
  case "$1" in ''|*[!0-9]*) return 1 ;; esac
  [ "$1" -ge 1 ] && [ "$1" -le 1000 ]
}

if [ "$(id -u)" != "0" ]; then
  # Unprivileged entry point (sshd): validate, then re-run this same root-owned file via sudo.
  # shellcheck disable=SC2086
  set -- ${SSH_ORIGINAL_COMMAND:-snapshot}
  case "${1:-snapshot}" in
    snapshot) exec sudo -n "$SELF" snapshot ;;
    logs)
      valid_name "${2:-}" || { echo "invalid container name" >&2; exit 2; }
      valid_lines "${3:-200}" || { echo "invalid line count" >&2; exit 2; }
      exec sudo -n "$SELF" logs "$2" "${3:-200}" ;;
    *) echo "unsupported request" >&2; exit 2 ;;
  esac
fi

mode="${1:-snapshot}"

if [ "$mode" = "logs" ]; then
  valid_name "${2:-}" || { echo "invalid container name" >&2; exit 2; }
  valid_lines "${3:-200}" || { echo "invalid line count" >&2; exit 2; }
  docker logs --tail "${3:-200}" --timestamps "$2" 2>&1 | head -c 262144
  exit 0
fi

[ "$mode" = "snapshot" ] || { echo "unsupported request" >&2; exit 2; }

json_escape() {
  printf '%s' "$1" | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

now=$(date +%s)
host=$(json_escape "$(cat /proc/sys/kernel/hostname 2>/dev/null || hostname)")
uptime_s=$(awk '{printf "%.0f", $1}' /proc/uptime)
ncpu=$(grep -c '^cpu[0-9]' /proc/stat)
cpu=$(awk '/^cpu /{t=0; for(i=2;i<=9&&i<=NF;i++) t+=$i; printf "{\"total\":%.0f,\"idle\":%.0f}", t, $5+$6; exit}' /proc/stat)
load=$(awk '{printf "%s,%s,%s", $1, $2, $3}' /proc/loadavg)
mem=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} /^MemFree:/{f=$2} /^SwapTotal:/{st=$2} /^SwapFree:/{sf=$2}
  END{if(a=="") a=f; printf "{\"total_kb\":%.0f,\"available_kb\":%.0f,\"swap_total_kb\":%.0f,\"swap_free_kb\":%.0f}", t, a, st, sf}' /proc/meminfo)
disks=$(df -P -k 2>/dev/null | awk 'NR>1 && $1 !~ /^(tmpfs|devtmpfs|overlay|shm|udev|none|efivarfs)$/ \
  && $6 !~ /^\/(proc|sys|dev|run|snap|var\/lib\/docker|boot\/efi)(\/|$)/ && $2 > 0 && !seen[$1]++ {
  gsub(/["\\]/, "", $6); printf "%s{\"mount\":\"%s\",\"total_kb\":%.0f,\"used_kb\":%.0f,\"avail_kb\":%.0f}", sep, $6, $2, $3, $4; sep="," }')

cgroup_stats() {
  # Prints "cpu_usec mem_bytes mem_limit" for container id $1, "null" for anything unavailable.
  id=$1
  for d in "/sys/fs/cgroup/system.slice/docker-$id.scope" "/sys/fs/cgroup/docker/$id"; do
    if [ -f "$d/cpu.stat" ]; then
      usec=$(awk '$1=="usage_usec"{print $2}' "$d/cpu.stat")
      cur=$(cat "$d/memory.current" 2>/dev/null || echo 0)
      inactive=$(awk '$1=="inactive_file"{print $2}' "$d/memory.stat" 2>/dev/null)
      limit=$(cat "$d/memory.max" 2>/dev/null || echo max)
      [ "$limit" = "max" ] && limit=null
      echo "${usec:-null} $((cur - ${inactive:-0})) $limit"
      return
    fi
  done
  for d in "/sys/fs/cgroup/memory/docker/$id" "/sys/fs/cgroup/memory/system.slice/docker-$id.scope"; do
    if [ -f "$d/memory.usage_in_bytes" ]; then
      cur=$(cat "$d/memory.usage_in_bytes")
      inactive=$(awk '$1=="total_inactive_file"{print $2}' "$d/memory.stat" 2>/dev/null)
      limit=$(cat "$d/memory.limit_in_bytes" 2>/dev/null || echo 0)
      [ "${#limit}" -gt 15 ] && limit=null
      usec=null
      for c in "/sys/fs/cgroup/cpuacct/docker/$id" "/sys/fs/cgroup/cpuacct/system.slice/docker-$id.scope"; do
        [ -f "$c/cpuacct.usage" ] && usec=$(( $(cat "$c/cpuacct.usage") / 1000 )) && break
      done
      echo "$usec $((cur - ${inactive:-0})) ${limit:-null}"
      return
    fi
  done
  echo "null null null"
}

docker_ok=false
containers=""
if command -v docker >/dev/null 2>&1; then
  ids=$(docker ps -aq --no-trunc 2>/dev/null) && docker_ok=true
  if [ -n "${ids:-}" ]; then
    # `index` instead of .State.Health: containers without a health check have no Health key, and a
    # direct field access makes docker skip them with a template error.
    # shellcheck disable=SC2086
    containers=$(docker inspect --format \
      '{{.Id}}|{{.Name}}|{{.State.Status}}|{{.RestartCount}}|{{.State.OOMKilled}}|{{.State.ExitCode}}|{{.State.StartedAt}}|{{with index .State "Health"}}{{index . "Status"}}{{end}}' \
      $ids 2>/dev/null | while IFS='|' read -r cid name state restarts oom code started health; do
        set -- $(cgroup_stats "$cid")
        printf '{"id":"%s","name":"%s","state":"%s","health":"%s","restarts":%s,"oom_killed":%s,"exit_code":%s,"started_at":"%s","cpu_usec":%s,"mem_bytes":%s,"mem_limit":%s},' \
          "$(printf '%.12s' "$cid")" "$(json_escape "${name#/}")" "$(json_escape "$state")" "$(json_escape "$health")" \
          "${restarts:-0}" "${oom:-false}" "${code:-0}" "$(json_escape "$started")" "$1" "$2" "$3"
      done)
    containers=${containers%,}
  fi
fi

printf '{"v":1,"ts":%s,"hostname":"%s","uptime_s":%s,"ncpu":%s,"cpu":%s,"load":[%s],"mem":%s,"disks":[%s],"docker":%s,"containers":[%s]}\n' \
  "$now" "$host" "$uptime_s" "$ncpu" "$cpu" "$load" "$mem" "$disks" "$docker_ok" "$containers"
