Unicode True
!include "MUI2.nsh"

!ifndef APP_VERSION
  !define APP_VERSION "0.8"
!endif
!ifndef APP_FILE_VERSION
  !define APP_FILE_VERSION "0.8.0.0"
!endif
!ifndef SOURCE_DIR
  !error "SOURCE_DIR is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif

!define APP_NAME "NAPS3"
!define PUBLISHER "NAPS3 contributors"

Name "${APP_NAME} ${APP_VERSION}"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\NAPS3"
InstallDirRegKey HKCU "Software\NAPS3" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
VIProductVersion "${APP_FILE_VERSION}"
VIAddVersionKey /LANG=1049 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=1049 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1049 "FileDescription" "NAPS3 — сканирование документов"
VIAddVersionKey /LANG=1049 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=1049 "LegalCopyright" "MIT License"

!define MUI_ABORTWARNING
!define MUI_ICON "naps3.ico"
!define MUI_UNICON "naps3.ico"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

Section "NAPS3" MainSection
  SetOutPath "$INSTDIR"
  File /r "${SOURCE_DIR}\*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\NAPS3"
  CreateShortcut "$SMPROGRAMS\NAPS3\NAPS3.lnk" "$INSTDIR\NAPS3.exe"
  CreateShortcut "$SMPROGRAMS\NAPS3\Удалить NAPS3.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortcut "$DESKTOP\NAPS3.lnk" "$INSTDIR\NAPS3.exe"

  WriteRegStr HKCU "Software\NAPS3" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NAPS3" "DisplayName" "NAPS3 ${APP_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NAPS3" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NAPS3" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NAPS3" "DisplayIcon" "$INSTDIR\NAPS3.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NAPS3" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NAPS3" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NAPS3" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\NAPS3.lnk"
  RMDir /r "$SMPROGRAMS\NAPS3"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\NAPS3"
  DeleteRegKey HKCU "Software\NAPS3"
SectionEnd
