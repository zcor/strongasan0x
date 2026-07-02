"""
Minimal, dependency-free markdown -> HTML renderer for WeeklyRollCall.full_text.

The roll call post format is produced by generate_substack_ode.py and
ingest_roll_call.py and is a constrained subset of markdown written for
Substack: '#'/'##'/'###' headings, '**bold**', '*italic*', '---' horizontal
rules, '- ' bullet lists (with '  - ' nested sub-bullets), '|' pipe tables,
'[text](url)' links, backslash-escaped punctuation (e.g. "0x\\_Vikt0r"), and
verse lines that may end with two-or-more trailing spaces to force a hard
line break within a paragraph.

Security: the source text is user-influenced (attestation text flows into
the post). All text is HTML-escaped up front with django.utils.html.escape
before any markup is layered on top, so no raw HTML/script from the source
can ever reach the page.
"""
import re
from django.utils.html import escape
from django.utils.safestring import mark_safe

_BOLD_RE = re.compile(r'\*\*(.+?)\*\*')
_ITALIC_RE = re.compile(r'(?<!\*)\*([^*\n]+?)\*(?!\*)')
_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')
_HR_RE = re.compile(r'^-{3,}\s*$')
_H_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_BULLET_RE = re.compile(r'^(\s*)-\s+(.*)$')
_TABLE_ROW_RE = re.compile(r'^\s*\|(.+)\|\s*$')
_TABLE_SEP_RE = re.compile(r'^\s*\|?[\s:|-]+\|?\s*$')

# Backslash-escaped markdown punctuation, as produced by the ranking/ode
# tooling for names containing underscores or pipes (e.g. "0x\_Vikt0r").
# Escaped characters are swapped for placeholders in the Unicode Private Use
# Area before inline parsing runs, so they're treated as literal text rather
# than markup, then restored at the end. PUA codepoints cannot occur in the
# source text since it was HTML-escaped before reaching this module.
_ESCAPABLE_CHARS = ['\\', '_', '*', '|', '[', ']', '(', ')', '#']
_ESCAPE_RE = re.compile('\\\\([' + ''.join(re.escape(c) for c in _ESCAPABLE_CHARS) + '])')
_PUA_BASE = 0xE000
_PLACEHOLDER_RE = re.compile('[' + chr(_PUA_BASE) + '-' + chr(_PUA_BASE + 0x2FF) + ']')


def _protect_escapes(text):
    """Replace a backslash-escaped char with a single PUA placeholder codepoint."""
    return _ESCAPE_RE.sub(lambda m: chr(_PUA_BASE + ord(m.group(1))), text)


def _restore_escapes(text):
    """Turn placeholders back into their literal characters."""
    return _PLACEHOLDER_RE.sub(lambda m: chr(ord(m.group(0)) - _PUA_BASE), text)


def _inline(text):
    """Apply inline formatting (links, bold, italic) to already-escaped text."""
    text = _protect_escapes(text)
    text = _LINK_RE.sub(r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', text)
    text = _BOLD_RE.sub(r'<strong>\1</strong>', text)
    text = _ITALIC_RE.sub(r'<em>\1</em>', text)
    text = _restore_escapes(text)
    return text


def _split_table_cells(row):
    """Split a pipe-table row into its cell strings, dropping the leading/trailing empties.

    A backslash-escaped pipe ('\\|', e.g. in "Jones \\| Rarestone Compass") must not be
    treated as a column separator, so escapes are protected before splitting and each
    cell is returned still holding its placeholder (restored later by `_inline`).
    """
    protected = _protect_escapes(row)
    parts = protected.split('|')
    if parts and parts[0].strip() == '':
        parts = parts[1:]
    if parts and parts[-1].strip() == '':
        parts = parts[:-1]
    return [_restore_escapes(p.strip()) for p in parts]


def render_roll_call_markdown(raw_text):
    """
    Convert WeeklyRollCall.full_text (markdown) into safe HTML.

    Returns a django SafeString. The input is escaped BEFORE any markdown
    conversion runs, so embedded HTML/script tags in the source are rendered
    as inert text rather than executed.
    """
    if not raw_text:
        return mark_safe('')

    # Escape first -- everything below operates on already-safe text and only
    # ever (re)introduces a small, fixed allowlist of tags.
    escaped = escape(raw_text)
    lines = escaped.split('\n')

    html_parts = []
    paragraph_buf = []
    list_stack = []  # indent levels of currently open <ul> elements
    table_buf = []  # raw table lines (not yet split into cells)

    def flush_paragraph():
        if not paragraph_buf:
            return
        # Within a paragraph, a line ending in 2+ trailing spaces forces a
        # <br>; otherwise consecutive lines are joined with a single space
        # (soft break), matching how the ode's verse lines render in
        # Substack/CommonMark.
        rendered_lines = []
        for line in paragraph_buf:
            hard_break = line.endswith('  ')
            rendered_lines.append((line.rstrip(), hard_break))
        out = []
        for i, (line, hard_break) in enumerate(rendered_lines):
            out.append(_inline(line))
            is_last = i == len(rendered_lines) - 1
            if not is_last:
                out.append('<br>' if hard_break else ' ')
        html_parts.append('<p>' + ''.join(out) + '</p>')
        paragraph_buf.clear()

    def close_lists(upto_level=-1):
        while list_stack and list_stack[-1] > upto_level:
            html_parts.append('</ul>')
            list_stack.pop()

    def flush_table():
        if not table_buf:
            return
        # First row = header, second row (if a '---|---' separator) is skipped, rest = body.
        raw_rows = list(table_buf)
        table_buf.clear()
        if not raw_rows:
            return
        header_cells = _split_table_cells(raw_rows[0])
        body_rows = raw_rows[1:]
        if body_rows and _TABLE_SEP_RE.match(body_rows[0]):
            body_rows = body_rows[1:]

        out = ['<div class="rc-table-wrap"><table class="rc-table">']
        out.append('<thead><tr>')
        for cell in header_cells:
            out.append(f'<th>{_inline(cell)}</th>')
        out.append('</tr></thead>')
        out.append('<tbody>')
        for row in body_rows:
            cells = _split_table_cells(row)
            out.append('<tr>')
            for cell in cells:
                out.append(f'<td>{_inline(cell)}</td>')
            out.append('</tr>')
        out.append('</tbody></table></div>')
        html_parts.append(''.join(out))

    for line in lines:
        stripped = line.strip()

        # Blank line: paragraph/list/table boundary.
        if stripped == '':
            flush_paragraph()
            close_lists()
            flush_table()
            continue

        # Table row (pipe-delimited).
        if _TABLE_ROW_RE.match(line):
            flush_paragraph()
            close_lists()
            table_buf.append(line)
            continue
        else:
            flush_table()

        # Horizontal rule.
        if _HR_RE.match(stripped):
            flush_paragraph()
            close_lists()
            html_parts.append('<hr>')
            continue

        # Heading.
        h_match = _H_RE.match(stripped)
        if h_match:
            flush_paragraph()
            close_lists()
            level = min(len(h_match.group(1)), 6)
            content = _inline(h_match.group(2).strip())
            html_parts.append(f'<h{level}>{content}</h{level}>')
            continue

        # Bullet list item (possibly nested via leading spaces).
        b_match = _BULLET_RE.match(line)
        if b_match:
            flush_paragraph()
            indent = len(b_match.group(1))
            level = indent // 2  # 0 = top-level, 1 = nested, ...
            content = _inline(b_match.group(2).strip())
            while list_stack and list_stack[-1] > level:
                html_parts.append('</ul>')
                list_stack.pop()
            if not list_stack or list_stack[-1] < level:
                html_parts.append('<ul>')
                list_stack.append(level)
            html_parts.append(f'<li>{content}</li>')
            continue
        else:
            close_lists()

        # Regular paragraph text (verse lines, attestation prose, etc).
        paragraph_buf.append(line)

    flush_paragraph()
    close_lists()
    flush_table()

    return mark_safe('\n'.join(html_parts))
