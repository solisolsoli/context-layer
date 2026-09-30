"""Common budgets, evidence identity and installed-interface behavior."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import test_integrity as fixture


class ComparisonTests(unittest.TestCase):
    setUp=fixture.IntegrityCLI.setUp
    save_config=fixture.IntegrityCLI.save_config
    build=fixture.IntegrityCLI.build
    def adapter(self,method,*flags):
        return subprocess.run([sys.executable,str(fixture.REPO/'eval/retrieve.py'),'--vault',str(self.vault),
            '--method',method,*flags,'alpha marker'],capture_output=True)

    def test_all_methods_obey_same_output_budget(self):
        for method in ['grep','fts','fts-canonical','router']:
            with self.subTest(method=method):
                run=self.adapter(method,'--budget','12','--per-source','8','--top-k','1')
                self.assertEqual(run.returncode,0,run.stdout)
                packet=json.loads(run.stdout)
                self.assertEqual(packet['schema'],'evidence-delivery-v1')
                self.assertEqual(packet['operation_status'],'ok')
                self.assertEqual(len(packet['evidence']),1)
                self.assertLessEqual(sum(len(e['content']) for e in packet['evidence']),12)
                self.assertLessEqual(len(packet['evidence'][0]['content']),8)
                self.assertIn(packet['evidence'][0]['content'].encode(),self.raw)

    def test_no_method_delivers_a_stale_source(self):
        # Lexical methods withhold the changed source on its own and name it; the
        # experimental router withholds its whole packet. None emits the new bytes.
        (self.vault/'canonical.md').write_bytes(self.raw+b'changed')
        for method in ['grep','fts','fts-canonical']:
            with self.subTest(method=method):
                run=self.adapter(method);self.assertEqual(run.returncode,0,run.stdout)
                packet=json.loads(run.stdout)
                self.assertNotIn('canonical.md',[e['source_path'] for e in packet['evidence']])
                self.assertIn('canonical.md',[w['source_path'] for w in packet['withheld']])
                self.assertNotIn(b'changed',run.stdout.replace(b'changed since indexing',b''))
        run=self.adapter('router');self.assertEqual(run.returncode,1)
        self.assertEqual(json.loads(run.stdout)['evidence'],[])

    def test_search_command_is_fts_by_default(self):
        run=subprocess.run([sys.executable,'-m','context_layer.cli','search',str(self.vault),
            '--prompt','alpha marker'],cwd=fixture.REPO,capture_output=True)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(run.stdout),json.loads(self.adapter('fts').stdout))

    def test_malformed_config_is_structured_failure_for_every_method(self):
        (self.ctx/'routes.json').write_text('[]')
        for method in ['grep','fts','fts-canonical','router']:
            with self.subTest(method=method):
                run=self.adapter(method)
                self.assertEqual(run.returncode,1)
                packet=json.loads(run.stdout)
                self.assertEqual(packet['evidence'],[])
                self.assertIn('routes object',packet['error'])

    def test_inapplicable_flags_are_usage_errors(self):
        # E-08: a flag that changes nothing for the method is refused (exit 2, one line
        # naming it), as `packet build` and `install` refuse it; never a silent exit 0.
        refused=[('fts',['--budget-tokens','400','--compact'],'--compact applies only to --method synaptic'),
                 ('fts',['--max-hops','2'],'--max-hops applies only to --method synaptic'),
                 ('grep',['--extra-tokens','80'],'--extra-tokens applies only to --method synaptic'),
                 ('fts-canonical',['--record-query'],'--record-query applies only to --method synaptic'),
                 ('synaptic',['--budget-tokens','400'],'--budget-tokens sizes only the --compact synaptic packet'),
                 ('synaptic',['--compact','--extra-tokens','80'],'--extra-tokens sizes the default synaptic packet'),
                 # F2-07: the advisor's side channel exists for fts and the default synaptic packet only
                 ('grep',['--jev-candidates','3'],'--jev-candidates applies to --method fts and the default synaptic'),
                 ('fts-canonical',['--jev-candidates','3'],'--jev-candidates applies to --method fts and the default synaptic'),
                 ('router',['--jev-candidates','3'],'--jev-candidates applies to --method fts and the default synaptic'),
                 ('synaptic',['--compact','--jev-candidates','3'],'--jev-candidates applies to --method fts and the default synaptic')]
        for method,flags,says in refused:
            with self.subTest(method=method,flags=flags):
                run=self.adapter(method,*flags)
                self.assertEqual(run.returncode,2,run.stdout)
                self.assertEqual(run.stdout,b'')
                self.assertEqual(len(run.stderr.strip().splitlines()),1)
                self.assertIn(says.encode(),run.stderr)
        cli=subprocess.run([sys.executable,'-m','context_layer.cli','search',str(self.vault),'--method','fts',
            '--budget-tokens','400','--compact','--prompt','alpha marker'],cwd=fixture.REPO,capture_output=True)
        self.assertEqual(cli.returncode,2)
        self.assertIn(b'--compact applies only to --method synaptic',cli.stderr)
        for flags in (['--compact','--budget-tokens','400'],['--extra-tokens','80','--max-hops','2','--record-query'],
                      ['--jev-candidates','3']):
            with self.subTest(flags=flags):
                self.assertEqual(self.adapter('synaptic',*flags).returncode,0)
        self.assertEqual(self.adapter('fts','--jev-candidates','3').returncode,0)

    def test_prompt_file_and_stdin_match_the_argument(self):
        expected=json.loads(self.adapter('fts').stdout)
        prompt=self.vault/'prompt.txt';prompt.write_text('alpha marker',encoding='utf-8')
        base=[sys.executable,str(fixture.REPO/'eval/retrieve.py'),'--vault',str(self.vault),'--method','fts']
        from_file=subprocess.run(base+['--prompt-file',str(prompt)],capture_output=True)
        from_stdin=subprocess.run(base+['--prompt-file','-'],input=b'alpha marker',capture_output=True)
        for run in (from_file,from_stdin):
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(json.loads(run.stdout),expected)
        for argv in (base,base+['--prompt-file',str(prompt),'alpha'],
                     base+['--prompt-file',str(self.vault/'missing.txt')]):
            with self.subTest(argv=argv[-2:]):
                self.assertEqual(subprocess.run(argv,capture_output=True).returncode,2)


class CommittedResults(unittest.TestCase):
    def test_results_json_reproduces_from_this_tree(self):
        # F2-02: eval/comparison/results.json is the committed output of eval/compare.py. A
        # retrieval change that moves a cost or a hit count must regenerate it in the same
        # change (see eval/comparison/README.md); the CI job compares the same three keys.
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'out';home=Path(temp)/'home';home.mkdir()
            run=subprocess.run([sys.executable,str(fixture.REPO/'eval/compare.py'),'--out',str(out)],
                cwd=fixture.REPO,capture_output=True,text=True,encoding='utf-8',env=dict(os.environ,HOME=str(home)))
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            fresh=json.loads((out/'summary.json').read_text(encoding='utf-8'))
        committed=json.loads((fixture.REPO/'eval/comparison/results.json').read_text(encoding='utf-8'))
        for key in ('results','bounds','contract_sha256','per_case'):
            with self.subTest(key=key):
                self.assertEqual(fresh[key],committed[key],f'eval/comparison/results.json is stale ({key}); regenerate it: '
                    'python3 eval/compare.py --out DIR && cp DIR/summary.json eval/comparison/results.json')


if __name__=='__main__':unittest.main()
