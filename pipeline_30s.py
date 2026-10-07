#!/usr/bin/env python3
import base64
import json
import subprocess
from pathlib import Path

from ark_client import create_video_task, download_file, poll_video_task

BASE_DIR = Path(__file__).parent
REFS_DIR = BASE_DIR / "refs"
WORK_DIR = BASE_DIR / "segments_30s"
FINAL_OUTPUT = BASE_DIR / "final_output_30s.mp4"

POV_STYLE = (
    "第一人称视角，模拟酒店店员佩戴的眼镜摄像机视角，视线高度自然，"
    "随对话与操作轻微移动，不刻意低头、不刻意抬头、不固定视角，无手持晃动感，"
    "画面中不出现店员本人的正脸（因为镜头即店员的眼睛），可以看到店员自己的手、"
    "手腕、工牌、制服袖口在画面下方自然入镜。写实风格，真实酒店环境光线。"
)

VOICE_STYLE = (
    "声音要求：全片对白均为普通话中文。店员音色为成熟稳重的年轻女性声音，"
    "音调中等偏低、语速平稳、吐字清晰，略带职业化的礼貌腔调，不尖锐、不做作。"
    "客人音色为20岁左右的年轻男性声音，音调中等偏高、语速自然放松、略带年轻人"
    "随和轻快的语气，与店员音色明显区分（客人整体语气更年轻随意，店员更沉稳"
    "职业化）。全片两人音色、语速、腔调风格保持前后一致，不出现忽然变声、"
    "变调或口音突变。"
)

CONTINUITY_CLAUSE = (
    "首帧为图片2：本镜头视频必须以图片2所示的人物位置、动作状态与环境构图作为"
    "第一帧画面，从这一构图自然继续后续动作，不得切换到完全不同的机位、景别或"
    "视角；图片1仅用于保持人物外观与场景风格的参考，不代表镜头起始画面。"
)

NEUTRAL_END_CLAUSE = (
    "镜头结尾时视角回到自然站立高度的中景第一人称视角，画面中能看到店员的手与"
    "客人的上半身，不要停留在设备、证件或物品的近距离特写画面上。"
)

SEGMENTS = [
    {
        "id": "01_front",
        "duration": 30,
        "refs": ["guest_front.png", "scene_frontdesk.png"],
        "scene": (
            "客人拖着行李箱走近前台，店员抬手微笑示意欢迎，说'您好，欢迎光临，"
            "请问您有预订吗'，客人回应并把订单信息说出来，店员低头看了一眼电脑"
            "屏幕确认信息后，重新抬头看向客人。随后客人从包里拿出身份证，双手"
            "递给镜头（店员），店员的手接过身份证放到读卡设备上刷证，画面完整"
            "展示身份证从客人手中递出、店员接收、放置读卡器刷证的全过程，同时"
            "店员另一只手在电脑上查询订单，客人在旁等待，简短对话确认入住天数"
            "与房型。核验完成后，店员把证件翻面放在桌面上，再双手递还给客人，"
            "证件表面不朝向镜头，客人双手接过放回包中。"
        ),
        "neutral_end": True,
    },
    {
        "id": "02_checkin",
        "duration": 30,
        "refs": ["guest_front.png"],
        "scene": (
            "店员对客人说'我们有会员计划，注册可享受房型升级和欢迎礼，需要为您"
            "办理吗'，客人点头同意，店员在电脑上操作注册会员页面（会员注册"
            "表单），随后转向客人说明升级到高层房间，并递上一份小礼品作为欢迎"
            "礼，客人微笑接过。随后店员的手拿起空白房卡放入制卡机读写，制卡机"
            "提示音，取出制作好的房卡，双手递给客人，画面完整展示拿取房卡、"
            "制卡、递交、客人双手接过的全过程，同时说明房间号与早餐时间，客人"
            "接过房卡收好。最后店员抬手指引电梯方向，说'祝您入住愉快，电梯在"
            "您左手边'，客人道谢后拖着行李箱转身离开画面。"
        ),
        "neutral_end": False,
    },
]


def _build_text(seg: dict, has_continuity: bool) -> str:
    parts = [POV_STYLE, VOICE_STYLE]
    if has_continuity:
        parts.append(CONTINUITY_CLAUSE)
    parts.append("场景：" + seg["scene"])
    if seg.get("neutral_end"):
        parts.append(NEUTRAL_END_CLAUSE)
    return " ".join(parts)


def _image_ref(url_or_path: str) -> dict:
    if url_or_path.startswith("http"):
        url = url_or_path
    else:
        data = Path(url_or_path).read_bytes()
        ext = Path(url_or_path).suffix.lstrip(".") or "png"
        url = f"data:image/{ext};base64,{base64.b64encode(data).decode()}"
    return {"type": "image_url", "image_url": {"url": url}, "role": "reference_image"}


def extract_last_frame(video_path: Path, out_path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-update", "1", "-q:v", "2", str(out_path)],
        check=True, capture_output=True,
    )
    return out_path


def concat_videos(video_paths: list[Path], output: Path) -> None:
    inputs = []
    filter_inputs = []
    for i, p in enumerate(video_paths):
        inputs += ["-i", str(p)]
        filter_inputs.append(f"[{i}:v:0][{i}:a:0]")
    filter_complex = "".join(filter_inputs) + f"concat=n={len(video_paths)}:v=1:a=1[outv][outa]"
    cmd = [
        "ffmpeg", "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "[outa]",
        str(output),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def main() -> None:
    WORK_DIR.mkdir(exist_ok=True)
    manifest = json.loads((REFS_DIR / "manifest.json").read_text())

    segment_files = []
    prev_last_frame = None

    for seg in SEGMENTS:
        mp4_path = WORK_DIR / f"{seg['id']}.mp4"
        last_frame_path = WORK_DIR / f"{seg['id']}_last_frame.jpg"

        if mp4_path.exists() and last_frame_path.exists():
            print(f"=== 跳过已完成段 {seg['id']}（复用已有文件） ===")
            segment_files.append(mp4_path)
            prev_last_frame = last_frame_path
            continue

        has_continuity = prev_last_frame is not None
        print(f"=== 生成段 {seg['id']}（{seg['duration']}s，续接图={has_continuity}） ===")
        text = _build_text(seg, has_continuity)
        content = [{"type": "text", "text": text}]

        for ref_name in seg["refs"]:
            content.append(_image_ref(manifest[ref_name]))
        if prev_last_frame is not None:
            content.append(_image_ref(str(prev_last_frame)))

        task_id = create_video_task(content, duration=seg["duration"])
        print(f"  任务已创建：{task_id}")
        video_url = poll_video_task(task_id)

        download_file(video_url, mp4_path)
        print(f"  已下载：{mp4_path}")
        segment_files.append(mp4_path)

        extract_last_frame(mp4_path, last_frame_path)
        print(f"  已提取尾帧：{last_frame_path}")
        prev_last_frame = last_frame_path

    print("=== 拼接最终视频 ===")
    concat_videos(segment_files, FINAL_OUTPUT)
    print(f"完成：{FINAL_OUTPUT}")


if __name__ == "__main__":
    main()
