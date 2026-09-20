; ============================================================================
;  GAUGE 衡 · AI Agent 监控台 —— Inno Setup 7 安装脚本
;  构建：installer\构建安装包.bat   （或 ISCC.exe "installer\GAUGE衡.iss"）
;
;  设计要点：
;   * 按用户安装（PrivilegesRequired=lowest，无需管理员）。冻结形态的 exe 会把
;     app.log / monitor.ico / smoke_result.json / AI-Agent监控台.html /
;     AI-Agent监控台.data.json / widget.json 写到「exe 所在目录」，故安装目录
;     必须可写，绝不能装进 Program Files。
;   * exe 已自包含（内嵌 refresh.py / widget.html / template.html），[Files] 只分发
;     单个 exe，不再附带任何 .py / .html 资源。
;   * 本文件须以 UTF-8 with BOM 保存，否则 ISCC 读中文会乱码/报错。
; ============================================================================

#define MyAppName "GAUGE 衡 · AI Agent 监控台"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "GAUGE 衡"
#define MyAppExeName "AI-Agent监控台.exe"
#define MyAppURL "https://developer.microsoft.com/microsoft-edge/webview2/"

[Setup]
; AppId 稳定 GUID（双大括号为 Inno 字面量写法），升级/卸载据此识别同一产品
AppId={{7F3A9C2E-5B41-4D8A-9E6F-1C0B2D4E6F80}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion=1.2.0.0
; 按用户安装：lowest 下 {autopf} = {localappdata}\Programs（可写，无需管理员）
PrivilegesRequired=lowest
DefaultDirName={autopf}\GAUGE 衡
DefaultGroupName=GAUGE 衡
DisableProgramGroupPage=yes
DisableWelcomePage=no
AllowNoIcons=yes
; 64 位
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 仅支持 Windows 10/11（产品要求；避免在 Win7/8 装出不可用环境）
MinVersion=10.0
; 压缩
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 图标
SetupIconFile=..\desktop\monitor.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
; 正在运行检测：覆盖前先尝试关闭占用 exe 的进程，安装完不自动重启
CloseApplications=yes
CloseApplicationsFilter=*.exe
RestartApplications=no
; 产物
OutputDir=out
OutputBaseFilename=GAUGE-衡-Setup-1.2.0

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce
Name: "startupicon"; Description: "开机时自动启动 GAUGE 衡"; GroupDescription: "启动选项："; Flags: unchecked

[Files]
; exe 已自包含，无需再打包 refresh.py / template.html / widget.html
Source: "..\dist\AI-Agent监控台.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\GAUGE 衡"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\{cm:UninstallProgram,GAUGE 衡}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\GAUGE 衡"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\GAUGE 衡"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,GAUGE 衡}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 清理运行期生成物，避免卸载后残留
Type: files; Name: "{app}\AI-Agent监控台.html"
Type: files; Name: "{app}\AI-Agent监控台.data.json"
Type: files; Name: "{app}\app.log"
Type: files; Name: "{app}\app.log.1"
Type: files; Name: "{app}\app.log.2"
Type: files; Name: "{app}\app.log.3"
Type: files; Name: "{app}\widget.json"
Type: files; Name: "{app}\monitor.ico"
Type: files; Name: "{app}\smoke_result.json"
Type: files; Name: "{app}\*.tmp"

[Code]
const
  { Edge WebView2 Runtime 的 EdgeUpdate Clients 键（与 desktop/monitor/dialogs.py 同源） }
  WEBVIEW2_REG_SUB =
    'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WEBVIEW2_URL = 'https://developer.microsoft.com/microsoft-edge/webview2/';

{ 检测 WebView2 Runtime：HKLM/HKCU 两处查 pv 值，非空且非 0.0.0.0 即视为已安装。 }
function WebView2Installed(): Boolean;
var
  Pv: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, WEBVIEW2_REG_SUB, 'pv', Pv) then
    if (Pv <> '') and (Pv <> '0.0.0.0') then
      Result := True;
  if (not Result) and RegQueryStringValue(HKCU, WEBVIEW2_REG_SUB, 'pv', Pv) then
    if (Pv <> '') and (Pv <> '0.0.0.0') then
      Result := True;
end;

{ 安装前检查：WebView2 缺失则提示并可打开官方下载页（不阻断）；ZCode 会话库缺失仅提示。 }
function InitializeSetup(): Boolean;
var
  ErrCode: Integer;
  DbPath: String;
begin
  Result := True;

  { a) WebView2 Runtime 预检 }
  if not WebView2Installed() then
  begin
    { 静默安装（/SILENT、/VERYSILENT）下跳过模态弹窗，避免无人值守/批量部署卡死；
      MsgBox 不受静默模式自动抑制，必须显式用 WizardSilent 守卫。 }
    if not WizardSilent then
    begin
      if MsgBox('未检测到 Microsoft Edge WebView2 Runtime。' + #13#10 + #13#10 +
                '「GAUGE 衡 · AI Agent 监控台」需要 WebView2 渲染界面。' + #13#10 +
                '是否现在打开微软官方下载页安装？' + #13#10 + #13#10 +
                '（可先继续安装，安装完成后再补装 WebView2。）',
                mbConfirmation, MB_YESNO) = IDYES then
        ShellExec('open', WEBVIEW2_URL, '', '', SW_SHOWNORMAL, ewNoWait, ErrCode);
    end;
  end;

  { b) ZCode 会话库提示性检查（仅提示，不阻断；静默安装跳过弹窗）。
    路径须用环境变量常量的 Inno 写法：花括号+百分号包裹 USERPROFILE。
    Inno 无 userprofile 具名常量，且编译期不校验，运行时才报
    Unknown constant 并致命中止，故此处不可写错。 }
  DbPath := ExpandConstant('{%USERPROFILE}\.zcode\cli\db\db.sqlite');
  if not FileExists(DbPath) then
    if not WizardSilent then
      MsgBox('本机未检测到 ZCode 会话库：' + #13#10 +
             DbPath + #13#10 + #13#10 +
             '安装后监控台将无数据，需先安装并运行 ZCode。' + #13#10 +
             '（仅提示，不影响继续安装。）',
             mbInformation, MB_OK);

  { 始终放行：两项检查均为提示性，不阻断安装 }
  Result := True;
end;
