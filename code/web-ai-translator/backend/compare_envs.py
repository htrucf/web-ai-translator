import re

with open('workspace/pipeline_test/source/iclr2026_conference.tex','r',encoding='utf-8') as f:
    orig = f.read()
with open('workspace/jobs/test_2603.01285/output/translated.tex','r',encoding='utf-8') as f:
    trans = f.read()

# Count all \begin{X} and \end{X} in non-comment lines
def count_envs(text):
    counts = {}
    for line in text.split('\n'):
        if line.strip().startswith('%'):
            continue
        for m in re.finditer(r'\\(begin|end)\{([^}]+)\}', line):
            key = f'\\{m.group(1)}{{{m.group(2)}}}'
            counts[key] = counts.get(key, 0) + 1
    return counts

oc = count_envs(orig)
tc = count_envs(trans)

all_keys = sorted(set(list(oc.keys()) + list(tc.keys())))
for k in all_keys:
    o = oc.get(k, 0)
    t = tc.get(k, 0)
    if o != t:
        print(f'MISMATCH {k}: orig={o} trans={t}')
