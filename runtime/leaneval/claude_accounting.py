"""Native Claude counters: message maxima, invocation deltas, session cumulatives."""
from dataclasses import dataclass, field
import json

KEYS=('input_tokens','cache_creation_input_tokens','cache_read_input_tokens','output_tokens')
MODEL_KEYS=dict(zip(KEYS,('inputTokens','cacheCreationInputTokens','cacheReadInputTokens','outputTokens')))
SEMANTICS='claude-message-invocation-session-v2'


def integer(value):
    if type(value) is not int or value<0:raise ValueError('invalid native token count')
    return value


def zero():return dict.fromkeys(KEYS,0)


@dataclass
class StreamAccount:
    messages: dict = field(default_factory=dict)
    current: set = field(default_factory=set)
    invocation_usage: dict = field(default_factory=zero)
    closed_usage: dict = field(default_factory=zero)
    models: dict = field(default_factory=dict)
    invocation_thinking: int = 0
    closed_thinking: int = 0
    model_thinking: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)

    @staticmethod
    def fields(usage):
        if not isinstance(usage,dict):raise ValueError('invalid usage object')
        return {k:integer(usage.get(k,0)) for k in KEYS}

    def begin_invocation(self):
        self.current=set();self.invocation_usage=zero();self.invocation_thinking=0

    def message(self,message_id,usage):
        if not isinstance(message_id,str) or not message_id:raise ValueError('missing message identity')
        values=self.fields(usage);old=self.messages.get(message_id,zero())
        self.messages[message_id]={k:max(values[k],old[k]) for k in KEYS}
        self.current.add(message_id)

    def result(self,usage,model_usage):
        if usage is not None:
            values=self.fields(usage)
            self.invocation_usage={k:max(self.invocation_usage[k],values[k]) for k in KEYS}
            details=usage.get('output_tokens_details') or {}
            if 'thinking_tokens' in details:
                self.invocation_thinking=max(self.invocation_thinking,integer(details['thinking_tokens']))
        if model_usage is not None and not isinstance(model_usage,dict):raise ValueError('invalid model usage')
        for model,row in (model_usage or {}).items():
            if not isinstance(model,str) or not isinstance(row,dict):raise ValueError('invalid model counter')
            previous=self.models.get(model,zero())
            values={k:integer(row.get(alias,previous[k])) for k,alias in MODEL_KEYS.items()}
            if any(values[k]<previous[k] for k in KEYS):raise ValueError('session cumulative counter regressed')
            self.models[model]=values
            if 'thinkingTokens' in row:
                thinking=integer(row['thinkingTokens'])
                if thinking<self.model_thinking.get(model,0):raise ValueError('session thinking counter regressed')
                self.model_thinking[model]=thinking

    def close_invocation(self,*,has_result):
        if has_result:
            self.closed_usage={k:self.closed_usage[k]+self.invocation_usage[k] for k in KEYS}
            self.closed_thinking+=self.invocation_thinking
        self.begin_invocation()

    @property
    def raw(self):return {k:sum(m[k] for m in self.messages.values()) for k in KEYS}

    def full(self):
        raw=self.raw
        model={k:sum(m[k] for m in self.models.values()) for k in KEYS}
        return {k:max(raw[k],self.closed_usage[k]+self.invocation_usage[k],model[k]) for k in KEYS}

    @property
    def thinking_tokens(self):
        return max(self.closed_thinking+self.invocation_thinking,sum(self.model_thinking.values()))

    @property
    def extra(self):return sum(self.full().values())-sum(self.raw.values())

    @property
    def tokens(self):
        return sum(self.full().values())-sum(self.baseline.get(k,0) for k in KEYS)

    def session_usage(self):
        full=self.full()
        return {**full,'thinking_tokens':self.thinking_tokens}

    def usage_fields(self):
        full=self.full();raw={k:max(0,full[k]-self.baseline.get(k,0)) for k in KEYS}
        return {'input_tokens':raw['input_tokens']+raw['cache_creation_input_tokens']+raw['cache_read_input_tokens'],
            'cached_input_tokens':raw['cache_read_input_tokens'],
            'cache_write_input_tokens':raw['cache_creation_input_tokens'],
            'output_tokens':raw['output_tokens'],
            'reasoning_output_tokens':max(0,self.thinking_tokens-self.baseline.get('thinking_tokens',0))}

    def snapshot(self):
        data={'semantics':SEMANTICS,'messages':self.messages,'closed_usage':self.closed_usage,
              'models':self.models,'closed_thinking':self.closed_thinking,'model_thinking':self.model_thinking}
        # Never make an unbounded resume checkpoint. Normal accounting continues.
        if len(self.messages)>10000 or len(json.dumps(data))>4*1024*1024:return None
        return data

    @classmethod
    def restore(cls,data,baseline):
        if not isinstance(data,dict) or data.get('semantics')!=SEMANTICS:
            raise ValueError('missing compatible native accounting checkpoint')
        if not isinstance(baseline,dict) or set(baseline)!=set(KEYS)|{'thinking_tokens'}:
            raise ValueError('missing session accounting baseline')
        account=cls()
        for name in ('messages','models'):
            value=data.get(name)
            if not isinstance(value,dict):raise ValueError('invalid accounting checkpoint')
            setattr(account,name,{k:cls.fields(v) for k,v in value.items()})
        account.closed_usage=cls.fields(data.get('closed_usage'))
        account.closed_thinking=integer(data.get('closed_thinking'))
        account.model_thinking={k:integer(v) for k,v in data.get('model_thinking',{}).items()}
        account.baseline={k:integer(v) for k,v in baseline.items()}
        if account.session_usage()!=baseline:raise ValueError('checkpoint/baseline counters differ')
        return account
