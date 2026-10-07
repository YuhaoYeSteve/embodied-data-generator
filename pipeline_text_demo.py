#!/usr/bin/env python3
"""Text-to-video fallback when the service rejects human reference images.

The original customer script remains unchanged. This demo uses its two scenes,
but does not upload images or claim frame-level continuity between segments.
"""
import argparse
import json
import os
from pathlib import Path

from ark_client import create_video_task, download_file, poll_video_task
from pipeline_30s import SEGMENTS, _build_text, concat_videos, extract_last_frame

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / 'text_segments'
FINAL_OUTPUT = BASE_DIR / 'hotel_checkin_60s.mp4'

VISUAL_DESCRIPTION = (
    '统一场景设定：一家现代商务酒店的浅米色石材前台，地面米色，左侧落地窗自然光，'
    '前台电脑与身份证读卡器在画面左侧，房卡制卡机在旁边，背景为干净的米色酒店大堂。'
    '唯一客人为二十多岁的成年中国男性，短黑发，白色长袖衬衫、深灰色长裤，'
    '身边一个黑色拉杆行李箱。店员为成年女性，只看到她的深蓝色制服袖口及双手。'
    '始终是站立店员眼镜摄像机的第一视角，客人在柜台正对面；自然轻微转头，'
    '禁止切换第三人称镜头，禁止镜头反打，禁止字幕。'
    '证件为拍摄道具，仅表现取出、递交、读卡与归还，不展示可辨认的个人号码或个人信息。'
)


def build_requests():
    requests = []
    for index, segment in enumerate(SEGMENTS):
        start = (
            '本段开头客人从大堂走到柜台前准备办理入住。'
            if index == 0 else
            '本段开头已经完成证件核验，身份证已归还并收进客人包中；'
            '店员处于自然站立视线，白衬衫客人正对柜台等待后续办理。'
        )
        prompt = VISUAL_DESCRIPTION + start + _build_text(segment, False)
        requests.append({'id': segment['id'], 'duration': segment['duration'],
                         'content': [{'type': 'text', 'text': prompt}]})
    return requests


def configure():
    os.environ.setdefault('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
    os.environ.setdefault('ARK_MODEL', 'doubao-seedance-2-5-260628')
    os.environ.setdefault('ARK_RESOLUTION', '720p')
    os.environ.setdefault('ARK_RATIO', '16:9')
    os.environ.setdefault('ARK_GENERATE_AUDIO', 'true')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--assemble-only', action='store_true')
    args = parser.parse_args()
    requests = build_requests()
    OUTPUT_DIR.mkdir(exist_ok=True)
    if args.prepare_only:
        folder = BASE_DIR / 'requests_text'
        folder.mkdir(exist_ok=True)
        for request in requests:
            path = folder / (request['id'] + '.json')
            path.write_text(json.dumps(request, ensure_ascii=False, indent=2))
            print(path)
        return
    files = [OUTPUT_DIR / (request['id'] + '.mp4') for request in requests]
    if not args.assemble_only:
        configure()
        # No image dependency: submit both scene tasks before waiting.
        tasks = [(request, path, create_video_task(request['content'], request['duration']))
                 for request, path in zip(requests, files)
                 if not path.exists() or path.stat().st_size == 0]
        for request, path, task_id in tasks:
            print(request['id'], task_id, flush=True)
            download_file(poll_video_task(task_id), path)
            extract_last_frame(path, OUTPUT_DIR / (request['id'] + '_last_frame.jpg'))
    if not all(path.exists() for path in files):
        raise RuntimeError('尚未取得两个视频片段，不能拼接。')
    concat_videos(files, FINAL_OUTPUT)
    print(FINAL_OUTPUT)


if __name__ == '__main__':
    main()
