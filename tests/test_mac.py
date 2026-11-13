import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from mac_kindle import annotations, cut_range, build_report

class MacTests(unittest.TestCase):
    def test_inclusive_range(self):
        result = cut_range([{'type': 1, 'position': 10, 'content': 'Hello world'}], 10, 14)
        self.assertEqual(result['text'], 'Hello')
        self.assertEqual(result['status'], 'recovered_range')

    def test_unicode_positions(self):
        result = cut_range([{'type': 1, 'position': 0, 'content': 'A😀éB'}], 1, 2)
        self.assertEqual(result['text'], '😀é')

    def test_gap_is_not_exact(self):
        result = cut_range([{'type': 1, 'position': 10, 'content': 'abc'},
                            {'type': 1, 'position': 15, 'content': 'def'}], 10, 17)
        self.assertEqual(result['status'], 'partial_range')
        self.assertEqual(result['gaps'], [[13, 15]])

    def test_missing_and_overlapping(self):
        self.assertEqual(cut_range([], 0, 5)['status'], 'unmapped_range')
        result = cut_range([{'type': 1, 'position': 0, 'content': 'abcdef'},
                            {'type': 1, 'position': 3, 'content': 'defghi'}], 0, 8)
        self.assertEqual(''.join(s['text'] for s in result['segments']), 'abcdefghi')

    def test_non_object_payloads_do_not_abort_inventory(self):
        valid_payload = json.dumps(dict(type='HIGHLIGHT', book_data={'asin': 'TEST'},
                                        start_position={'shortPosition': 5},
                                        end_position={'shortPosition': 10}, position_type=0))
        for table in ['server_view', 'nonsyncable_annotations']:
            for payload in [None, True, False, 0, 1.5, 'text', [], ['HIGHLIGHT']]:
                with self.subTest(table=table, payload=payload), tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    folder = root / 'KSDK' / 'account'
                    folder.mkdir(parents=True)
                    with sqlite3.connect(folder / 'ksdk_annotation_v1.db') as db:
                        db.execute(f'CREATE TABLE {table} (dataset_id TEXT, annotation_id TEXT, serialized_payload TEXT)')
                        db.executemany(f'INSERT INTO {table} VALUES (?,?,?)', [
                            ('book', 'malformed', json.dumps(payload)),
                            ('book', 'valid', valid_payload),
                        ])
                    report = build_report(root)
                    self.assertEqual(report['highlight_count'], 1)
                    self.assertEqual(report['highlights'][0]['id'], 'valid')
                    self.assertEqual(report['warnings'], ['An unsupported annotation record was skipped.'])

    def test_invalid_short_positions_do_not_abort_recovery(self):
        invalid_ranges = [(-1, 5), (-2, -1), (5, 4), (False, 5), (0, True),
                          (None, 5), (0, None), ('0', 5), (0, 1.5)]
        for table in ['server_view', 'nonsyncable_annotations']:
            for start, end in invalid_ranges:
                with self.subTest(table=table, start=start, end=end), tempfile.TemporaryDirectory() as d:
                    root = Path(d)
                    folder = root / 'KSDK' / 'account'
                    folder.mkdir(parents=True)
                    book = root / 'eBooks' / 'TEST' / 'book.azw8'
                    book.parent.mkdir(parents=True)
                    book.touch()
                    valid = dict(type='HIGHLIGHT', book_data={'asin': 'TEST'},
                                 start_position={'shortPosition': 0},
                                 end_position={'shortPosition': 0}, position_type=0)
                    invalid = dict(valid, start_position={'shortPosition': start},
                                   end_position={'shortPosition': end})
                    with sqlite3.connect(folder / 'ksdk_annotation_v1.db') as db:
                        db.execute(f'CREATE TABLE {table} (dataset_id TEXT, annotation_id TEXT, serialized_payload TEXT)')
                        db.executemany(f'INSERT INTO {table} VALUES (?,?,?)', [
                            ('book', 'invalid', json.dumps(invalid)),
                            ('book', 'valid', json.dumps(valid)),
                        ])
                    with patch('mac_kindle.decode', return_value=[{'type': 1, 'position': 0, 'content': 'Hello'}]):
                        report = build_report(root, recover=True)
                    self.assertEqual(report['highlight_count'], 1)
                    self.assertEqual(report['highlights'][0]['id'], 'valid')
                    self.assertEqual(report['highlights'][0]['status'], 'recovered_range')
                    self.assertEqual(report['highlights'][0]['text'], 'H')
                    self.assertEqual(report['warnings'], ['An unsupported annotation record was skipped.'])

    def test_invalid_legacy_ranges_do_not_abort_recovery(self):
        for start, end in [(-1, 5), (5, 4), (None, 5), (0, None), ('bad', 5), (0, 1.5)]:
            with self.subTest(start=start, end=end), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                book = root / 'eBooks' / 'TEST' / 'book.azw8'
                book.parent.mkdir(parents=True)
                book.touch()
                with sqlite3.connect(root / 'AnnotationStorage') as db:
                    db.execute('CREATE TABLE ZANNOTATION (Z_PK INTEGER, ZRAWANNOTATIONTYPE TEXT, '
                               'ZRAWBOOKID TEXT, ZRAWSTART, ZRAWEND, ZLONGSTART, ZLONGEND)')
                    db.executemany('INSERT INTO ZANNOTATION VALUES (?,?,?,?,?,?,?)', [
                        (1, 'highlight', 'A:TEST-EBOK', start, end, None, None),
                        (2, 'highlight', 'A:TEST-EBOK', 0, 0, None, None),
                    ])
                with patch('mac_kindle.decode', return_value=[{'type': 1, 'position': 0, 'content': 'Hello'}]):
                    report = build_report(root, recover=True)
                self.assertEqual(report['highlight_count'], 1)
                self.assertEqual(report['highlights'][0]['id'], '2')
                self.assertEqual(report['highlights'][0]['text'], 'H')
                self.assertEqual(report['warnings'], ['An unsupported annotation record was skipped.'])

    def test_modern_dedup_and_pending(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            folder = root / 'KSDK' / 'account'
            folder.mkdir(parents=True)
            db = sqlite3.connect(folder / 'ksdk_annotation_v1.db')
            for table in ['server_view', 'local_edit']:
                db.execute(f'CREATE TABLE {table} (dataset_id TEXT, annotation_id TEXT, serialized_payload TEXT)')
            payload = json.dumps(dict(type='HIGHLIGHT', book_data={'asin': 'TEST'},
                                      start_position={'shortPosition': 5}, end_position={'shortPosition': 10}, position_type=0))
            db.execute('INSERT INTO server_view VALUES (?,?,?)', ('book', 'id', payload))
            db.commit()
            rows, _ = annotations(root)
            self.assertEqual(len(rows), 1)
            report = build_report(root, True, Path('missing-plugin'))
            self.assertEqual(report['highlights'][0]['status'], 'book_not_downloaded')
            db.execute('INSERT INTO local_edit VALUES (?,?,?)', ('book', 'id', payload))
            db.commit()
            rows, warnings = annotations(root)
            self.assertEqual(rows, [])
            self.assertTrue(warnings)
            db.close()

if __name__ == '__main__':
    unittest.main()
