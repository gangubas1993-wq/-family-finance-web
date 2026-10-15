#!/usr/bin/env python3
"""Release smoke checks for the static Family Finance web client."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "index.html"
html = HTML_PATH.read_text(encoding="utf-8")

required = {
    "Russian document language": '<html lang="ru">' in html,
    "responsive viewport": 'name="viewport"' in html,
    "HTTPS dashboard API": 'const API="https://txebmzeyoqauitiyykto.supabase.co/functions/v1/family-finance-dashboard-api"' in html,
    "fail-closed default role": 'ROLE="member"' in html,
    "balance warning function": "function updateBalanceWarning()" in html,
    "save blocks on balance warning unless explicit owner history mode is selected": 'if(!$("balanceWarning").classList.contains("hidden")&&!historical)throw new Error' in html,
    "historical mode is owner-only and sent explicitly": 'historical_mode:historical' in html and 'ROLE==="owner"&&$("historicalMode").checked' in html,
    "operation save handler": "async function saveOperation()" in html,
    "login token stored for session": 'sessionStorage.setItem("ff_token", token)' in html,
    "URL cleaned after token capture": "history.replaceState" in html,
    "owner-only admin UI": 'adminNav.classList.toggle("hidden",ROLE!=="owner")' in html,
}
failed = [name for name, ok in required.items() if not ok]
for name, ok in required.items():
    print(f'{"PASS" if ok else "FAIL"} {name}')
if failed:
    raise SystemExit(f"{len(failed)} release smoke check(s) failed: {', '.join(failed)}")

class InlineScripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script":
            attrs = dict(attrs)
            if not attrs.get("src"):
                self.current = {"attrs": attrs, "parts": []}

    def handle_data(self, data):
        if self.current is not None:
            self.current["parts"].append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.current is not None:
            attrs = self.current["attrs"]
            kind = attrs.get("type", "").lower()
            if kind in ("", "text/javascript", "application/javascript", "module"):
                self.scripts.append("".join(self.current["parts"]))
            self.current = None

parser = InlineScripts()
parser.feed(html)
if not parser.scripts:
    raise SystemExit("FAIL no inline JavaScript found")
with tempfile.TemporaryDirectory() as tmp:
    for index, source in enumerate(parser.scripts, start=1):
        path = Path(tmp) / f"inline-{index}.js"
        path.write_text(source, encoding="utf-8")
        result = subprocess.run(["node", "--check", str(path)], text=True, capture_output=True)
        if result.returncode:
            print(f"FAIL inline JavaScript #{index}")
            print(result.stdout, end="")
            print(result.stderr, end="")
            raise SystemExit(result.returncode)
        print(f"PASS inline JavaScript syntax #{index}")
import json
import re
import urllib.error
import urllib.request

api_match = re.search(r'const API="(https://[^"]+)"', html)
if not api_match:
    raise SystemExit("FAIL could not locate HTTPS API URL")
api = api_match.group(1)

def request_status(path, method="GET"):
    data = b"{}" if method == "POST" else None
    request = urllib.request.Request(
        api + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()

try:
    status, body = request_status("")
    health = json.loads(body.decode("utf-8"))
    ok = status == 200 and health.get("status") == "healthy"
    print(f'{"PASS" if ok else "FAIL"} live API health endpoint ({status})')
    if not ok:
        raise SystemExit("Live API health check failed")

    protected_routes = [
        ("GET", "/dashboard"),
        ("GET", "/dictionary"),
        ("GET", "/recurring/list"),
        ("POST", "/operation/create"),
        ("POST", "/operation/update"),
        ("POST", "/operation/delete"),
        ("POST", "/account/create"),
        ("POST", "/account/update"),
        ("POST", "/account/toggle"),
        ("POST", "/category/create"),
        ("POST", "/category/update"),
        ("POST", "/category/toggle"),
        ("POST", "/alias/create"),
        ("POST", "/alias/update"),
        ("POST", "/alias/toggle"),
        ("POST", "/recurring/create"),
        ("POST", "/recurring/update"),
        ("POST", "/recurring/toggle"),
        ("POST", "/recurring/delete"),
    ]
    for method, path in protected_routes:
        status, _ = request_status(path, method)
        ok = status == 401
        print(f'{"PASS" if ok else "FAIL"} unauthenticated {method} {path} rejected ({status})')
        if not ok:
            raise SystemExit(f"Expected HTTP 401 for unauthenticated {method} {path}, got {status}")
except (TimeoutError, OSError, urllib.error.URLError) as error:
    raise SystemExit(f"Live API smoke check failed: {error}")

print(f"All {len(required) + len(parser.scripts) + 1 + len(protected_routes)} release smoke checks passed.")
