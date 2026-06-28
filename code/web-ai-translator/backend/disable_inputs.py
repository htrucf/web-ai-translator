with open('workspace/jobs/test_2603.01285/output/translated.tex', 'r', encoding='utf-8') as f:
    lines = f.readlines()

with open('workspace/jobs/test_2603.01285/output/translated_test.tex', 'w', encoding='utf-8') as f:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(r'\input{Tables/') or stripped.startswith(r'\input{Sections/'):
            f.write('% DISABLED: ' + line)
        else:
            f.write(line)
print('Done')
