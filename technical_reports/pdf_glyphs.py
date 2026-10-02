"""Repair only visually verified, byte-bound embedded Type1 glyph mappings."""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _timestamp(value):
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def glyph_unicode(name):
    """Glyph semantics, independent of a sidecar's proposed replacement."""
    base = name.split('.', 1)[0]
    # TeX size variants and assembled delimiter pieces need semantic text,
    # while the exact original shape is retained in the archived PDF.
    for stem, character in (('parenleft', '('), ('parenright', ')'),
                            ('bracketleft', '['), ('bracketright', ']'),
                            ('braceleft', '{'), ('braceright', '}'),
                            ('ceilingleft', '⌈'), ('ceilingright', '⌉'),
                            ('floorleft', '⌊'), ('floorright', '⌋'),
                            ('radical', '√'),
                            ('slash', '/')):
        if re.fullmatch(re.escape(stem) + r'(?:big|Big|bigg|Bigg)', base):
            return character
    known = {
        'vextenddouble': '∥', 'vextendsingle': '|', 'barex': '│',
        'summationdisplay': '∑', 'summationtext': '∑',
        'productdisplay': '∏', 'producttext': '∏',
        'integraldisplay': '∫', 'integraltext': '∫',
        'uniondisplay': '∪', 'uniontext': '∪',
        'radicalbig': '√', 'radicalBig': '√', 'radicalbt': '√',
        'radicaltp': '│', 'radicalvertex': '│',
        'hatwide': '^', 'hatwider': '^', 'hatwidest': '^',
        'tildewide': '~', 'tildewider': '~', 'tildewidest': '~',
        'parenlefttp': '⎛', 'parenleftbt': '⎝',
        'parenleftex': '⎜', 'parenrightex': '⎟',
        'parenrighttp': '⎞', 'parenrightbt': '⎠',
        'bracketlefttp': '⎡', 'bracketleftex': '⎢', 'bracketleftbt': '⎣',
        'bracketrighttp': '⎤', 'bracketrightex': '⎥', 'bracketrightbt': '⎦',
        'bracelefttp': '⎧', 'braceleftmid': '⎨', 'braceleftbt': '⎩',
        'bracerighttp': '⎫', 'bracerightmid': '⎬', 'bracerightbt': '⎭',
        'braceex': '⎪',
    }
    if base in known:
        return known[base]
    if re.fullmatch(r'uni(?:[0-9A-Fa-f]{4})+', base):
        try:
            value = bytes.fromhex(base[3:]).decode('utf-16-be', 'strict')
        except (ValueError, UnicodeError):
            return None
    elif re.fullmatch(r'u[0-9A-Fa-f]{4,6}', base):
        try:
            value = chr(int(base[1:], 16))
        except ValueError:
            return None
    else:
        try:
            from pypdf._codecs.adobe_glyphs import adobe_glyphs
            value = adobe_glyphs.get('/' + base)
        except ImportError:
            value = None
    # Private-use codepoints have no portable text semantics. In particular,
    # TeX horizontal-brace pieces need a full-page visual description of their
    # expression/label relationship, rather than opaque PUA characters.
    if not value or re.search(r'[\x00-\x1f\x7f\ufffd\ud800-\udfff\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]', value):
        return None
    return value


def _object(value):
    return value.get_object() if hasattr(value, 'get_object') else value


def _fonts(resources, visited=None):
    """Traverse page and Form XObject resources without following arbitrary refs."""
    resources = _object(resources) or {}
    visited = visited if visited is not None else set()
    if id(resources) in visited:
        return
    visited.add(id(resources))
    for font in (_object(resources.get('/Font')) or {}).values():
        yield _object(font)
    for reference in (_object(resources.get('/XObject')) or {}).values():
        obj = _object(reference)
        if obj.get('/Subtype') == '/Form' and obj.get('/Resources'):
            yield from _fonts(obj['/Resources'], visited)


def _embedded_encoding(data):
    # Type1 Encoding is in the clear-text header. Never inspect encrypted
    # charstrings as if random bytes were declarations of glyph semantics.
    prefix = data.split(b'currentfile eexec', 1)[0]
    encoding = {}
    for code, name in re.findall(rb'\bdup\s+(\d+)\s+/([A-Za-z0-9_.]+)\s+put\b', prefix):
        code, name = int(code), name.decode('ascii')
        if code in encoding and encoding[code] != name:
            raise RuntimeError('conflicting embedded Type1 Encoding declarations')
        encoding[code] = name
    return encoding


def _shown_bytes(value):
    if isinstance(value, bytes):
        return value
    # Parsed PDF text strings remember the original source bytes, regardless
    # of whether the parser represents them as Unicode or a byte string.
    try:
        return value.original_bytes
    except (AttributeError, ValueError):
        return None


def _semantic_match(actual, expected, glyph_name=None):
    """Accept a standard glyph variation without deleting it from the text."""
    if actual == expected:
        return True
    if not isinstance(actual, str) or not isinstance(expected, str) or not expected:
        return False
    base = actual
    if base and (0xFE00 <= ord(base[-1]) <= 0xFE0F or 0xE0100 <= ord(base[-1]) <= 0xE01EF):
        base = base[:-1]
    # These finite equivalences apply only to a known embedded glyph name.
    # Keep the original accent/combining-stroke codepoint in extracted text.
    name = (glyph_name or '').split('.', 1)[0]
    variants = {
        'vextendsingle': {'|', '∣', '│', '\u20d2', '\u20d3'},
        'vextenddouble': {'∥', '‖', '\u20e6'},
        'barex': {'│', '|', '∣'},
    }
    if re.fullmatch(r'hat(?:wide|wider|widest)', name):
        allowed = {'^', 'ˆ', '\u0302'}
    elif re.fullmatch(r'tilde(?:wide|wider|widest)', name):
        allowed = {'~', '˜', '\u0303'}
    else:
        allowed = variants.get(name, {expected})
    return base in allowed


def _used_font_bytes(reader, page):
    """Inspect only bytes actually drawn by text operators, including Forms."""
    from pypdf.generic import ContentStream
    found = {}

    def visit(content, resources, active_font=None, ancestors=None):
        if content is None:
            return
        resources = _object(resources) or {}
        ancestors = set(ancestors or ())
        font = active_font
        stack = []
        operations = content.operations if hasattr(content, 'operations') else ContentStream(content, reader).operations
        for operands, operator in operations:
            if operator == b'q':
                stack.append(font)
            elif operator == b'Q':
                font = stack.pop() if stack else active_font
            elif operator == b'Tf':
                fonts = _object(resources.get('/Font')) or {}
                font = _object(fonts.get(operands[0]))
            elif operator in (b'Tj', b'TJ', b"'", b'"') and font is not None:
                family = str(font.get('/BaseFont')).lstrip('/').rsplit('+', 1)[-1]
                if not re.fullmatch(r'CMEX\d+|txexs', family, re.I):
                    continue
                candidate = found.setdefault(id(font), {'font': font, 'codes': set(), 'source_bytes_unavailable': False})
                strings = operands[0] if operator == b'TJ' else [operands[-1]]
                for value in strings:
                    if not isinstance(value, (str, bytes)):
                        continue
                    raw = _shown_bytes(value)
                    if raw is None:
                        candidate['source_bytes_unavailable'] = True
                    else:
                        candidate['codes'].update(raw)
            elif operator == b'Do':
                objects = _object(resources.get('/XObject')) or {}
                form = _object(objects.get(operands[0]))
                if form is not None and form.get('/Subtype') == '/Form' and id(form) not in ancestors:
                    visit(form, form.get('/Resources') or resources, font, ancestors | {id(form)})

    visit(page.get_contents(), page.get('/Resources'))
    return found.values()


def diagnose_glyph_mappings(reader):
    """Read-only audit for silently incorrect mathematical text mappings.

    A valid Unicode string alone cannot certify an original: an incomplete
    CMEX cmap commonly turns drawn sums/integrals into ASCII X/Z and norm bars
    into layout controls. Unknown repairs still require visual verification.
    """
    from pypdf._cmap import get_encoding
    scanned, issues = [], []
    for number, page in enumerate(reader.pages, 1):
        for candidate in _used_font_bytes(reader, page):
            font, codes = candidate['font'], sorted(candidate['codes'])
            descriptor = _object(font.get('/FontDescriptor')) or {}
            embedded = _object(descriptor.get('/FontFile'))
            cmap = _object(font.get('/ToUnicode'))
            identity = {'page': number, 'base_font': str(font.get('/BaseFont')),
                        'used_code_hex': [f'{code:02X}' for code in codes]}
            scanned.append(identity)
            if (font.get('/Subtype') != '/Type1' or embedded is None or cmap is None
                    or candidate['source_bytes_unavailable']):
                issues.append(dict(identity, error='used mathematical font lacks inspectable source bytes, embedded Type1 Encoding or ToUnicode'))
                continue
            declarations = _embedded_encoding(embedded.get_data())
            encoding, mapping = get_encoding(font)
            for code in codes:
                name = declarations.get(code)
                expected = glyph_unicode(name) if name else None
                if isinstance(encoding, dict):
                    encoded = encoding.get(code)
                else:
                    try:
                        encoded = bytes([code]).decode(encoding, 'strict')
                    except (UnicodeError, LookupError):
                        encoded = None
                actual = mapping.get(encoded, encoded)
                invalid = (not isinstance(actual, str) or not actual
                           or re.search(r'[\x00-\x1f\x7f\ufffd\ud800-\udfff\ue000-\uf8ff]', actual))
                if expected is None or invalid or not _semantic_match(actual, expected, name):
                    issues.append({'page': number, 'base_font': identity['base_font'],
                        'code_hex': f'{code:02X}', 'glyph_name': name, 'actual_unicode': actual,
                        'expected_unicode': expected,
                        'font_file_sha256': _sha(embedded.get_data()),
                        'to_unicode_sha256': _sha(cmap.get_data()),
                        'error': 'used mathematical glyph has an unverified missing or incorrect text mapping'})
    return {'method': 'read_only_used_embedded_math_glyph_audit', 'scanned_fonts': scanned,
            'issues': issues, 'complete': not issues}


def _bound_file(root, path, sha, description):
    if not isinstance(path, str):
        raise RuntimeError(description + ' has no bound file path')
    file = (root / path).resolve()
    if (not file.is_relative_to(root) or not file.is_file()
            or _sha(file.read_bytes()) != sha):
        raise RuntimeError(description + ' does not match its bound bytes')
    return file


def _verified_visual_glyph(mapping, font, font_bytes, cmap_bytes, pdf_sha, root):
    """Unknown glyph names need a checked rendering of the exact source font.

    a4/a24 are arbitrary names across font families. These shapes must never
    become global Adobe-glyph aliases or unconditional replacements.
    """
    family = str(font.get('/BaseFont')).lstrip('/').rsplit('+', 1)[-1]
    shapes = {'LCIRCLE10': {'a4': '╮', 'a5': '╯', 'a6': '╰', 'a7': '╭'},
              'LINE10': {'a24': '╱'}}
    expected = shapes.get(family, {}).get(mapping.get('glyph_name'))
    proof = mapping.get('visual_glyph_evidence') or {}
    if (expected is None or mapping.get('unicode') != expected
            or proof.get('reviewer') != 'agent'
            or proof.get('scope') != 'isolated_embedded_glyph'
            or not _timestamp(proof.get('read_at'))
            or proof.get('source_pdf_sha256') != pdf_sha
            or proof.get('font_file_sha256') != _sha(font_bytes)
            or not isinstance(proof.get('notes'), str) or not proof['notes'].strip()):
        raise RuntimeError('unknown glyph lacks exact source-font visual semantics')
    _bound_file(root, proof.get('image_path'), proof.get('image_sha256'), 'isolated glyph image')
    gallery_path = _bound_file(root, proof.get('gallery_pdf_path'),
                               proof.get('gallery_pdf_sha256'), 'isolated glyph gallery')
    from pypdf import PdfReader
    gallery = PdfReader(gallery_path)
    number = proof.get('gallery_page')
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= len(gallery.pages):
        raise RuntimeError('isolated glyph gallery page is invalid')
    page = gallery.pages[number - 1]
    # The proof gallery must contain only the one glyph, so a screenshot cannot
    # accidentally bind the proposed code to an unrelated label or example.
    content = page.get_contents().get_data()
    match = re.fullmatch(rb'\s*BT\s+(/[A-Za-z0-9_.]+)\s+[0-9.]+\s+Tf\s+'
                         rb'[-0-9.]+\s+[-0-9.]+\s+Td\s+<([0-9A-Fa-f]{2})>\s+Tj\s+ET\s*', content)
    if not match or int(match.group(2), 16) != int(mapping['code_hex'], 16):
        raise RuntimeError('isolated gallery does not draw only the verified source byte')
    resources = _object(page.get('/Resources')) or {}
    gallery_fonts = _object(resources.get('/Font')) or {}
    reference = gallery_fonts.get(match.group(1).decode('ascii'))
    if reference is None or len(gallery_fonts) != 1:
        raise RuntimeError('isolated gallery does not select one exact source font')
    rendered_font = _object(reference)
    descriptor = _object(rendered_font.get('/FontDescriptor')) or {}
    embedded = _object(descriptor.get('/FontFile'))
    cmap = _object(rendered_font.get('/ToUnicode'))
    if (rendered_font.get('/Subtype') != '/Type1'
            or rendered_font.get('/BaseFont') != font.get('/BaseFont')
            or rendered_font.get('/Encoding') != font.get('/Encoding')
            or embedded is None or _sha(embedded.get_data()) != _sha(font_bytes)
            or cmap is None or _sha(cmap.get_data()) != _sha(cmap_bytes)):
        raise RuntimeError('isolated gallery font does not match the source font bytes')
    return expected


def apply_verified_glyphs(reader, path, root):
    """Append verified cmap entries in memory; never alter original PDF bytes."""
    path, root = Path(path), Path(root).resolve()
    sidecar = path.with_suffix('.glyphs.json')
    if not sidecar.exists():
        return None
    record = json.loads(sidecar.read_text(encoding='utf-8'))
    if (record.get('pdf_sha256') != _sha(path.read_bytes())
            or record.get('reviewer') != 'agent'
            or record.get('scope') != 'font_glyph_mapping'
            or not _timestamp(record.get('read_at'))
            or not isinstance(record.get('notes'), str) or not record['notes'].strip()):
        raise RuntimeError('invalid PDF-bound glyph verification: ' + str(sidecar))
    checked = set()
    for page in record.get('visual_checked_pages', []):
        number = page.get('page')
        if (not isinstance(number, int) or isinstance(number, bool)
                or not 1 <= number <= len(reader.pages)
                or page.get('reviewer') != 'agent' or page.get('scope') != 'glyph_verification'
                or not _timestamp(page.get('read_at'))
                or not isinstance(page.get('notes'), str) or not page['notes'].strip()
                or not isinstance(page.get('image_path'), str)):
            raise RuntimeError('glyph sidecar lacks a valid visual page verification')
        _bound_file(root, page['image_path'], page.get('image_sha256'), 'glyph visual page image')
        checked.add(number)
    if not checked or not record.get('fonts'):
        raise RuntimeError('glyph sidecar lacks visually checked embedded fonts')
    candidates = []
    seen = {}
    for number, page in enumerate(reader.pages, 1):
        for font in _fonts(page.get('/Resources')):
            if id(font) not in seen:
                candidate = {'font': font, 'pages': set()}
                candidates.append(candidate)
                seen[id(font)] = candidate
            seen[id(font)]['pages'].add(number)
    changes = []
    # Validate everything before mutating any in-memory cmap.
    pending = []
    scheduled = set()
    for proof in record['fonts']:
        matches = []
        for candidate in candidates:
            font = candidate['font']
            if str(font.get('/BaseFont')) != proof.get('base_font'):
                continue
            descriptor = _object(font.get('/FontDescriptor')) or {}
            file = _object(descriptor.get('/FontFile'))
            cmap = _object(font.get('/ToUnicode'))
            if (font.get('/Subtype') != '/Type1' or file is None or cmap is None
                    or _sha(file.get_data()) != proof.get('font_file_sha256')
                    or _sha(cmap.get_data()) != proof.get('to_unicode_sha256')):
                continue
            matches.append((candidate, file.get_data(), cmap.get_data()))
        if not matches:
            raise RuntimeError('glyph font identity does not match PDF bytes: ' + str(proof.get('base_font')))
        mappings = proof.get('mappings')
        if not isinstance(mappings, list) or not mappings:
            raise RuntimeError('glyph verification has no explicit mappings')
        for candidate, font_bytes, cmap_bytes in matches:
            if id(candidate['font']) in scheduled:
                raise RuntimeError('duplicate glyph verification for an embedded font')
            if not candidate['pages'].intersection(checked):
                raise RuntimeError('glyph font has no visually verified representative page')
            encoding, entries, codes = _embedded_encoding(font_bytes), [], set()
            for mapping in mappings:
                code_hex = mapping.get('code_hex', '')
                name, value = mapping.get('glyph_name'), mapping.get('unicode')
                if (not isinstance(code_hex, str) or not re.fullmatch(r'[0-9A-Fa-f]{2}', code_hex)
                        or not isinstance(name, str) or not isinstance(value, str)):
                    raise RuntimeError('invalid single-byte Type1 glyph code')
                code = int(code_hex, 16)
                if (code in codes or encoding.get(code) != name
                        or mapping.get('encoding_evidence') != f'dup {code} /{name} put'):
                    raise RuntimeError('glyph semantics do not match embedded Encoding: ' + code_hex)
                expected = glyph_unicode(name)
                if expected is None:
                    expected = _verified_visual_glyph(mapping, candidate['font'], font_bytes, cmap_bytes,
                                                       record['pdf_sha256'], root)
                if expected != value:
                    raise RuntimeError('glyph semantics do not match embedded Encoding: ' + code_hex)
                codes.add(code)
                entries.append((code_hex.upper(), value.encode('utf-16-be').hex().upper()))
            if cmap_bytes.count(b'endcmap') != 1:
                raise RuntimeError('verified font has ambiguous ToUnicode cmap')
            blocks = []
            for start in range(0, len(entries), 100):
                batch = entries[start:start + 100]
                blocks.append(str(len(batch)) + ' beginbfchar\n' + '\n'.join(
                    '<' + code + '> <' + unicode + '>' for code, unicode in batch) + '\nendbfchar\n')
            revised = cmap_bytes.replace(b'endcmap', ''.join(blocks).encode('ascii') + b'endcmap')
            pending.append((candidate['font'], revised))
            scheduled.add(id(candidate['font']))
            changes.append({'base_font': proof['base_font'], 'font_file_sha256': proof['font_file_sha256'],
                            'original_to_unicode_sha256': proof['to_unicode_sha256'],
                            'mapping_count': len(entries), 'pages': sorted(candidate['pages']),
                            'visual_checked_pages': sorted(candidate['pages'].intersection(checked)),
                            'mappings': mappings})
    from pypdf.generic import DecodedStreamObject, NameObject
    for font, revised in pending:
        stream = DecodedStreamObject()
        stream.set_data(revised)
        font[NameObject('/ToUnicode')] = stream
    return {'method': 'embedded_type1_encoding_verified_cmap', 'sidecar': str(sidecar),
            'pdf_sha256': record['pdf_sha256'], 'fonts': changes, 'verification': record,
            'original_pdf_unchanged': True}
