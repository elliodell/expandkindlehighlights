#!/usr/bin/env python3
"""Read Mac Kindle selection boundaries and recover locally readable KFX passages."""
import argparse
from collections import Counter
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import sys

DEFAULT_ROOT = Path.home() / 'Library/Containers/com.amazon.Lassen/Data/Library'


def snapshot(path):
    source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    memory = sqlite3.connect(':memory:')
    try:
        source.backup(memory)
    finally:
        source.close()
    memory.row_factory = sqlite3.Row
    return memory


def annotations(root):
    """Prefer modern stores; avoid double-counting the legacy mirror."""
    databases = sorted((root / 'KSDK').rglob('ksdk_annotation_v1.db'))
    records, warnings = {}, []
    for path in databases:
        with contextlib.closing(snapshot(path)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # Local edits supersede synced values; action codes are not assumed.
            for table in ['server_view', 'nonsyncable_annotations', 'local_edit']:
                if table not in tables:
                    continue
                for row in db.execute(f'SELECT * FROM {table}'):
                    ident = (str(path), row['dataset_id'] if 'dataset_id' in row.keys() else row['book_id'], row['annotation_id'])
                    if table == 'local_edit':
                        records.pop(ident, None)
                        warnings.append('A pending local edit was excluded; sync Kindle and rerun.')
                        continue
                    try:
                        payload = json.loads(row['serialized_payload'])
                        if payload.get('type') != 'HIGHLIGHT':
                            continue
                        book = payload['book_data']
                        start = payload['start_position']['shortPosition']
                        end = payload['end_position']['shortPosition']
                        if not isinstance(start, int) or not isinstance(end, int) or end < start:
                            raise ValueError('Invalid position range')
                        records[ident] = dict(id=row['annotation_id'], asin=book['asin'],
                                             content_type=book.get('contentType'), start=start, end=end,
                                             position_type=payload.get('position_type'),
                                             start_long=payload['start_position'].get('longPosition'),
                                             end_long=payload['end_position'].get('longPosition'),
                                             database=str(path), table=table)
                    except (ValueError, KeyError, TypeError):
                        warnings.append('An unsupported annotation record was skipped.')
    if not databases and (root / 'AnnotationStorage').exists():
        with contextlib.closing(snapshot(root / 'AnnotationStorage')) as db:
            for row in db.execute("SELECT * FROM ZANNOTATION WHERE ZRAWANNOTATIONTYPE='highlight'"):
                book = row['ZRAWBOOKID'] or ''
                asin = book.removeprefix('A:').rsplit('-', 1)[0]
                records[('legacy', row['Z_PK'])] = dict(id=str(row['Z_PK']), asin=asin,
                    start=row['ZRAWSTART'], end=row['ZRAWEND'], position_type=None,
                    start_long=row['ZLONGSTART'], end_long=row['ZLONGEND'],
                    database=str(root / 'AnnotationStorage'), table='ZANNOTATION')
    return list(records.values()), sorted(set(warnings))


def cut_range(chunks, start, end):
    """KFX short positions are Unicode character offsets; end is inclusive.

    Preserve segments so reconstructed separators are never mistaken for source text.
    """
    if start < 0 or end < start:
        raise ValueError('Invalid range')
    segments, covered_until, gaps = [], start, []
    for chunk in sorted(chunks, key=lambda c: c.get('position', -1)):
        if chunk.get('type') != 1 or not isinstance(chunk.get('content'), str):
            continue
        position, text = chunk['position'], chunk['content']
        left, right = max(start, position, covered_until), min(end + 1, position + len(text))
        if left >= right:
            continue
        if left > covered_until:
            gaps.append([covered_until, left])
        segments.append(dict(start=left, end_exclusive=right, text=text[left-position:right-position]))
        covered_until = right
    if covered_until < end + 1:
        gaps.append([covered_until, end + 1])
    if not segments:
        return dict(status='unmapped_range', gaps=gaps)
    # Chunk boundaries often represent paragraph or inline formatting boundaries.
    # Report both raw segments and a readable rendering, without asserting typography.
    display = ''
    for segment in segments:
        text = segment['text']
        if display and not display[-1].isspace() and not text[:1].isspace():
            display += ' '
        display += text
    return dict(status='partial_range' if gaps else 'recovered_range', text=display,
                segments=segments, gaps=gaps, endpoint_convention='inclusive',
                review_required=True, reason='Verify endpoint and reconstructed chunk separators in Kindle')


def book_files(root, asin):
    folder = root / 'eBooks' / asin
    if not folder.resolve().is_relative_to((root / 'eBooks').resolve()):
        return []
    return sorted(folder.rglob('*.azw8')) if folder.is_dir() else []


def decode(path, plugin):
    if not plugin.is_file():
        raise ValueError('Install the Calibre KFX Input plugin or supply --kfx-plugin')
    if str(plugin) not in sys.path:
        sys.path.insert(0, str(plugin))
    try:
        from kfxlib import YJ_Book
    except ImportError as exc:
        raise ValueError(f'Missing KFX dependency: {exc}. Install requirements.txt') from exc
    # Do not expose book excerpts in conversion diagnostics.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        data = json.loads(YJ_Book(str(path)).convert_to_json_content())
    return data['data']


def build_report(root, recover=False, plugin=None, only_asin=None):
    rows, warnings = annotations(root)
    if only_asin:
        rows = [r for r in rows if r['asin'] == only_asin]
    books = []
    for asin in sorted({r['asin'] for r in rows}):
        selected = [r for r in rows if r['asin'] == asin]
        files = book_files(root, asin)
        entry = dict(asin=asin, highlight_count=len(selected), downloaded_kfx_files=len(files))
        if recover:
            chunks = None
            error = None
            # Multiple versions cannot safely be chosen just by modification time.
            if len(files) == 1:
                try:
                    chunks = decode(files[0], plugin)
                except Exception as exc:
                    error = f'{type(exc).__name__}: local book could not be decoded; check dependencies, DRM, or format'
            for row in selected:
                if not files:
                    row.update(status='book_not_downloaded')
                elif len(files) > 1:
                    row.update(status='multiple_book_versions')
                elif error:
                    row.update(status='decode_error', reason=error)
                elif row.get('position_type') not in (0, None) or row.get('start_long') or row.get('end_long'):
                    row.update(status='unsupported_position_type')
                else:
                    row.update(**cut_range(chunks, row['start'], row['end']))
        books.append(entry)
    return dict(schema_version=1, mode='recover' if recover else 'inventory',
                scope='Locally synced annotations only; not an account-wide total',
                highlight_count=len(rows), books=books, highlights=rows, warnings=warnings)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['inventory', 'recover'])
    parser.add_argument('--kindle-root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--asin', help='Limit report to one book identifier')
    parser.add_argument('--kfx-plugin', type=Path, default=Path.home() / 'Library/Preferences/calibre/plugins/KFX Input.zip')
    args = parser.parse_args()
    if not args.kindle_root.is_dir():
        parser.error('Kindle data folder not found; install and sync the Mac Kindle app first')
    if args.output.exists():
        parser.error('Output already exists; choose a new filename')
    report = build_report(args.kindle_root, args.command == 'recover', args.kfx_plugin, args.asin)
    with args.output.open('x', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"{report['highlight_count']} locally synced highlights across {len(report['books'])} books")
    print(dict(Counter(r.get('status', 'inventoried') for r in report['highlights'])))
    print(f'Saved {args.output}')


if __name__ == '__main__':
    main()
