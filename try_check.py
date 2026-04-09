import re
from pathlib import Path
p = Path('app/api.py')
text = p.read_text(encoding='utf-8')
lines = text.splitlines()

tries = []
excepts = []
finals = []
for idx, line in enumerate(lines, start=1):
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    if re.match(r"try:\s*(#.*)?$", stripped):
        tries.append((idx, indent, line.rstrip()))
    if re.match(r"except\b", stripped):
        excepts.append((idx, indent, line.rstrip()))
    if re.match(r"finally\b", stripped):
        finals.append((idx, indent, line.rstrip()))

print('Found tries:')
for t in tries:
    print(f"  try at line {t[0]} indent {t[1]}: {t[2].strip()[:120]}")
print('\nFound excepts:')
for e in excepts:
    print(f"  except at line {e[0]} indent {e[1]}: {e[2].strip()[:120]}")
print('\nFound finals:')
for f in finals:
    print(f"  finally at line {f[0]} indent {f[1]}: {f[2].strip()[:120]}")

# crude matching: try->except at same indent
unmatched = []
stack = []
for idx, line in enumerate(lines, start=1):
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    if re.match(r"try:\s*(#.*)?$", stripped):
        stack.append((idx, indent))
    if re.match(r"(except\b|finally\b)", stripped):
        # pop last try with same indent
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][1] == indent:
                stack.pop(i)
                break

if stack:
    print('\nUnclosed try blocks:')
    for t_idx, t_indent in stack:
        print(f'  try at line {t_idx} (indent {t_indent})')
else:
    print('\nAll try blocks matched')
