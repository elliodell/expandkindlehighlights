#!/usr/bin/env python3
"""Offline, conservative recovery of truncated ebook highlights. Python 3.10+."""
import argparse
import csv
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
from pathlib import Path
import xml.etree.ElementTree as ET


def normalized(text):
    """Return searchable text and source offsets, including Unicode expansions."""
    result, offsets = [], []
    for i, char in enumerate(text):
        for c in unicodedata.normalize('NFKC', char).casefold():
            if c == '\u00ad':
                continue
            c = {'“': '"', '”': '"', '‘': "'", '’': "'", '–': '-', '—': '-'}.get(c, c)
            if c.isspace():
                c = ' '
                if not result or result[-1] == ' ':
                    continue
            result.append(c)
            offsets.append(i)
    return ''.join(result), offsets


def key(text):
    return re.sub(r'[^\w]+', ' ', normalized(text)[0]).strip()


def read_highlights(path):
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            for index, row in enumerate(reader, 1):
                fields = {key(k or ''): v or '' for k, v in row.items()}
                text = fields.get('highlight', fields.get('text', ''))
                if not text.strip():
                    continue
                yield dict(id=str(index), title=fields.get('book title', fields.get('title', '')),
                           author=fields.get('book author', fields.get('author', '')),
                           text=text, metadata=row)
    else:
        for index, block in enumerate(path.read_text(encoding='utf-8-sig').split('=========='), 1):
            lines = block.strip().splitlines()
            if len(lines) < 3 or not re.search(r'\bhighlight\b', lines[1], re.I):
                continue
            title = lines[0].strip()
            author = re.search(r'\s+\(([^()]*)\)$', title)
            yield dict(id=str(index), title=title[:author.start()] if author else title,
                       author=author.group(1) if author else '', text='\n'.join(lines[2:]).strip(),
                       metadata={'kindle_metadata': lines[1]})


def library_books(root):
    """Use Calibre's metadata database read-only; never modify its files."""
    db = root / 'metadata.db'
    if not db.is_file():
        raise ValueError(f'No Calibre metadata.db in {root}')
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        for ident, title, authors, relative in connection.execute('SELECT id,title,author_sort,path FROM books'):
            directory = (root / relative).resolve()
            if not directory.is_relative_to(root.resolve()):
                continue
            files = sorted(p for p in directory.iterdir() if p.is_file()) if directory.is_dir() else []
            formats = [p for ext in ('.epub', '.txt', '.azw3', '.mobi', '.pdf') for p in files if p.suffix.lower() == ext]
            yield dict(id=ident, title=title, author=authors or '', files=formats)


def extract_text(path, converter=None):
    if path.suffix.lower() == '.txt':
        return path.read_text(encoding='utf-8-sig')
    # Calibre handles EPUB spine order, HTML entities, and other ebook formats.
    executable = converter or shutil.which('ebook-convert')
    mac = Path('/Applications/calibre.app/Contents/MacOS/ebook-convert')
    if not executable and mac.is_file():
        executable = str(mac)
    if not executable:
        raise ValueError('Install Calibre or supply --ebook-convert /path/to/ebook-convert')
    with tempfile.TemporaryDirectory(prefix='expandkindle-') as directory:
        output = Path(directory) / 'book.txt'
        completed = subprocess.run([str(executable), str(path), str(output)],
                                   capture_output=True, text=True, timeout=180)
        if completed.returncode or not output.is_file():
            raise ValueError(f'Calibre conversion failed for {path.name}; it may be DRM-protected or unsupported')
        return output.read_text(encoding='utf-8-sig')


def recover(highlight, source, max_chars=4000, scan_all=False):
    text = highlight.strip()
    marker = re.search(r'(?:\.{3}|…|\[\s*(?:truncated|highlight truncated)\s*\])\s*$', text, re.I)
    if not marker and not scan_all:
        return dict(status='not_flagged', reason='No recognized truncation marker')
    prefix = text[:marker.start()].rstrip() if marker else text
    needle, _ = normalized(prefix)
    needle = needle.strip()
    if len(needle) < 25 or len(needle.split()) < 5:
        return dict(status='too_short', reason='Need at least 25 characters and five words for reliable matching')
    haystack, offsets = normalized(source)
    positions, start = [], 0
    while True:
        start = haystack.find(needle, start)
        if start < 0:
            break
        positions.append(start)
        start += 1
        if len(positions) > 1:
            return dict(status='ambiguous', reason='Passage occurs more than once in this book')
    if not positions:
        return dict(status='not_found', reason='No normalized exact match; check edition or book selection')
    begin = offsets[positions[0]]
    end = offsets[positions[0] + len(needle) - 1] + 1
    # Paragraph completion is a suggestion, never proof of the original selection endpoint.
    boundary = re.search(r'\n\s*\n', source[end: end + max_chars + 1])
    stop = end + boundary.start() if boundary else min(len(source), end + max_chars)
    if not boundary and stop < len(source):
        # Avoid ending midway through a word when the safety cap is reached.
        last_space = source.rfind(' ', end, stop)
        if last_space > end:
            stop = last_space
    proposed = source[begin:stop].strip()
    continuation = source[end:stop].strip()
    if not continuation:
        return dict(status='no_continuation', reason='Matched text ends at a paragraph or book boundary',
                    matched_text=source[begin:end], source_start=begin, source_end=end)
    return dict(status='review_required', reason='Unique match; original selection endpoint is unknown',
                proposed_text=proposed, matched_text=source[begin:end],
                source_start=begin, source_end=stop,
                endpoint='paragraph' if boundary else ('book_end' if stop == len(source) else 'length_cap'))


def run(args):
    highlights = list(read_highlights(args.highlights))
    books = list(library_books(args.library))
    overrides = json.loads(args.book_map.read_text()) if args.book_map else {}
    cache, results = {}, []
    for h in highlights:
        row = {**h, 'original_text': h['text']}
        requested = overrides.get(h['title'])
        candidates = [b for b in books if b['id'] == requested] if requested is not None else [b for b in books if key(b['title']) == key(h['title'])]
        if len(candidates) > 1 and h['author']:
            author_matches = [b for b in candidates if sorted(key(b['author']).split()) == sorted(key(h['author']).split())]
            if author_matches:
                candidates = author_matches
        if len(candidates) != 1:
            row.update(status='book_ambiguous' if candidates else 'book_not_found',
                       candidate_books=[{'id': b['id'], 'title': b['title'], 'author': b['author']} for b in candidates])
        else:
            book = candidates[0]
            row['calibre_id'] = book['id']
            if book['id'] not in cache:
                errors = []
                for file in book['files']:
                    try:
                        cache[book['id']] = (extract_text(file, args.ebook_convert), str(file), None)
                        break
                    except (ValueError, OSError, subprocess.SubprocessError, UnicodeError) as exc:
                        errors.append(str(exc))
                else:
                    cache[book['id']] = ('', '', '; '.join(errors) or 'No supported ebook format')
            source, file, error = cache[book['id']]
            if error:
                row.update(status='extraction_error', reason=error)
            else:
                row.update(source_file=file, **recover(h['text'], source, args.max_chars, args.scan_all))
        results.append(row)
    # Exclusive creation protects previous reports from accidental overwrite.
    with args.output.open('x', encoding='utf-8') as f:
        json.dump({'schema_version': 1, 'highlights': results}, f, ensure_ascii=False, indent=2)
    counts = {}
    for row in results:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    print(json.dumps(counts, indent=2))
    print(f'Review report: {args.output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True, help='Calibre library folder containing metadata.db')
    parser.add_argument('--highlights', type=Path, required=True, help='Readwise CSV or English My Clippings.txt')
    parser.add_argument('--output', type=Path, default=Path('highlight-review.json'))
    parser.add_argument('--book-map', type=Path, help='JSON object mapping export titles to numeric Calibre book IDs')
    parser.add_argument('--ebook-convert', help='Path to Calibre ebook-convert')
    parser.add_argument('--max-chars', type=int, default=4000, help='Maximum suggested continuation characters')
    parser.add_argument('--scan-all', action='store_true', help='Also suggest continuations for unmarked highlights')
    args = parser.parse_args()
    if args.max_chars < 1:
        parser.error('--max-chars must be positive')
    try:
        run(args)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
