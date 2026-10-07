"""Build and verify a derived preview from existing generated segments.

Video is stream-copied onto its frame clock. Audio is decoded, trimmed/padded
with technical silence at the video boundaries, concatenated, and AAC encoded.
This does not generate speech, remove noise, change voices, or add transitions.
Original files and an uncorrected stream-copy concat remain available.
"""
import argparse
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import subprocess


def _run(argv):
    result = subprocess.run(argv, capture_output=True, timeout=300)
    stderr = result.stderr.decode('utf-8', 'replace')
    if result.returncode:
        raise RuntimeError('media command failed: ' + stderr)
    return result.stdout, stderr


def _probe(path, options):
    output, _ = _run(['ffprobe', '-v', 'error'] + options + ['-of', 'json', str(path)])
    return json.loads(output)


def _sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def _frames(path, selector):
    return _probe(path, ['-select_streams', selector, '-show_frames', '-show_entries',
                         'frame=best_effort_timestamp_time,nb_samples'])['frames']


def _pts(frames):
    return [float(row['best_effort_timestamp_time']) for row in frames]


def timing_summary(points, frame_interval):
    gaps = [right - left for left, right in zip(points, points[1:])]
    return {'frames': len(points), 'min_gap_seconds': min(gaps) if gaps else None,
            'max_gap_seconds': max(gaps) if gaps else None,
            'strictly_increasing': all(gap > 0 for gap in gaps),
            'uniform': bool(points) and all(abs(gap - frame_interval) <= 0.000002 for gap in gaps)}


def audio_plan(video_duration, sample_rate, source_samples, audio_offset_seconds=0,
               target_samples=None):
    """Keep the source's A/V offset; fill only regions without source samples."""
    target = round(Fraction(video_duration) * sample_rate) if target_samples is None else target_samples
    offset = round(audio_offset_seconds * sample_rate)
    head_trim = min(max(-offset, 0), source_samples)
    head_pad = min(max(offset, 0), target)
    used = min(source_samples - head_trim, target - head_pad)
    if target <= 0 or used <= 0:
        raise ValueError('source audio must overlap the video interval')
    return {'target_samples': target, 'source_samples': source_samples,
            'audio_offset_seconds': audio_offset_seconds, 'trim_head_samples': head_trim,
            'trim_tail_samples': source_samples - head_trim - used,
            'pad_head_samples': head_pad, 'pad_tail_samples': target - head_pad - used,
            'used_samples': used}


def _audio_pcm(path):
    return _run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:a:0',
                 '-vn', '-c:a', 'pcm_f32le', '-f', 'f32le', '-'])[0]


def _frame_hashes(path, destination):
    output, _ = _run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(path), '-map', '0:v:0',
                      '-an', '-fps_mode', 'passthrough', '-f', 'framemd5', '-'])
    destination.write_bytes(output)
    return [(parts[-2].strip(), parts[-1].strip())
            for line in output.decode().splitlines() if line and not line.startswith('#')
            for parts in [line.split(',')]]


def _concat_line(path):
    value = str(path.resolve())
    if '\n' in value or '\r' in value:
        raise ValueError('input paths cannot contain line breaks')
    return "file '" + value.replace("'", "'\\''") + "'\n"


def assemble_preview(inputs, output_dir):
    """Write raw_concat.mov, derived_preview.mov and verification.json locally.

    Requires compatible constant-rate video and equal audio rates/layouts.
    Internal timestamp gaps are rejected. Existing output directories must be
    empty so previously produced artifacts cannot be overwritten.
    """
    inputs = [Path(path).resolve() for path in inputs]
    if not inputs or any(not path.is_file() for path in inputs):
        raise ValueError('existing source files are required')
    folder = Path(output_dir).resolve()
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('output directory must be empty; existing artifacts are preserved')
    folder.mkdir(parents=True, exist_ok=True)
    report = {'description': 'Derived preview, not an original recording. Video frames stream-copied; audio boundary trim/technical-silence padding and AAC re-encoding.',
              'script_sha256': _sha(__file__), 'sources': [], 'render_commands': [], 'checks': {}}
    source_hashes = []
    expected_pcm = bytearray()
    total_time = Fraction(0)
    previous_samples = 0
    filters = []
    signature = None
    raw_listing, timed_listing = '', ''
    for index, path in enumerate(inputs):
        metadata = _probe(path, ['-show_streams', '-show_format'])
        video = next(row for row in metadata['streams'] if row['codec_type'] == 'video')
        audio = next(row for row in metadata['streams'] if row['codec_type'] == 'audio')
        fps = Fraction(video['r_frame_rate'])
        rate, channels = int(audio['sample_rate']), int(audio['channels'])
        current = (video['codec_name'], video['width'], video['height'], video.get('pix_fmt'), fps,
                   rate, channels, audio.get('channel_layout'))
        if signature is not None and signature != current:
            raise ValueError('source codec, dimensions, frame rate and audio layout must match')
        signature = current
        video_frames, audio_frames = _frames(path, 'v:0'), _frames(path, 'a:0')
        video_points, audio_points = _pts(video_frames), _pts(audio_frames)
        if not timing_summary(video_points, float(1 / fps))['uniform']:
            raise ValueError('source video does not have a uniform frame clock')
        for left, right in zip(audio_frames, audio_frames[1:]):
            expected = float(left['best_effort_timestamp_time']) + int(left['nb_samples']) / rate
            if abs(float(right['best_effort_timestamp_time']) - expected) > 2 / rate:
                raise ValueError('source audio has internal timestamp gaps; refusing to shift content')
        duration = Fraction(len(video_frames), 1) / fps
        total_time += duration
        end_samples = round(total_time * rate)
        pcm = _audio_pcm(path)
        sample_bytes = channels * 4
        source_samples = len(pcm) // sample_bytes
        if len(pcm) % sample_bytes or source_samples != sum(int(row['nb_samples']) for row in audio_frames):
            raise ValueError('decoded audio sample count does not match timestamp evidence')
        plan = audio_plan(duration, rate, source_samples, audio_points[0] - video_points[0],
                          target_samples=end_samples - previous_samples)
        trim_start, used = plan['trim_head_samples'], plan['used_samples']
        expected_pcm.extend(bytes(plan['pad_head_samples'] * sample_bytes))
        expected_pcm.extend(pcm[trim_start * sample_bytes:(trim_start + used) * sample_bytes])
        expected_pcm.extend(bytes(plan['pad_tail_samples'] * sample_bytes))
        chain = '[%d:a:0]atrim=start_sample=%d:end_sample=%d,asetpts=N/SR/TB' % (index, trim_start, trim_start + used)
        if plan['pad_head_samples']:
            chain += ',adelay=%dS:all=1' % plan['pad_head_samples']
        if plan['pad_tail_samples']:
            chain += ',apad=pad_len=%d' % plan['pad_tail_samples']
        chain += ',atrim=end_sample=%d,asetpts=N/SR/TB[a%d]' % (plan['target_samples'], index)
        filters.append(chain)
        padding = []
        for position, count, local_start in [('head', plan['pad_head_samples'], 0),
                                             ('tail', plan['pad_tail_samples'], plan['pad_head_samples'] + used)]:
            if count:
                padding.append({'position': position, 'samples': count, 'seconds': count / rate,
                                'segment_start_seconds': local_start / rate,
                                'preview_start_seconds': (previous_samples + local_start) / rate})
        packets = _probe(path, ['-select_streams', 'a:0', '-show_packets', '-show_entries',
                                'packet=pts_time,dts_time,duration_time,side_data_list'])['packets']
        hashes = _frame_hashes(path, folder / ('source_%02d.framemd5' % (index + 1)))
        source_hashes.extend(hashes)
        report['sources'].append({'file': str(path), 'sha256': _sha(path), 'video_frames': len(video_frames),
                                  'video_duration_seconds': float(duration), 'video_start_pts_seconds': video_points[0],
                                  'audio_first_decoded_pts_seconds': audio_points[0],
                                  'audio_end_decoded_pts_seconds': audio_points[-1] + int(audio_frames[-1]['nb_samples']) / rate,
                                  'first_audio_packets': packets[:2], 'audio_plan': plan,
                                  'technical_silence_padding': padding})
        previous_samples = end_samples
        raw_listing += _concat_line(path)
        timed_listing += _concat_line(path) + 'duration %.12f\n' % float(duration)
    raw_list, timed_list = folder / 'raw_concat.txt', folder / 'video_timed_concat.txt'
    raw_list.write_text(raw_listing)
    timed_list.write_text(timed_listing)

    def render(arguments):
        argv = ['ffmpeg', '-nostdin', '-v', 'warning', '-n'] + arguments
        report['render_commands'].append(argv)
        return _run(argv)[1]

    raw_path, video_path = folder / 'raw_concat.mov', folder / 'video_clock.mov'
    report['raw_concat_warnings'] = render(['-f', 'concat', '-safe', '0', '-i', str(raw_list),
                                            '-map', '0:v:0', '-map', '0:a:0', '-c', 'copy', str(raw_path)])
    warnings = render(['-f', 'concat', '-safe', '0', '-i', str(timed_list), '-map', '0:v:0',
                       '-an', '-c:v', 'copy', str(video_path)])
    audio_path = folder / 'aligned_audio.wav'
    audio_inputs = [arg for path in inputs for arg in ('-i', str(path))]
    filters.append(''.join('[a%d]' % i for i in range(len(inputs))) + 'concat=n=%d:v=0:a=1[aout]' % len(inputs))
    warnings += render(audio_inputs + ['-filter_complex', ';'.join(filters), '-map', '[aout]',
                                       '-c:a', 'pcm_f32le', str(audio_path)])
    preview = folder / 'derived_preview.mov'
    warnings += render(['-i', str(video_path), '-ch_layout', audio['channel_layout'], '-i', str(audio_path), '-map', '0:v:0', '-map', '1:a:0',
                         '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', str(preview)])
    preview_hashes = _frame_hashes(preview, folder / 'preview.framemd5')
    aligned_pcm = _audio_pcm(audio_path)
    preview_timing = timing_summary(_pts(_frames(preview, 'v:0')), float(1 / fps))
    raw_timing = timing_summary(_pts(_frames(raw_path, 'v:0')), float(1 / fps))
    metadata = _probe(preview, ['-show_streams', '-show_format'])
    final_video = next(row for row in metadata['streams'] if row['codec_type'] == 'video')
    final_audio = next(row for row in metadata['streams'] if row['codec_type'] == 'audio')
    packets = _probe(preview, ['-show_packets', '-show_entries', 'packet=stream_index,dts_time'])['packets']
    dts = {}
    for row in packets:
        if 'dts_time' not in row:
            raise ValueError('output packet lacks DTS')
        dts.setdefault(row['stream_index'], []).append(float(row['dts_time']))
    dts_checks = {str(index): all(b > a for a, b in zip(values, values[1:])) for index, values in dts.items()}
    av_delta = abs(float(final_video['duration']) - float(final_audio['duration']))
    report.update({'preview_file': str(preview), 'preview_sha256': _sha(preview), 'preview_warnings': warnings,
                   'video': {'fps': str(fps), 'source_frame_count': len(source_hashes),
                             'preview_frame_count': len(preview_hashes), 'duration_seconds': float(total_time),
                             'raw_concat_clock': raw_timing, 'preview_clock': preview_timing},
                   'audio': {'sample_rate': rate, 'channels': channels, 'aligned_pcm_samples': len(aligned_pcm) // sample_bytes,
                             'target_samples': previous_samples, 'aligned_pcm_sha256': hashlib.sha256(aligned_pcm).hexdigest(),
                             'expected_trim_pad_pcm_sha256': hashlib.sha256(expected_pcm).hexdigest(),
                             'aac_duration_seconds': float(final_audio['duration']), 'av_duration_delta_seconds': av_delta,
                             'av_duration_tolerance_seconds': 0.002,
                             'note': 'PCM matches original decoded samples with documented trim/padding. Final AAC is lossy re-encoding, not bit-identical audio.'},
                   'dts_strictly_increasing_by_stream': dts_checks})
    report['checks'] = {'decoded_video_frames_identical': source_hashes == preview_hashes,
                        'frame_count_unchanged': len(source_hashes) == len(preview_hashes),
                        'aligned_pcm_matches_trim_pad_only': aligned_pcm == expected_pcm,
                        'video_pts_uniform': preview_timing['uniform'],
                        'all_dts_strictly_increasing': bool(dts_checks) and all(dts_checks.values()),
                        'audio_video_lengths_aligned': av_delta <= 0.002,
                        'no_timestamp_warnings': not bool(re.search(r'non.?monoton|dts.*out of order', warnings, re.I)),
                        'sources_unchanged': all(_sha(path) == row['sha256'] for path, row in zip(inputs, report['sources']))}
    (folder / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if not all(report['checks'].values()):
        raise RuntimeError('preview verification failed; see verification.json')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', action='append', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    report = assemble_preview(args.input, args.output_dir)
    print(json.dumps({'preview_file': report['preview_file'], 'checks': report['checks'],
                      'video': report['video'], 'audio': report['audio']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
