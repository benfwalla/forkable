"""mitmdump addon: dump a saved mitmproxy capture's forkable.com traffic to JSON.

  mitmdump -nq -r ~/Downloads/flows -s tools/export_flows.py --set outfile=captures/flows.json

Each GraphQL request is listed with its query, variables and response, which is the
quickest way to see what a new UI action sends.
"""

import json

from mitmproxy import ctx

out = []


def load(loader):
    loader.add_option("outfile", str, "flows.json", "where to write the JSON dump")


def response(flow):
    if "forkable.com" not in flow.request.pretty_host:
        return
    req, resp = flow.request, flow.response
    entry = {"method": req.method, "url": req.pretty_url, "status": resp.status_code if resp else None}
    body = req.get_text(strict=False)
    if "graphql" in req.path and body:
        try:
            entry["graphql"] = json.loads(body)
        except ValueError:
            entry["body"] = body
    if resp:
        try:
            entry["response"] = json.loads(resp.get_text(strict=False) or "null")
        except ValueError:
            entry["response"] = (resp.get_text(strict=False) or "")[:2000]
    out.append(entry)


def done():
    with open(ctx.options.outfile, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {len(out)} forkable.com flows to {ctx.options.outfile}")
