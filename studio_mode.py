"""Bind a persistent queue to real or simulated execution before workers start."""
import fcntl
import json
from pathlib import Path

from longform_pipeline import atomic_json


def bind_mode(data_dir, simulation):
    folder = Path(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    expected = 'simulation' if simulation else 'real'
    marker = folder / 'runtime-mode.json'
    with (folder / '.mode.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if marker.exists():
            saved = json.loads(marker.read_text())
            if saved != {'version': 1, 'mode': expected}:
                raise ValueError('数据目录运行模式不匹配，请为正式和演练环境使用不同的 --data-dir')
            return
        state_path = folder / 'studio.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if any(not job.get('read_only') for job in state.get('jobs', {}).values()):
                raise ValueError('旧队列缺少运行模式标记，请使用新的 --data-dir；原记录保持不变')
        atomic_json(marker, {'version': 1, 'mode': expected})
