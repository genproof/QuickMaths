"""Bind checkpoint types to controller-written inspection records.

The records are diagnostics, never official proof acceptance. Manual notes stay
available separately without being promoted to compiler-derived types.
"""
from pathlib import Path
import hashlib
import json
import re

from leaneval.artifacts import digest
from leaneval.verifier import atomic_json


def safe(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Unsafe controller type receipt path')
    return path


def receipt_path(job, step):
    if not isinstance(step, str) or not re.fullmatch('[0-9a-f]{32}', step):
        raise ValueError('Invalid inspection identity')
    return safe(Path(job)/'research-type-receipts'/(step+'.json'))


def load(path):
    path = safe(path)
    if not path.is_file() or path.stat().st_size > 256*1024:
        raise ValueError('Missing or oversized controller type receipt')
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict): raise ValueError('Expected receipt object')
    return value


def evidence_kind(contract):
    if contract.get('kind') == 'compiler-type-and-diagnostic':
        return {'kind': 'controller-inspected-type'}
    if contract.get('kind') != 'partial-compiler-type':
        raise ValueError('Unsupported type evidence')
    probe = contract.get('partial_probe_sha256')
    code = contract.get('module_exitcode')
    if (not isinstance(probe, str) or not re.fullmatch('[0-9a-f]{64}', probe)
            or type(code) is not int or contract.get('candidate_checked') is not False):
        raise ValueError('Invalid partial type evidence')
    return {'kind': 'controller-inspected-partial-type', 'partial_probe_sha256': probe,
            'module_exitcode': code, 'candidate_checked': False}


def seal(job, result):
    partial = result.get('action') == 'inspect_partial'
    full = result.get('action') == 'inspect'
    if not full and not partial: return None
    if (type(result.get('exitcode')) is not int or result.get('timed_out')
            or result.get('source_current') is not True
            or result.get('checked_files_unchanged') is not True
            or result.get('invocation_id') != result.get('step_id')):
        return None
    if full:
        if result.get('scope') != 'type-and-axiom-diagnostic' or result['exitcode'] != 0:
            return None
        kind = {'kind': 'controller-inspected-type'}
    else:
        if (result.get('scope') != 'partial-type-diagnostic'
                or result.get('phase') != 'partial_declaration_inspection'
                or result.get('partial_source_prefix_exact') is not True
                or result['exitcode'] not in (0, 1)):
            return None
        try:
            kind = evidence_kind({**result, 'kind': 'partial-compiler-type'})
        except ValueError:
            return None
    types = result.get('exact_types')
    source = result.get('source_files')
    if (not isinstance(types, list) or not types or any(not isinstance(t,str) or not t for t in types)
            or len(set(types)) != 1 or len(types[0]) > 100000
            or not isinstance(source, dict) or not source
            or result.get('checked_files') != source):
        return None
    query = result.get('query', '')
    if not isinstance(query, str) or query.count(':') != 1: return None
    module, target = query.split(':')
    name = r'[A-Za-z_][A-Za-z0-9_\']*(?:\.[A-Za-z_][A-Za-z0-9_\']*)*'
    if (not re.fullmatch(name, module) or not re.fullmatch(name, target)
            or not (module == 'Submission' or module.startswith('Submission.'))):
        return None
    path = receipt_path(job, result['step_id'])
    value = {'schema_version': 1, 'job': str(safe(job)), 'step_id': result['step_id'],
             'module': module, 'target': target, 'remaining_type': types[0],
             'source_sha256': digest(source), **kind, 'accepted': False}
    value['receipt_digest'] = digest(value)
    if path.exists():
        if load(path) != value: raise ValueError('Inspection identity already has different evidence')
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(path, value)
    return {'path': str(path), 'receipt_digest': value['receipt_digest']}


def validate(job, contract, source):
    if not contract: return False, 'missing_contract'
    if contract.get('source_files') != source: return False, 'source_inventory_not_current'
    try:
        kind = evidence_kind(contract)
        receipt = load(receipt_path(job, contract.get('step_id')))
        claimed = receipt.pop('receipt_digest', None)
        if claimed != digest(receipt): return False, 'receipt_digest_mismatch'
        expected = {'schema_version': 1, 'job': str(safe(job)), 'step_id': contract['step_id'],
                    'module': contract.get('module'), 'target': contract.get('target'),
                    'remaining_type': contract.get('remaining_type'),
                    'source_sha256': digest(source), **kind, 'accepted': False}
        if receipt != expected or contract.get('source_sha256') != digest(source):
            return False, 'contract_differs_from_inspection'
        if contract.get('invocation_id') != contract['step_id']:
            return False, 'invocation_mismatch'
    except (OSError, ValueError, TypeError, KeyError):
        return False, 'controller_receipt_unavailable'
    return True, 'controller_receipt_matches'


def pending(contract, reason):
    if not contract: return None
    value = {'kind': 'unverified-residual-note', 'reason': reason, 'accepted': False,
             'compiler_type_verified': False}
    for key in ('target', 'module', 'remaining_type', 'owner', 'next_check'):
        item = contract.get(key)
        if isinstance(item, str): value[key] = item[:5000]
    value['required_action'] = ('Choose the exact residual declaration and its owning module, then run '
        './research capture --module Submission.Module --declaration Namespace.name '
        '--next-check "a concrete next check". A manual remaining_type is a claim, not a Lean type receipt.')
    return value


def resolve(job, contract, source, inherited):
    """Retain validated types across unchanged-source generations, without
    duplicating the potentially large full source inventory in each contract.
    """
    if contract:
        valid, reason = validate(job, contract, source)
        if not valid:
            from leaneval.auto_residual import current
            automatic=current(job,contract,source)
            if automatic:
                return resolve(job,automatic,source,{})
            if inherited:
                candidates, receipts, _ = resolve(job,{},source,inherited)
                matching=[c for c in candidates if c.get('target')==contract.get('target')]
                plan=contract.get('next_check')
                if matching and isinstance(plan,str) and plan.strip() and len(plan)<=2000:
                    matching=[{**c,'next_check':plan,'next_check_verified':False,
                               'manual_remaining_type':str(contract.get('remaining_type',''))[:5000]}
                              for c in matching]
                    return matching,{c['step_id']:receipts[c['step_id']] for c in matching},None
            return [], {}, pending(contract, reason)
        receipt = load(receipt_path(job, contract['step_id']))
        compact = {k:v for k,v in contract.items() if k != 'source_files'}
        compact['type_receipt_job'] = str(job)
        return [compact], {contract['step_id']: receipt}, None
    if not inherited: return [], {}, None
    unsigned = {k:v for k,v in inherited.items() if k != 'checkpoint_digest'}
    if digest(unsigned) != inherited.get('checkpoint_digest'):
        return [], {}, pending({}, 'inherited_checkpoint_digest_mismatch')
    candidates = inherited.get('contracts') or []
    if inherited.get('source_files') != source:
        return [], {}, (pending(candidates[0], 'source_inventory_not_current') if candidates
                        else inherited.get('pending_contract'))
    contracts, receipts = [], {}
    for candidate in candidates[:3]:
        try:
            step = candidate['step_id']
            receipt = inherited['type_receipts'][step]
            value = {k:v for k,v in receipt.items() if k != 'receipt_digest'}
            expected = {'schema_version': 1, 'job': candidate['type_receipt_job'], 'step_id': step,
                        'module': candidate['module'], 'target': candidate['target'],
                        'remaining_type': candidate['remaining_type'], 'source_sha256': digest(source),
                        **evidence_kind(candidate), 'accepted': False}
            if (receipt.get('receipt_digest') != digest(value) or value != expected
                    or candidate.get('source_sha256') != digest(source)
                    or candidate.get('invocation_id') != step):
                continue
            contracts.append({k:v for k,v in candidate.items() if k != 'source_files'})
            receipts[step] = receipt
        except (KeyError, TypeError, ValueError):
            continue
    unresolved = None if contracts else (pending(candidates[0], 'inherited_type_receipt_unavailable')
                                         if candidates else inherited.get('pending_contract'))
    return contracts, receipts, unresolved
