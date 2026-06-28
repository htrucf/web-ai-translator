"""Fix common LaTeX issues in translated.tex caused by Gemini translation."""
import re

tex_path = 'workspace/jobs/test_2603.01285/output/translated.tex'

with open(tex_path, 'r', encoding='utf-8') as f:
    content = f.read()

original_len = len(content)

# Fix 1: Ensure \begin{figure*} is on its own line
content = re.sub(r'([^\n])(\\begin\{figure\*?\}\[)', r'\1\n\2', content)

# Fix 2: Ensure \end{itemize/enumerate} is on its own line before next text
content = re.sub(r'(\\end\{itemize\})([^\n])', r'\1\n\2', content)
content = re.sub(r'(\\end\{enumerate\})([^\n])', r'\1\n\2', content)

# Fix 3: Ensure \section/\subsection has newline before next paragraph
content = re.sub(r'(\\(?:sub)*section\*?\{[^}]+\})([A-Z\xC0-\xFF])', r'\1\n\2', content)

# Fix 4: Fix consecutive \begin{equation}\begin{equation} (Gemini artifact)
content = re.sub(r'\\begin\{equation\}\s*\\begin\{equation\}', r'\\begin{equation}', content)
content = re.sub(r'\\end\{equation\}\s*\\end\{equation\}', r'\\end{equation}', content)

# Fix 5: Remove "Code snippet" lines
content = re.sub(r'^Code snippet\s*$', '', content, flags=re.MULTILINE)

# Fix 6: Ensure \ref{...}.\textbf has newline
content = re.sub(r'(\})\.(\\textbf)', r'\1.\n\n\2', content)

# Fix 7: Ensure \end{table*?} on own line
content = re.sub(r'([^\n])(\\end\{table\*?\})', r'\1\n\2', content)

print(f'Original: {original_len} chars')
print(f'Fixed: {len(content)} chars')

with open(tex_path, 'w', encoding='utf-8') as f:
    f.write(content)
print('Saved.')
