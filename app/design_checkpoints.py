"""Recover completed local image bytes after a database write interruption."""
import hashlib
import json
import os
from pathlib import Path


def _path(main,run,task):
    root=Path(main.DATA_ROOT)/'design_checkpoints'
    if root.is_symlink():raise ValueError('Checkpoint directory cannot be a link')
    return root/(str(run['id'])+'-'+str(task['id'])+'.json')

def save(main,run,task,result):
    from . import simple_design_flow as flow
    image=flow.local_path(result['url'],pid=run['project_id'])
    if image.is_symlink() or flow.file_hash(image)!=result['sha256']:raise ValueError('Completed image bytes changed')
    path=_path(main,run,task);path.parent.mkdir(parents=True,exist_ok=True)
    packet={'run_id':run['id'],'task_id':task['id'],'input_hash':run['input_hash'],'prompt_sha256':hashlib.sha256(task['prompt'].encode()).hexdigest(),'result':result}
    temp=path.with_suffix('.tmp')
    try:temp.write_text(json.dumps(packet,ensure_ascii=False),encoding='utf-8');os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)

def load(main,run,task):
    from . import simple_design_flow as flow
    path=_path(main,run,task)
    if not path.exists():return None
    if path.is_symlink():raise ValueError('Checkpoint cannot be a link')
    packet=json.loads(path.read_text(encoding='utf-8'))
    expected=(run['id'],task['id'],run['input_hash'],hashlib.sha256(task['prompt'].encode()).hexdigest())
    actual=tuple(packet.get(k) for k in ('run_id','task_id','input_hash','prompt_sha256'))
    if actual!=expected:raise ValueError('Saved image belongs to different design inputs')
    result=packet['result'];image=flow.local_path(result['url'],pid=run['project_id'])
    if image.is_symlink() or flow.file_hash(image)!=result['sha256']:raise ValueError('Saved image is missing or changed; original request must be checked')
    return result

def discard(main,run,task):
    path=_path(main,run,task)
    if path.is_symlink():raise ValueError('Checkpoint cannot be a link')
    path.unlink(missing_ok=True)
