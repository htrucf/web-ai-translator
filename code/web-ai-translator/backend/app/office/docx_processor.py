"""Extract translatable paragraphs from .docx and inject translations back.

ZIP-faithful design (zipfile + lxml — thay cho python-docx):
- Copy MỌI entry của gói .docx (ZIP) y nguyên byte; chỉ những part XML chứa
  text thân bài mới được parse/sửa. Media, styles, numbering, relationships,
  [Content_Types].xml, theme, settings… đi qua nguyên vẹn — không re-serialize.
- Một "block" = một paragraph (<w:p>) ở BẤT KỲ đâu trong gói: document chính,
  header, footer, footnote, endnote — kể cả paragraph trong bảng, bảng lồng
  nhau và text box (mọi thứ chứa <w:p>).
- Chỉ đọc/ghi node <w:t> (text hiển thị). <w:instrText> (field code),
  <w:fldChar>, hyperlink target (nằm trong rels), numbering và style KHÔNG bao
  giờ bị chạm → field code / hyperlink giữ nguyên.

Inline bold/italic (thẻ định dạng):
- Với paragraph "đơn giản" (chỉ các run text liền nhau, không field/drawing/
  break/hyperlink…), extraction bọc các run in đậm/nghiêng/gạch chân/super-sub
  bằng thẻ [[#k]]...[[/#k]] (xem _common). AI dịch CẢ đoạn nhưng giữ thẻ; lúc
  inject ta cắt theo thẻ và TÁI DỰNG run với đúng <w:rPr> đã lưu → giữ định dạng.
- Với paragraph "phức tạp" (field/drawing/break/hyperlink/nested…) hoặc khi AI
  làm hỏng thẻ: fallback an toàn = ghi cả bản dịch vào <w:t> đầu, làm rỗng phần
  còn lại (giữ định dạng run đầu, mất inline) — đúng như hành vi cũ.
"""

from __future__ import annotations

import os
import zipfile
from copy import deepcopy
from dataclasses import dataclass, field

from app.office._common import (
    inline_open,
    inline_close,
    strip_inline_tags,
    split_inline_segments,
)

try:
    from lxml import etree
except ImportError as e:  # pragma: no cover - lxml luôn đi kèm python-docx
    etree = None  # type: ignore[assignment]
    _IMPORT_ERR: Exception | None = e
else:
    _IMPORT_ERR = None


# ── OOXML namespaces / tag names ─────────────────────────────────────────────
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(name: str) -> str:
    return f"{{{_W_NS}}}{name}"


_T = _w("t")            # <w:t>   — text hiển thị (cái duy nhất ta sửa)
_P = _w("p")            # <w:p>   — paragraph
_R = _w("r")            # <w:r>   — run
_RPR = _w("rPr")        # <w:rPr> — run properties (định dạng inline)
_VAL = _w("val")
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# Run con dấu hiệu "nhấn mạnh" → cần giữ qua thẻ.
_B, _I, _U, _STRIKE, _VERTALIGN = _w("b"), _w("i"), _w("u"), _w("strike"), _w("vertAlign")
# Run chứa các thứ này → KHÔNG tái dựng run được (rebuild sẽ làm mất) → paragraph phức tạp.
_RUN_DISQUAL = frozenset({
    _w("br"), _w("tab"), _w("cr"), _w("drawing"), _w("pict"), _w("object"),
    _w("fldChar"), _w("instrText"), _w("sym"),
    _w("footnoteReference"), _w("endnoteReference"),
})


# Location = chỉ số trỏ vào _DocxPackage.paras.
DocxLocation = int


@dataclass
class DocxBlock:
    block_idx: int
    location: DocxLocation
    text: str
    translated_text: str = ""

    def __post_init__(self):
        if self.text is None:
            self.text = ""


@dataclass
class _Para:
    """Trạng thái mỗi paragraph dịch được, giữ sống giữa extract và inject."""
    para: object                          # phần tử <w:p> trong cây lxml
    t_nodes: list                         # các <w:t> (document order) — cho fallback
    tagged: bool = False                  # có dùng thẻ inline không?
    owned_runs: list = field(default_factory=list)   # dải <w:r> liền nhau sẽ thay
    base_rpr: object = None               # <w:rPr> clone cho segment KHÔNG thẻ
    tag_rpr: dict = field(default_factory=dict)      # tag_id -> <w:rPr> clone


@dataclass
class _DocxPackage:
    """Giữ sống giữa extract và inject (thay vai trò object `doc` của python-docx)."""
    src_path: str
    roots: dict = field(default_factory=dict)   # {part_name -> lxml root} — CHỈ part đã sửa
    paras: list = field(default_factory=list)   # location -> _Para


def _ensure_lxml() -> None:
    if etree is None:
        raise RuntimeError(f"lxml chưa cài đặt: {_IMPORT_ERR}")


def _is_translatable_part(name: str) -> bool:
    """Part XML nào có thể chứa text thân bài: document / header* / footer* /
    footnotes / endnotes. Mọi part khác (styles, numbering, rels, media…) bỏ qua."""
    if not name.startswith("word/") or not name.endswith(".xml"):
        return False
    base = name[len("word/"):]
    if base in ("document.xml", "footnotes.xml", "endnotes.xml"):
        return True
    for stem in ("header", "footer"):
        if base.startswith(stem) and base[len(stem):-4].isdigit():
            return True
    return False


def _collect_text_nodes(node, para, out: list) -> None:
    """Gom <w:t> THUỘC paragraph `para` theo document order, KHÔNG vào paragraph lồng."""
    for child in node:
        tag = child.tag
        if tag == _P and child is not para:
            continue                       # nested paragraph → block riêng
        if tag == _T:
            out.append(child)
            continue
        _collect_text_nodes(child, para, out)


def _paragraph_text_nodes(para) -> list:
    out: list = []
    _collect_text_nodes(para, para, out)
    return out


# ── Phân tích định dạng run (cho cơ chế thẻ inline) ──────────────────────────

def _run_rpr(run):
    return run.find(_RPR)


def _is_emphasized(run) -> bool:
    """Run có in đậm / nghiêng / gạch chân / gạch ngang / super-sub không?"""
    rpr = _run_rpr(run)
    if rpr is None:
        return False
    for child in rpr:
        tag = child.tag
        if tag in (_B, _I, _STRIKE):
            if (child.get(_VAL) or "true") not in ("false", "0", "off"):
                return True
        elif tag == _U:
            if (child.get(_VAL) or "single") != "none":
                return True
        elif tag == _VERTALIGN:
            if child.get(_VAL) in ("superscript", "subscript"):
                return True
    return False


def _rpr_sig(run) -> bytes:
    rpr = _run_rpr(run)
    return etree.tostring(rpr) if rpr is not None else b""


def _run_text(run) -> str:
    return "".join(t.text or "" for t in run.findall(_T))


def _is_simple_para(para, t_nodes) -> bool:
    """Paragraph có thể TÁI DỰNG run an toàn không?

    Điều kiện: mọi <w:t> nằm trong <w:r> con TRỰC TIẾP của para; không run nào
    chứa break/tab/drawing/field/sym…; và các run text liền mạch (không có phần
    tử lạ — hyperlink/bookmark/sdt — xen giữa).
    """
    direct_text_runs = []
    for t in t_nodes:
        r = t.getparent()
        if r is None or r.tag != _R or r.getparent() is not para:
            return False
        if r not in direct_text_runs:
            direct_text_runs.append(r)

    children = list(para)
    for r in direct_text_runs:
        for el in r.iter():
            if el.tag in _RUN_DISQUAL:
                return False

    idxs = [children.index(r) for r in direct_text_runs]
    first, last = min(idxs), max(idxs)
    for c in children[first:last + 1]:
        if c.tag != _R:           # có phần tử lạ xen giữa các run text
            return False
    return True


def _build_tagged_text(para, info: _Para) -> str | None:
    """Dựng chuỗi nguồn có thẻ [[#k]] cho paragraph đơn giản, đồng thời nạp
    `info.owned_runs/base_rpr/tag_rpr`. Trả None nếu KHÔNG có run nhấn mạnh nào
    (khi đó dùng plain + fallback injection)."""
    text_runs = [r for r in para if r.tag == _R and r.find(_T) is not None]
    if not text_runs:
        return None

    children = list(para)
    idxs = [children.index(r) for r in text_runs]
    info.owned_runs = children[min(idxs):max(idxs) + 1]   # gồm cả run rỗng xen giữa
    info.base_rpr = deepcopy(_run_rpr(text_runs[0]))

    # Gộp các run liền nhau có CÙNG <w:rPr> để bớt thẻ.
    groups: list[list] = []   # [sig, emphasized, text, representative_run]
    for r in text_runs:
        sig, emph, txt = _rpr_sig(r), _is_emphasized(r), _run_text(r)
        if groups and groups[-1][0] == sig:
            groups[-1][2] += txt
        else:
            groups.append([sig, emph, txt, r])

    parts: list[str] = []
    next_id = 1
    any_tag = False
    for _sig, emph, txt, r in groups:
        if not txt:
            continue
        if emph:
            info.tag_rpr[next_id] = deepcopy(_run_rpr(r))
            parts.append(inline_open(next_id) + txt + inline_close(next_id))
            next_id += 1
            any_tag = True
        else:
            parts.append(txt)

    if not any_tag:
        info.owned_runs = []
        info.base_rpr = None
        info.tag_rpr = {}
        return None

    info.tagged = True
    return "".join(parts)


def extract_blocks(docx_path: str) -> tuple[list[DocxBlock], _DocxPackage]:
    """Mở .docx và trả về (translatable_blocks, package).

    Caller PHẢI giữ `package` sống — ta sửa nó trong `inject_translations` rồi
    `save_docx` mới copy lại ZIP.
    """
    _ensure_lxml()
    pkg = _DocxPackage(src_path=docx_path)
    blocks: list[DocxBlock] = []

    with zipfile.ZipFile(docx_path) as zin:
        for name in zin.namelist():
            if not _is_translatable_part(name):
                continue
            try:
                root = etree.fromstring(zin.read(name))
            except etree.XMLSyntaxError:
                continue

            touched = False
            for para in root.iter(_P):
                t_nodes = _paragraph_text_nodes(para)
                if not t_nodes:
                    continue
                raw = "".join(t.text or "" for t in t_nodes)
                if not raw.strip():
                    continue

                info = _Para(para=para, t_nodes=t_nodes)
                tagged = _build_tagged_text(para, info) if _is_simple_para(para, t_nodes) else None
                text = (tagged if tagged is not None else raw).strip()

                loc = len(pkg.paras)
                pkg.paras.append(info)
                blocks.append(DocxBlock(block_idx=len(blocks), location=loc, text=text))
                touched = True

            if touched:
                pkg.roots[name] = root   # chỉ re-serialize part đã sửa

    return blocks, pkg


# ── Injection ────────────────────────────────────────────────────────────────

def _make_run(para, text: str, rpr):
    """Tạo <w:r>[<w:rPr>]<w:t xml:space="preserve">text</w:t></w:r> trong ngữ
    cảnh namespace của `para` (để serialize ra đúng prefix `w:`)."""
    r = etree.SubElement(para, _R)        # gắn tạm vào para → thừa nsmap (prefix w)
    if rpr is not None:
        r.append(deepcopy(rpr))
    t = etree.SubElement(r, _T)
    t.text = text
    t.set(_XML_SPACE, "preserve")
    para.remove(r)                        # tách ra, sẽ chèn lại đúng vị trí
    return r


def _inject_simple(info: _Para, text: str) -> None:
    """Ghi cả `text` vào <w:t> đầu, làm rỗng phần còn lại (giữ định dạng run đầu)."""
    t_nodes = info.t_nodes
    if not t_nodes:
        return
    t_nodes[0].text = text
    t_nodes[0].set(_XML_SPACE, "preserve")
    for t in t_nodes[1:]:
        t.text = ""


def _inject_tagged(info: _Para, translated: str) -> bool:
    """Cắt `translated` theo thẻ, tái dựng run với đúng <w:rPr>. Trả False (không
    đụng gì) nếu không tách được segment nào để caller fallback."""
    segments = split_inline_segments(translated)
    if not segments:
        return False

    para = info.para
    new_runs = [
        _make_run(para, text, info.tag_rpr.get(tid) if tid is not None else info.base_rpr)
        for text, tid in segments
    ]

    at = list(para).index(info.owned_runs[0])
    for r in info.owned_runs:
        para.remove(r)
    for offset, r in enumerate(new_runs):
        para.insert(at + offset, r)
    return True


def inject_translations(pkg: _DocxPackage, blocks: list[DocxBlock]) -> int:
    """Ghi block.translated_text vào cây XML. Trả về số block thực sự ghi được.

    Paragraph có thẻ → tái dựng run (giữ inline bold/italic); còn lại hoặc khi
    thẻ hỏng → fallback ghi đơn (strip thẻ thừa để không lọt ra output)."""
    applied = 0
    for block in blocks:
        translated = (block.translated_text or "").strip()
        if not translated:
            continue
        info = pkg.paras[block.location]
        if info.tagged and info.owned_runs and _inject_tagged(info, translated):
            pass
        else:
            _inject_simple(info, strip_inline_tags(translated))
        applied += 1
    return applied


def save_docx(pkg: _DocxPackage, output_path: str) -> None:
    """Copy ZIP gốc byte-for-byte, chỉ thay các part đã sửa bằng XML mới."""
    _ensure_lxml()
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    edited = {
        name: etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True,
        )
        for name, root in pkg.roots.items()
    }

    with zipfile.ZipFile(pkg.src_path) as zin, \
            zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = edited.get(info.filename)
            if data is None:
                data = zin.read(info.filename)
            zout.writestr(info, data)   # giữ nguyên compress_type / date_time / attr
