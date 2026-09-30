#!/usr/bin/env python3
"""Run the frozen synthetic comparison; creates no live-vault state."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from evidence_contract import validate_contract

HERE=Path(__file__).resolve().parent
METHODS=['grep','fts','fts-canonical','router']
# summary.json is what eval/comparison/results.json commits: the per-case rows keep only these
# fields (no artifact paths), so the file does not depend on where it was produced.
PER_CASE_FIELDS=['id','expected_count','hit_count','cost_chars','split','answerable','group_hits',
                 'delivery_pass','correct_abstention','case_pass','errors']


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',required=True,type=Path)
    args=p.parse_args()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=True)
    frozen=HERE/'comparison'
    contract=validate_contract(json.loads((frozen/'contract.json').read_text(encoding='utf-8')))
    results={}
    with tempfile.TemporaryDirectory(prefix='context-comparison-') as td:
        vault=Path(td)/'vault';vault.mkdir()
        for name,source in contract['sources'].items():
            path=vault/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(source['text'].encode('utf-8'))
        (vault/'.context').mkdir();shutil.copy2(frozen/'routes.json',vault/'.context/routes.json')
        build=subprocess.run([sys.executable,str(HERE.parent/'router/build_index.py'),'--vault',str(vault)],capture_output=True,text=True,encoding='utf-8')
        if build.returncode: raise RuntimeError(build.stdout+build.stderr)
        for method in METHODS:
            command=[sys.executable,str(HERE/'retrieve.py'),'--method',method,'--vault',str(vault),
                     '--top-k','3','--budget','6000','--per-source','2000']
            target=out/(method+'.json')
            run=subprocess.run([sys.executable,str(HERE/'evaluate.py'),'--command',shlex.join(command),
                '--stimuli',str(frozen/'stimuli.jsonl'),'--evidence-contract',str(frozen/'contract.json'),
                '--facets','split,answerable','--packets-dir',str(out/(method+'-packets')),'--out',str(target),'--quiet'],capture_output=True,text=True,encoding='utf-8')
            (out/(method+'.log')).write_text(run.stdout+run.stderr,encoding='utf-8')
            if run.returncode: raise RuntimeError(method+' evaluator failed')
            results[method]=json.loads(target.read_text(encoding='utf-8'))
    summary={'scope':'synthetic demonstration; semantic quality not measured','python':platform.python_version(),
        'contract_sha256':hashlib.sha256((frozen/'contract.json').read_bytes()).hexdigest(),
        'code_sha256':{str(path.relative_to(HERE.parent)):hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [HERE/'retrieve.py',HERE/'evaluate.py',HERE/'evidence_contract.py',HERE.parent/'router/context_router.py',HERE.parent/'router/source_policy.py']},
        'bounds':{'top_k':3,'content_characters':6000,'per_source':2000},
        'results':{m:{'summary':r['summary'],'by_split':r['by_facet']['split']} for m,r in results.items()},
        'promotion_eligible':False,
        'per_case':{m:[{k:case[k] for k in PER_CASE_FIELDS} for case in r['results']] for m,r in results.items()}}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for method,result in results.items():
        s=result['summary'];print(f"{method}: {s['total_hit']}/{s['total_expected']} groups, {s['delivery_passes']} complete, {s['correct_abstentions']} correct abstentions, {s['router_failures']} errors")
    return 0


if __name__=='__main__':raise SystemExit(main())
