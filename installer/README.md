# GAUGE 衡 · AI Agent 监控台 —— Windows 安装包

## 这是什么

本目录用于把 `dist\AI-Agent监控台.exe`（自包含离线包）打包成一份**按用户安装、
无需管理员权限**的 Windows 安装程序 `GAUGE-衡-Setup-1.2.0.exe`。

- 安装脚本：`GAUGE衡.iss`（Inno Setup 7，UTF-8 with BOM 保存）
- 一键构建：`构建安装包.bat`
- 构建产物：`out\GAUGE-衡-Setup-1.2.0.exe`

## 如何构建

1. 先确保 exe 已构建：运行 `desktop\构建EXE.bat`，产物为 `dist\AI-Agent监控台.exe`。
2. 双击 `构建安装包.bat`（脚本会自动定位 ISCC.exe：优先 `where ISCC`，再回退
   `%ProgramFiles%\Inno Setup 7\ISCC.exe` 与 `%ProgramFiles(x86)%\Inno Setup 7\ISCC.exe`）。
3. 成功后安装包位于 `installer\out\GAUGE-衡-Setup-1.2.0.exe`。

也可手动编译：

```
"C:\Program Files\Inno Setup 7\ISCC.exe" "installer\GAUGE衡.iss"
```

## 安装位置

按用户安装，默认目录：

```
%LOCALAPPDATA%\Programs\GAUGE 衡
```

> 为什么不用 `Program Files`？冻结形态的 exe 会把日志、配置、成品 HTML 等
> 写到「exe 所在目录」，该目录必须可写；按用户安装正好满足（无需管理员）。

## 安装后会创建什么

- 安装目录：`AI-Agent监控台.exe`（唯一分发文件，exe 已内嵌所有运行期资源）。
- 开始菜单：`GAUGE 衡` 程序组，内含启动快捷方式与卸载入口。
- 桌面快捷方式：可选任务 `desktopicon`，**默认勾选**。
- 开机自启：可选任务 `startupicon`，**默认不勾选**；勾选后写入
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\GAUGE 衡`。
- 安装向导末页可选「立即启动」（静默安装时不弹）。

## 前置依赖

- Windows 10 / 11 **x64**。
- **Microsoft Edge WebView2 Runtime**：安装前会自动检测（注册表
  `SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-...}` 的 `pv` 值，
  HKLM/HKCU 都查）。缺失时提示并可打开官方下载页，但**不阻断安装**。
  下载：https://developer.microsoft.com/microsoft-edge/webview2/
- 本机 **ZCode 会话库** `%USERPROFILE%\.zcode\cli\db\db.sqlite`：安装前提示性检查，
  缺失时提示「监控台将无数据，需先安装并运行 ZCode」，同样**不阻断安装**。

## 卸载

从「设置 → 应用」或开始菜单的卸载入口卸载。卸载时会一并清理运行期生成物：

```
AI-Agent监控台.html
AI-Agent监控台.data.json
app.log（含 app.log.1~3 轮转备份）
widget.json
monitor.ico
smoke_result.json
*.tmp
```

卸载不会触碰你的 ZCode 会话库（`%USERPROFILE%\.zcode\...`），用户数据不受影响。

## 正在运行检测

安装时若监控台正在运行，向导会尝试自动关闭占用 exe 的进程以便覆盖
（`CloseApplications=yes`，`RestartApplications=no`，安装后不自动重启）。

## 文件清单

| 文件 | 说明 |
| --- | --- |
| `GAUGE衡.iss` | Inno Setup 7 安装脚本（UTF-8 with BOM） |
| `构建安装包.bat` | 一键构建脚本（GBK 编码） |
| `out\GAUGE-衡-Setup-1.2.0.exe` | 构建产物（编译后生成） |
