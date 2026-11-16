#!/bin/bash
# polybot server installer: one command on a fresh Ubuntu 24.04 VPS, run as root.
#
#   curl -fsSL https://raw.githubusercontent.com/antoharemiz-design/polymarket-oracle-bot/<BRANCH>/deploy/setup_vps.sh \
#     | POLYBOT_BRANCH=<BRANCH> bash
#
# What it does (safe to run again; keys and data are kept):
#   1. checks the OS, installs python3, git and curl
#   2. asks Polymarket whether this server's IP is blocked (stops if it is) and checks the country
#      against the plan (FI, SE, CH, AT); shows the hosting provider of the IP
#   3. creates the unprivileged user `polybot`, downloads the code, builds it, runs the tests
#   4. writes /etc/polybot/env (paper mode, no keys), creates the key for the private polybot-reports repo
#   5. installs and starts the bot and three timers: health check (5 min), status.md (5 min), auto-update (10 min)
#
# Options (environment variables):
#   POLYBOT_BRANCH              branch to deploy (default main)
#   POLYBOT_REPO                code repository (default https://github.com/antoharemiz-design/polymarket-oracle-bot.git)
#   POLYBOT_REPORTS_REPO        private status repository (default git@github.com:<owner>/polybot-reports.git)
#   POLYBOT_ALLOWED_COUNTRIES   default "FI SE CH AT"
#   POLYBOT_IGNORE_GEOBLOCK=1   TEST ONLY: continue on a blocked IP (paper only; live still refuses such a server)
set -euo pipefail

BRANCH=${POLYBOT_BRANCH:-main}
REPO=${POLYBOT_REPO:-https://github.com/antoharemiz-design/polymarket-oracle-bot.git}
OWNER=$(echo "$REPO" | sed -E 's#^(https://github.com/|git@github.com:)([^/]+)/.*#\2#')
REPORTS=${POLYBOT_REPORTS_REPO:-git@github.com:$OWNER/polybot-reports.git}
ALLOWED=${POLYBOT_ALLOWED_COUNTRIES:-FI SE CH AT}
ROOT=/opt/polybot
DATA=/var/lib/polybot
ETC=/etc/polybot

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31mSTOP: %s\033[0m\n' "$*"; exit 1; }
warn() { printf '\033[1;33mWARNING: %s\033[0m\n' "$*"; }

say "1/6 system"
[ "$(id -u)" = 0 ] || fail "run as root (log in as root, or: curl ... | sudo bash)"
. /etc/os-release
if [ "${ID:-}" != ubuntu ]; then fail "Ubuntu 24.04 is required, this is ${PRETTY_NAME:-unknown}"; fi
case "${VERSION_ID:-}" in
  24.04) echo "Ubuntu $VERSION_ID" ;;
  22.04) warn "Ubuntu 22.04 works but 24.04 is the tested version" ;;
  *) fail "Ubuntu 24.04 is required, this is $PRETTY_NAME" ;;
esac
[ -d /run/systemd/system ] || fail "systemd is not running; use a normal VPS (not a container)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git curl ca-certificates openssh-client util-linux > /dev/null
echo "packages: ok (python $(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'))"

say "2/6 location"
GEO=$(curl -fsS --max-time 20 https://polymarket.com/api/geoblock || true)
[ -n "$GEO" ] || fail "Polymarket geoblock check did not answer (https://polymarket.com/api/geoblock). Check the server's internet access and run the command again."
read -r BLOCKED COUNTRY IP <<<"$(python3 -c 'import json,sys; g=json.loads(sys.argv[1]); print(str(g.get("blocked")).lower(), g.get("country") or "?", g.get("ip") or "?")' "$GEO")"
if [ "$BLOCKED" != false ]; then
  if [ "${POLYBOT_IGNORE_GEOBLOCK:-}" = 1 ]; then
    warn "Polymarket blocks this IP ($IP, $COUNTRY). POLYBOT_IGNORE_GEOBLOCK=1: continuing for a paper TEST only; live will refuse this server."
  else
    fail "Polymarket blocks this server's IP ($IP, country $COUNTRY).
Delete this server and create one in another location: Finland, Sweden, Switzerland or Austria
(README_install.md, step 1). Then run the same command again."
  fi
fi
if ! grep -qw "$COUNTRY" <<<"$ALLOWED"; then
  if [ "${POLYBOT_IGNORE_GEOBLOCK:-}" = 1 ]; then
    warn "country $COUNTRY is not in the plan ($ALLOWED); continuing because POLYBOT_IGNORE_GEOBLOCK=1"
  else
    fail "the server is in $COUNTRY; the plan allows only: $ALLOWED. Create the server in Finland, Sweden, Switzerland or Austria."
  fi
fi
echo "geoblock: $([ "$BLOCKED" = false ] && echo "not blocked" || echo BLOCKED) ($COUNTRY, $IP)"
ORG=$(curl -fsS --max-time 15 https://ipinfo.io/org 2>/dev/null || echo "unknown")
echo "provider: $ORG"
if grep -qiE "hetzner|digitalocean|ovh|amazon|aws|google|microsoft|azure|linode|akamai|vultr|contabo|upcloud|scaleway|hostinger|netcup|exoscale|infomaniak|ionos|leaseweb|m247|datacamp|choopa|oracle|alibaba|tencent|hostkey|aeza|timeweb|selectel|kamatera|ramnode|psychz" <<<"$ORG"; then
  warn "this IP belongs to a hosting provider (datacenter ASN). Polymarket does not block it today (checked above),
         but rules for datacenter IPs can change. The bot re-checks every 10 minutes in live and pulls all quotes if
         the IP becomes blocked. Do NOT route trading through proxies or VPNs to get around a block: Polymarket's
         terms forbid it and the account can be frozen."
fi

say "3/6 user, code, tests"
id polybot > /dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin --user-group polybot
install -d -m 755 "$ROOT" "$ROOT/releases" "$ROOT/bin"
install -d -m 750 -o polybot -g polybot "$DATA" "$DATA/logs" "$DATA/.ssh"
install -d -m 750 -o root -g polybot "$ETC"
if [ -d "$ROOT/src/.git" ]; then
  git -C "$ROOT/src" remote set-url origin "$REPO"
  git -C "$ROOT/src" fetch -q origin "$BRANCH"
else
  git clone -q --branch "$BRANCH" "$REPO" "$ROOT/src"
fi
if [ ! -f "$ETC/env" ]; then
  umask 027
  cat > "$ETC/env" <<EOF
# polybot settings. Keys: sudo polybot keys. Mode: sudo polybot mode paper|live. Details: .env.example in the repo.
POLYBOT_MODE=paper
POLYBOT_ALLOW_LIVE=no
POLYBOT_BRANCH=$BRANCH
POLYBOT_DATA=$DATA
# empty: the markets are selected at the first start (6 book snapshots, ~25 min), as pre-registered
POLYBOT_MARKETS=
POLYBOT_REPORTS_REPO=$REPORTS
POLYBOT_AUTO_UPDATE=yes
LP_TEST_DAYS=2
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
  umask 022
else
  sed -i "s#^POLYBOT_BRANCH=.*#POLYBOT_BRANCH=$BRANCH#" "$ETC/env"
  echo "kept existing $ETC/env (keys and settings unchanged; branch set to $BRANCH)"
fi
chown root:polybot "$ETC/env"; chmod 640 "$ETC/env"
"$ROOT/src/deploy/bin/polybot-update" --install || fail "build or tests failed; the last lines of $DATA/logs/update.log say why"

say "4/6 key for the private status repository"
KEY=$DATA/.ssh/reports_ed25519
if [ ! -f "$KEY" ]; then
  runuser -u polybot -- ssh-keygen -q -t ed25519 -N "" -C "polybot@$(hostname)" -f "$KEY"
fi
# GitHub's SSH host keys from its TLS-authenticated API, so the first connection cannot be intercepted.
# Fallback: GitHub's published Ed25519 key (fingerprint SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU,
# docs.github.com "GitHub's SSH key fingerprints").
if ! curl -fsS --max-time 20 https://api.github.com/meta 2> /dev/null | python3 -c '
import json, sys
for k in json.load(sys.stdin)["ssh_keys"]:
    print("github.com", k)' > "$DATA/.ssh/known_hosts.new" 2> /dev/null || [ ! -s "$DATA/.ssh/known_hosts.new" ]; then
  echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" > "$DATA/.ssh/known_hosts.new"
  echo "GitHub API unavailable; using GitHub's published Ed25519 host key"
fi
mv "$DATA/.ssh/known_hosts.new" "$DATA/.ssh/known_hosts"
chown polybot:polybot "$DATA/.ssh/known_hosts"; chmod 644 "$DATA/.ssh/known_hosts"
echo "reports repository: $REPORTS"

say "5/6 services"
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\n' > /etc/systemd/journald.conf.d/polybot.conf   # cap on system logs
systemctl restart systemd-journald || true
systemctl daemon-reload
rm -f "$DATA/.stopped"
systemctl enable -q polybot.service polybot-health.timer polybot-report.timer polybot-update.timer
systemctl restart polybot.service
systemctl restart polybot-health.timer polybot-report.timer polybot-update.timer

say "6/6 check"
ok=
for _ in $(seq 1 30); do
  sleep 3
  if systemctl is-active --quiet polybot && python3 -c '
import json, sys, time
s = json.load(open(sys.argv[1])); sys.exit(0 if time.time() - s["ts"] < 60 else 1)' "$DATA/service_status.json" 2> /dev/null; then ok=1; break; fi
done
TESTS=$(cat "$(readlink -f "$ROOT/current")/.ready" 2> /dev/null || echo "?")
echo
echo "=================================================================="
echo " geoblock: $([ "$BLOCKED" = false ] && echo "not blocked" || echo "BLOCKED (test install)") ($COUNTRY)"
echo " provider: $ORG"
echo " tests: passed ($TESTS)"
if [ -n "$ok" ]; then
  echo " polybot: active ($(sed -n 's/^POLYBOT_MODE=//p' "$ETC/env" | tail -1))"
else
  echo " polybot: NOT RUNNING - see: journalctl -u polybot -n 50"
fi
echo "=================================================================="
[ -n "$ok" ] || exit 1
cat <<EOF

Next (README_install.md, step 4): give the server write access to your private repository polybot-reports.
  1. https://github.com/new  ->  name: polybot-reports, Private, no README  ->  Create repository
  2. Settings -> Deploy keys -> Add deploy key
       Title: polybot-server
       Key:   $(cat "$KEY.pub")
       [x] Allow write access  ->  Add key
Within 5-15 minutes status.md appears in polybot-reports.

On the server:  polybot status   |   sudo polybot report   (publish now, shows any error)
EOF
