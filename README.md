# 岚心——AI智能情感陪伴机器人

本目录包含项目的电脑端服务和 ESP32-S3 硬件固件源码。它们分工不同：大模型、语音识别和语音合成服务运行在电脑上；开发板负责录音和屏幕显示，回复语音由电脑默认扬声器播放。

## 文件夹

- `01_模型端/岚心 —— AI智能情感陪伴机器人`：本地网页、后端服务、人格配置和测试代码。它调用配置好的云端大模型、豆包语音识别和豆包语音合成接口；模型权重和 API 密钥不包含在交付副本中。
- `02_硬件端/xiaolan_voice_robot`：ESP32-S3 Arduino 固件和烧录脚本。固件使用 INMP441 麦克风、TFT 彩屏和电脑扬声器，外接按钮接 GPIO13，BOOT 键可备用。
- `00_项目文档`：项目方案、接线图等文档可放在这里。

## 运行顺序

1. 在电脑端进入 `01_模型端/岚心 —— AI智能情感陪伴机器人`，复制 `.env.example` 并改名为 `.env`，填入自己的大模型、豆包 TTS 和语音识别接口信息。密钥不要提交或分享。
2. 按该目录的 `README.md` 安装 Python 依赖并启动 `启动小岚.bat`。电脑和开发板需要连接同一个 Wi-Fi；电脑端口需允许开发板访问。
3. 打开 `02_硬件端/xiaolan_voice_robot/xiaolan_voice_robot.ino`，把 `WIFI_SSID`、`WIFI_PASSWORD`、`SERVER_HOST` 中的占位内容改成当前 Wi-Fi 名称、密码和电脑局域网 IPv4 地址，再用 Arduino IDE 编译并烧录。请勿把真实配置提交到公开仓库。
4. 开发板上电后，按住外接按钮或 BOOT 键说话，松开后上传录音；服务处理后，文字显示在屏幕上，语音通过电脑默认扬声器播放。

## 当前硬件引脚

| 模块 | 模块引脚 | ESP32-S3 |
|---|---|---|
| TFT | SCL / SDA / RES / DC / CS | GPIO12 / GPIO11 / GPIO8 / GPIO9 / GPIO10 |
| INMP441 | SCK / WS / SD | GPIO4 / GPIO5 / GPIO6 |
| TFT | VCC / BL | 3V3 / 3V3 |
| INMP441 | VDD | 3V3 |
| 所有模块 | GND | ESP32-S3 GND（共地） |

外接按钮接 `GPIO13` 和 `GND`，四脚按钮使用两组触点各一脚。成品不接功放和喇叭，回复由电脑扬声器播放。屏幕型号/控制器不同可能需要调整固件初始化参数。

## 说明

- ESP32-S3 上运行的是设备固件，不运行大语言模型；模型和语音接口服务依赖电脑与网络。
- 人物图片在模型端 `assets/` 中，录音时不要循环刷新整张图片，避免占用 CPU/SPI 导致麦克风丢采样。
- 复制包不包含 `.env`、虚拟环境、日志、对话记录和编译产物。首次运行需在模型端自行安装依赖并配置密钥。
- `flash_xiaolan_speaker.bat` 当前使用本机 Arduino CLI 路径 `D:\Arduino\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`；若路径不同，请先修改脚本，或直接用 Arduino IDE 打开 `.ino` 编译烧录。
- Arduino IDE 板卡选项见模型端 README：`ESP32S3 Dev Module`、16MB Flash、OPI PSRAM、Huge APP 分区和 USB CDC On Boot。
