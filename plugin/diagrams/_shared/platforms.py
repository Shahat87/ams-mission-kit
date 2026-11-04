"""Official platform logos (simple-icons, CC0) stored in assets/logos/platforms/. svg(key) -> inline <svg> using currentColor."""
import re
from pathlib import Path
D = Path(__file__).resolve().parent.parent / "assets" / "logos" / "platforms"
FILES = {"claude-code": "claude.svg", "cursor": "cursor.svg", "codex": "codex.svg"}
NAMES = {"claude-code": "Claude Code", "cursor": "Cursor", "codex": "Codex"}
def svg(key, cls="pi"):
    t = (D / FILES[key]).read_text(encoding="utf-8")
    paths = "".join(re.findall(r"<path[^>]*/>", t))
    return f'<svg class="{cls} pi-{key}" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">{paths}</svg>'
