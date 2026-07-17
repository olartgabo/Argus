"""ARGUS — MCP ecosystem watcher.

Daily pipeline: fetch sources -> normalize -> dedup (DynamoDB) ->
rank/summarize (Bedrock) -> render HTML digest -> deliver (SES) ->
persist seen ids.

Runs as a single Lambda (argus-run). No dependencies beyond boto3/stdlib.
"""

import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import boto3

TABLE_NAME = os.environ.get("TABLE_NAME", "argus-seen")
SENDER = os.environ["SENDER_EMAIL"]
RECIPIENT = os.environ["RECIPIENT_EMAIL"]
MODEL_ID = os.environ.get("MODEL_ID", "us.amazon.nova-pro-v1:0")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "36"))
MAX_ITEMS_TO_MODEL = int(os.environ.get("MAX_ITEMS_TO_MODEL", "60"))
SEEN_TTL_DAYS = 90
LA_PAZ = timezone(timedelta(hours=-4))

RELEASE_WATCHLIST = [
    "modelcontextprotocol/servers",
    "modelcontextprotocol/specification",
    "modelcontextprotocol/python-sdk",
    "modelcontextprotocol/typescript-sdk",
    "modelcontextprotocol/inspector",
]

dynamodb = boto3.client("dynamodb")
bedrock = boto3.client("bedrock-runtime")
ses = boto3.client("ses")


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "argus-mcp-watcher", **(headers or {})})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


# ---------------------------------------------------------------- fetchers

def fetch_github_new_repos(since):
    """Repos with topic:mcp created within the lookback window."""
    q = urllib.parse.quote(f"topic:mcp created:>{since:%Y-%m-%d}")
    url = f"https://api.github.com/search/repositories?q={q}&sort=stars&order=desc&per_page=50"
    items = []
    for repo in http_get_json(url, gh_headers()).get("items", []):
        items.append({
            "source": "github",
            "type": "new_repo",
            "id": f"github:new_repo:{repo['full_name']}",
            "title": repo["full_name"],
            "url": repo["html_url"],
            "description": (repo.get("description") or "")[:300],
            "published_at": repo["created_at"],
            "metrics": {"stars": repo["stargazers_count"]},
        })
    return items


def fetch_github_releases(since):
    """New releases on the curated watchlist."""
    items = []
    for repo in RELEASE_WATCHLIST:
        try:
            releases = http_get_json(
                f"https://api.github.com/repos/{repo}/releases?per_page=5", gh_headers())
        except urllib.error.HTTPError:
            continue
        for rel in releases:
            published = rel.get("published_at")
            if not published or published < since.strftime("%Y-%m-%dT%H:%M:%SZ"):
                continue
            items.append({
                "source": "github",
                "type": "release",
                "id": f"github:release:{repo}@{rel['tag_name']}",
                "title": f"{repo} {rel['tag_name']}",
                "url": rel["html_url"],
                "description": (rel.get("body") or "")[:400],
                "published_at": published,
                "metrics": {},
            })
    return items


def fetch_npm_new(since):
    """Recently published packages matching mcp keywords."""
    items = []
    for text in ("keywords:mcp", "mcp-server"):
        url = f"https://registry.npmjs.org/-/v1/search?text={urllib.parse.quote(text)}&size=100"
        try:
            results = http_get_json(url).get("objects", [])
        except urllib.error.HTTPError:
            continue
        for obj in results:
            pkg = obj["package"]
            date = pkg.get("date", "")
            if not date or date < since.strftime("%Y-%m-%dT%H:%M:%S"):
                continue
            items.append({
                "source": "npm",
                "type": "new_version",
                "id": f"npm:{pkg['name']}@{pkg['version']}",
                "title": f"{pkg['name']}@{pkg['version']}",
                "url": pkg.get("links", {}).get("npm", f"https://www.npmjs.com/package/{pkg['name']}"),
                "description": (pkg.get("description") or "")[:300],
                "published_at": date,
                "metrics": {},
            })
    return items


def fetch_all(since):
    items, errors = [], []
    for fetcher in (fetch_github_new_repos, fetch_github_releases, fetch_npm_new):
        try:
            items.extend(fetcher(since))
        except Exception as exc:  # a dead source must not kill the digest
            errors.append(f"{fetcher.__name__}: {exc}")
            print(f"FETCH ERROR {fetcher.__name__}: {exc}")
    # de-dup within the run (npm queries overlap)
    unique = {i["id"]: i for i in items}
    return list(unique.values()), errors


# ---------------------------------------------------------------- dedup

def filter_unseen(items):
    unseen = []
    for item in items:
        resp = dynamodb.get_item(
            TableName=TABLE_NAME,
            Key={"pk": {"S": f"SEEN#{item['source']}"}, "sk": {"S": item["id"]}},
            ProjectionExpression="pk",
        )
        if "Item" not in resp:
            unseen.append(item)
    return unseen


def mark_seen(items):
    now = datetime.now(timezone.utc).isoformat()
    ttl = int(time.time()) + SEEN_TTL_DAYS * 86400
    for item in items:
        dynamodb.put_item(TableName=TABLE_NAME, Item={
            "pk": {"S": f"SEEN#{item['source']}"},
            "sk": {"S": item["id"]},
            "first_seen": {"S": now},
            "title": {"S": item["title"][:200]},
            "url": {"S": item["url"]},
            "ttl": {"N": str(ttl)},
        })


def record_digest(date_str, count):
    dynamodb.put_item(TableName=TABLE_NAME, Item={
        "pk": {"S": f"DIGEST#{date_str}"},
        "sk": {"S": "meta"},
        "item_count": {"N": str(count)},
        "sent_at": {"S": datetime.now(timezone.utc).isoformat()},
    })


# ---------------------------------------------------------------- bedrock

PROMPT = """You are ARGUS, an analyst tracking the Model Context Protocol (MCP) ecosystem \
for a busy MCP tool builder. Below is a JSON array of items that appeared in the last day \
(new GitHub repos, releases, npm packages).

Rank and curate them. Drop low-signal noise (empty forks, trivial name-squats, tutorials \
with no substance). Cluster near-duplicates. Return ONLY valid JSON, no prose, no markdown \
fences, with this exact shape:

{"sections": [{"title": "New servers" | "Notable releases" | "Spec & SDKs" | "Other",
  "items": [{"title": "...", "url": "...", "why": "one line on why an MCP builder should care"}]}],
 "headline": "one-sentence summary of the day"}

Omit empty sections. Keep at most 15 items total, best first.

ITEMS:
"""


def summarize(items):
    payload = [
        {k: v for k, v in i.items() if k != "id"}
        for i in sorted(items, key=lambda x: x.get("published_at", ""), reverse=True)[:MAX_ITEMS_TO_MODEL]
    ]
    resp = bedrock.converse(
        modelId=MODEL_ID,
        messages=[{"role": "user", "content": [{"text": PROMPT + json.dumps(payload)}]}],
        inferenceConfig={"maxTokens": 2000, "temperature": 0.2},
    )
    text = resp["output"]["message"]["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    return json.loads(text)


def fallback_digest(items):
    """Plain list if the model call or JSON parse fails — digest still ships."""
    by_type = {}
    for i in items[:20]:
        by_type.setdefault(i["type"], []).append(
            {"title": i["title"], "url": i["url"], "why": i["description"][:120]})
    return {
        "headline": f"{len(items)} new MCP items today (unranked — summarizer unavailable).",
        "sections": [{"title": t.replace("_", " ").title(), "items": v} for t, v in by_type.items()],
    }


# ---------------------------------------------------------------- render + send

def safe_url(url):
    """Item titles/urls come from public registries and model output — treat as hostile."""
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else "#"


def render_html(digest, date_str, total_new):
    esc = html.escape
    parts = [
        f"<h2 style='margin-bottom:4px'>ARGUS &mdash; MCP digest, {esc(date_str)}</h2>",
        f"<p style='color:#555'>{esc(str(digest.get('headline', '')))}</p>",
    ]
    for section in digest.get("sections", []):
        if not section.get("items"):
            continue
        parts.append(f"<h3 style='margin-bottom:2px'>{esc(str(section['title']))}</h3><ul>")
        for it in section["items"]:
            parts.append(
                f"<li style='margin-bottom:6px'><a href=\"{esc(safe_url(it.get('url')), quote=True)}\">"
                f"{esc(str(it.get('title', '')))}</a>"
                f"<br><span style='color:#555;font-size:13px'>{esc(str(it.get('why', '')))}</span></li>")
        parts.append("</ul>")
    parts.append(
        f"<hr><p style='color:#999;font-size:12px'>{total_new} new items scanned &middot; "
        f"run at {datetime.now(LA_PAZ):%Y-%m-%d %H:%M} (La Paz) &middot; sent by ARGUS, unattended.</p>")
    return "".join(parts)


def send_email(subject, html):
    ses.send_email(
        Source=SENDER,
        Destination={"ToAddresses": [RECIPIENT]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Html": {"Data": html}},
        },
    )


# ---------------------------------------------------------------- handler

def handler(event, context):
    date_str = f"{datetime.now(LA_PAZ):%Y-%m-%d}"
    since = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)

    items, fetch_errors = fetch_all(since)
    print(f"fetched={len(items)} errors={fetch_errors}")

    new_items = filter_unseen(items)
    print(f"new={len(new_items)}")

    if not new_items:
        send_email(f"ARGUS {date_str}: all quiet",
                   render_html({"headline": "All quiet in MCP-land today.", "sections": []},
                               date_str, 0))
        record_digest(date_str, 0)
        return {"status": "quiet", "fetched": len(items)}

    try:
        digest = summarize(new_items)
    except Exception as exc:
        print(f"BEDROCK ERROR: {exc}")
        digest = fallback_digest(new_items)

    send_email(f"ARGUS {date_str}: {len(new_items)} new in MCP-land",
               render_html(digest, date_str, len(new_items)))
    mark_seen(new_items)
    record_digest(date_str, len(new_items))
    return {"status": "sent", "fetched": len(items), "new": len(new_items)}
