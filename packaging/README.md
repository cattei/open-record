# 构建说明

分发包的构建流程。仅维护者需要，普通用户直接下载 zip 即可。

## 前置条件

本机需有完整的 Python 环境（含 PySide6 / numpy / sounddevice / lameenc / lark_oapi）。
默认路径 `C:\env\python`，可用环境变量 `REC_SRC_PYTHON` 覆盖。

## 三步构建

```bash
# 1. 抽取最小 runtime（约 134MB，耗时 1-2 分钟）
python packaging/build_runtime.py

# 2. 组装发行目录并打 zip
python packaging/package.py

# 3. 产物
#    dist/Project-record/            未压缩，可直接运行
#    dist/Project-record-YYYYMMDD.zip  分发包
```

## 为什么不用 PyInstaller

早期尝试过，产物被 Smart App Control 拦截（`应用程序控制策略已阻止此文件`）。
SAC 是 Windows 11 全新安装的默认强制策略，拦截一切无签名且口碑不足的 exe。

替代方案对比：

| 方案 | SAC 是否放行 | 体积 | 结论 |
|---|---|---|---|
| PyInstaller 单文件 exe | ❌ 被拦 | 178MB | 不可用 |
| exe + 自签名证书 | ❌ 仍被拦 | 178MB | 不可用 |
| 购买 OV 代码签名证书 | ✅ 放行 | 178MB | 需年费+企业资质 |
| **官方 python.exe + bat** | **✅ 放行** | 134MB | **采用** |

关键事实：`python.exe` 由 Python 软件基金会官方分发，
在微软云端信誉库中已收录，SAC 将其判定为可信；而 PyInstaller 生成的
新 exe 是全新二进制，无任何信誉记录，必然被拦。

## runtime 裁剪原则

从完整环境（2.1GB）抽取，保留录音 GUI 最小闭包：

**整目录复制标准库**，只排除 `test/tests/tkinter/idlelib/venv/ensurepip` 等。
不手工挑选模块——早期手工清单导致 `reprlib`/`copy`/`uuid`/`queue`
等模块逐个缺失，反复返工。

**第三方白名单**：sounddevice.py / _sounddevice.py / _cffi_backend /
lameenc / numpy / numpy.libs / lark_oapi / PySide6(精简) / shiboken6。

**PySide6 只留四个模块**：Core / Gui / Widgets / Network，
对应的 `.pyd` 与 `Qt6*.dll` 各一份，插件只留 platforms/styles/imageformats。

**第三方依赖用递归探测补齐**：脚本运行 `import lark_oapi`，
从 `ModuleNotFoundError` 中提取缺失模块名，自动从源环境复制，直到导入成功。
这样不用猜 lark_oapi 依赖了 requests / websockets / pycryptodome / httpx 等。

## 关键坑位

| 现象 | 原因 | 处理 |
|---|---|---|
| `No module named '_sounddevice'` | 缺 CFFI 生成的模块 | 补 `_sounddevice.py` |
| `No module named '_cffi_backend'` | sounddevice 依赖 | 补 `.pyd` |
| `DLL load failed importing QtCore` | 缺 `pyside6.abi3.dll` | 放进 PySide6 目录 |
| `libshiboken does not exist` | 缺 shiboken6 | 整个目录复制 |
| 合并出 0 字节 mp3 | `merge.py` 无条件导入 scipy | 改为条件导入 |
| logs 不生成 | 把 app/ 当成了根目录 | 区分 APP_DIR / ROOT_DIR |
| bat 路径失效 | UTF-8 编码被 cmd 按 GBK 解析 | bat 存为 GBK |

## 验证方法

不要只看"打包成功"，必须实测全链路：

```bash
python <测试脚本>   # 解压到全新目录 → 双击 bat → 录音 → 检查 mp3
```

注意 `finish_session` 是异步的，检查 mp3 时要轮询等文件大小稳定，
否则会读到 0 字节的中间态。
