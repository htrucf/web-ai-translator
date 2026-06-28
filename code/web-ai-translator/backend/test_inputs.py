"""Test which input file causes compile error."""
import os
import subprocess
import shutil

output_dir = 'workspace/jobs/test_2603.01285/output'
tex_path = os.path.join(output_dir, 'translated.tex')
test_path = os.path.join(output_dir, 'translated_test2.tex')

with open(tex_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find all input lines
input_lines = []
for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith(r'\input{Tables/') or stripped.startswith(r'\input{Sections/'):
        input_lines.append((i, stripped))

print(f"Found {len(input_lines)} input files")

# Test each one individually
for idx, (line_num, input_cmd) in enumerate(input_lines):
    # Create test file: disable all inputs EXCEPT this one
    with open(test_path, 'w', encoding='utf-8') as f:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(r'\input{Tables/') or stripped.startswith(r'\input{Sections/'):
                if i == line_num:
                    f.write(line)  # Keep this one
                else:
                    f.write('% ' + line)  # Disable others
            else:
                f.write(line)

    pdflatex = r'C:\Users\LENOVO\AppData\Local\Programs\MiKTeX\miktex\bin\x64\pdflatex.exe'
    result = subprocess.run(
        [pdflatex, '-interaction=nonstopmode', 'translated_test2.tex'],
        capture_output=True, text=True, cwd=output_dir,
    )

    error_count = result.stdout.count('\n! ')
    pdf_exists = os.path.exists(os.path.join(output_dir, 'translated_test2.pdf'))
    status = 'OK' if pdf_exists and error_count < 5 else 'FAIL'
    print(f"  {status} [{error_count} errors] {input_cmd}")

    # Clean up pdf
    for ext in ['.pdf', '.aux', '.log', '.out']:
        p = os.path.join(output_dir, f'translated_test2{ext}')
        if os.path.exists(p):
            os.remove(p)
