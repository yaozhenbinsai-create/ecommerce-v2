"""Finite retry decisions, with no sleeping or hidden retry loops."""
from dataclasses import dataclass
from contextvars import ContextVar
import math

CURRENT_ROLE=ContextVar('current_model_role',default=None)

class ModelFailure(Exception):
    def __init__(self,message,kind='transient',retry_after=0):
        super().__init__(message)
        self.kind=kind
        self.retry_after=retry_after

@dataclass(frozen=True)
class Decision:
    status:str
    role:int
    attempts:int
    delay:float=0
    retry_round:int=1

def decide(role, attempts, has_backup, failure, fast_failover=False, *, model_count=None, retry_round=1):
    """Each configured model once per round; pause after three complete rounds.

    Pending/unknown accepted image requests are reconciled, not a new generation.
    Local input errors cannot be repaired by cycling provider models.
    """
    count=max(1,int(model_count if model_count is not None else (2 if has_backup else 1)))
    retry_round=max(1,int(retry_round))
    kind=getattr(failure,'kind','invalid')
    if kind in {'local','unknown'}: return Decision('paused',role,attempts,retry_round=retry_round)
    if kind=='poll':
        return Decision('retry_wait',role,attempts,60,retry_round) if attempts<31 else Decision('paused',role,attempts,retry_round=retry_round)
    if role+1<count:
        return Decision('queued',role+1,0,retry_round=retry_round)
    if retry_round>=3:
        return Decision('paused',role,attempts,retry_round=retry_round)
    try:retry_after=float(getattr(failure,'retry_after',0))
    except (TypeError,ValueError,OverflowError):retry_after=0
    if not math.isfinite(retry_after):retry_after=0
    return Decision('retry_wait',0,0,max(2,min(retry_after,3600)),retry_round+1)
