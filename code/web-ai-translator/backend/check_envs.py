import re

with open('workspace/jobs/test_2603.01285/output/translated.tex', 'r', encoding='utf-8') as f:
    lines = f.readlines()

pattern = re.compile(r'\\(begin|end)\{([^}]+)\}')
stack = []
for i, line in enumerate(lines, 1):
    # Skip comments
    stripped = line.lstrip()
    if stripped.startswith('%'):
        continue
    for m in pattern.finditer(line):
        cmd, env = m.group(1), m.group(2)
        if cmd == 'begin':
            stack.append((env, i))
        elif cmd == 'end':
            if stack and stack[-1][0] == env:
                stack.pop()
            else:
                expected = stack[-1][0] if stack else 'NONE'
                print(f'MISMATCH line {i}: \\end{{{env}}} but expected \\end{{{expected}}}')
                if stack:
                    stack.pop()

if stack:
    print(f'Unclosed environments:')
    for env, line_num in stack:
        print(f'  \\begin{{{env}}} at line {line_num}')
else:
    print('All environments balanced')
