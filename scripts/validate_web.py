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
    "save blocks when balance warning is visible": 'if(!$("balanceWarning").classList.contains("hidden"))throw new Error' in html,
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
print(f"All {len(required) + len(parser.scripts)} release smoke checks passed.")
