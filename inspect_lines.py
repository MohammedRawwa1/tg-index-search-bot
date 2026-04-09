from pathlib import Path
p=Path('app/api.py')
lines=p.read_text(encoding='utf-8').splitlines()
start=1008
end=1048
for i in range(start-1,end):
    ln=lines[i]
    lead=len(ln)-len(ln.lstrip(' '))
    print(f"{i+1:5}: [{lead:2}] {ln.rstrip()}")
