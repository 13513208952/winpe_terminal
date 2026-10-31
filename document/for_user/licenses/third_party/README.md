# 第三方声明索引

`notices/` 保存所选版本的软件包附带版权／许可声明的原文副本，包含组件内部不同文件的许可与例外。`license-texts/` 保存这些声明所引用的标准许可文本及 GCC 运行库例外。

这些文件是第三方内容，不适用本项目的 MIT 授权。完整条款的提供不代表其适用于整个项目；请按具体组件、具体文件以及实际分发方式判断。

## 当前归档

| 组件 | 原始声明文件 | 来源包／用途 |
| --- | --- | --- |
| QEMU | [qemu.copyright.txt](notices/qemu.copyright.txt) | Ubuntu qemu-system-common；独立虚拟机进程 |
| SeaBIOS | [seabios.copyright.txt](notices/seabios.copyright.txt) | Ubuntu seabios；固件 |
| MinGW-w64 | [mingw-w64.copyright.txt](notices/mingw-w64.copyright.txt) | Ubuntu mingw-w64-common；头文件及运行库 |
| GCC | [gcc-mingw-w64.copyright.txt](notices/gcc-mingw-w64.copyright.txt) | Ubuntu gcc-mingw-w64-base；含运行库例外 |
| Binutils | [binutils-mingw-w64.copyright.txt](notices/binutils-mingw-w64.copyright.txt) | Ubuntu binutils-mingw-w64-base；构建工具 |
| libfdt | [libfdt.copyright.txt](notices/libfdt.copyright.txt) | Ubuntu libfdt1；QEMU 包依赖 |
| libpmem | [libpmem.copyright.txt](notices/libpmem.copyright.txt) | Ubuntu libpmem1；QEMU 包依赖 |
| librdmacm | [librdmacm.copyright.txt](notices/librdmacm.copyright.txt) | Ubuntu librdmacm1t64；QEMU 包依赖 |
| libslirp | [libslirp.copyright.txt](notices/libslirp.copyright.txt) | Ubuntu libslirp0；QEMU 包依赖，当前不启用网络设备 |
| libndctl | [libndctl.copyright.txt](notices/libndctl.copyright.txt) | Ubuntu libndctl6；私有依赖 |
| libdaxctl | [libdaxctl.copyright.txt](notices/libdaxctl.copyright.txt) | Ubuntu libdaxctl1；私有依赖 |
| Python | [python.copyright.txt](notices/python.copyright.txt) | 系统 python3.14；未随本项目打包 |
| 7-Zip | [7zip.copyright.txt](notices/7zip.copyright.txt) | 系统 7zip；提取工具 |
| xorriso | [xorriso.copyright.txt](notices/xorriso.copyright.txt) | 系统 xorriso；镜像工具 |
| LibreOffice | [libreoffice.copyright.txt](notices/libreoffice.copyright.txt) | 系统 libreoffice-common；仅文档查看与转换 |

具体包版本、原始文件位置、下载来源及 SHA-256 见 [provenance.json](provenance.json)。原始声明中指向 `/usr/share/common-licenses/` 的常见条款，可在本目录的 `license-texts/` 查阅。不要把 GNU 项目的版本别名 `GPL`／`LGPL` 当成所有组件都使用同一版本；应以组件声明的具体版本为准。

## 微软组件

参见 [microsoft/README.md](microsoft/README.md)。不能把开源组件允许复制声明的规则套用到微软许可文本或 WinPE 系统文件。
