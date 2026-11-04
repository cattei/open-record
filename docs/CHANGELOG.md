# 变更记录

## 2026-11-04 审计缺陷全修（22 项断言验证）

### 做了什么

按 `docs/AUDIT-2026-11-03.md` 逐条修复，每条都有回归测试。
新增 `tests/test_regressions.py`，22 项断言全通过。

### 修复

| 项 | 根因 | 实测对比 |
|---|---|---|
| P0-2 停止多录 5 秒 | `end()` 先排空再关流，reader 仍在入队 → 队列永不空，且这 5 秒用户说的话被写进文件 | 录 10s 落盘 15.1s→**10.4s**，stop 阻塞 5.06s→**1.07s** |
| 缺陷 1 分片 10s | `chunk_frames_target` 在进循环时用 `_rate=16000` 求值，开流后变 48000 | 分片 10.0s→**30.0s** |
| P0-3 status() 卡顿 | 每次 glob + 逐文件读 wav header | 720 分片 238.5ms→**0.00ms** |
| 缺陷 8 配置 KeyError | `service`/`gui` 各一份默认值且已漂移（prebuffer 120 vs 15、keep_native_rate False vs True）| 新增 `config.py` 唯一真源 + 递归深合并 |
| P1 max_hours 失效 | watchdog 只在 `main.py` 调用，GUI 路径永不执行 | 移入 `boot()` |
| P1 预录不符承诺 | 待命麦克风关闭，15 秒预录结构上不可能 | 新增 `always_on_mic`，默认 false 如实不承诺 |
| P2 retranscribe 不可用 | 精简 runtime 无 `av`/`faster_whisper` | 加依赖预检，给可执行说明 |
| P2 docstring 命令错误 | 写 `python -m core.*`，模块已扁平化 | 全部改为实际命令 |
| P2 finish_session 无互斥 | 并发收尾可能重复合并 | 加 `_finish_lock` |

### 额外发现

- `miclock` 只认 `state == "recording"`，对端 `stopping` 时麦克风未释放会被误判空闲
  → 纳入 `stopping` / `phase` / `mic_open`
- `miclock` 的 `PEERS` 与 abandoned 分支硬编码 8899 → 改为通用推导
- `packaging/package.py` 是第四份默认值副本（端口 8899）→ 改为 import `config.DEFAULTS`

### 文档纠正

README 原来写「`prebuffer_sec` 15 秒保证开头不丢」——不成立，已改为如实说明。
「1 小时约 82MB」只算了成品 mp3，补上录音期间峰值：48kHz WAV 约 330MB/h，
成品约 84MB/h，录 6 小时约需 2GB 临时空间。

---

## 2026-11-03 托盘常驻化（无感形态）

### 做了什么

把「双击 bat → 弹出窗口」改成**开机自启 + 右下角托盘常驻**的无感形态。
同时修掉了审计里 3 个会实际伤害用户的缺陷。

### 新增

| 文件 | 作用 |
|---|---|
| `app/tray.py` | 托盘图标 + 右键菜单。单击开窗，右键含退出。蓝=待命 / 红=录音中，悬停提示带实时计时 |
| `app/autostart.py` | 开机自启。写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，**免提权、不弹 UAC** |
| `app/singleton.py` | 单实例锁。独占绑定 `127.0.0.1:47311`，第二个实例把「显示」指令送给已有实例后自己退出 |
| `app/assets/` | 19 个图标（16–256px PNG + 多尺寸 ICO），由 `packaging/make_icons.py` 用 Pillow 生成 |
| `packaging/make_icons.py` | 图标生成器（构建期工具，运行时不需要 Pillow）|

### 关键决策

**1. 用 `pythonw.exe` 而不是 `.bat` 做自启目标**

`bat` 启动会闪一个黑色控制台窗口，「无感」就废了。注册表里直接指向
`runtime\python\pythonw.exe`（无控制台版）+ `launcher.py --background`。

**2. 用 HKCU 而不是 HKLM**

HKLM 需要管理员权限、会弹 UAC；HKCU 普通用户双击即生效。代价是只对当前用户生效，
对这个工具的定位（个人录音）正合适。

**3. 单实例锁用 `SO_EXCLUSIVEADDRUSE`，不用 `SO_REUSEADDR`**

这是被本项目自己的审计逼出来的选择：实测 Windows 上 `SO_REUSEADDR` 允许两个进程
绑同一端口。开两份程序会**抢麦克风**，所以必须用独占绑定。

**4. 关窗 ≠ 退出**

`setQuitOnLastWindowClosed(False)`，关窗只 `hide()` 收进托盘。
真正退出只能走托盘菜单「退出」，且正在录音时会先弹确认（避免误丢录音）。

### 同时修掉的审计缺陷

| 缺陷 | 修法 | 验证 |
|---|---|---|
| P0-1 保存配置会静默删掉正在录的音 | 录音中禁用「保存并生效」+ `on_save` 里二次拦截 | 录音态 `save_enabled=False` |
| 缺陷 7 双 HTTP 绑定：旧实例仍响应请求 | `serve()` 改 `allow_reuse_address=False` | 第二次 bind 如实抛 `OSError 10048` |
| 自启路径不随目录迁移 | 每次启动都 `sync(True)` 对齐注册表 | 移动目录后注册表自动改指新路径 |

### 踩过的坑

| 问题 | 现象 | 解决 |
|---|---|---|
| QLabel 全被加上边框 | `QFrame{...}` 选择器把边框糊到所有标签上 | QLabel 继承自 QFrame；卡片改用 `objectName` 选择器 |
| 后台启动的窗口唤不起来 | `isVisible()` 为 True，但 Win32 `IsWindowVisible()` 仍 False | 显式调 `user32.ShowWindow(hwnd, SW_RESTORE)` |
| 跨线程投递指令失效 | `QTimer.singleShot` 在纯 Python 监听线程里永不触发 | 改用 `Signal`，Qt 自动做队列投递 |
| 发行包自启指向源码树 | `value_matches: false`，开机去启动不存在的路径 | 每次启动对齐注册表，而非只在首次运行写一次 |
| 开始录音后立刻退出会崩 | `RuntimeError: Signal source has been deleted` | worker 线程持有 self，窗口销毁后仍 emit；加 `_dead` 标志 + `_emit_log/_emit_state` 守卫包装 |

### 验证结果

26 项断言全通过，逐条对应需求原话：默认开机自启 / 后台常驻不弹窗 / 托盘图标常驻 /
单击开界面 / 录音态图标变红且悬停显示计时 / 右键菜单含退出且为末项 / 图标界面为自产。

端到端实测两轮（源码树 + zip 全新解压）：双击 bat → 弹窗 + 自启写入并指向新位置 →
录 8~10 秒产出 313~365KB mp3、分片清理干净 → 托盘退出后进程与端口干净释放。

### 产物

- `dist/Project-record-YYYYMMDD.zip` — 分发包，48.4MB
- 完整性自检 17 项全 OK（新增 tray/singleton/autostart/assets 检查项）

### 仍未修（需另行拍板）

审计报告里 P0-2（停止多录 5 秒且阻塞）、P0-3（`status()` O(n) 导致长录音卡顿）、
分片实为 10s、预录不可用、`max_hours` 未生效 —— 均需改动录音核心，
不在本次「无感化」范围内，详见 `docs/AUDIT-2026-11-03.md`。

---

## 2026-11-03 首次发行

### 做了什么

把原先常驻后台的录音服务改造为**双击即用的桌面工具**，并独立成 Project-record 项目准备开源。

### 关键决策

**1. 放弃 PyInstaller 单文件 exe，改用内置 Python 运行时 + bat**

原因：本机 Smart App Control 处于强制模式（`VerifiedAndReputablePolicyState=1`），
会拦截所有无签名的第三方 exe。实测：
- PyInstaller 产物 → `应用程序控制策略已阻止此文件`（本地路径同样被拦）
- 自签名 → 失败，SAC 只认可信 CA 链
- `python.exe` → 放行（PSF 官方分发，云端信誉库已收录）

所以采用「内置官方 python.exe + bat 启动」的方案：既满足 SAC 放行，
又保持零依赖、解压即用。代价是分发包 134MB（压缩后 48MB），而非单文件。

**2. runtime 精确裁剪至 134MB**

从 2.1GB 的本机环境提取，排除：
- 转写依赖 faster-whisper / ctranslate2 / scipy（约 1.5GB）
- PySide6 的 WebEngine / translations / qml（约 600MB）
- 无关第三方包（tencentcloud / cv2 / playwright 等）

保留：Python 标准库、PySide6 的 Core/Gui/Widgets/Network、
sounddevice + PortAudio、numpy、lameenc、lark_oapi。

**3. 内存优化：飞书与转写均为按需加载**

逐模块实测 RSS 增量：
| 模块 | 增量 |
|---|---|
| 基础 + numpy + sounddevice | 45 MB |
| 飞书 SDK (lark_oapi) | 147 MB |

改为：未配置 App ID/Secret 时不导入飞书组件；`transcribe=false` 时不创建 ASR 引擎。
实测 GUI 常驻 RSS 约 104MB（含飞书）。

**4. 转写功能判定不可用，默认关闭**

以用户提供的付费版转写结果（6221 字 docx）为基准，用 20 个技术术语检验，
本地 whisper 命中率 0-5%，300 秒录音仅转出 103-524 字（正常应 1000+）。
电平归一化后反而更差。根因是采集信噪比低（峰值 -30dBFS、低频噪声占 47%），
不是模型能力问题。

用户拍板：只录音，不做转写。代码保留但默认关闭。

### 踩过的坑（按发现顺序）

| 问题 | 现象 | 解决 |
|---|---|---|
| Smart App Control 拦截 | exe 无法启动，报"应用程序控制策略已阻止" | 改用官方 python.exe + bat |
| bat 中文乱码 | cmd 按 GBK 解析 UTF-8 脚本，路径失效 | bat 存为 GBK 编码，不用 chcp |
| runtime 缺标准库 | `No module named 'reprlib'`/'copy'/'uuid' 等逐个冒出 | 改为整目录复制标准库，只排除测试包 |
| 缺 PortAudio 模块 | `No module named '_sounddevice'` | 补 `_sounddevice.py` + `_cffi_backend.pyd` |
| Qt DLL 加载失败 | `DLL load failed while importing QtCore` | 补 `pyside6.abi3.dll` / `shiboken6.abi3.dll` |
| shiboken 缺失 | `libshiboken does not exist` | 整个 shiboken6 目录（2.7MB） |
| scipy 硬依赖 | 合并时 `No module named 'scipy'`，mp3 只出 0 字节 | 高通滤波改为条件导入，默认关闭时不需要 |
| 配置/日志路径错 | logs 不生成、录音存到 app/ 下 | 区分 APP_DIR（app/）与 ROOT_DIR（发行根目录）|
| 验收读得太早 | mp3 显示 0KB | finish_session 是异步的，需轮询等大小稳定 |
| GUI 与 HTTP 各管一半 | HTTP 停止后不触发收尾 | `/stop` 内部自行启动 finish_session 线程 |

### 验证结果

全新目录解压 → 双击 bat → 录音 12 秒 → 停止 → **2.0 秒**产出 mp3（406KB），
分片自动清理。GUI 进程 RSS 103.9MB。

### 产物

- `dist/Project-record-YYYYMMDD.zip` — 分发包，48.3MB
- `dist/Project-record/` — 未压缩发行目录，134MB

### 遗留

- 录音电平偏低（-30dBFS），已用 12 倍软件增益补偿；调 `config.json` 的 `gain`
- 麦克风信噪比是硬件限制，远场录音无法根治
- 若日后需要转写，需先改善采集 SNR，再考虑云端 ASR
