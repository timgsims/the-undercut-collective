# One-off (1 Nov 2026, Tim's go-ahead): Cloudflare for f1push.simsey.co.nz, the
# push service the dashboard talks to. Runs on mako-sentinel with
# ~/.cloudflare-api-token. From push-service/ on the laptop:
#   ssh sentinel-admin@192.168.0.50 "bash -s" < ops/cloudflare-push.sh
# Backs up the tunnel config, adds the route f1push.simsey.co.nz ->
# http://undercut-push:3000 (no Access: league members aren't on it) and a
# proxied CNAME. Stops at the first failure. Don't re-run once done.
set -e
H="Authorization: Bearer $(cat ~/.cloudflare-api-token)"
A=5a4871f9be0c2f54dd52c2766bb8d146; Z=8cafc73b981f516e9fd98cff89d71570; T=690aabac-1d6d-452a-aef3-a2faaed8a891
CF=https://api.cloudflare.com/client/v4
J="Content-Type: application/json"
B=~/tunnel-config-backup-2026-11-01-pre-f1push.json

curl -s -H "$H" $CF/accounts/$A/cfd_tunnel/$T/configurations > $B
python3 -c "import json;assert json.load(open('$B'))['success']" && echo "backup $B"

python3 - "$B" > /tmp/f1push-tunnel.json <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))['result']['config']
ing = c['ingress']
assert not any(r.get('hostname') == 'f1push.simsey.co.nz' for r in ing)
ing.insert(len(ing) - 1, {"hostname": "f1push.simsey.co.nz", "service": "http://undercut-push:3000"})
print(json.dumps({"config": c}))
PY
curl -s -H "$H" -H "$J" -X PUT $CF/accounts/$A/cfd_tunnel/$T/configurations --data @/tmp/f1push-tunnel.json \
  | python3 -c "import sys,json;r=json.load(sys.stdin);assert r['success'],r;print('tunnel version',r['result']['version'])"
rm /tmp/f1push-tunnel.json

curl -s -H "$H" -H "$J" -X POST $CF/zones/$Z/dns_records \
  -d '{"type":"CNAME","name":"f1push","content":"'$T'.cfargotunnel.com","proxied":true,"comment":"Undercut Collective push notifications (tunnel mako-sentinel, no Access)"}' \
  | python3 -c "import sys,json;r=json.load(sys.stdin);assert r['success'],r;print('dns',r['result']['id'])"
echo done
