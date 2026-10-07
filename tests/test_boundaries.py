"""Boundary tests reuse the proven valid CLI fixture, never accept arbitrary errors."""
import json
import unittest
from unittest.mock import patch
import sys
from pathlib import Path
import test_integrity as fixture
from _portable_helpers import sqlite_connection

sys.path.insert(0, str(fixture.REPO / 'router'))


class BoundaryTests(unittest.TestCase):
    setUp = fixture.IntegrityCLI.setUp
    save_config = fixture.IntegrityCLI.save_config
    build = fixture.IntegrityCLI.build
    route = fixture.IntegrityCLI.route
    assert_error = fixture.IntegrityCLI.assert_error

    def test_healthy_control_before_each_negative(self):
        result = self.route('--evidence-json')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['evidence'][0]['content'].encode(), self.raw)

    def test_excluded_canonical_and_factcard(self):
        # It was indexed before the exclusion was introduced. Retrieval must
        # honour current scope even when old SQLite rows contain the source.
        self.config['exclude_prefixes'] = ['canonical.md']
        self.save_config()
        self.assertIn('Excluded source', self.assert_error(self.route('--json'))['error'])
        self.config['routes']['canonical']['canonical_sources'] = []
        self.save_config()
        (self.ctx/'facts.json').write_text(json.dumps({'cards':[{'id':'x','path':'canonical.md',
            'quote':'alpha marker naïve café','answer':'sample','terms':['alpha','marker'],'evidence_grade':'SUPPORTED'}]}))
        self.assertIn('Excluded source', self.assert_error(self.route('--json'))['error'])

    def test_excluded_directory_not_read_at_index_time(self):
        import build_index
        hidden=self.vault/'private';hidden.mkdir(); secret=hidden/'secret.md';secret.write_bytes(b'alpha DENIED')
        self.config['exclude_prefixes']=['private'];self.save_config()
        original=Path.read_bytes;opened=[]
        def checked(path):
            opened.append(path)
            if path==secret: raise AssertionError('Excluded source was opened')
            return original(path)
        with patch.object(Path,'read_bytes',checked):
            self.assertEqual(build_index.main(['--vault',str(self.vault)]),0)
        self.assertNotIn(secret,opened)
        with sqlite_connection(self.ctx/'index.sqlite') as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE source_path='private/secret.md'").fetchone()[0],0)

    def test_literal_prefix_sibling_is_retained(self):
        for folder in ["deny'_%", "deny'_%sibling"]:
            target=self.vault/folder;target.mkdir();(target/'note.md').write_text('alpha marker '+folder)
        self.config['exclude_prefixes']=["deny'_%"]
        self.config['routes']['canonical']['canonical_sources']=[];self.save_config()
        self.assertEqual(self.build().returncode,0)
        with sqlite_connection(self.ctx/'index.sqlite') as db:
            names={r[0] for r in db.execute('SELECT source_path FROM records')}
        self.assertNotIn("deny'_%/note.md",names)
        self.assertIn("deny'_%sibling/note.md",names)
        result=self.route('--evidence-json');self.assertEqual(result.returncode,0)
        names={e['source_path'] for e in json.loads(result.stdout)['evidence']}
        self.assertIn("deny'_%sibling/note.md",names)

    def test_exclusion_matches_across_case_and_unicode_normalisation(self):
        import unicodedata
        import source_policy
        composed = unicodedata.normalize('NFC', 'Caf\u00e9-Private')
        decomposed = unicodedata.normalize('NFD', composed)
        self.assertNotEqual(composed, decomposed)
        for rule, name in [('private', 'Private/secret.md'), ('PRIVATE/', 'private/secret.md'),
                           (composed, decomposed + '/secret.md'),
                           (decomposed.upper(), composed + '/secret.md')]:
            with self.subTest(rule=rule, name=name):
                self.assertTrue(source_policy.excluded(name, [rule]))
        # Still literal and still a path prefix, never a substring or a wildcard.
        self.assertFalse(source_policy.excluded('Private-notes/a.md', ['private']))
        self.assertFalse(source_policy.excluded('xrivate/a.md', ['_rivate']))
        # End to end: a rule with the wrong case keeps the folder out of the index
        # and out of every packet.
        hidden = self.vault / 'Private'; hidden.mkdir()
        (hidden / 'secret.md').write_bytes(b'alpha marker DENIED-SENTINEL')
        self.config['exclude_prefixes'] = ['private']
        self.config['routes']['canonical']['canonical_sources'] = []
        self.save_config()
        self.assertEqual(self.build().returncode, 0)
        with sqlite_connection(self.ctx/'index.sqlite') as db:
            names = {r[0] for r in db.execute('SELECT source_path FROM records')}
        self.assertNotIn('Private/secret.md', names)
        result = self.route('--evidence-json')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn(b'DENIED-SENTINEL', result.stdout)

    def test_absolute_and_parent_paths_rejected(self):
        for name in [str(self.vault/'canonical.md'), '../outside.md', '.', './']:
            with self.subTest(name=name):
                self.config['routes']['canonical']['canonical_sources']=[name];self.save_config()
                result=self.assert_error(self.route('--json'))
                self.assertIn('Source path',result['error'])

    def test_symlink_after_index_cannot_be_verified_even_with_identical_bytes(self):
        # Identical bytes isolate the path-boundary test from the hash check.
        target=self.vault/'target.md';target.write_bytes(self.raw)
        canonical=self.vault/'canonical.md';canonical.unlink()
        try: canonical.symlink_to(target)
        except OSError: self.skipTest('symlinks unavailable')
        self.assertIn('Symlink',self.assert_error(self.route('--json'))['error'])

    def test_exclusion_entries_that_exclude_nothing_are_refused(self):
        # A-02: "private/ " names a folder called "private " and would exclude nothing,
        # so the private note would be indexed and quoted. The loader refuses it.
        import source_policy
        for key in ('exclude_prefixes', 'retrieval_exclude_prefixes'):
            for entry, says in [('private/ ', "has blanks around it, so it would exclude "
                                              "nothing; write 'private/'"),
                                (' private', "write 'private'"),
                                ('\tprivate/', "write 'private/'"),
                                ('private//x', 'has an empty path component'),
                                ('a/ /b', 'has an empty path component'),
                                ('notes /x', "blanks around the component 'notes '"),
                                ('/private', 'is not a vault-relative POSIX path')]:
                with self.subTest(key=key, entry=entry):
                    with self.assertRaises(source_policy.ConfigError) as caught:
                        source_policy.parse_config(json.dumps({key: [entry]}))
                    self.assertIn(says, str(caught.exception))
        for entry in ('private', 'private/', 'My Notes/', 'archive/2024/', 'x/./y'):
            source_policy.parse_config(json.dumps({'exclude_prefixes': [entry]}))
        # End to end, as reported: nothing is indexed and nothing is searched.
        hidden = self.vault / 'private'; hidden.mkdir()
        (hidden / 'secret.md').write_bytes(b'alpha marker SECRET-SENTINEL')
        self.config['exclude_prefixes'] = ['private/ ']
        self.save_config()
        built = self.build()
        self.assertEqual(built.returncode, 1)
        self.assertIn(b"has blanks around it", built.stderr)
        for method in ('fts', 'synaptic', 'grep'):
            with self.subTest(method=method):
                result = fixture.subprocess.run(
                    [fixture.sys.executable, '-m', 'context_layer.cli', 'search', str(self.vault),
                     '--method', method, '--prompt', 'SECRET-SENTINEL'],
                    cwd=fixture.REPO, capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stdout)['status'], 'ERROR')
                self.assertIn(b'has blanks around it', result.stdout)
                self.assertNotIn(b'SECRET-SENTINEL', result.stdout)

    def test_fast_exclusion_matcher_agrees_with_excluded(self):
        import random
        import source_policy
        rng = random.Random(11)
        parts = ['notes', 'Private', 'private', 'Caf\u00e9', 'Cafe\u0301', 'a b', '.git',
                 'node_modules', 'x_y', "deny'_%", 'ARCHIVE', 'archive']
        prefixes = ['private', 'archive/2024', 'caf\u00e9/', "deny'_%"]
        matcher = source_policy.exclusion_matcher(prefixes)
        for _ in range(2000):
            name = '/'.join(rng.choice(parts) for _ in range(rng.randint(1, 4))) + '.md'
            self.assertEqual(matcher(name), source_policy.excluded(name, prefixes), name)
        for unparseable in ('C:/x.md', 'a\\b.md', '../x.md', '/abs.md', ''):
            self.assertTrue(matcher(unparseable))          # never ranked, never read
        self.assertEqual(source_policy.walked_name_state('private/a\\b.md', prefixes),
                         'excluded')                        # exclusion wins over naming
        self.assertEqual(source_policy.walked_name_state('Meeting: 1.md', prefixes),
                         'unsupported')
        self.assertIsNone(source_policy.walked_name_state('notes/a.md', prefixes))

    def test_invalid_config_and_limits_have_specific_errors(self):
        (self.ctx/'routes.json').write_text('[]')
        self.assertIn('routes object',self.assert_error(self.route('--json'))['error'])
        self.save_config()
        self.assertIn('must be positive',self.assert_error(self.route('--json','--max-sources','-1'))['error'])


class NameRuleFastPaths(unittest.TestCase):
    """relative_name() and the exclusion fold skip pathlib and Unicode normalisation for
    names that are already normal / ASCII; the result must be the slow path's, always."""

    def test_fast_paths_equal_the_slow_definitions_on_random_names(self):
        import random
        import unicodedata
        from pathlib import PurePosixPath
        import source_policy

        def slow_relative(value):
            if not isinstance(value, str) or not value or '\\' in value:
                raise ValueError
            path = PurePosixPath(value)
            if not path.parts or path.is_absolute() or '..' in path.parts or ':' in path.parts[0]:
                raise ValueError
            return path.as_posix().rstrip('/')

        def outcome(function, value):
            try:
                return 'ok', function(value)
            except ValueError:
                return ('error',)

        alphabet = list('aZ./:-_ \\') + ['..', '.', '//', '\u00e9', '\u00c9', '\u00df',
                                          '\u0130', 'e\u0301', '\u212b', '\ufb01', '\u03a3']
        rng = random.Random(11)
        for _ in range(20000):
            value = ''.join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
            self.assertEqual(outcome(source_policy.relative_name, value),
                             outcome(slow_relative, value), value)
            self.assertEqual(source_policy._fold(value), unicodedata.normalize(
                'NFC', unicodedata.normalize('NFC', value).casefold()), value)


if __name__ == '__main__': unittest.main()
