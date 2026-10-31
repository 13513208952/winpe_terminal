# WinPE_Terminal

在 Linux 下用 QEMU/KVM 运行 Windows PE，并把 Windows 命令行透传到当前终端，
用于ubuntu和windows双系统用户在linux环境下对 Windows 分区进行管理和恢复操作（如使用 `chkdsk`修复ntfs分区）。

**用户文档入口：[document/for_user/readme.txt](document/for_user/readme.txt)**

其他文档：

- [命令与参数参考](document/for_user/protocol/usage_protocol.md)
- [配置格式](document/for_user/protocol/configuration.md)
- [运行环境、终端行为与设备安全](document/for_user/technique_parts/runtime_requirements.md)
- [许可证与第三方声明](document/for_user/licenses/README.md)

## 安装

发行包为 Debian 二进制包，通过 GitHub Releases 提供：

```bash
sudo apt install ./winpe-terminal_<版本>_amd64.deb
sudo winpe_terminal prepare      # 从微软官方来源获取组件并本地构建 WinPE
sudo winpe_terminal start        # 或：sudo winpe_terminal start -/dev/nvme0n1p3
```

Windows PE、ADK、WIM 与 ISO 不随本项目分发；`prepare` 会先显示微软许可并要求你明确接受，
不会执行微软安装器，不会静默下载。

## 许可

本项目自行编写的代码、脚本与文档采用 [MIT License](document/for_user/licenses/project/LICENSE.txt)。
第三方组件适用各自许可，见 [licenses](document/for_user/licenses/README.md)。
