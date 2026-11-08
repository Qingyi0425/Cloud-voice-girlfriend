"""电脑扬声器自检：播放一段 3 秒的提示音，不需要联网、不调用豆包。

用法（在项目目录打开 PowerShell）：

    .venv\\Scripts\\python.exe 测试扬声器.py

能听到“嘀——”的长音，说明小岚服务播放回复语音这条路是通的。
"""

import math
import struct
import sys

from app.playback import SpeakerPlayer, output_device_count

SAMPLE_RATE = 24000
SECONDS = 3


def build_tone() -> bytes:
    total = SAMPLE_RATE * SECONDS
    fade = SAMPLE_RATE // 10
    samples = []
    for index in range(total):
        amplitude = 9000
        if index < fade:
            amplitude = amplitude * index // fade
        elif index > total - fade:
            amplitude = amplitude * (total - index) // fade
        samples.append(int(amplitude * math.sin(2 * math.pi * 440 * index / SAMPLE_RATE)))
    return b"".join(struct.pack("<h", value) for value in samples)


def main() -> int:
    devices = output_device_count()
    print(f"系统音频输出设备数量：{devices}")
    if devices <= 0:
        print("没有检测到音频输出设备，请检查音箱、耳机或声卡驱动。")
        return 1
    player = SpeakerPlayer()
    if not player.play_pcm(build_tone(), SAMPLE_RATE):
        print("播放失败，可能是系统音量静音或设备被占用。")
        return 1
    print("已播放 3 秒提示音。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
