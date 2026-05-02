import re
from pathlib import Path
p=Path('app/api.py')
text=p.read_text(encoding='utf-8')
lines=text.splitlines()
stack=[]
for idx,line in enumerate(lines, start=1):
    stripped=line.lstrip()
    indent=len(line)-len(stripped)
    # detect try: at line ending with 'try:'
    if re.match(r"try:\s*(#.*)?$", stripped):
        stack.append((idx, indent))
    # detect except or finally at this indent or less
    if re.match(r"(except\b|finally\b)", stripped):
        # find last try with same indent
        if stack:
            # pop the last try that has indent >= current indent
            # but ensure proper nesting
            found=False
            for i in range(len(stack)-1, -1, -1):
                t_idx, t_indent = stack[i]
                if t_indent==indent:
                    stack.pop(i)
                    found=True
                    break
            if not found:
                # try with different indent
                pass
# After scanning, print remaining tries
if stack:
    print('Unclosed try blocks:')
    for t_idx,t_indent in stack:
        print(f'  try at line {t_idx} (indent {t_indent})')
else:
    print('All try blocks matched')
