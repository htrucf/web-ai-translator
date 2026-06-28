"""Service xử lý file LaTeX: parse, tách đoạn, và compile PDF."""

import os
import subprocess
import tarfile
import zipfile
import re


def _safe_extract_tar(tar: tarfile.TarFile, dest: str) -> None:
    """Extract `tar` into `dest`, blocking entries that escape the directory.

    arXiv source archives are external/untrusted; without filtering, a crafted
    tar entry like `../../etc/passwd` can overwrite arbitrary files
    (CVE-2007-4559). Python 3.12+ ships a tar filter — we use it when present
    and fall back to manual path validation for older interpreters.
    """
    dest_real = os.path.realpath(dest)

    # Preferred path: stdlib tar filter (Python 3.12+).
    try:
        tar.extractall(dest, filter="data")
        return
    except TypeError:
        pass  # Older Python — fall through to manual check.

    for member in tar.getmembers():
        member_path = os.path.realpath(os.path.join(dest, member.name))
        if not (member_path == dest_real or member_path.startswith(dest_real + os.sep)):
            raise RuntimeError(f"Blocked unsafe tar entry: {member.name!r}")
        # Refuse symlinks/hardlinks pointing outside dest.
        if member.issym() or member.islnk():
            link_target = os.path.realpath(os.path.join(dest, member.linkname))
            if not (link_target == dest_real or link_target.startswith(dest_real + os.sep)):
                raise RuntimeError(f"Blocked unsafe tar link: {member.name!r} -> {member.linkname!r}")
    tar.extractall(dest)


def _safe_extract_zip(zf: zipfile.ZipFile, dest: str) -> None:
    """Extract `zf` into `dest`, blocking zip-slip and symlink escape entries.

    User-uploaded ZIPs are untrusted; without filtering, a crafted entry like
    `../../etc/passwd` can overwrite arbitrary files (CVE-2007-4559 family).
    """
    dest_real = os.path.realpath(dest)
    for info in zf.infolist():
        # Skip absolute paths
        name = info.filename
        if name.startswith(("/", "\\")) or ":" in name:
            raise RuntimeError(f"Blocked unsafe zip entry: {name!r}")
        member_path = os.path.realpath(os.path.join(dest, name))
        if not (member_path == dest_real or member_path.startswith(dest_real + os.sep)):
            raise RuntimeError(f"Blocked unsafe zip entry: {name!r}")
    zf.extractall(dest)


def _find_main_tex(extract_dir: str) -> str:
    """Locate the `.tex` containing `\\begin{document}`. Fallback: first `.tex`."""
    for root, _, files in os.walk(extract_dir):
        for f in files:
            if f.endswith(".tex"):
                filepath = os.path.join(root, f)
                with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
                if r"\begin{document}" in content:
                    return filepath

    for root, _, files in os.walk(extract_dir):
        for f in files:
            if f.endswith(".tex"):
                return os.path.join(root, f)

    raise FileNotFoundError("Không tìm thấy file .tex trong source")


def extract_source(archive_path: str, output_dir: str) -> str:
    """Giải nén source LaTeX từ file tar.gz (từ arXiv hoặc upload thủ công)."""
    extract_dir = os.path.join(output_dir, "source")
    os.makedirs(extract_dir, exist_ok=True)

    with tarfile.open(archive_path, "r:gz") as tar:
        _safe_extract_tar(tar, extract_dir)

    return _find_main_tex(extract_dir)


def extract_source_zip(archive_path: str, output_dir: str) -> str:
    """Giải nén source LaTeX từ file ZIP người dùng upload (Overleaf export, v.v.)."""
    extract_dir = os.path.join(output_dir, "source")
    os.makedirs(extract_dir, exist_ok=True)

    with zipfile.ZipFile(archive_path, "r") as zf:
        _safe_extract_zip(zf, extract_dir)

    return _find_main_tex(extract_dir)


def save_single_tex(tex_bytes: bytes, output_dir: str, filename: str = "main.tex") -> str:
    """Lưu file .tex đơn lẻ vào source/ directory; trả về đường dẫn tex chính."""
    extract_dir = os.path.join(output_dir, "source")
    os.makedirs(extract_dir, exist_ok=True)
    tex_path = os.path.join(extract_dir, filename)
    try:
        text = tex_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = tex_bytes.decode("latin-1", errors="replace")
    with open(tex_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return tex_path


def split_into_chunks(latex_content: str, target_size: int = 1500) -> list[str]:
    """Tách nội dung LaTeX thành các đoạn để dịch, giữ nguyên cấu trúc.

    Rules:
    - Preamble (trước \\begin{document}) là chunk riêng, KHÔNG dịch.
    - KHÔNG BAO GIỜ cắt giữa \\begin{env}...\\end{env}.
    - Tách tại ranh giới an toàn: giữa các section, giữa các environment hoàn chỉnh,
      hoặc giữa các paragraph (dòng trống kép).
    - Target ~target_size ký tự nhưng cho phép lớn hơn để giữ nguyên environment.
    """
    chunks: list[str] = []

    # --- 1. Tách preamble ---
    doc_match = re.search(r'\\begin\{document\}', latex_content)
    if doc_match:
        preamble = latex_content[:doc_match.end()]
        body_and_end = latex_content[doc_match.end():]
        chunks.append(preamble)  # chunk preamble – sẽ được đánh dấu không dịch
    else:
        body_and_end = latex_content

    # --- 2. Tách body thành các top-level block ---
    blocks = _split_into_blocks(body_and_end)

    # --- 3. Gộp các block nhỏ lại thành chunk có kích thước hợp lý ---
    current = ""
    for block in blocks:
        # Nếu thêm block này vượt target VÀ current đã có nội dung → flush
        if current.strip() and len(current) + len(block) > target_size:
            chunks.append(current)
            current = ""
        current += block

    if current.strip():
        chunks.append(current)

    return chunks if chunks else [latex_content]


# Các environment KHÔNG BAO GIỜ được cắt đôi
_PROTECTED_ENVS = {
    'abstract', 'figure', 'figure*', 'table', 'table*',
    'equation', 'equation*', 'align', 'align*',
    'gather', 'gather*', 'multline', 'multline*',
    'enumerate', 'itemize', 'description',
    'theorem', 'lemma', 'proof', 'corollary', 'definition',
    'algorithm', 'algorithmic', 'lstlisting', 'verbatim',
    'tabular', 'tabular*', 'minipage', 'thebibliography',
}


def _split_into_blocks(text: str) -> list[str]:
    """Tách text thành danh sách block, mỗi block là một đơn vị không thể cắt.

    Một block có thể là:
    - Một environment hoàn chỉnh (\\begin{...}...\\end{...})
    - Một section heading + nội dung text cho tới block tiếp theo
    - Một paragraph (cách bởi dòng trống kép)
    """
    blocks: list[str] = []
    pos = 0
    length = len(text)

    while pos < length:
        # Tìm marker tiếp theo: \begin{env} hoặc \section-like
        next_begin = _find_next_begin(text, pos)
        next_section = _find_next_section(text, pos)

        # Xác định marker gần nhất
        candidates = []
        if next_begin is not None:
            candidates.append(('env', next_begin))
        if next_section is not None:
            candidates.append(('sec', next_section))

        if not candidates:
            # Không còn marker nào → phần còn lại là một block
            remainder = text[pos:]
            if remainder.strip():
                blocks.append(remainder)
            break

        marker_type, marker_pos = min(candidates, key=lambda x: x[1])

        # Text trước marker → tách thành paragraph blocks
        if marker_pos > pos:
            pre_text = text[pos:marker_pos]
            if pre_text.strip():
                blocks.extend(_split_by_paragraphs(pre_text))

        if marker_type == 'env':
            # Tìm environment name
            env_match = re.match(r'\\begin\{([^}]+)\}', text[marker_pos:])
            if env_match:
                env_name = env_match.group(1)
                env_end = _find_matching_end(text, marker_pos, env_name)
                if env_end is not None:
                    blocks.append(text[marker_pos:env_end])
                    pos = env_end
                    continue
            # Fallback: không tìm được \end → lấy dòng đơn
            line_end = text.find('\n', marker_pos)
            if line_end == -1:
                line_end = length
            blocks.append(text[marker_pos:line_end + 1])
            pos = line_end + 1

        elif marker_type == 'sec':
            # Section heading: lấy cả dòng heading (tới hết dòng)
            line_end = text.find('\n', marker_pos)
            if line_end == -1:
                line_end = length
            blocks.append(text[marker_pos:line_end + 1])
            pos = line_end + 1

    return blocks


def _find_next_begin(text: str, start: int):
    """Tìm vị trí \\begin{...} tiếp theo từ start. Trả về index hoặc None."""
    match = re.search(r'\\begin\{([^}]+)\}', text[start:])
    if match:
        return start + match.start()
    return None


def _find_next_section(text: str, start: int):
    """Tìm vị trí section/subsection/chapter tiếp theo."""
    match = re.search(r'\\(?:section|subsection|subsubsection|chapter|paragraph|subparagraph)\*?\{', text[start:])
    if match:
        return start + match.start()
    return None


def _find_matching_end(text: str, begin_pos: int, env_name: str):
    """Tìm \\end{env_name} tương ứng, xử lý nesting đúng.

    Trả về vị trí NGAY SAU \\end{env_name} (+ newline nếu có), hoặc None.
    """
    # Escape tên env cho regex (xử lý dấu * trong tên như figure*)
    escaped = re.escape(env_name)
    pattern = re.compile(r'\\(begin|end)\{' + escaped + r'\}')

    depth = 0
    for match in pattern.finditer(text, begin_pos):
        if match.group(1) == 'begin':
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                end_pos = match.end()
                # Bao gồm luôn newline sau \end nếu có
                if end_pos < len(text) and text[end_pos] == '\n':
                    end_pos += 1
                return end_pos
    return None


def _split_by_paragraphs(text: str) -> list[str]:
    """Tách text thuần thành các paragraph block (cách bởi dòng trống kép)."""
    parts = re.split(r'(\n\s*\n)', text)
    blocks: list[str] = []
    current = ""
    for part in parts:
        current += part
        # Nếu part là separator (dòng trống) → flush
        if re.match(r'^\n\s*\n$', part) and current.strip():
            blocks.append(current)
            current = ""
    if current.strip():
        blocks.append(current)
    return blocks if blocks else ([text] if text.strip() else [])


def _find_latex_bin() -> str:
    """Tim duong dan toi thu muc bin cua MiKTeX/TeX Live.

    Tra ve duong dan thu muc chua pdflatex/xelatex, hoac "" neu da co tren PATH.
    Ho tro Windows (MiKTeX/TeX Live) va Linux (texlive-xetex tu apt).
    """
    import shutil
    import sys
    if shutil.which("xelatex") or shutil.which("pdflatex"):
        return ""  # Da co tren PATH

    candidates = []
    if sys.platform == "win32":
        # MiKTeX/TeX Live tren Windows
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidates.append(os.path.join(local_app, "Programs", "MiKTeX", "miktex", "bin", "x64"))
        candidates.append(os.path.join("C:\\", "Program Files", "MiKTeX", "miktex", "bin", "x64"))
        candidates.append(os.path.join("C:\\", "texlive", "2024", "bin", "windows"))
        candidates.append(os.path.join("C:\\", "texlive", "2023", "bin", "windows"))
        exe_suffix = ".exe"
    else:
        # Linux/macOS: TeX Live tu apt hoac MacTeX
        candidates.extend([
            "/usr/bin",
            "/usr/local/bin",
            "/usr/local/texlive/2024/bin/x86_64-linux",
            "/usr/local/texlive/2023/bin/x86_64-linux",
            "/Library/TeX/texbin",  # MacTeX
        ])
        exe_suffix = ""

    for c in candidates:
        if os.path.isfile(os.path.join(c, f"xelatex{exe_suffix}")) or \
           os.path.isfile(os.path.join(c, f"pdflatex{exe_suffix}")):
            return c

    raise RuntimeError(
        "Khong tim thay xelatex/pdflatex. Tren Windows hay cai MiKTeX "
        "(https://miktex.org/download) hoac TeX Live; tren Linux: "
        "'apt install texlive-xetex texlive-fonts-recommended texlive-latex-extra'."
    )


def _copy_bbl_file(source_dir: str, output_dir: str, tex_basename: str):
    """Copy file .bbl goc tu source sang output voi ten phu hop.

    Nhieu bai bao arXiv chi co .bbl (pre-compiled) ma khong co .bib.
    File goc co the la main.bbl nhung output can translated.bbl.
    """
    import shutil
    target_bbl = os.path.join(output_dir, f"{tex_basename}.bbl")

    # Tim file .bbl trong source_dir va output_dir
    for search_dir in [source_dir, output_dir]:
        for f in os.listdir(search_dir):
            if f.endswith(".bbl") and f != f"{tex_basename}.bbl":
                src_bbl = os.path.join(search_dir, f)
                # Chi copy neu file nguon co noi dung (khong rong)
                if os.path.getsize(src_bbl) > 50:
                    shutil.copy2(src_bbl, target_bbl)
                    print(f"[compile] Copied {f} -> {tex_basename}.bbl")
                    return

    # Neu khong tim thay .bbl khac ten, kiem tra xem target_bbl da co va du noi dung chua
    if os.path.exists(target_bbl) and os.path.getsize(target_bbl) > 50:
        return  # Da co san

    print(f"[compile] Warning: No .bbl file found for {tex_basename}")


def _ensure_packages_installed(tex_path: str, latex_bin: str):
    """Quet file .tex, tim tat ca usepackage va cai dat cac package thieu qua MiKTeX.

    Buoc 1: Update filename database (initexmf --update-fndb)
    Buoc 2: Quet tat ca \\usepackage{...} trong file .tex
    Buoc 3: Dung kpsewhich de kiem tra tung package
    Buoc 4: Dung miktex command de cai package thieu
    """
    def _cmd(name):
        return os.path.join(latex_bin, name) if latex_bin else name

    # Buoc 1: Update fndb
    initexmf = _cmd("initexmf")
    try:
        subprocess.run(
            [initexmf, "--update-fndb"],
            capture_output=True, timeout=60,
        )
        print("[compile] Updated MiKTeX filename database")
    except Exception as e:
        print(f"[compile] Warning: initexmf --update-fndb failed: {e}")

    # Buoc 2: Quet usepackage trong file .tex (va cac file \input)
    packages = set()
    try:
        with open(tex_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        # Match \usepackage[options]{pkg1,pkg2,...} va \usepackage{pkg}
        for m in re.finditer(r'\\usepackage(?:\[[^\]]*\])?\{([^}]+)\}', content):
            for pkg in m.group(1).split(","):
                pkg = pkg.strip()
                if pkg:
                    packages.add(pkg)
        # Match \RequirePackage
        for m in re.finditer(r'\\RequirePackage(?:\[[^\]]*\])?\{([^}]+)\}', content):
            for pkg in m.group(1).split(","):
                pkg = pkg.strip()
                if pkg:
                    packages.add(pkg)
    except Exception as e:
        print(f"[compile] Warning: Could not scan packages: {e}")
        return

    if not packages:
        return

    print(f"[compile] Found {len(packages)} packages in .tex file")

    # Buoc 3: Kiem tra tung package co ton tai khong
    kpsewhich = _cmd("kpsewhich")
    missing = []
    for pkg in packages:
        try:
            result = subprocess.run(
                [kpsewhich, f"{pkg}.sty"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                missing.append(pkg)
        except Exception:
            missing.append(pkg)

    if not missing:
        print("[compile] All packages found")
        return

    print(f"[compile] Missing packages: {missing}")

    # Buoc 4: Cai dat tung package thieu
    miktex_cmd = _cmd("miktex")
    for pkg in missing:
        try:
            # Thu cai qua miktex packages install (MiKTeX 25+)
            result = subprocess.run(
                [miktex_cmd, "packages", "install", pkg],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                print(f"[compile] Installed package: {pkg}")
            else:
                # Mot so package nam trong bundle khac ten (vd stfloats -> sttools)
                # Thu dung --enable-installer trong compile se tu xu ly
                print(f"[compile] Could not install '{pkg}' directly (may be auto-installed during compile)")
        except Exception as e:
            print(f"[compile] Warning: Failed to install {pkg}: {e}")

    # Update fndb lan nua sau khi cai packages moi
    try:
        subprocess.run(
            [initexmf, "--update-fndb"],
            capture_output=True, timeout=60,
        )
    except Exception:
        pass


def compile_to_pdf(tex_path: str, output_dir: str) -> str:
    """Compile file .tex thanh PDF.

    Uu tien xelatex (ho tro UTF-8 + tieng Viet tot nhat),
    fallback sang pdflatex neu khong co xelatex.
    """
    os.makedirs(output_dir, exist_ok=True)

    tex_path = os.path.abspath(tex_path)
    output_dir = os.path.abspath(output_dir)
    source_dir = os.path.dirname(tex_path)

    tex_basename = os.path.splitext(os.path.basename(tex_path))[0]

    # Tim latex bin directory
    latex_bin = _find_latex_bin()

    def _cmd(name):
        return os.path.join(latex_bin, name) if latex_bin else name

    # Kiem tra va cai dat packages thieu truoc khi compile
    _ensure_packages_installed(tex_path, latex_bin)

    # Chon compiler: uu tien xelatex cho tieng Viet
    import shutil
    xelatex_path = _cmd("xelatex")
    pdflatex_path = _cmd("pdflatex")
    bibtex_cmd = _cmd("bibtex")

    if latex_bin:
        use_xelatex = os.path.isfile(xelatex_path + ".exe") or os.path.isfile(xelatex_path)
    else:
        use_xelatex = shutil.which("xelatex") is not None

    latex_cmd = xelatex_path if use_xelatex else pdflatex_path
    compiler_name = "xelatex" if use_xelatex else "pdflatex"
    print(f"[compile] Using {compiler_name}")

    env = os.environ.copy()

    # Copy file .bbl goc (pre-compiled bibliography) neu co
    # Vi file tex goc co the la main.tex nhung output la translated.tex,
    # can copy main.bbl -> translated.bbl de giu nguyen references
    _copy_bbl_file(source_dir, output_dir, tex_basename)

    # Lan 1: tao .aux
    r1 = subprocess.run(
        [latex_cmd, "--enable-installer", "-interaction=nonstopmode",
         "-output-directory", output_dir, tex_path],
        capture_output=True, cwd=source_dir, timeout=300, env=env,
    )
    print(f"[compile] {compiler_name} run 1 exit code: {r1.returncode}")

    # Chay bibtex de resolve citations
    bib_env = env.copy()
    bib_env["BSTINPUTS"] = source_dir + os.pathsep + bib_env.get("BSTINPUTS", "")
    bib_env["BIBINPUTS"] = source_dir + os.pathsep + bib_env.get("BIBINPUTS", "")
    subprocess.run(
        [bibtex_cmd, tex_basename],
        capture_output=True, cwd=output_dir, env=bib_env, timeout=120,
    )

    # Copy lai .bbl goc sau bibtex (bibtex co the ghi de bang file rong)
    _copy_bbl_file(source_dir, output_dir, tex_basename)

    # Lan 2 + 3 + 4: resolve references va citations
    # Can 3 lan them (4 tong) vi: run2 doc .bbl, ghi bibcite vao .aux;
    # run3 doc .aux co bibcite, resolve cite; run4 dam bao cross-ref on dinh
    for i in range(3):
        ri = subprocess.run(
            [latex_cmd, "--enable-installer", "-interaction=nonstopmode",
             "-output-directory", output_dir, tex_path],
            capture_output=True, cwd=source_dir, timeout=300, env=env,
        )
        print(f"[compile] {compiler_name} run {i+2} exit code: {ri.returncode}")

    pdf_name = tex_basename + ".pdf"
    pdf_path = os.path.join(output_dir, pdf_name)

    if not os.path.exists(pdf_path):
        # Doc log de bao loi chi tiet hon
        log_path = os.path.join(output_dir, tex_basename + ".log")
        error_detail = ""
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            errors = [l.strip() for l in lines if l.startswith("! ")]
            if errors:
                error_detail = "; ".join(errors[:5])
        raise RuntimeError(
            f"Compile that bai ({compiler_name}), khong tao duoc PDF. "
            f"Loi: {error_detail or 'Xem log tai ' + log_path}"
        )

    return pdf_path
