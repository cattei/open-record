# 依赖说明

本项目运行时使用的第三方组件及许可。

## 运行时（已内置在 runtime/）

| 组件 | 版本 | 许可 | 用途 |
|---|---|---|---|
| Python | 3.13.14 | PSF License | 运行时本体 |
| PySide6 (Qt6) | 6.12.0 | LGPL-3.0 | 图形界面 |
| numpy | 2.5.4 | BSD-3-Clause | 音频数组运算 |
| sounddevice | 0.5.x | MIT | 音频采集（PortAudio 封装）|
| PortAudio | - | MIT | 跨平台音频 I/O |
| lameenc | 1.8.x | LGPL-2.1 | MP3 编码 |
| lark_oapi | - | MIT | 飞书开放平台 SDK（可选）|
| cffi | - | MIT | sounddevice 的 C 绑定 |

## Qt / PySide6 的 LGPL 合规

PySide6 以 LGPL-3.0 分发，本项目采用**动态链接**方式（独立 DLL），符合 LGPL 要求：
- 未修改 Qt 源码
- 用户可用自己的 Qt 版本替换 `runtime/python/Lib/site-packages/PySide6/` 下的 DLL
- 源码完全公开

## 可选依赖（未内置）

| 组件 | 用途 | 说明 |
|---|---|---|
| faster-whisper | 语音转文字 | 需自行 `pip install faster-whisper`，本项目默认不启用 |
| scipy | 高通滤波 | 仅在 `highpass_hz > 0` 时需要 |

## 开发依赖

构建分发包需要本机已安装完整 Python 与上述包，详见 `packaging/build_runtime.py` 顶部说明。
