"""Explicit offline end-to-end exercise. Never submits a model request."""
import hashlib
import json
import threading
import time
import uuid
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont
from longform_pipeline import atomic_json
from studio_models import resolution_dimensions

MEDIA_LOCK = threading.Lock()


class SimulationClient:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def submit(self, body):
        resolution_dimensions(body['resolution'])
        task_id = 'simulation-' + uuid.uuid4().hex
        atomic_json(self.folder/(task_id+'.json'), {
            'id':task_id,'created_at':time.time(),'duration':body['duration'],
            'model':body.get('model'),'resolution':body['resolution'],
            'has_video':any(x.get('type')=='video_url' for x in body['content'])})
        return {'id':task_id,'status':'queued','simulation':True}

    def get(self, task_id):
        data = json.loads((self.folder/(task_id+'.json')).read_text())
        if time.time()-data['created_at'] < 1.2:
            return {'id':task_id,'status':'running','usage':{},'simulation':True}
        seconds = data['duration']
        w,h = resolution_dimensions(data['resolution'])
        media = self.folder/('simulation_%d_%s.mp4'%(seconds,data['resolution']))
        with MEDIA_LOCK:
            if not media.exists():
                picture = self.folder/('simulation_%s.png'%data['resolution'])
                image = Image.new('RGB',(w,h),'#e3eee7')
                draw = ImageDraw.Draw(image)
                try: font=ImageFont.truetype('/System/Library/Fonts/Supplemental/Arial.ttf',max(20,w//25))
                except OSError: font=ImageFont.load_default()
                draw.text((w//10,h//3),'SIMULATION',font=font,fill='#235847')
                draw.text((w//10,h//2),'No model API calls',font=font,fill='#4b6f61')
                image.save(picture)
                subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-loop','1','-i',str(picture),
                    '-f','lavfi','-i','anullsrc=r=32000:cl=stereo','-t',str(seconds),'-r','24',
                    '-c:v','libx264','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac',
                    '-movflags','+faststart',str(media)],check=True,capture_output=True,timeout=60)
        frames=seconds*24
        tokens=int((frames+(seconds*24 if data['has_video'] else 0))*w*h/1024)
        return {'id':task_id,'status':'succeeded','duration':seconds,'resolution':data['resolution'],
                'model':data.get('model'),
                'output_url':media.resolve().as_uri(),'usage':{'completion_tokens':tokens,'total_tokens':tokens},
                'simulation':True}


def simulation_review(exp,index):
    return {'decision':'accept','visual_continuity':'pass','action_integrity':'pass','audio_consistency':'pass',
            'observed_end_state':'模拟任务已完成本段，用于验证工作台流程，没有调用生成模型。',
            'evidence':'离线演练结果，不作为真实视频质量结论。','automatic':True,
            'requires_manual_review':False,'review_cost_cny':0.0,'summary':'离线演练检查通过'}


def make_manager(root, data_dir):
    from studio_backend import StudioManager
    from studio_server import exporter
    from studio_mode import bind_mode
    bind_mode(data_dir, True)
    folder = Path(data_dir)/'simulation_provider'
    return StudioManager(root,data_dir,client_factory=lambda *_:SimulationClient(folder),
                         reviewer=simulation_review,exporter=exporter,poll_interval=.25)
