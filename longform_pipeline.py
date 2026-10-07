"""Auditable, review-gated long-form Seedance experiments (Python 3.9+).

Generation is explicit: init/inspect are offline, submit makes one paid request,
retry explicitly replaces a failed/rejected candidate, and poll only queries an
existing task. Original candidates and their costs are always retained.
"""
import argparse
import fcntl
import functools
import hashlib
import json
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path

from ark_client import download_file
import studio_models as models
from studio_config import SETTINGS


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def estimate_cost(output_seconds, reference_seconds, width, height, model_family=None, resolution=None):
    family = model_family or models.DEFAULT_MODEL_FAMILY
    resolution = resolution or next((key for key in ('480p', '720p', '1080p', '4k')
                                    if models.resolution_dimensions(key) == (width, height)), '720p')
    rate = models.price_rate(family, resolution, bool(reference_seconds))
    if rate is None:
        return None
    billed_reference = max(math.ceil(output_seconds * 2 / 3), math.floor(reference_seconds)) if reference_seconds else 0
    tokens = (output_seconds + billed_reference) * width * height * 24 / 1024
    return tokens / 1000 * rate


def usage_cost(usage, has_video, model_family=None, resolution=None):
    tokens = usage.get('completion_tokens')
    if not isinstance(tokens, (int, float)) or isinstance(tokens, bool) or not math.isfinite(tokens) or tokens < 0:
        return None
    rate = models.price_rate(model_family or models.DEFAULT_MODEL_FAMILY, resolution or '720p', has_video)
    return tokens / 1000 * rate if rate is not None else None


def phase(result):
    status = result.get('status')
    return status.get('phase') if isinstance(status, dict) else status


def video_url(result):
    return (result.get('content') or {}).get('video_url') or result.get('video_url') or result.get('output_url')


def reference_result(record, requested_duration):
    result=dict(record['result'])
    actual=record['media'].get('video_duration',record['media'].get('duration',0))
    if abs(actual-requested_duration)>0.5:
        raise ValueError('实际视频时长与请求不符，不能作为连续参考')
    result['duration']=requested_duration
    return result


def _stored_family(data):
    family = data.get('model_family')
    if family in ('seedance-2.0', 'seedance-2.5'):
        return family
    return models.infer_model_family(family,
        default=models.infer_model_family(data.get('model'), default=models.DEFAULT_MODEL_FAMILY))


def locked(method):
    @functools.wraps(method)
    def call(self,*args,**kwargs):
        with (self.folder/'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            self.data=json.loads(self.path.read_text())
            self.data['model_family'] = _stored_family(self.data)
            try: return method(self,*args,**kwargs)
            finally: fcntl.flock(lock,fcntl.LOCK_UN)
    return call


def build_request(spec, index, model, previous=None, observed_state=None,
                  assets=None, resolution='720p', bootstrap=None, model_family=None):
    family = model_family or models.infer_model_family(model, default=models.DEFAULT_MODEL_FAMILY)
    capability = models.get_model(family)
    known_family = models.infer_model_family(model)
    if known_family and known_family != family:
        raise ValueError('模型与所选型号不匹配')
    models.validate_spec_model(spec, family, resolution, full_length=False)
    seg = spec['segments'][index]
    content = []
    prompt = spec['global_prompt'] + '\n' + seg['prompt']
    if spec.get('dataset'):
        # Structured editor fields are generation requirements as well as
        # planned annotations. Continuations must start from observed reality.
        requirements = ['本段结构化执行要求：动作与结束目标优先于上文的笼统描述。']
        if index:
            requirements.append('起点以参考视频和已核实的实际结束状态为准，不重复已经完成的动作。')
        elif seg.get('expected_start_state'):
            requirements.append('开场状态：' + seg['expected_start_state'])
        if seg.get('required_actions'):
            requirements.append('必须完整展示的动作：\n- ' + '\n- '.join(seg['required_actions']))
        if seg.get('expected_end_state'):
            requirements.append('本段需要达到的结束状态：' + seg['expected_end_state'])
        prompt += '\n' + '\n'.join(requirements)
    ref = previous if index else bootstrap
    if index and not observed_state:
        raise ValueError('续写需要已核实的实际结束状态')
    if index and not previous:
        raise ValueError('续写需要上一段原始视频')
    if ref:
        if not video_url(ref): raise ValueError('缺少原始 video_url')
        if not 0 < ref.get('duration', 0) <= capability['max_reference_duration']:
            raise ValueError('参考视频必须有已核实的时长，且不超过%d秒' % capability['max_reference_duration'])
        content.append({'type': 'video_url', 'video_url': {'url': video_url(ref)}, 'role': 'reference_video'})
        if index:
            prompt = ('向后延长@视频1。仅生成视频结束后的新内容，承接实际姿态、镜头运动和声音。\n'
                      '已核实的结束状态：' + observed_state + '\n' + prompt)
        else:
            prompt = (ref.get('reference_description',
                      '@视频1定义本场景的参与者、物体、空间布局和视听风格。重新生成下述开场，保持已指定的角色、物体与环境一致；本次任务为参考生视频。')
                      + '\n' + prompt)
    for item in assets or []:
        if 'segment_ids' in item:
            scope = item['segment_ids']
            known_ids = {str(part.get('id', position + 1)) for position, part in enumerate(spec['segments'])}
            if (not isinstance(scope, list) or not scope
                    or any(not isinstance(value, str) or not value.strip() for value in scope)
                    or len(set(scope)) != len(scope) or not set(scope).issubset(known_ids)):
                raise ValueError('参考素材segment_ids必须包含存在且不重复的分段编号')
            if str(seg.get('id', index + 1)) not in scope:
                continue
        if item['role'] == 'reference_video': raise ValueError('长期参考不接受额外视频，避免超出模型参考视频时长上限')
        if item['role'] not in ('reference_image', 'reference_audio'):
            raise ValueError('长期资产只能是图像或音频')
        content.append(item['content'])
        prompt += '\n' + item['description']
    result = {'model': model, 'content': [{'type': 'text', 'text': prompt}] + content,
            'duration': seg['duration'], 'ratio': 'adaptive' if index else '16:9',
            'resolution': resolution, 'generate_audio': True, 'return_last_frame': True}
    if capability['request_output_format']:
        result['output_format'] = capability['request_output_format']
    if capability['explicit_task_type']:
        result['omni_reference_task_type'] = 'extend' if index else 'reference'
    return result


def probe(path):
    p = subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)],
                       check=True,capture_output=True,text=True)
    result = json.loads(p.stdout)
    v = next((s for s in result['streams'] if s['codec_type']=='video'), None)
    if not v: raise ValueError('下载文件没有视频流')
    return {'duration': float(result['format']['duration']),
            'video_duration':float(v.get('duration',result['format']['duration'])), 'width':v['width'],'height':v['height'],
            'fps':v['r_frame_rate'],'video_codec':v['codec_name'],
            'audio_streams':[{'codec':s['codec_name'],'sample_rate':s.get('sample_rate'),
                              'channels':s.get('channels')} for s in result['streams'] if s['codec_type']=='audio'],
            'bytes':Path(path).stat().st_size}


class Experiment:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / 'experiment.json'
        self.data = json.loads(self.path.read_text())
        self.data['model_family'] = _stored_family(self.data)

    @classmethod
    def create(cls, folder, spec, profile, model, budget_cny=200, resolution='720p',
               external_cost_cny=0, bootstrap=None, assets=None, *, model_family=None, budget_enforced=True):
        folder = Path(folder)
        if (folder/'experiment.json').exists(): raise ValueError('实验已存在，请恢复原记录')
        if (isinstance(budget_cny, bool) or not isinstance(budget_cny, (int, float)) or
                not math.isfinite(budget_cny) or budget_cny < 0 or
                (budget_enforced and budget_cny == 0) or external_cost_cny < 0):
            raise ValueError('参考预算与已有费用不能为负，强制预算模式须为正')
        if type(budget_enforced) is not bool:
            raise ValueError('budget_enforced必须为布尔值')
        family = model_family or models.infer_model_family(model, default=models.DEFAULT_MODEL_FAMILY)
        models.validate_spec_model(spec, family, resolution, full_length=False)
        known_family = models.infer_model_family(model)
        if known_family and known_family != family:
            raise ValueError('模型与所选型号不匹配')
        atomic_json(folder/'scenario.json', spec)
        atomic_json(folder/'experiment.json', {'created_at':time.time(),'profile':profile,'model':model,
            'model_family':family,'spec':spec,'spec_sha256':digest(spec),
            'resolution':resolution,'budget_cny':budget_cny,'external_cost_cny':external_cost_cny,
            'budget_enforced':budget_enforced,
            'rates_cny_per_1000':{'nv2v':models.price_rate(family,resolution,False),
                                  'v2v':models.price_rate(family,resolution,True)},
            'rates_checked_date':'2026-09-22' if family=='seedance-2.5' else None,
            'bootstrap':bootstrap,'assets':assets or [],'segments':[]})
        return cls(folder)

    def save(self): atomic_json(self.path,self.data)

    def committed_cost(self):
        values = [
            r['actual_estimated_cny'] if r.get('actual_estimated_cny') is not None
            else r.get('reserved_cny') for r in self.data['segments']]
        if any(value is None for value in values):
            return None
        return self.data['external_cost_cny'] + sum(values)

    def record(self,index):
        rows=[r for r in self.data['segments'] if r['index']==index]
        return rows[-1] if rows else None

    @locked
    def submit(self,index,client,resample=False):
        """Resume by default; explicitly resample only an eligible latest attempt.

        The lock reloads the ledger before eligibility and budget checks, so a
        second concurrent retry sees the newly queued attempt and is refused.
        """
        if index < 0 or index >= len(self.data['spec']['segments']): raise ValueError('分段编号超出范围')
        existing=self.record(index)
        if resample:
            if not existing: raise ValueError('没有已有候选，首次生成请使用submit')
            if any(r['index']>index for r in self.data['segments']):
                raise ValueError('已有下游，禁止重采样上游；请创建新实验分支')
            decision=(existing.get('review') or {}).get('decision')
            eligible=existing['status'] in ('failed','cancelled','canceled','expired','submission_rejected') or (
                existing['status']=='succeeded' and decision=='reject')
            if decision=='accept' or not eligible:
                raise ValueError('只允许明确未受理、失败、取消、过期或评审拒绝的候选显式重采样')
            if existing['status']=='submission_rejected' and not existing.get('task_id'):
                existing.update(reserved_cny=0,actual_estimated_cny=0,
                    cost_basis='definite rejection: request not accepted, no generation task created')
                self.save()
        elif existing:
            if existing.get('task_id'): return {'id':existing['task_id'],'status':existing['status']}
            raise ValueError('提交状态未知，请核对原任务，禁止重复付费提交')
        prev=self.record(index-1) if index else None
        if index and (not prev or (prev.get('review') or {}).get('decision')!='accept'):
            raise ValueError('上一段尚未通过评审，不能生成下游')
        reference=reference_result(prev,self.data['spec']['segments'][index-1]['duration']) if prev else self.data.get('bootstrap')
        state=prev['review']['observed_end_state'] if prev else None
        body=build_request(self.data['spec'],index,self.data['model'],previous=reference if index else None,
             observed_state=state,assets=self.data['assets'],resolution=self.data['resolution'],
             bootstrap=self.data.get('bootstrap'),model_family=self.data['model_family'])
        seconds=reference.get('duration',0) if reference else 0
        w,h=models.resolution_dimensions(self.data['resolution'])
        estimated=estimate_cost(body['duration'],seconds,w,h,
            model_family=self.data['model_family'],resolution=self.data['resolution'])
        reserved=estimated*1.05 if estimated is not None else None
        if self.data.get('budget_enforced', True):
            committed=self.committed_cost()
            if committed is None or reserved is None:
                raise ValueError('当前费率未核实，无法执行强制预算；工作台使用费用展示模式')
            if committed+reserved>self.data['budget_cny']:
                raise ValueError('本次预算不足：已占用%.2f，本次预留%.2f，上限%.2f' %
                                 (committed,reserved,self.data['budget_cny']))
        attempt=1+sum(r['index']==index for r in self.data['segments'])
        directory='segment_%02d' % (index+1)
        if attempt>1: directory+='_attempt_%02d' % attempt
        sources={name:Path(__file__).with_name(name).read_bytes()
                 for name in ('longform_pipeline.py','longform_client.py','studio_models.py')}
        code_hashes={name:hashlib.sha256(source).hexdigest() for name,source in sources.items()}
        code_directory='code_snapshots/'+digest(code_hashes)
        (self.folder/code_directory).mkdir(parents=True,exist_ok=True)
        for name,source in sources.items():
            try:
                with (self.folder/code_directory/name).open('xb') as snapshot: snapshot.write(source)
            except FileExistsError: pass
        entry={'index':index,'attempt':attempt,'reason':'explicit resample' if resample else 'initial submission',
               'directory':directory,'status':'submitting','submitted_at':time.time(),
               'code_sha256':code_hashes,'code_snapshot_directory':code_directory,
               'request_sha256':digest(body),'reference_seconds':seconds,'estimated_cny':estimated,
               'reserved_cny':reserved,'actual_estimated_cny':None,
               'upstream_task_id':prev['task_id'] if prev else None,'review':None}
        atomic_json(self.folder/directory/'request.json',body)
        self.data['segments'].append(entry);self.save()
        try:
            result=client.submit(body)
        except Exception as exc:
            entry['status']='submission_unknown';entry['error_type']=type(exc).__name__;self.save()
            from longform_client import ArkCLIError,SubmissionUnknown
            if isinstance(exc,ArkCLIError):
                entry['error']=str(exc)
                if not isinstance(exc,SubmissionUnknown):
                    entry.update(status='submission_rejected',reserved_cny=0,actual_estimated_cny=0,
                        cost_basis='definite rejection: request not accepted, no generation task created')
                if exc.raw is not None: atomic_json(self.folder/directory/'error_response.json',exc.raw)
                self.save()
            raise
        if not result.get('id'):
            entry['status']='submission_unknown';self.save()
            raise ValueError('提交状态未知：响应没有task ID')
        entry.update(task_id=result['id'],status=phase(result) or 'submitted')
        atomic_json(self.folder/directory/'submit_response.json',result)
        self.save()
        return result

    @locked
    def poll(self,index,client):
        record=self.record(index)
        if not record or not record.get('task_id'): raise ValueError('没有可查询的任务')
        result=client.get(record['task_id'])
        atomic_json(self.folder/record['directory']/'task_response.json',result)
        record.update(status=phase(result),result=result)
        cost=usage_cost(result.get('usage',{}),record['reference_seconds']>0,
            model_family=self.data['model_family'],resolution=self.data['resolution'])
        if cost is not None: record['actual_estimated_cny']=cost
        self.save()
        if phase(result)=='succeeded':
            url=video_url(result)
            if not url: raise ValueError('任务成功但没有video_url')
            local=self.folder/record['directory']/'original.mov'
            if not local.exists(): download_file(url,local)
            media=probe(local)
            atomic_json(local.with_name('media.json'),media)
            record['media']=media
            record['sha256']=hashlib.sha256(local.read_bytes()).hexdigest()
            self.save()
        return result

    @locked
    def review(self,index,review):
        record=self.record(index)
        if not record or record['status']!='succeeded' or not record.get('media'):
            raise ValueError('必须先成功生成、下载并检查媒体')
        if review.get('decision') not in ('accept','reject'): raise ValueError('decision必须为accept/reject')
        duration=record['media'].get('video_duration',record['media'].get('duration',0))
        requested=self.data['spec']['segments'][index]['duration']
        if review['decision']=='accept' and abs(duration-requested)>0.5:
            raise ValueError('实际视频时长不符合本段请求')
        dimensions=('visual_continuity','action_integrity','audio_consistency')
        if any(review.get(k) not in ('pass','fail','unverified') for k in dimensions):
            raise ValueError('必须分别记录画面、动作、声音检查结果')
        if review['decision']=='accept' and any(review[k]!='pass' for k in dimensions):
            raise ValueError('未通过全部质量检查，不能接受')
        if not review.get('observed_end_state') or not review.get('evidence'):
            raise ValueError('需要实际末状态和评审证据')
        if any(r['index']>index for r in self.data['segments']):
            raise ValueError('已有下游，禁止改变上游评审；请创建新实验分支')
        record['review']=dict(review,reviewed_at=time.time())
        atomic_json(self.folder/record['directory']/'review.json',record['review']);self.save()

    @locked
    def assemble(self):
        rows=[self.record(i) for i in range(len(self.data['spec']['segments']))]
        if any(not r or (r.get('review') or {}).get('decision')!='accept' for r in rows):
            raise ValueError('所有分段通过评审后才可合成长片')
        signatures={(r['media']['width'],r['media']['height'],r['media']['fps'],r['media']['video_codec'],
                     json.dumps(r['media']['audio_streams'],sort_keys=True)) for r in rows}
        if len(signatures)!=1: raise ValueError('分段编码规格不一致，不能直接复制合成')
        lines=[];boundaries=[];elapsed=0
        for r in rows:
            rel=r['directory']+'/original.mov'
            lines.append("file '"+rel+"'")
            boundaries.append({'task_id':r['task_id'],'start_seconds':elapsed,'duration':r['media']['duration']})
            elapsed+=r['media']['duration']
        listing=self.folder/'concat.txt';listing.write_text('\n'.join(lines)+'\n')
        output=self.folder/('longform_%ds.mov' % self.data['spec']['target_duration_seconds'])
        subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','1',
                        '-i',str(listing),'-map','0:v:0','-map','0:a:0','-c','copy',str(output)],check=True)
        media=probe(output)
        if abs(media['video_duration']-self.data['spec']['target_duration_seconds'])>0.5*len(rows):
            raise ValueError('合成后的实际时长与目标不一致')
        manifest={'file':str(output),'processing':'Concatenated generated segments by stream copy; not an original recording.',
                  'delivery_status':'Experimental candidate; continuation reviews do not establish customer acceptance or real-world ground truth.',
                  'boundaries':boundaries,'media':media,'cost_estimated_cny':self.committed_cost(),
                  'cost_basis':'Mixed ledger: usage-based estimates when known, otherwise conservative reservations; external costs separately supplied. Not a settled bill.',
                  'external_cost_cny':self.data['external_cost_cny'],
                  'cost_items':[{'index':r['index'],'attempt':r.get('attempt',1),
                                 'directory':r['directory'],'task_id':r.get('task_id'),
                                 'selected':r['directory'] in {chosen['directory'] for chosen in rows},
                                 'usage_estimate_cny':r.get('actual_estimated_cny'),
                                 'reservation_cny':r['reserved_cny']} for r in self.data['segments']]}
        atomic_json(self.folder/'final_manifest.json',manifest)
        return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','submit','retry','poll','review','inspect','assemble'])
    parser.add_argument('--run-dir',required=True)
    parser.add_argument('--scenario',default='scenarios/object_sorting_60s.json')
    parser.add_argument('--profile',default=SETTINGS['video_profile'])
    parser.add_argument('--model',default=models.get_model()['model'])
    parser.add_argument('--budget',type=float,default=200)
    parser.add_argument('--external-cost',type=float,default=0)
    parser.add_argument('--bootstrap-json')
    parser.add_argument('--assets-json')
    parser.add_argument('--resolution',default='720p')
    parser.add_argument('--segment',type=int,default=0,help='Zero-based segment index')
    parser.add_argument('--review-json')
    args=parser.parse_args()
    if args.command=='init':
        exp=Experiment.create(Path(args.run_dir),json.loads(Path(args.scenario).read_text()),args.profile,args.model,
                 args.budget,args.resolution,args.external_cost,
                 json.loads(Path(args.bootstrap_json).read_text()) if args.bootstrap_json else None,
                 json.loads(Path(args.assets_json).read_text()) if args.assets_json else None)
    else: exp=Experiment(args.run_dir)
    if args.command in ('submit','retry','poll'):
        from longform_client import ArkCLIClient
        client=ArkCLIClient(exp.data['profile'],exp.data['model'])
        result=exp.submit(args.segment,client,resample=True) if args.command=='retry' else getattr(exp,args.command)(args.segment,client)
        print(json.dumps({'task_id':result.get('id'),'status':phase(result),
                          'committed_cost_cny':exp.committed_cost()},ensure_ascii=False))
    elif args.command=='review':
        if not args.review_json: parser.error('--review-json is required')
        exp.review(args.segment,json.loads(Path(args.review_json).read_text()))
        print('Review saved')
    elif args.command=='assemble': print(json.dumps(exp.assemble(),ensure_ascii=False,indent=2))
    else:
        print(json.dumps({'run_dir':args.run_dir,'budget':exp.data['budget_cny'],
            'committed_cost_cny':exp.committed_cost(),'segments':[{k:r.get(k) for k in ('index','task_id','status','actual_estimated_cny','review')}
             for r in exp.data['segments']]},ensure_ascii=False,indent=2))


if __name__=='__main__': main()
