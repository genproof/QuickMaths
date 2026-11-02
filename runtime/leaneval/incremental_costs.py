"""Cost observations for the current incremental compiler context, kept separate."""
from collections import Counter
import copy
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time

from leaneval import costs, incremental
from leaneval.incremental_progress import read, validate
from leaneval.planning import profile
from leaneval.verifier import environment

LIMIT=40
MAX_SCAN_BYTES=64*1024*1024
SCAN_SECONDS=2.0


def compiler_context(job):
    from leaneval.agent_runner import workbench_path
    cache=job/'workspace/.incremental'
    if cache.is_symlink() or not (cache/'.lake').is_dir():raise ValueError('no incremental cache')
    before=incremental.configuration(cache)
    toolchain=(cache/'lean-toolchain').read_text().strip()
    if toolchain!=profile()['benchmark']['toolchain']:raise ValueError('incremental toolchain mismatch')
    bench=workbench_path()
    prefix=Path(subprocess.check_output(['lean','--print-prefix'],cwd=bench,
                env=environment(bench),text=True,timeout=5).strip())
    binaries={name:hashlib.sha256((prefix/'bin'/name).read_bytes()).hexdigest() for name in ('lean','lake')}
    if incremental.configuration(cache)!=before:raise ValueError('incremental configuration changed during capture')
    return {'prefix':str(prefix),'toolchain':toolchain,'binaries':binaries,'configuration':before}


def attach(job,report,graph,source,tools,before):
    started=time.monotonic();counts=Counter();candidates=[]
    output={'status':'no_matching_incremental_measurements','records':[],
            'accepted':False,'scope':'incremental compiler context only'}
    try:
        paths=list((job/'research-incremental-checks').glob('*/result.json'))
        paths.sort(key=lambda p:(p.stat().st_mtime_ns,str(p)),reverse=True)
        counts['outside_record_bound']=max(0,len(paths)-LIMIT)
        scanned_bytes=0
        for path in paths[:LIMIT]:
            if time.monotonic()-started>SCAN_SECONDS:
                counts['scan_time_limit']+=1;break
            if scanned_bytes+path.stat().st_size>MAX_SCAN_BYTES:
                counts['scan_byte_limit']+=1;break
            raw=read(path,32*1024*1024);scanned_bytes+=len(raw)
            record=json.loads(raw);r=record.get('result')
            if not isinstance(r,dict):counts['invalid_record']+=1;continue
            if r.get('source_files')!=source:counts['different_source']+=1;continue
            finished=r.get('finished_at')
            if type(finished) not in (int,float) or not math.isfinite(finished) or finished>before:
                counts['not_a_prior_measurement']+=1;continue
            identity=dict(r.get('tool_identity') or {});identity.pop('incremental_compiler',None)
            if identity!=tools:counts['different_base_tool_identity']+=1;continue
            if type(r.get('exitcode')) is not int or r['exitcode']!=0 or r.get('outcome_class')!='check_succeeded':
                counts['failed_check']+=1;continue
            ok,reason=validate(job,r)
            if not ok:counts['unverified_record']+=1;continue
            captured_hash=hashlib.sha256(raw).hexdigest()
            if hashlib.sha256(read(path,32*1024*1024)).hexdigest()!=captured_hash:
                counts['changed_record']+=1;continue
            candidates.append((path,r,captured_hash))
        if candidates:
            context=compiler_context(job);eligible=[]
            for path,r,captured_hash in candidates:
                if r['tool_identity'].get('incremental_compiler')!=context:
                    counts['different_incremental_context']+=1;continue
                value=copy.deepcopy(r)
                names=Counter(n.get('target') for stage in value.get('stage_measurements',[]) for n in stage.get('reported_nodes',[]))
                for stage in value.get('stage_measurements',[]):
                    stage['reported_nodes']=[n for n in stage.get('reported_nodes',[]) if names[n.get('target')]==1]
                counts['ambiguous_node_names']+=sum(n>1 for n in names.values())
                eligible.append(value)
                output['records'].append({'path':str(path),'sha256':captured_hash,
                                          'step_id':r['step_id'],'finished_at':r['finished_at']})
            if eligible:
                view={'_timing_graph':graph,'complete':report.get('complete'), 'graph_digest':report.get('graph_digest')}
                costs.attach_history(view,source,{**tools,'incremental_compiler':context},eligible)
                output.update(status='matched_incremental_context',context=context,
                              measurements=view['same_source_environment_measurements'],node_costs=view['node_costs'],
                              critical_path_prediction_seconds=view['critical_path_prediction_seconds'])
                report['route_advice']=(report.get('route_advice','')+' Separate incremental-context observations are available; '
                    'inspect incremental_context_costs before choosing another incremental check. These do not predict a clean or official build.')
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as error:
        output.update(status='incremental_costs_unavailable',error=str(error))
    output.update(selection=dict(counts),collection_seconds=time.monotonic()-started,
                  limitations=['Observed prior costs under the matching incremental configuration.',
                               'Cache traces and concurrent host load may change; no end-to-end guarantee.',
                               'Missing node costs stay unknown; no new compiler check or proof acceptance.'])
    report['incremental_context_costs']=output
