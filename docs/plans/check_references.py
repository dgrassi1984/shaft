"""Check plan references without reading runtime data or the concurrently edited README."""
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
errors = []
for name in filter(None, files):
    if name == 'README.md' or name.startswith(('template/', 'instances/', '.supervisor/', 'secrets/')):
        continue
    path = ROOT / name
    if name.startswith('PLAN') and '/' not in name:
        path = ROOT / 'docs/plans' / name
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        continue
    if path.parent == ROOT / 'docs/plans':
        continue
    for number, line in enumerate(text.splitlines(), 1):
        for match in re.finditer(r'PLAN(?:-[\w-]+(?:\.md)?|\.md)', line):
            if line[max(0, match.start()-11):match.start()] != 'docs/plans/':
                errors.append(f'{name}:{number}:{line}')
print('\n'.join(errors) if errors else 'PASS: all plan references outside README and docs/plans use docs/plans/.')
raise SystemExit(bool(errors))
