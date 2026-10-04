"""Copy the built widget to the portfolio's static assets and update its script tag.

Build first: npm run build --prefix widget
Then: python3 scripts/sync_widget.py --site-dir /path/to/portfolio --api https://agent.example
Review and publish the portfolio changes through its normal GitHub Pages deployment.
"""

import argparse
import hashlib
import html
import re
from pathlib import Path
from urllib.parse import urlparse


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", type=Path, required=True)
    parser.add_argument("--api", required=True)
    args = parser.parse_args()
    api = urlparse(args.api)
    if api.scheme != "https" or not api.netloc or api.username or api.password:
        parser.error("--api must be an HTTPS backend URL without credentials")
    origin = f"{api.scheme}://{api.netloc}"
    bundle = Path(__file__).resolve().parents[1] / "widget/dist/twin-widget.js"
    content = bundle.read_bytes()
    filename = f"twin-widget-{hashlib.sha256(content).hexdigest()[:12]}.js"
    index = args.site_dir / "index.html"
    source = index.read_text()
    # Match only Twin's existing external or static script tag.
    pattern = r'<script\b[^>]*\bsrc="[^"]*(?:/widget\.js|/twin-widget-[a-f0-9]+\.js)"[^>]*>\s*</script>'
    matches = list(re.finditer(pattern, source))
    if len(matches) != 1:
        parser.error("expected exactly one existing Twin script tag in index.html")
    live = ' data-live="1"' if 'data-live="1"' in matches[0].group() else ""
    tag = (f'<script src="/assets/{filename}" data-api="{html.escape(origin, quote=True)}"'
           f'{live} defer></script>')
    updated = source[:matches[0].start()] + tag + source[matches[0].end():]
    assets = args.site_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / filename).write_bytes(content)
    index.write_text(updated)
    print(f"Prepared {assets / filename}")
    print(f"Updated {index}; backend: {origin}")


if __name__ == "__main__":
    main()
