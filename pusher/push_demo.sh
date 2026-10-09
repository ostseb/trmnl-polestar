#!/bin/sh
# Push the sample cars in ../preview.json to a webhook, e.g. for the public recipe's preview image.
#   ./push_demo.sh https://trmnl.com/api/custom_plugins/<uuid>
set -eu
url="${1:?usage: $0 <webhook-url>}"
python3 -c 'import json,sys; print(json.dumps({"merge_variables": json.load(open(sys.argv[1]))["data"]}))' \
  "$(dirname "$0")/../preview.json" |
  curl -sS -X POST -H "Content-Type: application/json" --data-binary @- "$url"
echo
