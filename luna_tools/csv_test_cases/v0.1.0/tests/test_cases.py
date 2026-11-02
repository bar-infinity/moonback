"""Corpus consistency assertions only; no production validator or external I/O."""
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
HEADER = ['record_id', 'label', 'quantity']


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate manifest key')
        result[key] = value
    return result


def fixture_path(case):
    name = case['file']
    if not re.fullmatch(r'samples/csv-[0-9]{3}-[a-z0-9-]+\.csv', name):
        raise ValueError('Invalid fixture filename')
    path = ROOT / name
    if path.is_symlink() or path.resolve().parent != (ROOT / 'samples').resolve():
        raise ValueError('Invalid fixture location')
    return path


def parsed_rows(case):
    with fixture_path(case).open(encoding='utf-8', errors='strict', newline='') as source:
        return list(csv.reader(source, delimiter=',', quotechar='"',
                               doublequote=True, escapechar=None,
                               skipinitialspace=False, strict=True,
                               quoting=csv.QUOTE_MINIMAL))


def structure_issues(rows):
    issues = []
    if not rows or rows[0] != HEADER:
        issues.append({'record': 1, 'code': 'header'})
    for number, row in enumerate(rows, 1):
        if len(row) != len(HEADER):
            issues.append({'record': number, 'code': 'field_count',
                           'expected': len(HEADER), 'actual': len(row)})
    return issues


def quality_issues(rows):
    """Small assertions for this corpus's declared schema, not a public tool."""
    if structure_issues(rows):
        raise ValueError('Business checks require valid structure')
    issues = []
    seen = set()
    for number, (ident, label, quantity) in enumerate(rows[1:], 2):
        if re.fullmatch(r'T[0-9]{3}', ident) is None:
            issues.append({'record': number, 'column': 'record_id', 'code': 'id_format'})
        if ident in seen:
            issues.append({'record': number, 'column': 'record_id', 'code': 'duplicate_id'})
        seen.add(ident)
        if not label.strip():
            issues.append({'record': number, 'column': 'label', 'code': 'required_value'})
        if re.fullmatch(r'0|[1-9][0-9]*', quantity) is None:
            issues.append({'record': number, 'column': 'quantity', 'code': 'nonnegative_integer'})
    return issues


class CSVCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((ROOT / 'cases.json').read_text(encoding='utf-8'),
                                  object_pairs_hook=unique_object)
        cls.cases = cls.manifest['cases']
        cls.by_id = {case['id']: case for case in cls.cases}

    def test_manifest_version_provenance_and_usage(self):
        self.assertEqual(self.manifest['schema_version'], '1.0')
        self.assertEqual(self.manifest['version'], ROOT.name[1:])
        self.assertEqual(self.manifest['publication_status'], 'local_prototype_not_published')
        self.assertEqual(self.manifest['language'], 'en')
        self.assertEqual(self.manifest['license']['spdx_id'], 'CC0-1.0')
        self.assertIn('synthetic', self.manifest['provenance']['data_origin'])
        self.assertEqual({ref['id'] for ref in self.manifest['provenance']['references']},
                         {'rfc4180', 'python-csv'})
        for ref in self.manifest['provenance']['references']:
            self.assertTrue(ref['url'].startswith('https://'))
        for filename in ('README.md', 'LICENSE.md'):
            self.assertIn('CC0-1.0', (ROOT / filename).read_text())

    def test_manifest_has_exactly_ten_unique_cases_and_no_orphan_csv(self):
        self.assertEqual(len(self.cases), 10)
        self.assertEqual(len(self.by_id), len(self.cases))
        self.assertEqual(set(self.by_id), {'csv-%03d' % i for i in range(1, 11)})
        paths = [fixture_path(case) for case in self.cases]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(set(paths), set((ROOT / 'samples').glob('*.csv')))
        self.assertEqual({p for p in (ROOT / 'samples').iterdir()}, set(paths))
        for case in self.cases:
            for key in ('name', 'purpose', 'features'):
                self.assertTrue(case[key])
            self.assertEqual(case['profile_id'], self.manifest['profile']['id'])

    def test_declared_profile_matches_reference_reader_settings(self):
        profile = self.manifest['profile']
        self.assertEqual(profile['encoding'], 'utf-8')
        self.assertIs(profile['bom'], False)
        self.assertEqual(profile['record_separator'], 'CRLF')
        self.assertIs(profile['final_record_separator'], True)
        self.assertEqual(profile['structure'], {'header': HEADER, 'field_count': 3})
        self.assertEqual(profile['reference_parser'], dict(
            implementation='Python standard library csv.reader', python_minimum='3.9',
            open_newline='', delimiter=',', quotechar='"', doublequote=True,
            escapechar=None, skipinitialspace=False, strict=True, quoting='QUOTE_MINIMAL'))
        self.assertEqual(profile['business_rules']['record_id']['pattern'], 'T[0-9]{3}')
        self.assertIs(profile['business_rules']['record_id']['required'], True)
        self.assertIs(profile['business_rules']['record_id']['unique'], True)
        self.assertIs(profile['business_rules']['label']['required'], True)
        self.assertIs(profile['business_rules']['quantity']['required'], True)
        self.assertEqual(profile['business_rules']['quantity']['pattern'], '0|[1-9][0-9]*')

    def test_manifest_paths_cannot_escape_sample_directory(self):
        for value in ('../private.csv', '/tmp/private.csv', 'samples/../../private.csv',
                      'samples/csv-001-test.csv/extra', 'samples/csv-001-test.csv?x=1'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                fixture_path({'file': value})

    def test_original_byte_lengths_hashes_and_line_endings(self):
        for case in self.cases:
            with self.subTest(case=case['id']):
                raw = fixture_path(case).read_bytes()
                self.assertEqual(len(raw), case['byte_length'])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), case['sha256'])
                self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
                self.assertTrue(raw.endswith(b'\r\n'))
                remainder = raw.replace(b'\r\n', b'')
                self.assertNotIn(b'\r', remainder)
                self.assertNotIn(b'\n', remainder)
                raw.decode('utf-8', errors='strict')
                self.assertEqual(case['expected']['decode'], 'success')

    def test_parser_outcomes_and_all_expected_string_cells(self):
        for case in self.cases:
            expected = case['expected']['parse']
            with self.subTest(case=case['id']):
                if expected['outcome'] == 'error':
                    self.assertEqual(expected['error_category'], 'csv.Error')
                    with self.assertRaises(csv.Error):
                        parsed_rows(case)
                    self.assertIsNone(expected['rows'])
                    self.assertIsNone(expected['data_record_count'])
                else:
                    self.assertEqual(expected['outcome'], 'success')
                    self.assertIsNone(expected['error_category'])
                    rows = parsed_rows(case)
                    self.assertEqual(rows, expected['rows'])
                    self.assertEqual(len(rows) - 1, expected['data_record_count'])
                    self.assertTrue(all(type(cell) is str for row in rows for cell in row))

    def test_structure_outcomes_and_exact_issue_locations(self):
        for case in self.cases:
            stages = case['expected']
            with self.subTest(case=case['id']):
                if stages['parse']['outcome'] == 'error':
                    self.assertEqual(stages['structure'], {'outcome': 'not_evaluated', 'issues': []})
                else:
                    issues = structure_issues(parsed_rows(case))
                    self.assertEqual(stages['structure']['issues'], issues)
                    self.assertEqual(stages['structure']['outcome'], 'fail' if issues else 'pass')

    def test_business_quality_outcomes_and_exact_issue_locations(self):
        for case in self.cases:
            stages = case['expected']
            with self.subTest(case=case['id']):
                if stages['structure']['outcome'] != 'pass':
                    self.assertEqual(stages['quality'], {'outcome': 'not_evaluated', 'issues': []})
                else:
                    issues = quality_issues(parsed_rows(case))
                    self.assertEqual(stages['quality']['issues'], issues)
                    self.assertEqual(stages['quality']['outcome'], 'fail' if issues else 'pass')

    def test_classification_follows_stages_not_blanket_invalid_csv(self):
        counts = {'valid': 0, 'syntax_error': 0, 'data_quality': 0}
        for case in self.cases:
            stages = case['expected']
            derived = ('syntax_error' if stages['parse']['outcome'] == 'error' else
                       'data_quality' if stages['structure']['outcome'] == 'fail' or
                       stages['quality']['outcome'] == 'fail' else 'valid')
            self.assertEqual(case['classification'], derived)
            counts[derived] += 1
        self.assertEqual(counts, {'valid': 5, 'syntax_error': 2, 'data_quality': 3})

    def test_quoted_comma_and_doubled_quote_preserve_literal_text(self):
        self.assertIn(b'"Sample item, compact"', fixture_path(self.by_id['csv-002']).read_bytes())
        self.assertEqual(parsed_rows(self.by_id['csv-002'])[1][1], 'Sample item, compact')
        self.assertIn(b'""A""', fixture_path(self.by_id['csv-003']).read_bytes())
        self.assertEqual(parsed_rows(self.by_id['csv-003'])[1][1], 'Sample item "A"')

    def test_multiline_is_one_logical_record_with_preserved_crlf(self):
        case = self.by_id['csv-004']
        self.assertEqual(len(fixture_path(case).read_bytes().splitlines()), 3)
        rows = parsed_rows(case)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], 'Sample item\r\nsecond line')

    def test_utf8_roundtrip_preserves_non_ascii_labels(self):
        labels = [row[1] for row in parsed_rows(self.by_id['csv-005'])[1:]]
        self.assertEqual(labels, ['試作部品', 'Δοκιμή', 'Pièce-école'])
        self.assertEqual([s.encode('utf-8').decode('utf-8') for s in labels], labels)

    def test_parsed_records_roundtrip_without_type_inference(self):
        for case in self.cases:
            if case['expected']['parse']['outcome'] != 'success':
                continue
            with self.subTest(case=case['id']):
                rows = parsed_rows(case)
                destination = io.StringIO(newline='')
                csv.writer(destination, lineterminator='\r\n').writerows(rows)
                self.assertEqual(list(csv.reader(io.StringIO(destination.getvalue(), newline=''),
                                                strict=True)), rows)

    def test_quality_assertions_reject_boundary_mutations(self):
        self.assertEqual(quality_issues([HEADER, ['T001', 'Sample', '0']]), [])
        for value in ('-1', '+1', '01', '1.0', 'one', ' 1', '１', ''):
            with self.subTest(quantity=value):
                issues = quality_issues([HEADER, ['T001', 'Sample', value]])
                self.assertEqual([issue['code'] for issue in issues], ['nonnegative_integer'])
        self.assertEqual(quality_issues([HEADER, ['T001', ' ', '1']])[0]['code'], 'required_value')
        self.assertEqual(quality_issues([HEADER, [' T001', 'Sample', '1']])[0]['code'], 'id_format')
        with self.assertRaises(ValueError):
            quality_issues([HEADER, ['T001', 'Sample']])


if __name__ == '__main__':
    unittest.main()
