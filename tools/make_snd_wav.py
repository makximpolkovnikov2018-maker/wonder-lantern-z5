# SPDX-License-Identifier: GPL-2.0-or-later
"""把任意 WAV 转成 bootsound/silentsound 能播放的 A:\\SND.WAV（纯 Python，无第三方依赖）。

模块（SDK wav.c）只认“标准 44 字节文件头”：0x28 处即 data 长度、数据从 44 字节起；长度 < 2000 字节视为没有
文件；最多读 256 KiB（48 kHz 立体声约 1.365 s）；音频引擎固定 48 kHz / 16 位 / 立体声。
本工具按块解析输入（跳过 LIST、fact 等），支持 PCM 8/16/24/32 位、32 位浮点及 EXTENSIBLE 格式，
混成立体声、用 Lanczos-3 重采样到 48 kHz、必要时截断并淡出，写到新的 output/ 目录，再用与模块相同的
规则复核。结果仍需上机试听（`camera_verified: false`）。
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path.cwd()
RATE, CHANNELS, CAP = 48000, 2, 240 * 1024
MIN_BYTES = 2000


def parse_wav(data):
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('not a RIFF/WAVE file')
    fmt = pcm = None
    off = 12
    while off + 8 <= len(data):
        cid, size = data[off:off + 4], struct.unpack_from('<I', data, off + 4)[0]
        body = data[off + 8:off + 8 + size]
        if cid == b'fmt ':
            fmt = body
        elif cid == b'data':
            pcm = body
        off += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError('missing fmt or data chunk')
    tag, channels, rate, _, align, bits = struct.unpack_from('<HHIIHH', fmt)
    if tag == 0xFFFE and len(fmt) >= 40:
        tag = struct.unpack_from('<H', fmt, 24)[0]        # SubFormat GUID's first word
    if tag not in (1, 3) or not channels or not rate or align != channels * bits // 8:
        raise ValueError(f'unsupported WAV format tag={tag} channels={channels} bits={bits}')
    return tag, channels, rate, bits, pcm[:len(pcm) - len(pcm) % align]


def decode(tag, channels, bits, pcm):
    """Return per-channel float lists in [-1, 1]."""
    width = bits // 8
    count = len(pcm) // width
    if tag == 3 and bits == 32:
        values = struct.unpack(f'<{count}f', pcm)
    elif bits == 8:
        values = [(b - 128) / 128 for b in pcm]
    elif bits == 16:
        values = [v / 32768 for v in struct.unpack(f'<{count}h', pcm)]
    elif bits == 24:
        values = [int.from_bytes(pcm[i:i + 3], 'little', signed=True) / 8388608 for i in range(0, len(pcm), 3)]
    elif bits == 32:
        values = [v / 2147483648 for v in struct.unpack(f'<{count}i', pcm)]
    else:
        raise ValueError(f'unsupported sample width {bits}')
    return [list(values[c::channels]) for c in range(channels)]


def to_stereo(chans):
    if len(chans) == 1:
        return [chans[0], chans[0]]
    if len(chans) == 2:
        return chans
    left = [sum(f) / len(chans) for f in zip(*chans)]       # >2 channels: average both sides
    return [left, left]


def resample(x, src, dst, taps=3):
    """Lanczos resampler; widens the kernel when downsampling (anti-aliasing)."""
    if src == dst:
        return list(x)
    ratio = dst / src
    scale = min(1.0, ratio)
    support = taps / scale
    out = []
    n_out = int(len(x) * ratio)
    for i in range(n_out):
        t = i / ratio
        lo, hi = max(0, math.floor(t - support) + 1), min(len(x) - 1, math.floor(t + support))
        acc = norm = 0.0
        for j in range(lo, hi + 1):
            u = (t - j) * scale
            w = 1.0 if u == 0 else (taps * math.sin(math.pi * u) * math.sin(math.pi * u / taps)) / (math.pi * math.pi * u * u)
            acc += x[j] * w
            norm += w
        out.append(acc / norm if norm else 0.0)
    return out


def encode(left, right, fade_frames):
    n = len(left)
    frames = []
    for i in range(n):
        g = min(1.0, (n - i) / fade_frames) if fade_frames else 1.0
        for v in (left[i] * g, right[i] * g):
            frames.append(max(-32768, min(32767, round(v * 32767))))
    return struct.pack(f'<{len(frames)}h', *frames)


def canonical(pcm):
    return (b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE' +
            b'fmt ' + struct.pack('<IHHIIHH', 16, 1, CHANNELS, RATE, RATE * 4, 4, 16) +
            b'data' + struct.pack('<I', len(pcm)) + pcm)


def module_check(data):
    """Same acceptance as SDK z5_wav_load + bootsound: RIFF, length at 0x28, >= 2000, <= cap."""
    if len(data) < 44 or data[:4] != b'RIFF':
        return 'missing the 44-byte RIFF header'
    dlen = struct.unpack_from('<I', data, 0x28)[0]
    if dlen == 0 or dlen > CAP:
        dlen = CAP
    dlen &= ~3
    if dlen < MIN_BYTES:
        return f'module would read only {dlen} bytes (treated as no WAV)'
    if data[0x24:0x28] != b'data' or len(data) < 44 + dlen:
        return 'data does not start at byte 44'
    tag, channels, rate, _, _, bits = struct.unpack_from('<HHIIHH', data, 20)
    if (tag, channels, rate, bits) != (1, CHANNELS, RATE, 16):
        return 'not 48 kHz / stereo / 16-bit PCM'
    return None


def convert(data, gain_db=0.0):
    tag, channels, rate, bits, pcm = parse_wav(data)
    left, right = to_stereo(decode(tag, channels, bits, pcm))
    left, right = resample(left, rate, RATE), resample(right, rate, RATE)
    gain = 10 ** (gain_db / 20)
    left, right = [v * gain for v in left], [v * gain for v in right]
    max_frames = CAP // 4
    truncated = len(left) > max_frames
    left, right = left[:max_frames], right[:max_frames]
    peak = max((abs(v) for v in left + right), default=0.0)
    out = canonical(encode(left, right, int(0.01 * RATE) if truncated else 0))
    info = dict(source_format=dict(tag=tag, channels=channels, rate=rate, bits=bits,
                                   seconds=round(len(pcm) / (channels * bits // 8) / rate, 3)),
                output_seconds=round(len(left) / RATE, 3), output_bytes=len(out), truncated=truncated,
                clipped=peak > 1.0, peak=round(peak, 3))
    return out, info


def read_input(path):
    if path.suffix.lower() != '.mp3':
        return path.read_bytes(), None
    ffmpeg = shutil.which('ffmpeg') or shutil.which('ffmpeg.exe')
    if not ffmpeg:
        configured = Path(os.environ.get('FFMPEG', ''))
        ffmpeg = str(configured) if configured.is_file() else None
    if not ffmpeg:
        raise ValueError('MP3 input requires ffmpeg. Install ffmpeg, add it to PATH, or set FFMPEG to its full path.')
    with tempfile.TemporaryDirectory(prefix='z5-mp3-') as temp:
        wav = Path(temp) / 'decoded.wav'
        command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path),
                   '-vn', '-ac', str(CHANNELS), '-ar', str(RATE), '-c:a', 'pcm_s16le', str(wav)]
        try:
            subprocess.run(command, check=True)
        except subprocess.CalledProcessError as exc:
            raise ValueError(f'ffmpeg could not decode the MP3 file (exit code {exc.returncode})') from exc
        return wav.read_bytes(), 'ffmpeg'


def main():
    p = argparse.ArgumentParser(description='Convert MP3 or WAV to Nikon Z 5 SHUTTER audio (max 1.28 s).')
    p.add_argument('input', type=Path, help='input WAV or MP3 file')
    p.add_argument('--slot', type=int, choices=range(1, 6), help='save as 1.WAV through 5.WAV instead of SND.WAV')
    p.add_argument('--gain-db', type=float, default=0.0, help='optional gain, e.g. -3')
    p.add_argument('--out', type=Path, help='new directory for the result (default: output/snd-<time>)')
    a = p.parse_args()
    source_path = a.input.resolve()
    if not source_path.is_file():
        p.error(f'input file not found: {source_path}')
    source = source_path.read_bytes()
    decoded, decoder = read_input(source_path)
    out_data, info = convert(decoded, a.gain_db)
    if decoder:
        info['decoder'] = decoder
    problem = module_check(out_data)
    if problem:
        p.error('converted file fails the module check: ' + problem)
    out = (a.out or ROOT / f'output/snd-{time.strftime("%Y%m%d-%H%M%S")}').resolve()
    out.mkdir(parents=True, exist_ok=False)
    output_name = f'{a.slot}.WAV' if a.slot else 'SND.WAV'
    (out / output_name).write_bytes(out_data)
    info.update(input=str(a.input), input_sha256=hashlib.sha256(source).hexdigest(),
                input_module_check=module_check(source) or 'ok',
                output_sha256=hashlib.sha256(out_data).hexdigest(), gain_db=a.gain_db, camera_verified=False)
    (out / 'manifest.json').write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(info, indent=2, ensure_ascii=False))
    print(out / output_name)


if __name__ == '__main__':
    main()
