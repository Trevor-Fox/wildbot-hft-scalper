#!/bin/bash
TOKEN=$(python3 -c "
import os, json, urllib.request
hostname = os.environ.get('REPLIT_CONNECTORS_HOSTNAME')
repl_identity = os.environ.get('REPL_IDENTITY')
web_repl_renewal = os.environ.get('WEB_REPL_RENEWAL')
tok = ('repl ' + repl_identity) if repl_identity else ('depl ' + web_repl_renewal) if web_repl_renewal else ''
url = f'https://{hostname}/api/v2/connection?include_secrets=true&connector_names=github'
req = urllib.request.Request(url, headers={'Accept': 'application/json', 'X_REPLIT_TOKEN': tok})
data = json.loads(urllib.request.urlopen(req).read())
item = data.get('items', [None])[0]
s = item.get('settings', {})
print(s.get('access_token') or s.get('oauth',{}).get('credentials',{}).get('access_token',''))
")

if [ -z "$TOKEN" ]; then
  echo "Failed to get GitHub token"
  exit 1
fi

git remote set-url origin "https://x-access-token:${TOKEN}@github.com/Trevor-Fox/wildbot-hft-scalper.git"
git push --force -u origin main
git remote set-url origin "https://github.com/Trevor-Fox/wildbot-hft-scalper.git"
echo "Done! Pushed to GitHub successfully."
