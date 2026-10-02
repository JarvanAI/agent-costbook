"""Check local Markdown link targets in the public source tree."""

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
documents = [*ROOT.glob("*.md")]
for folder in ("docs", "skills"):
    documents.extend((ROOT / folder).rglob("*.md"))
errors = []
for document in sorted(documents):
    text = document.read_text(encoding="utf-8")
    # Examples inside fenced code blocks are not documentation hyperlinks.
    text = re.sub(r"(?ms)^\s*(```|~~~).*?^\s*\1[^\n]*$", "", text)
    targets = re.findall(r"\]\(\s*(<[^>]*>|[^\s)]+)", text)
    targets += re.findall(r"(?m)^\s*\[[^\]]+\]:\s*(<[^>]*>|\S+)", text)
    for target in targets:
        target = target.strip("<>")
        url = urlsplit(target)
        if url.scheme or url.netloc or not url.path:
            continue
        path = unquote(url.path)
        destination = ROOT / path.lstrip("/") if path.startswith("/") else document.parent / path
        if not destination.exists():
            errors.append(f"{document.relative_to(ROOT)}: missing link target {target}")
if errors:
    raise SystemExit("\n".join(errors))
print(f"Local links verified in {len(documents)} Markdown documents")
