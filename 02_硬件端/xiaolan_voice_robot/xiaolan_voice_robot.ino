// 小岚语音机器人 —— ESP32-S3-N16R8 + INMP441 + ST7735 TFT + MAX98357A 喇叭功放
//
// 用法：按住外接说话键（GPIO13 接地）或开发板自带的 BOOT 键说话，松开后自动上传到电脑。
// 屏幕显示“正在录音 → 正在识别 → 识别内容 → 小岚回复”，
// 回复由电脑默认扬声器播放；ESP32 负责录音、屏幕显示和口型动画。
//
// 需要填写：WIFI_SSID、WIFI_PASSWORD、SERVER_HOST（电脑的局域网地址）。
// Arduino IDE 板卡选项：ESP32S3 Dev Module / Flash Size 16MB / PSRAM OPI PSRAM /
// Partition Scheme Huge APP / USB CDC On Boot Enabled。

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>
#include <U8g2_for_Adafruit_GFX.h>
#include <driver/i2s.h>
#include <esp_wifi.h>
#include <vector>
#include <math.h>
#include "xiaolan_faces.h"

// ==================== 1. 配置 ====================
// 注意：手机热点的真实名称可能带看不见的空格，请以扫描到的名称为准。
// 这里写不准也没关系，固件会自己扫描并按扫到的原始名称去连接。
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// 电脑地址：在电脑上执行 ipconfig，看“无线局域网适配器 WLAN”的 IPv4 地址。
// 电脑换网络后这里要跟着改。
const char* SERVER_HOST = "YOUR_COMPUTER_LAN_IP";
const uint16_t SERVER_PORT = 8001;

// ==================== 2. 接线 ====================
constexpr int TFT_SCLK = 12;
constexpr int TFT_MOSI = 11;
constexpr int TFT_CS = 10;
constexpr int TFT_DC = 9;
constexpr int TFT_RST = 8;

constexpr int MIC_SCK = 4;
constexpr int MIC_WS = 5;
constexpr int MIC_SD = 6;
constexpr int SPEAKER_DIN = 7;

constexpr int BOOT_BUTTON_PIN = 0;  // 开发板自带的 BOOT 键，按住为低电平
constexpr int TALK_BUTTON_PIN = 13;  // 外接按钮：GPIO13 与 GND，按下为低电平

// ==================== 3. 录音参数 ====================
constexpr uint32_t SAMPLE_RATE = 16000;      // 豆包流式识别要求 16kHz
constexpr uint32_t SPEAKER_SAMPLE_RATE = 24000;
constexpr uint32_t MAX_RECORD_MS = 15000;    // 单次最长录音
constexpr uint32_t MIN_RECORD_MS = 300;      // 少于 0.3 秒的录音直接丢弃
constexpr uint32_t STT_MIN_UPLOAD_MS = 1000; // 短录音补静音到 1 秒，提升豆包识别稳定性
constexpr size_t MAX_SAMPLES = SAMPLE_RATE * MAX_RECORD_MS / 1000;
constexpr size_t MAX_WAV_BYTES = 44 + MAX_SAMPLES * 2;
constexpr size_t MAX_REPLY_PCM_BYTES = 2 * 1024 * 1024;

// ==================== 4. 全局对象 ====================
Adafruit_ST7735 tft(TFT_CS, TFT_DC, TFT_RST);
U8G2_FOR_ADAFRUIT_GFX zhFont;

uint8_t* wavBuffer = nullptr;      // PSRAM 里的整段 WAV
size_t recordedSamples = 0;
bool wifiReady = false;
bool idleScreenDrawn = false;
int lastLevelBlocks = -1;
uint32_t lastFaceFrameMs = 0;
uint8_t faceFrame = 0;

// ==================== 5. 显示工具 ====================
void drawHeader(const char* title, uint16_t color = ST77XX_CYAN) {
  zhFont.setFont(u8g2_font_wqy12_t_gb2312);
  zhFont.setForegroundColor(color);
  zhFont.setCursor(3, 14);
  zhFont.print(title);
  tft.drawFastHLine(3, 18, 122, 0x7BEF);
}

void drawLine(int y, const String& text, uint16_t color = ST77XX_WHITE) {
  zhFont.setFont(u8g2_font_wqy12_t_gb2312);
  zhFont.setForegroundColor(color);
  zhFont.setCursor(3, y);
  zhFont.print(text.c_str());
}

void showMessage(const char* title, const String& line1 = "", const String& line2 = "", uint16_t color = ST77XX_RED) {
  tft.fillScreen(ST77XX_BLACK);
  drawHeader("小岚", color);
  drawLine(52, title, color);
  if (line1.length()) drawLine(74, line1, ST77XX_WHITE);
  if (line2.length()) drawLine(94, line2, 0x7BEF);
}

// 十格电平条，画在指定区域
void drawLevelBar(int y, int blocks, uint16_t color) {
  constexpr int left = 4, width = 10, height = 12, gap = 2;
  for (int i = 0; i < 10; ++i) {
    const int x = left + i * (width + gap);
    if (i < blocks) {
      tft.fillRect(x, y, width, height, color);
    } else {
      tft.fillRect(x, y, width, height, ST77XX_BLACK);
      tft.drawRect(x, y, width, height, 0x4208);
    }
  }
}

bool anyButtonDown();

// 长文本按宽度折行后分页显示；按任一说话键可以提前跳过
// holdLastPage = true 时，最后一页会一直停住，直到按键（方便听完语音再看文字）
bool waitOrPress(uint32_t ms) {
  const uint32_t start = millis();
  while (millis() - start < ms) {
    if (anyButtonDown()) {
      delay(150);
      return true;
    }
    delay(20);
  }
  return false;
}

// 一直等到按键为止（最长等 5 分钟，避免永远卡住）
bool waitForPress(uint32_t timeoutMs = 300000) {
  const uint32_t start = millis();
  while (millis() - start < timeoutMs) {
    if (anyButtonDown()) {
      delay(150);
      return true;
    }
    delay(20);
  }
  return false;
}

void showPagedText(const char* heading, const String& body, bool holdLastPage = false) {
  std::vector<String> lines;
  String line;
  for (size_t index = 0; index < body.length();) {
    const uint8_t first = static_cast<uint8_t>(body[index]);
    if (first == '\n') {
      lines.push_back(line);
      line = "";
      ++index;
      continue;
    }
    size_t bytes = first < 0x80 ? 1 : (first < 0xE0 ? 2 : (first < 0xF0 ? 3 : 4));
    if (index + bytes > body.length()) break;
    const String glyph = body.substring(index, index + bytes);
    const String candidate = line + glyph;
    if (!line.isEmpty() && zhFont.getUTF8Width(candidate.c_str()) > 120) {
      lines.push_back(line);
      line = glyph;
    } else {
      line = candidate;
    }
    index += bytes;
    if (lines.size() >= 120) break;
  }
  if (!line.isEmpty() && lines.size() < 120) lines.push_back(line);
  if (lines.empty()) lines.push_back("（没有内容）");

  constexpr size_t LINES_PER_PAGE = 8;
  const size_t pageCount = (lines.size() + LINES_PER_PAGE - 1) / LINES_PER_PAGE;
  for (size_t page = 0; page < pageCount; ++page) {
    tft.fillScreen(ST77XX_BLACK);
    drawHeader(heading);
    const size_t start = page * LINES_PER_PAGE;
    const size_t end = min(start + LINES_PER_PAGE, lines.size());
    int y = 32;
    for (size_t i = start; i < end; ++i) {
      drawLine(y, lines[i].c_str());
      y += 13;
    }
    const bool lastPage = (page + 1 == pageCount);
    String footer = pageCount > 1 ? String(page + 1) + "/" + String(pageCount) + "  " : "";
    if (lastPage) {
      footer += "按 BOOT 返回";
    } else {
      footer += "按 BOOT 翻页";
    }
    zhFont.setForegroundColor(ST77XX_YELLOW);
    zhFont.setCursor(3, 154);
    zhFont.print(footer);
    if (lastPage && holdLastPage) {
      waitForPress();
      return;
    }
    if (waitOrPress(4200)) return;
  }
}

// ==================== 6. 待机界面 ====================
void drawFaceFrame(const uint16_t* pixels, const uint8_t* mask) {
  constexpr int16_t x = (128 - XIAOLAN_FACE_W) / 2;
  constexpr int16_t y = 22;
  tft.drawRGBBitmap(x, y, pixels, mask, XIAOLAN_FACE_W, XIAOLAN_FACE_H);
}

void drawClosedFace() {
  drawFaceFrame(xiaolan_closed_pixels, xiaolan_closed_mask);
}

void drawTalkingFace(bool animate) {
  if (!animate) {
    drawClosedFace();
    return;
  }
  // 每个口型停留约 300ms：闭嘴 -> 微张 -> 张嘴 -> 微张。
  const uint8_t phase = (millis() / 300) % 4;
  if (phase == 0) drawFaceFrame(xiaolan_closed_pixels, xiaolan_closed_mask);
  else if (phase == 1 || phase == 3) drawFaceFrame(xiaolan_small_pixels, xiaolan_small_mask);
  else drawFaceFrame(xiaolan_open_pixels, xiaolan_open_mask);
}

void drawIdleScreen() {
  tft.fillScreen(ST77XX_BLACK);
  drawHeader("小岚");
  drawClosedFace();
  drawLine(143, "待机中·按键说话", ST77XX_GREEN);
  drawLevelBar(148, 3, ST77XX_GREEN);
  lastLevelBlocks = -1;
  idleScreenDrawn = true;
}

// ==================== 7. 麦克风 ====================
bool startMicrophone() {
  i2s_config_t config = {};
  config.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_RX);
  config.sample_rate = SAMPLE_RATE;
  config.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  config.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  config.dma_buf_count = 8;
  config.dma_buf_len = 256;
  config.use_apll = false;
  config.tx_desc_auto_clear = false;
  config.fixed_mclk = 0;
  if (i2s_driver_install(I2S_NUM_0, &config, 0, nullptr) != ESP_OK) return false;

  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = MIC_SCK;
  pins.ws_io_num = MIC_WS;
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = MIC_SD;
  return i2s_set_pin(I2S_NUM_0, &pins) == ESP_OK;
}

bool startSpeaker() {
  i2s_config_t config = {};
  config.mode = static_cast<i2s_mode_t>(I2S_MODE_MASTER | I2S_MODE_TX);
  config.sample_rate = SPEAKER_SAMPLE_RATE;
  config.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  config.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  config.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  config.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
  config.dma_buf_count = 8;
  config.dma_buf_len = 256;
  config.use_apll = false;
  config.tx_desc_auto_clear = true;
  config.fixed_mclk = 0;
  if (i2s_driver_install(I2S_NUM_0, &config, 0, nullptr) != ESP_OK) return false;

  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;
  pins.bck_io_num = MIC_SCK;
  pins.ws_io_num = MIC_WS;
  pins.data_out_num = SPEAKER_DIN;
  pins.data_in_num = I2S_PIN_NO_CHANGE;
  if (i2s_set_pin(I2S_NUM_0, &pins) != ESP_OK) {
    i2s_driver_uninstall(I2S_NUM_0);
    return false;
  }
  return true;
}

int base64Digit(char value) {
  if (value >= 'A' && value <= 'Z') return value - 'A';
  if (value >= 'a' && value <= 'z') return value - 'a' + 26;
  if (value >= '0' && value <= '9') return value - '0' + 52;
  if (value == '+') return 62;
  if (value == '/') return 63;
  return -1;
}

bool decodeBase64(const char* encoded, uint8_t* output, size_t capacity, size_t& outputLength) {
  outputLength = 0;
  uint32_t accumulator = 0;
  int bits = 0;
  for (const char* cursor = encoded; cursor && *cursor; ++cursor) {
    if (*cursor == '=') break;
    const int digit = base64Digit(*cursor);
    if (digit < 0) return false;
    accumulator = (accumulator << 6) | static_cast<uint32_t>(digit);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      if (outputLength >= capacity) return false;
      output[outputLength++] = static_cast<uint8_t>((accumulator >> bits) & 0xFF);
    }
  }
  return outputLength > 0 && (outputLength % 2 == 0);
}

bool playReplyPcm(const uint8_t* pcm, size_t pcmLength) {
  if (!pcm || pcmLength < 2 || pcmLength > MAX_REPLY_PCM_BYTES || (pcmLength % 2)) return false;
  if (!startSpeaker()) {
    Serial.println("喇叭 I2S 初始化失败");
    return false;
  }

  constexpr size_t MONO_SAMPLES_PER_CHUNK = 128;
  int16_t stereo[MONO_SAMPLES_PER_CHUNK * 2];
  const int16_t* mono = reinterpret_cast<const int16_t*>(pcm);
  const size_t sampleCount = pcmLength / sizeof(int16_t);
  const uint32_t playbackStarted = millis();
  bool success = true;

  for (size_t offset = 0; offset < sampleCount;) {
    const size_t count = min(MONO_SAMPLES_PER_CHUNK, sampleCount - offset);
    for (size_t i = 0; i < count; ++i) {
      stereo[i * 2] = mono[offset + i];
      stereo[i * 2 + 1] = mono[offset + i];
    }
    size_t written = 0;
    const size_t bytesToWrite = count * 2 * sizeof(int16_t);
    const esp_err_t result = i2s_write(I2S_NUM_0, stereo, bytesToWrite, &written, portMAX_DELAY);
    if (result != ESP_OK || written != bytesToWrite) {
      Serial.printf("喇叭 I2S 写入失败：错误码 %d，写入 %u/%u 字节\n",
                    static_cast<int>(result), static_cast<unsigned>(written),
                    static_cast<unsigned>(bytesToWrite));
      success = false;
      break;
    }
    offset += count;
  }

  const uint32_t playbackMs = static_cast<uint32_t>((sampleCount * 1000ULL + SPEAKER_SAMPLE_RATE - 1) / SPEAKER_SAMPLE_RATE);
  const uint32_t elapsedMs = millis() - playbackStarted;
  if (success && elapsedMs < playbackMs + 12) delay(playbackMs + 12 - elapsedMs);
  i2s_zero_dma_buffer(I2S_NUM_0);
  i2s_driver_uninstall(I2S_NUM_0);
  return success;
}

// 串口输入 t/T 时播放固定音调。它不经过网络和 TTS，只用于验证
// ESP32 -> MAX98357A -> 喇叭这一段硬件链路。
bool playSpeakerTestTone() {
  constexpr uint32_t TONE_MS = 1500;
  constexpr uint32_t TONE_HZ = 440;
  constexpr size_t SAMPLES_PER_CHUNK = 128;
  int16_t stereo[SAMPLES_PER_CHUNK * 2];
  const size_t totalSamples = SPEAKER_SAMPLE_RATE * TONE_MS / 1000;

  i2s_driver_uninstall(I2S_NUM_0);
  if (!startSpeaker()) {
    Serial.println("测试音：喇叭 I2S 初始化失败");
    startMicrophone();
    return false;
  }

  Serial.println("测试音：开始播放 440Hz");
  size_t offset = 0;
  while (offset < totalSamples) {
    const size_t count = min(SAMPLES_PER_CHUNK, totalSamples - offset);
    for (size_t i = 0; i < count; ++i) {
      const float phase = 2.0f * PI * static_cast<float>(TONE_HZ) * static_cast<float>(offset + i) /
                          static_cast<float>(SPEAKER_SAMPLE_RATE);
      const int16_t sample = static_cast<int16_t>(12000.0f * sinf(phase));
      stereo[i * 2] = sample;
      stereo[i * 2 + 1] = sample;
    }
    size_t written = 0;
    const size_t bytesToWrite = count * 2 * sizeof(int16_t);
    const esp_err_t result = i2s_write(I2S_NUM_0, stereo, bytesToWrite, &written, portMAX_DELAY);
    if (result != ESP_OK || written != bytesToWrite) {
      Serial.printf("测试音：I2S 写入失败，错误码 %d，写入 %u/%u 字节\n",
                    static_cast<int>(result), static_cast<unsigned>(written),
                    static_cast<unsigned>(bytesToWrite));
      i2s_zero_dma_buffer(I2S_NUM_0);
      i2s_driver_uninstall(I2S_NUM_0);
      startMicrophone();
      return false;
    }
    offset += count;
  }
  delay(80);
  i2s_zero_dma_buffer(I2S_NUM_0);
  i2s_driver_uninstall(I2S_NUM_0);
  startMicrophone();
  Serial.println("测试音：播放结束");
  return true;
}

void handleSerialCommands() {
  while (Serial.available() > 0) {
    const char command = static_cast<char>(Serial.read());
    if (command == 't' || command == 'T') {
      showMessage("喇叭测试", "正在播放 440Hz", "请听喇叭", ST77XX_YELLOW);
      playSpeakerTestTone();
      drawIdleScreen();
    }
  }
}

// 读一小段并返回峰值（0..32767），同时丢掉缓冲里的旧数据
int32_t readMicPeak(size_t* samplesRead = nullptr) {
  int32_t raw[256];
  size_t bytesRead = 0;
  if (i2s_read(I2S_NUM_0, raw, sizeof(raw), &bytesRead, 100) != ESP_OK || bytesRead == 0) {
    if (samplesRead) *samplesRead = 0;
    return 0;
  }
  const size_t count = bytesRead / sizeof(int32_t);
  int32_t peak = 0;
  for (size_t i = 0; i < count; ++i) {
    const int32_t value = raw[i] >> 16;
    const int32_t magnitude = value < 0 ? -value : value;
    if (magnitude > peak) peak = magnitude;
  }
  if (samplesRead) *samplesRead = count;
  return peak;
}

int levelBlocksOf(int32_t peak) {
  // 说话时 INMP441 的峰值通常在 1000~8000 之间，这里按对数分十格
  int blocks = 0;
  int32_t threshold = 40;
  while (blocks < 10 && peak > threshold) {
    ++blocks;
    threshold *= 2;
  }
  return blocks;
}

// ==================== 8. 按键 ====================
bool anyButtonDown() {
  return digitalRead(BOOT_BUTTON_PIN) == LOW || digitalRead(TALK_BUTTON_PIN) == LOW;
}

bool bootDown() { return anyButtonDown(); }

// 待机时持续刷新麦克风电平，顺便做按键去抖
void idleTick() {
  size_t count = 0;
  const int32_t peak = readMicPeak(&count);
  if (count == 0) return;
  const int blocks = levelBlocksOf(peak);
  if (blocks != lastLevelBlocks) {
    lastLevelBlocks = blocks;
    drawLevelBar(148, blocks, blocks >= 7 ? ST77XX_ORANGE : ST77XX_GREEN);
    tft.fillRect(3, 136, 122, 11, ST77XX_BLACK);
    zhFont.setForegroundColor(ST77XX_WHITE);
    zhFont.setCursor(3, 144);
    zhFont.print(String("峰值 ") + String(peak));
  }
}

// ==================== 9. 录音 ====================
void writeLe16(uint8_t* target, uint16_t value) {
  target[0] = value & 0xFF;
  target[1] = value >> 8;
}

void writeLe32(uint8_t* target, uint32_t value) {
  target[0] = value & 0xFF;
  target[1] = (value >> 8) & 0xFF;
  target[2] = (value >> 16) & 0xFF;
  target[3] = (value >> 24) & 0xFF;
}

void writeWavHeader(uint8_t* wav, size_t samples) {
  const uint32_t dataBytes = samples * 2;
  memcpy(wav, "RIFF", 4);
  writeLe32(wav + 4, 36 + dataBytes);
  memcpy(wav + 8, "WAVEfmt ", 8);
  writeLe32(wav + 16, 16);
  writeLe16(wav + 20, 1);
  writeLe16(wav + 22, 1);
  writeLe32(wav + 24, SAMPLE_RATE);
  writeLe32(wav + 28, SAMPLE_RATE * 2);
  writeLe16(wav + 32, 2);
  writeLe16(wav + 34, 16);
  memcpy(wav + 36, "data", 4);
  writeLe32(wav + 40, dataBytes);
}

void drawRecordingScreen() {
  tft.fillScreen(ST77XX_BLACK);
  drawHeader("正在听", ST77XX_ORANGE);
  drawTalkingFace(true);
  drawLevelBar(136, 0, ST77XX_GREEN);
  drawLine(153, "松开按钮结束", 0x7BEF);
}

void updateRecordingUi(uint32_t elapsedMs, int blocks) {
  // 录音时不要反复刷新整张 112x112 图片，避免占用 SPI 导致 I2S 麦克风丢采样。
  drawLevelBar(136, blocks, blocks >= 7 ? ST77XX_ORANGE : ST77XX_GREEN);
  tft.fillRect(3, 147, 122, 12, ST77XX_BLACK);
  zhFont.setForegroundColor(ST77XX_WHITE);
  zhFont.setCursor(3, 156);
  zhFont.print(String(elapsedMs / 1000.0f, 1) + "s");
}

// 按住录音，返回是否录到了足够长的音频
bool recordWhileHeld() {
  // 丢掉开始前缓冲里的旧数据
  for (int i = 0; i < 6; ++i) readMicPeak();

  drawRecordingScreen();
  recordedSamples = 0;
  const uint32_t start = millis();
  uint32_t lastUi = 0;
  int releaseFrames = 0;
  int32_t raw[128];

  while (true) {
    size_t bytesRead = 0;
    if (i2s_read(I2S_NUM_0, raw, sizeof(raw), &bytesRead, 200) != ESP_OK || bytesRead == 0) {
      return false;
    }
    const size_t count = bytesRead / sizeof(int32_t);
    int32_t peak = 0;
    for (size_t i = 0; i < count; ++i) {
      const int32_t value = raw[i] >> 16;
      const int32_t magnitude = value < 0 ? -value : value;
      if (magnitude > peak) peak = magnitude;
      if (recordedSamples < MAX_SAMPLES) {
        const size_t offset = 44 + recordedSamples * 2;
        wavBuffer[offset] = value & 0xFF;
        wavBuffer[offset + 1] = (value >> 8) & 0xFF;
        ++recordedSamples;
      }
    }

    const uint32_t elapsed = millis() - start;
    if (elapsed - lastUi > 100) {
      lastUi = elapsed;
      updateRecordingUi(elapsed, levelBlocksOf(peak));
    }

    // 松手检测：连续两帧为高电平才算松开
    releaseFrames = bootDown() ? 0 : releaseFrames + 1;
    if (releaseFrames >= 2) break;
    if (elapsed >= MAX_RECORD_MS) break;
  }

  return recordedSamples > (SAMPLE_RATE * MIN_RECORD_MS / 1000);
}

// ==================== 10. 上传与显示 ====================
bool uploadWav() {
  size_t uploadSamples = recordedSamples;
  const size_t minUploadSamples = SAMPLE_RATE * STT_MIN_UPLOAD_MS / 1000;
  if (uploadSamples < minUploadSamples) {
    const size_t paddingBytes = (minUploadSamples - uploadSamples) * sizeof(int16_t);
    memset(wavBuffer + 44 + uploadSamples * sizeof(int16_t), 0, paddingBytes);
    uploadSamples = minUploadSamples;
  }
  writeWavHeader(wavBuffer, uploadSamples);
  const size_t totalBytes = 44 + uploadSamples * 2;

  WiFiClient client;
  HTTPClient http;
  http.setTimeout(120000);
  http.setConnectTimeout(8000);
  const String url = String("http://") + SERVER_HOST + ":" + String(SERVER_PORT) + "/api/chat/audio";
  if (!http.begin(client, url)) {
    showMessage("打不开电脑服务", "检查地址和 Wi-Fi");
    return false;
  }
  http.addHeader("Content-Type", "audio/wav");
  http.addHeader("X-Play-Audio", "true");  // 让电脑本地扬声器播放回复
  http.addHeader("X-Allow-Partial", "true");  // 大模型不可用时也要拿回识别文字

  const uint32_t startedAt = millis();
  const int status = http.POST(wavBuffer, totalBytes);
  const String response = http.getString();
  http.end();
  Serial.printf("POST %s -> %d (%lu ms, %u samples, %u bytes)\n", url.c_str(), status,
                static_cast<unsigned long>(millis() - startedAt), static_cast<unsigned>(uploadSamples),
                static_cast<unsigned>(totalBytes));

  if (status != HTTP_CODE_OK) {
    Serial.println(response);
    JsonDocument errorDoc;
    if (deserializeJson(errorDoc, response) == DeserializationError::Ok) {
      String detail = errorDoc["detail"] | "服务请求失败";
      Serial.println(detail);
      showMessage("电脑返回错误", detail.c_str(), String("HTTP ") + String(status) + " 按 BOOT 重试");
    } else {
      showMessage("电脑返回错误", String("HTTP ") + String(status).c_str(), "详情看串口监视器");
    }
    return false;
  }

  JsonDocument doc;
  if (deserializeJson(doc, response) != DeserializationError::Ok) {
    Serial.println(response);
    showMessage("回复解析失败", "详情看串口监视器");
    return false;
  }

  const String transcript = doc["transcript"] | "";
  const String reply = doc["reply"] | "";
  const String error = doc["error"] | "";
  const char* audioBase64 = doc["audio"] | "";

  Serial.println("识别内容：");
  Serial.println(transcript);
  Serial.println("小岚回复：");
  Serial.println(reply);
  if (error.length()) {
    Serial.println("错误信息：");
    Serial.println(error);
  }

  Serial.printf("识别 %u 字节 / 回复 %u 字节 / PCM=%u 个编码字符\n",
                static_cast<unsigned>(transcript.length()),
                static_cast<unsigned>(reply.length()),
                static_cast<unsigned>(strlen(audioBase64)));

  if (transcript.length()) {
    // 识别内容自动翻页，不用按键
    showPagedText("你说", transcript);
  }
  const bool playedOnComputer = doc["played"] | false;
  if (playedOnComputer) {
    showMessage("电脑正在播放", "请听电脑扬声器", "开发板只显示文字", ST77XX_GREEN);
    waitOrPress(1200);
  } else if (error.length()) {
    Serial.println(error);
    showMessage("语音生成失败", error.c_str(), "仍可查看文字回复", ST77XX_ORANGE);
    waitOrPress(2200);
  } else {
    showMessage("电脑未播放声音", "检查电脑默认输出设备", "仍可查看文字回复", ST77XX_ORANGE);
    waitOrPress(1800);
  }
  if (reply.length()) {
    // 回复停在最后一页，听完语音后仍可查看
    showPagedText("小岚回复", reply, true);
  } else if (error.length()) {
    showPagedText("回复失败", error, true);
  } else {
    showMessage("没有拿到回复", "检查电脑上的服务", "");
    waitOrPress(2500);
  }
  return true;
}

// ==================== 11. 启动 ====================
volatile int wifiFailReason = 0;

void onWiFiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    wifiFailReason = info.wifi_sta_disconnected.reason;
    Serial.printf("Wi-Fi 断开，原因码 %d\n", wifiFailReason);
  }
}

const char* wifiReasonHint(int reason) {
  switch (reason) {
    case 2:
    case 15:
      return "密码可能不对";
    case 200:
      return "信号中断或太弱";
    case 201:
      return "完全没扫到这个热点";
    case 202:
      return "鉴权失败";
    case 203:
      return "关联失败";
    case 204:
      return "握手超时";
    case 205:
      return "连接失败";
    default:
      return "原因未知";
  }
}

// 连不上时扫描周围热点，直接显示在屏幕上
String trimmed(const char* text) {
  String value(text);
  value.trim();
  return value;
}

// 名称完全一致、忽略大小写、或以目标开头，都算作“就是这个热点”
bool sameAccessPoint(const String& scanned, const String& wanted) {
  if (scanned == wanted) return true;
  String a = scanned;
  String b = wanted;
  a.toUpperCase();
  b.toUpperCase();
  return a == b || a.startsWith(b);
}

void showScanReport(int targetIndex, int found) {
  String body;
  if (targetIndex >= 0) {
    String name = WiFi.SSID(targetIndex);
    name.trim();
    body = "找到 " + name + "\n信号 " + String(WiFi.RSSI(targetIndex)) + " dBm\n信道 " +
           String(WiFi.channel(targetIndex));
  } else {
    body = "没扫到 " + trimmed(WIFI_SSID) + "\n";
  }
  body += "\n共 " + String(found) + " 个网络，较强的是：\n";

  // 取信号最强的 6 个，方便和手机上的热点名称对照
  std::vector<int> shown;
  for (int round = 0; round < 6; ++round) {
    int best = -1;
    int bestRssi = -1000;
    for (int i = 0; i < found; ++i) {
      bool used = false;
      for (int index : shown) {
        if (index == i) {
          used = true;
          break;
        }
      }
      if (!used && WiFi.RSSI(i) > bestRssi) {
        bestRssi = WiFi.RSSI(i);
        best = i;
      }
    }
    if (best < 0) break;
    shown.push_back(best);
  }
  for (int index : shown) {
    String name = WiFi.SSID(index);
    name.trim();
    if (name.isEmpty()) name = "(隐藏)";
    body += name + " " + String(WiFi.RSSI(index)) + "dBm\n";
  }
  showPagedText("扫描结果", body);
}

bool connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.onEvent(onWiFiEvent);
  // 中国大陆允许 1-13 信道；关闭 802.11d 跟随，避免连接时信道表被热点改写
  esp_wifi_set_country_code("CN", false);
  delay(200);

  showMessage("正在扫描 Wi-Fi", "查找目标热点", "请稍等", ST77XX_CYAN);
  const int found = WiFi.scanNetworks();
  Serial.printf("扫描到 %d 个 Wi-Fi\n", found);
  for (int i = 0; i < found; ++i) {
    String name = WiFi.SSID(i);
    name.trim();
    Serial.printf("  [%d] %s  RSSI=%d  信道=%d\n", i + 1, name.c_str(), WiFi.RSSI(i), WiFi.channel(i));
  }

  const String wanted = trimmed(WIFI_SSID);
  int target = -1;
  int targetRssi = -1000;
  for (int i = 0; i < found; ++i) {
    String name = WiFi.SSID(i);
    name.trim();
    if (sameAccessPoint(name, wanted) && WiFi.RSSI(i) > targetRssi) {
      targetRssi = WiFi.RSSI(i);
      target = i;
    }
  }

  String ssidToUse = wanted;
  String targetRaw;                            // 原样保留扫描到的 SSID，不做 trim
  uint8_t targetBssid[6] = {0, 0, 0, 0, 0, 0};
  int targetChannel = 0;
  if (target >= 0) {
    targetRaw = WiFi.SSID(target);
    memcpy(targetBssid, WiFi.BSSID(target), 6);
    targetChannel = WiFi.channel(target);
    ssidToUse = targetRaw;
    Serial.printf("目标热点 %s RSSI=%d 信道=%d\n", targetRaw.c_str(), WiFi.RSSI(target), targetChannel);
    Serial.printf("SSID 长度=%u 字节：", static_cast<unsigned>(targetRaw.length()));
    for (size_t i = 0; i < targetRaw.length(); ++i) {
      Serial.printf("%02X ", static_cast<uint8_t>(targetRaw[i]));
    }
    Serial.println();
  } else {
    Serial.println("扫描结果里没有目标热点");
  }

  for (int attempt = 1; attempt <= 3; ++attempt) {
    wifiFailReason = 0;
    WiFi.disconnect();
    delay(300);
    showMessage("正在连接 Wi-Fi", ssidToUse, String("第 ") + attempt + " 次尝试", ST77XX_CYAN);
    if (attempt == 1 && target >= 0) {
      // 直接锁定扫描到的 BSSID 和信道，绕开会话前的再一次扫描
      WiFi.begin(targetRaw.c_str(), WIFI_PASSWORD, targetChannel, targetBssid, true);
    } else {
      // 不锁定 BSSID，按名称连接
      WiFi.begin(ssidToUse.c_str(), WIFI_PASSWORD);
    }
    const uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 20000) {
      delay(200);
    }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.print("Wi-Fi 地址：");
      Serial.println(WiFi.localIP());
      return true;
    }
    Serial.printf("第 %d 次连接失败，原因码 %d\n", attempt, wifiFailReason);
  }

  // 失败后重新扫描一次，把结论和周围热点一起显示出来
  showMessage("Wi-Fi 连接失败",
              String("原因码 ") + String(wifiFailReason) + " " + wifiReasonHint(wifiFailReason),
              "扫描结果见下一页", ST77XX_RED);
  waitOrPress(2500);
  WiFi.disconnect();
  delay(300);
  const int again = WiFi.scanNetworks();
  int targetAgain = -1;
  int rssiAgain = -1000;
  for (int i = 0; i < again; ++i) {
    String name = WiFi.SSID(i);
    name.trim();
    if (sameAccessPoint(name, wanted) && WiFi.RSSI(i) > rssiAgain) {
      rssiAgain = WiFi.RSSI(i);
      targetAgain = i;
    }
  }
  showScanReport(targetAgain, again);
  return false;
}

void setup() {
  Serial.begin(115200);
  delay(300);
  pinMode(BOOT_BUTTON_PIN, INPUT_PULLUP);
  pinMode(TALK_BUTTON_PIN, INPUT_PULLUP);

  SPI.begin(TFT_SCLK, -1, TFT_MOSI, TFT_CS);
  tft.initR(INITR_BLACKTAB);
  tft.setRotation(0);
  zhFont.begin(tft);
  showMessage("正在启动", "初始化屏幕和麦克风", "", ST77XX_CYAN);

  if (String(WIFI_SSID) == "YOUR_WIFI_NAME") {
    showMessage("请先填写 Wi-Fi", "名称和密码");
    return;
  }

  wavBuffer = static_cast<uint8_t*>(ps_malloc(MAX_WAV_BYTES));
  if (!wavBuffer) wavBuffer = static_cast<uint8_t*>(malloc(MAX_WAV_BYTES));
  if (!wavBuffer) {
    showMessage("内存不足", "请在板卡选项里开 PSRAM");
    return;
  }

  if (!startMicrophone()) {
    showMessage("麦克风初始化失败", "检查 I2S 接线");
    return;
  }

  if (!connectWiFi()) {
    // 失败界面和扫描结果已经在 connectWiFi() 里显示，这里停在屏幕上等重新上电
    return;
  }

  wifiReady = true;
  drawIdleScreen();
  showMessage("可以开始", "按住外接键或 BOOT 键说话", "松开后自动上传", ST77XX_GREEN);
  waitOrPress(2000);
  drawIdleScreen();
}

void loop() {
  // 喇叭测试命令不依赖 Wi-Fi，网络未连接时也可以单独排查功放和喇叭。
  handleSerialCommands();

  if (!wifiReady) {
    delay(1000);
    return;
  }
  if (!idleScreenDrawn) drawIdleScreen();

  // 待机时持续监测麦克风，顺便等待按键
  idleTick();
  if (!bootDown()) return;

  delay(20);  // 去抖
  if (!bootDown()) return;

  if (!recordWhileHeld()) {
    showMessage("录音太短", "按住 BOOT 说完再松手", "", ST77XX_ORANGE);
    waitOrPress(1800);
    drawIdleScreen();
    return;
  }

  i2s_driver_uninstall(I2S_NUM_0);  // 录音结束，切换到回复播放 I2S
  showMessage("正在识别", "正在思考", "请稍等", ST77XX_CYAN);
  uploadWav();
  if (!startMicrophone()) {
    showMessage("麦克风重启失败", "检查 I2S 接线", "重新上电再试", ST77XX_RED);
    wifiReady = false;
    return;
  }
  // 回复页是按 BOOT 关掉的，等手指松开再回待机，避免立刻又开始录音
  while (bootDown()) delay(20);
  drawIdleScreen();
}
