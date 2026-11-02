"""A set of Lean build targets is independent of JSON spelling and duplicates."""
import json
import re

PATTERN = re.compile(r"Submission(?:\.[A-Za-z_][A-Za-z0-9_']*)*")


def normalize(query):
    modules=json.loads(query)
    if (not isinstance(modules,list) or not modules or len(modules)>100 or
            any(not isinstance(x,str) or not PATTERN.fullmatch(x) for x in modules)):
        raise ValueError('Incremental checks require Submission module names')
    return json.dumps(sorted(set(modules)),separators=(',',':'),ensure_ascii=False)


def equivalent(action,left,right):
    if action!='incremental':return left==right
    try:return normalize(left)==normalize(right)
    except (ValueError,TypeError):return False


def observation(value):
    value=dict(value)
    check=value.get('check')
    if isinstance(check,dict) and check.get('action')=='incremental':
        check=dict(check)
        try:check['query']=normalize(check.get('query'))
        except (ValueError,TypeError):pass
        value['check']=check
    return value
