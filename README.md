# 听码 · Windows 直播口令输入

点击悬浮“我想要”或按 F8，识别电脑正在播放的直播语音，将明确的数字、字母口令填入点击时的输入框。每轮只填一次，不按回车发送。

默认模型为用户指定的官方 **`qwen-audio-3.0-asr-flash-streaming`**。在软件的“API 设置”中填写自己的 Key，也可以修改完整接口地址、模型和协议。Windows 电脑不需要本地运行大模型，不需要独立显卡。

## 在 Windows 启动

1. 安装 [Python 3.12 的 64 位 Windows 版本](https://www.python.org/downloads/windows/)，保留安装程序中的 Python Launcher（`py`）。建议使用 Windows 10/11 的 x64 电脑。
2. 解压完整项目到有写入权限的文件夹，例如“文档\听码”。不要直接在 ZIP 里运行。
3. 双击 **`启动听码.cmd`**。首次启动会创建环境并下载依赖，需要联网；以后直接启动。
4. 点“API 设置”，填写 Key，点“测试连接”，再“保存设置”。Key 只保留到退出软件，下次需要重新填写。
5. 点“准备系统声音”，打开直播。采集的是 Windows 默认播放设备的声音，直播应使用这个设备输出。
6. 第一次建议勾选“仅预览”，确认识别结果正确。正式使用时取消勾选，先点中目标输入框，再点击悬浮按钮或按 F8。

再次点击悬浮按钮/F8 可取消本轮；识别时若 Esc 快捷键注册成功，也可以按 Esc 取消。F8 被其他软件占用时，使用悬浮按钮。右键拖动悬浮按钮可移动位置。

每轮最长收音 12 秒，检测到一小段讲话后的约 650 毫秒安静会结束收音。等待结果最长约 35 秒。直播音乐、断续口令、很轻的声音会影响自动断句；未得到明确结果时，重新听一轮。

取消后如果显示“上一轮连接仍在结束”，说明网络握手尚未返回。稍后再试，软件不会积累新的等待连接。

## 申请与填写 API

从用户提供的 [官方模型页](https://www.qianwenai.com/models/qwen-audio-3.0-asr-flash-streaming) 点击“调用 API”，或进入 [千问 AI 的 Key 管理页](https://platform.qianwenai.com/home/api-keys)。按控制台要求开通模型服务与计费权限。Key 不需要发给开发者或粘贴到聊天里。

默认预设对应模型页提供的示例：

| 设置 | 默认值 |
| --- | --- |
| 接口协议 | 千问 Audio 3.0 · 实时语音 |
| 完整 API 地址 | `wss://dashscope.aliyuncs.com/api-ws/v1/inference` |
| 模型名称 | `qwen-audio-3.0-asr-flash-streaming` |
| API Key | 你自己申请的 Key |

**以你的控制台提供的地址为准**：地域、工作空间和 Key 必须匹配。部分账号可能使用带工作空间前缀的地址，可在界面中直接覆盖预设。

另外保留三种兼容选项：

- **百炼 · 实时语音**：Qwen3 Realtime 的 `session.update` / `input_audio_buffer.append` 协议，示例模型 `qwen3-asr-flash-realtime`，地址以 `/api-ws/v1/realtime` 结尾。
- **百炼 · 短音频识别**：完整 HTTPS `/chat/completions` 地址，使用 Qwen `input_audio` 数据格式，示例模型 `qwen3-asr-flash`。
- **自定义 · 标准音频转写**：完整 HTTPS `/audio/transcriptions` 地址，以 multipart 上传 WAV 文件和模型名称，响应应包含 `text`。填入服务商支持该协议的真实模型名。

不同协议不能只靠替换模型名称互换；“自定义”并不代表兼容任意服务商私有接口。流式千问 Audio 3.0 使用 `run-task` / `finish-task` 与二进制 PCM，和 Qwen3 Realtime 不同。

“测试连接”不上传声音。Audio 3.0 测试仅验证 WebSocket 鉴权握手，不证明模型调用成功；HTTP 测试查询同版本的 `/models`，不支持此路由的服务可能无法通过测试，但仍可保存设置尝试识别。真正识别前，请确认服务商的额度和计费规则。

## 纠错和防误填

| 识别原话 | 处理结果 |
| --- | --- |
| 两个 m / 2 个 m / 两个艾姆 | `mm` |
| 2m | `2m`（没有“个”不当作重复指令） |
| 零零八 | `008` |
| 两个大写 a | `AA` |
| 两个 m 零零八 | `mm008` |
| 一百零二 | `102` |
| 一百二 | 含义不明确，拒绝自动填写 |

可选“仅数字”或“字母 + 数字”，设置最短/最长长度。未特别说明时字母转为小写。字母模式支持数字、英文字母、点、下划线、减号；数字模式支持负数和小数。不同用途应设置合适长度，减少把普通讲话当口令的机会。

- 只处理最终识别结果；中间文字仅作预览。
- 整句必须能够按明确规则解析，不从任意聊天里随意截取一个数字。
- 填写前重新核对目标窗口、控件、原始内容和选区；核对失败则显示结果供手动复制。
- 取消、超时后的旧结果不能进入下一轮；不自动重试云端请求。
- 密码框、只读控件、不提供完整 Windows 辅助功能信息的输入框不自动填写。
- 输入范围只能确认到当前支持的 UI Automation 控件；部分直播客户端、网页富文本框或游戏内输入框可能不支持。可以改用“仅预览”和“复制结果”。

下方“试算”可直接检查纠错规则，不调用 API。

## 声音与配置

使用 WASAPI loopback 获取默认播放设备，转换为 16 kHz 单声道 PCM16；不使用麦克风。准备后音频在本机经过回调，但仅点击后的当前一轮音频进入云端队列，不包含点击前的音频。不会将录音或转写自动保存到文件。系统其他声音也可能被采集，使用时请关闭不需要的播放来源。

接口地址、模型、协议、格式和长度保存到 `%APPDATA%\Tingma\settings.json`；API Key 默认仅在内存中保留。不得将 Key 写进接口 URL。上传到自填地址的音频由该服务商处理。

如果播放设备改变，关闭软件后重新打开并“准备系统声音”。目标程序和听码建议都以普通权限运行；权限等级不同可能无法访问目标控件。

## 验证状态

本版本包含可运行的 Python 源码、Windows 启动脚本和 Windows 打包脚本。目前开发与测试在 macOS 上进行，测试使用模拟 API/目标控件，覆盖协议、纠错、取消、重复结果隔离和设置。Qt 界面可用 `python -m tingma --demo` 预览。

**尚未在真实 Windows 播放设备、真实直播输入框与付费 API Key 的组合上完成端到端验证。** 此源码 ZIP 不等同于已验证的 Windows EXE。首次使用请先开启“仅预览”，并在普通文本框中检查填写行为。

开发验证命令：

```text
python -m unittest discover -s tests -v
python -m tingma
```

要生成免 Python 启动的程序，请在 **Windows** 中先运行软件完成依赖安装，关闭软件后执行 `scripts\build-windows.cmd`。脚本会运行测试并用 PyInstaller 生成 `dist\Tingma\Tingma.exe`；分发时复制整个 `Tingma` 文件夹。构建产物仍需在目标 Windows 环境验证。

关键文件：`tingma/app.py` 为界面和流程，`tingma/cloud_api.py` 为云端协议，`tingma/windows_audio.py` 为系统声音，`tingma/windows_input.py` 为目标输入保护，`backend/normalizer.py` 为口令纠错。

接口依据：[官方流式 WebSocket 文档](https://help.aliyun.com/zh/model-studio/fun-asr-realtime-websocket-api)、[客户端事件](https://help.aliyun.com/zh/model-studio/fun-asr-client-events)、[提升识别准确率](https://help.aliyun.com/zh/model-studio/improve-asr-accuracy)。
