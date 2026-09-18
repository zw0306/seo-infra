#!/usr/bin/env python3
"""
Hotfix: Escape all a.get("title", ...) in google_report.py HTML f-strings
to prevent Lighthouse audit titles containing <html>, backticks, etc.
from breaking the HTML DOM and causing sections after 2.4 to vanish.
"""
import re
import pathlib
import sys

script = pathlib.Path.home() / ".agents" / ".claude-seo-upstream" / "scripts" / "google_report.py"

if not script.exists():
    print(f"ERROR: {script} not found", file=sys.stderr)
    sys.exit(1)

content = script.read_text()

# Replace {var.get("title", "anything")} with {escape(var.get("title", "anything"))}
# Handles single/double quotes and different variable names (a, o, item)
patched = re.sub(
    r'\{([a-zA-Z0-9_]+)\.get\(([\'\"])title\2,\s*([\'\"][^\'\"]*[\'\"])\)\}',
    r'{escape(\1.get(\2title\2, \3))}',
    content
)

new_fixes = patched.count('escape(') - content.count('escape(')

if new_fixes > 0:
    script.write_text(patched)
    print(f"✅ Hotfix applied: {new_fixes} title field(s) wrapped with escape()")
else:
    print("ℹ️ All title fields already escaped, no changes needed")
