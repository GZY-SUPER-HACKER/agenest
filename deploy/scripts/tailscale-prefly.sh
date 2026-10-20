#!/usr/bin/env bash
#
# Pre-flight for installing Tailscale on the Aliyun ECS.
#
# This exists because Tailscale on this box has broken Aliyun's own data plane twice, in two
# different ways, and the second time this script did not catch it.
#
#  2026-10-02  Tailscale's `ts-input` chain is rule #1 in INPUT and drops
#              `-s 100.64.0.0/10 !tailscale0` *before* conntrack, which swallowed return
#              packets for Aliyun's internal addresses (metadata 100.100.100.200, internal
#              MQTT 100.100.45.182, internal OSS 100.118.28.43). The symptom was a
#              crash-looping hbrclient (5964 restarts) and a backup frozen 13 days back, and
#              nothing in Tailscale's own output said so.
#
#  2026-10-18  The same rule, but the collateral was wider than "metadata and MQTT": the
#              box's *DHCP-supplied DNS servers* (100.100.2.136 / 100.100.2.138) are also in
#              100.64.0.0/10, so DNS resolution failed too. That took out ACME, so
#              `tailscale serve` could not get a certificate, and the failure surfaced as a
#              TLS error on a phone rather than as anything resembling a firewall problem.
#
#              This script said `[ok] no INPUT rule references 100.64.0.0/10` the whole time.
#              It was checking `iptables -L INPUT`, and the rule lives in the nftables table
#              `ip filter`, chain `ts-input` — a separate table that `iptables -L` does not
#              print. A check that reports health by looking somewhere the rule cannot be is
#              worse than no check, because it is believed.
#
# So the checks below are ordered by how much they can be trusted: *reachability* is the
# verdict, and rule inspection only explains it. Reachability caught the 2026-10-18 incident
# while rule inspection missed it, which settles which one should be authoritative.
#
# Run this BEFORE installing, and again after, and compare. It is read-only.
#
# Usage: ./deploy/scripts/tailscale-prefly.sh

set -uo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

fail=0

hr() { printf '%s\n' "------------------------------------------------------------"; }
bad() { echo -e "${RED}[FAIL]${NC} $*"; fail=1; }

hr
echo "Tailscale pre-flight (read-only)"
echo "host: $(hostname)  date: $(date -Is)"
hr

# --- 1. Is Tailscale already present? -------------------------------------- #
if command -v tailscale >/dev/null 2>&1; then
  echo -e "${YELLOW}[warn]${NC} tailscale is already on PATH: $(command -v tailscale)"
  tailscale status --json 2>/dev/null | head -c 200 || true
  echo
else
  echo "[ok] tailscale not currently installed"
fi

# --- 2. The collateral damage, checked by reachability --------------------- #
# This is the authoritative section. Everything else here explains these results.
echo
echo "Aliyun data plane — the addresses every previous incident broke"
aliyun_contacts=""
for endpoint in "100.100.100.200:80:metadata" "100.100.45.182:1883:internal-mqtt"; do
  host=${endpoint%%:*}
  rest=${endpoint#*:}
  port=${rest%%:*}
  name=${rest##*:}
  if timeout 4 bash -c "echo > /dev/tcp/$host/$port" 2>/dev/null; then
    echo "  [ok] $name ($endpoint) reachable"
  else
    bad "$name ($endpoint) NOT reachable"
    echo "       Before Tailscale this was reachable. The usual cause is the nftables rule"
    echo "       in table 'ip filter', chain 'ts-input':"
    echo "         ip saddr 100.64.0.0/10 iifname != \"tailscale0\" drop"
    echo "       Aliyun's metadata, internal MQTT, internal OSS *and its DHCP DNS servers*"
    echo "       all live in 100.64.0.0/10, so one rule takes all of them."
    aliyun_contacts="$aliyun_contacts $name"
  fi
done

# The DHCP-supplied resolvers, read from the routing table rather than hardcoded — they are
# the collateral that was missed entirely in 2026-10-18 and the reason a certificate could
# not be issued.
for resolver in $(ip route show 2>/dev/null | awk '/^100\.100\./ {print $1}' | sort -u); do
  if timeout 4 bash -c "echo > /dev/tcp/${resolver}/53" 2>/dev/null; then
    echo "  [ok] DNS resolver $resolver reachable"
  else
    bad "DNS resolver $resolver NOT reachable — resolution will fail, which breaks ACME"
  fi
done

echo
echo "Public DNS resolution (the thing whose failure looked like a TLS bug)"
if timeout 6 getent hosts acme-v02.api.letsencrypt.org >/dev/null 2>&1; then
  echo "  [ok] acme-v02.api.letsencrypt.org resolves"
else
  bad "cannot resolve acme-v02.api.letsencrypt.org"
  echo "       With DNS down, 'tailscale serve' cannot obtain a certificate and the"
  echo "       symptom you will see is a TLS error on the client, not a DNS one."
fi

# --- 3. The rule itself, in both rule stores ------------------------------- #
# Explanatory, not authoritative: nftables can express this drop in ways a grep will not
# match, and `iptables -L` cannot see nft's `ts-input` chain at all. Both are checked because
# the iptables-only version of this check is what failed to catch 2026-10-18.
echo
echo "Firewall rules that could drop 100.64.0.0/10 (explains section above)"
found_rule=0
if command -v nft >/dev/null 2>&1 && nft list ruleset 2>/dev/null | grep -qE '100\.64\.0\.0/10'; then
  found_rule=1
  echo "  [rule] nftables:"
  nft list ruleset 2>/dev/null | grep -nE '100\.64\.0\.0/10' | sed 's/^/         /'
fi
if iptables -L INPUT -n --line-numbers 2>/dev/null | grep -qE '100\.64\.0\.0/10'; then
  found_rule=1
  echo "  [rule] iptables INPUT:"
  iptables -L INPUT -n --line-numbers 2>/dev/null | grep -E '100\.64\.0\.0/10' | sed 's/^/         /'
fi
if [ "$found_rule" = 0 ]; then
  echo "  none found in either store — but trust section 2, not this line."
fi
echo "  Checked with: nft list ruleset  AND  iptables -L INPUT"

# --- 4. The thing that actually broke the first time ----------------------- #
echo
echo "Backup client health (this is what the ts-input rule killed)"
if systemctl list-unit-files 2>/dev/null | grep -q hybridbackup; then
  systemctl is-active hybridbackup 2>/dev/null || echo "  (no hybridbackup unit)"
elif pgrep -f hbrclient >/dev/null 2>&1; then
  restarts=$(journalctl --since "-24h" 2>/dev/null | grep -c "hbrclient" || echo 0)
  echo "  [warn] hbrclient running; ${restarts} log mentions in 24h"
  echo "         A healthy client logs almost nothing. Restart spam = the incident signature."
  pgrep -cf hbrclient | xargs -I{} echo "  hbrclient processes: {}"
else
  echo "  [??] no hbrclient process found"
  echo "       Confirm in the Aliyun console whether this instance is enrolled in hybrid"
  echo "       cloud backup at all. If it is not, this whole class of breakage is moot."
fi

# --- 5. Firewall posture ---------------------------------------------------- #
echo
echo "Host firewall posture (the security group is the real gate; the host is open by default)"
if command -v ufw >/dev/null 2>&1; then
  ufw status | head -3
else
  echo "  ufw not installed"
fi
iptables -S INPUT 2>/dev/null | head -6

# --- 6. Ports already in use ----------------------------------------------- #
echo
echo "Existing listeners (a new service must not collide)"
ss -tlnp 2>/dev/null | awk 'NR==1 || /LISTEN/' | head -12

# --- 7. What is exposed, if anything --------------------------------------- #
echo
echo "Exposure (serve = tailnet only, funnel = public internet)"
if command -v tailscale >/dev/null 2>&1; then
  tailscale serve status 2>/dev/null | head -8 || echo "  (no serve or funnel configured)"
  echo "  Funnel, if any:"
  tailscale funnel status 2>/dev/null | head -8 || echo "  (none)"
else
  echo "  n/a — tailscale not installed"
fi

hr
if [ "$fail" -ne 0 ]; then
  echo -e "${RED}RESULT: Aliyun's data plane is broken.${NC}"
  echo
  echo "Do not install or enable Tailscale on this box until this is resolved."
  echo "If Tailscale is what broke it, the usual fix is:"
  echo
  echo "    tailscale up --hostname=\$(hostname) --netfilter-mode=off"
  echo
  echo "  That stops Tailscale managing iptables/nftables at all. It is acceptable *on this"
  echo "  box specifically* because there is no host firewall here and the Aliyun security"
  echo "  group is the only gate — so the rules being removed were not protecting anything"
  echo "  this machine relies on, while they were actively breaking DNS and the metadata"
  echo "  service. On a host that does rely on its own firewall, do not do this: add explicit"
  echo "  ACCEPT rules for the Aliyun addresses instead."
  echo
  echo "Then re-run this script and compare."
else
  echo -e "${GREEN}RESULT: no known blocker found.${NC}"
  if command -v tailscale >/dev/null 2>&1; then
    echo
    echo "Tailscale is present. Before trusting it, confirm:"
    echo "  - section 2 is still all [ok] (reachability is the verdict, not the rule grep)"
    echo "  - if you configured serve/funnel, that section 7 shows what you intended"
  else
    echo "Re-run this after installing and compare sections 2, 4 and 7."
  fi
fi
hr
