# 许可证与第三方声明

## 本项目：MIT

本项目自行编写的程序、脚本及原创文档采用 **MIT License**，完整条款见 [project/LICENSE.txt](project/LICENSE.txt)。复制或分发这些内容时，须保留版权与许可声明。

此授权**不包含**第三方程序、系统镜像、第三方许可文本、商标或其他并非本项目原创的内容。这些内容继续适用各自的许可，不能因为放在同一目录或一起使用而被改为 MIT。

项目旨在帮助双系统用户便捷维护、恢复自己的 Windows 系统。这是产品定位，不是在 MIT 条款之外为本项目原创代码添加用途或商业使用限制。使用 WinPE 时仍须遵守微软对 WinPE 的独立用途限制。

## 第三方阅读入口

| 组件 | 与项目的关系 | 阅读入口 |
| --- | --- | --- |
| Microsoft ADK／WinPE | 用户自行合法获取的 Windows 恢复环境，非开源组件 | [官方来源与本地许可](third_party/microsoft/README.md) |
| QEMU | 独立进程运行的虚拟机程序 | [原始版权与许可声明](third_party/notices/qemu.copyright.txt)、[GPL-2](third_party/license-texts/GPL-2.txt) |
| SeaBIOS | 虚拟机固件（随系统 QEMU 提供） | [原始声明](third_party/notices/seabios.copyright.txt)、[LGPL-3](third_party/license-texts/LGPL-3.txt)；具体文件另有条款 |
| Python | Linux 主程序使用的系统运行时 | [Python 许可与所含代码声明](third_party/notices/python.copyright.txt) |
| msitools／libmsi | 只读导出微软 MSI 的目录、组件与文件表 | [原始声明](third_party/notices/msitools.copyright.txt)、[libmsi](third_party/notices/libmsi.copyright.txt)、[LGPL-2.1](third_party/license-texts/LGPL-2.1.txt) |
| wimlib | 读取与定制 WinPE 的 WIM 镜像 | [工具声明](third_party/notices/wimlib-tools.copyright.txt)、[库声明](third_party/notices/wimlib-library.copyright.txt)、[GPL-3](third_party/license-texts/GPL-3.txt) |
| libgsf | libmsi 的运行时依赖 | [原始声明](third_party/notices/libgsf.copyright.txt)、[LGPL-2.1](third_party/license-texts/LGPL-2.1.txt) |
| hivex | 离线读写 BCD（注册表 hive）以启用 EMS | [原始声明](third_party/notices/hivex.copyright.txt)、[LGPL-2.1](third_party/license-texts/LGPL-2.1.txt) |
| 系统 QEMU 的依赖库 | 由发行版提供，本项目不随包分发 | [依赖清单与声明](third_party/README.md) |
| 7-Zip、xorriso | 组件提取、镜像制作工具 | [7-Zip 声明](third_party/notices/7zip.copyright.txt)、[xorriso 声明](third_party/notices/xorriso.copyright.txt) |

这里提供条款供用户查阅，不表示每个开源组件的普通使用都需要单独点击接受，也不代替修改、打包或再分发时应履行的义务。

## 分发边界

- 本项目公开发布内容不包含 WinPE ISO、WIM 或微软系统文件；由用户从官方获取并在本地准备镜像。
- 运行时不附带任何自编译的 Windows 二进制：终端由 Windows 自带的 EMS/SAC 提供。
- 不将微软组件重新许可为 MIT，也不把某位用户的接受视为所有用户已接受。
- 第三方声明按来源保留，不删改原文中的版权、例外或免责声明；格式化说明与原文分开。
- 附上许可证并不自动完成二进制再分发合规。例如分发 GPL／LGPL 组件时，还可能需要对应源码、构建材料及其他交付。
- 当前清单是已选直接组件和工具的声明快照，不是对任意未来发行包的完整合规保证。实际软件附带的版本对应条款优先；系统自带而未随项目分发的其他依赖，以系统提供的声明为准。