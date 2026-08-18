#!/usr/bin/env bash
# Deploy abbie.py and index.html to the Pi, then restart the detector.
#
#   ./pi/deploy.sh              deploy both
#   ./pi/deploy.sh index.html   deploy one file
#
# Never deploys config.json: zones are edited in the browser and written
# straight to the Pi, so pushing a local copy throws those edits away.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

HOSTS=(abbie 100.91.217.86 192.168.86.44)
FILES=("${@:-abbie.py index.html}")

host=""
for h in "${HOSTS[@]}"; do
  if ssh -o BatchMode=yes -o ConnectTimeout=6 "nicolai@$h" true 2>/dev/null; then
    host="$h"; break
  fi
done
if [[ -z "$host" ]]; then
  cat >&2 <<'MSG'
Kan ikke naa Pi'en. Tjek i denne raekkefoelge:

  1. Koerer Tailscale?     /Applications/Tailscale.app/Contents/MacOS/Tailscale status
     Er den stoppet:       /Applications/Tailscale.app/Contents/MacOS/Tailscale up
  2. Ligger et LAN-kabel i? Fortinet-net blokerer Tailscale. Traek kablet ud,
     saa gaar trafikken over WiFi eller hotspot.
  3. Lever Pi'en?          curl -s -o /dev/null -w '%{http_code}' https://abbie.deveo.dk/login
     530 betyder at Pi'en selv er nede. Traek strømmen og saet den i igen.
MSG
  exit 1
fi
echo "==> deployer til $host"

# shellcheck disable=SC2086
scp -o BatchMode=yes -q ${FILES[*]} "nicolai@$host:~/abbie-deploy/"
for f in ${FILES[*]}; do
  mode=644; [[ "$f" == *.py ]] && mode=755
  ssh -o BatchMode=yes "nicolai@$host" \
    "sudo install -o nicolai -g nicolai -m 0$mode ~/abbie-deploy/$f /opt/abbie/$f"
  echo "    $f"
done

ssh -o BatchMode=yes "nicolai@$host" 'sudo systemctl restart abbie'
sleep 6
state=$(ssh -o BatchMode=yes "nicolai@$host" 'systemctl is-active abbie')
echo "==> abbie: $state"
[[ "$state" == active ]] || { ssh -o BatchMode=yes "nicolai@$host" 'journalctl -u abbie -n 15 --no-pager -o cat'; exit 1; }

for f in ${FILES[*]}; do
  l=$(shasum -a256 "$f" | cut -d' ' -f1)
  r=$(ssh -o BatchMode=yes "nicolai@$host" "sha256sum /opt/abbie/$f | cut -d' ' -f1")
  [[ "$l" == "$r" ]] && echo "==> $f verificeret" || { echo "!! $f afviger efter deploy" >&2; exit 1; }
done
