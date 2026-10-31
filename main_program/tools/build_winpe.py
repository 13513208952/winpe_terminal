#!/usr/bin/python3 -I
# SPDX-License-Identifier: MIT
"""Build a local x64 WinPE ISO from pinned official Microsoft payloads.

Downloads are resumable and hash-checked; the image is assembled in a private
build directory and only handed to the caller once it verifies. Nothing here
runs as root and no Microsoft installer is executed.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

BUNDLES = {
    "winpe-addon": (
        "https://download.microsoft.com/download/a4a79e7a-f085-41c4-aebf-2538fd000790/adkwinpeaddons/",
        "adkwinpesetup.exe", "d4de67acc83de253ccc941dd0590808d83726bdd8ea768a374a7beaff7949d6a"),
    "adk": (
        "https://download.microsoft.com/download/8e0c0f5a-abb5-4358-a51b-168eb40b1590/adk/",
        "adksetup.exe", "ac6a930fdb5c2980ba5fefe606d47edaafcf5f647b4337411500d158ea77300f"),
}
PACKAGES = (
    ("winpe-addon", "package_WindowsPEBootFilesOnecoreUAP_x86_en_us"),
    ("winpe-addon", "package_WindowsPEBootFilesDesktopEditions_x86_en_us"),
    ("winpe-addon", "package_WindowsPEwimsDesktopEditions_x86_en_us"),
    ("adk", "package_OscdimgOnecoreUAP_x86_en_us"),
)
EMS_PORT = 1
EMS_BAUD = 115200
EMSSETTINGS = "{0ce4991b-e6b3-4b16-b23c-5e0d9250e5d9}"
WINPE_LOADER = "{7619dcc9-fafe-11d9-b411-000476eba25f}"
TOOLS = {"curl": "curl", "7z": "7zip", "xorriso": "xorriso", "msiinfo": "msitools",
         "wimlib-imagex": "wimtools", "hivexsh": "libhivex-bin", "hivexml": "libhivex-bin"}


def digester(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def which(name):
    path = shutil.which(name)
    if not path:
        raise SystemExit(f"missing tool {name}; install the '{TOOLS.get(name, name)}' package")
    return path


class Builder:
    def __init__(self, downloads, build, output):
        self.downloads = Path(downloads).absolute()
        self.build_dir = Path(build).absolute()
        self.output = Path(output).absolute()
        self.work = self.build_dir / ("winpe-" + str(uuid.uuid4()))
        self.media = self.work / "media"
        self.media.mkdir(parents=True)
        self.payloads = {}
        self.receipts = []

    def run(self, args, **kwargs):
        return subprocess.run([str(a) for a in args], check=True, **kwargs)

    # -- downloads ------------------------------------------------------
    def download(self, url, target, *, expected, algorithm="sha1", size=None):
        """Reuse a verified file, resume a partial one, restart on mismatch."""
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        if target.is_file() and (size is None or target.stat().st_size == size) \
                and digester(target, algorithm) == expected.lower():
            self.receipts.append({"url": url, "file": target.name, "size": target.stat().st_size,
                                  "algorithm": algorithm, "digest": expected.lower(), "reused": True})
            return target
        os.replace(part, part.with_suffix(".stale")) if part.exists() else None
        for attempt in (1, 2):
            resume = part.exists() and size is not None and part.stat().st_size < size
            mode = "--continue-at" if resume else "--output"
            print(f"download: {target.name}{' (resume)' if resume else ''}", flush=True)
            if not resume and part.exists():
                part.unlink()
            command = [which("curl"), "--fail", "--location", "--proto", "=https",
                       "--proto-redir", "=https", "--connect-timeout", "10",
                       "--max-time", "1800", "--retry", "1", "--progress-bar"]
            command += ["--continue-at", "-", "--output", str(part), url] if resume \
                else ["--output", str(part), url]
            self.run(command)
            good = part.is_file() and (size is None or part.stat().st_size == size) \
                and digester(part, algorithm) == expected.lower()
            if good:
                os.replace(part, target)
                self.receipts.append({"url": url, "file": target.name,
                                      "size": target.stat().st_size, "algorithm": algorithm,
                                      "digest": expected.lower(), "reused": False})
                return target
            # A server that ignores Range corrupts a resumed file; start over.
            print(f"verify failed for {target.name}; restarting from zero", flush=True)
            part.unlink(missing_ok=True)
        raise SystemExit(f"could not obtain a verified copy of {target.name}")

    def bundle(self, name):
        base, filename, sha = BUNDLES[name]
        exe = self.download(base + filename, self.downloads / "microsoft" / filename,
                            expected=sha, algorithm="sha256")
        bootstrap = self.build_dir / name / "bootstrap"
        bootstrap.mkdir(parents=True, exist_ok=True)
        self.run([which("7z"), "x", "-y", "-o" + str(bootstrap), exe], stdout=subprocess.DEVNULL)
        for node in ET.parse(bootstrap / "0").getroot().iter():
            if node.tag.rsplit("}", 1)[-1] == "Payload":
                self.payloads[(name, node.get("Id"))] = node.attrib

    def payload(self, bundle, item):
        relative = PurePosixPath(item["FilePath"].replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise SystemExit("unsafe installer payload path")
        folder = self.downloads / "microsoft" / ("adk" if bundle == "adk" else "")
        return self.download(BUNDLES[bundle][0] + quote(str(relative), safe="/"),
                             folder / str(relative), expected=item["Hash"],
                             size=int(item["FileSize"]))

    # -- MSI tables -----------------------------------------------------
    def table(self, msi, name):
        result = self.run([which("msiinfo"), "export", msi, name], capture_output=True, text=True)
        rows = list(csv.reader(io.StringIO(result.stdout), delimiter="\t"))
        return [dict(zip(rows[0], row)) for row in rows[3:]]

    def files(self, bundle, key):
        msi = self.payload(bundle, self.payloads[(bundle, key)])
        dirs = {r["Directory"]: r for r in self.table(msi, "Directory")}
        components = {r["Component"]: r for r in self.table(msi, "Component")}
        media = sorted(self.table(msi, "Media"), key=lambda r: int(r["LastSequence"]))
        selected = []
        for item in self.table(msi, "File"):
            ident = components[item["Component_"]]["Directory_"]
            parts, visited = [], set()
            while ident:
                if ident in visited:
                    raise SystemExit("installer directory cycle")
                visited.add(ident)
                row = dirs[ident]
                parts.append(row["DefaultDir"].split(":", 1)[0].split("|")[-1])
                ident = row["Directory_Parent"]
            parts.reverse()
            filename = item["FileName"].split("|")[-1]
            if "amd64" not in parts:
                continue
            relative = PurePosixPath(*parts[parts.index("amd64"):], filename)
            keep = (str(relative).startswith("amd64/Media/") or
                    str(relative) == "amd64/en-us/winpe.wim") if bundle == "winpe-addon" \
                else filename == "etfsboot.com"
            if not keep:
                continue
            if ".." in relative.parts:
                raise SystemExit("unsafe MSI file destination")
            cabinet = next(r["Cabinet"] for r in media if int(r["LastSequence"]) >= int(item["Sequence"]))
            selected.append({"bundle": bundle, "id": item["File"], "path": str(relative),
                             "size": int(item["FileSize"]), "cab": cabinet})
        return selected

    # -- assembly -------------------------------------------------------
    def assemble(self, plan):
        for bundle, cab in sorted({(r["bundle"], r["cab"]) for r in plan}):
            payload = next(item for (b, _), item in self.payloads.items()
                           if b == bundle and item["FilePath"].replace("\\", "/").split("/")[-1] == cab)
            archive = self.payload(bundle, payload)
            extract = self.work / "cabs" / bundle / cab
            extract.mkdir(parents=True)
            entries = [r for r in plan if r["bundle"] == bundle and r["cab"] == cab]
            self.run([which("7z"), "x", "-y", "-o" + str(extract), archive,
                      *(r["id"] for r in entries)], stdout=subprocess.DEVNULL)
            for item in entries:
                source = extract / item["id"]
                if source.stat().st_size != item["size"]:
                    raise SystemExit("MSI file size mismatch: " + item["path"])
                relative = PurePosixPath(item["path"])
                if "Media" in relative.parts:
                    target = self.media.joinpath(*relative.parts[2:])
                    if target.name.lower() == "bootfix.bin":
                        continue  # avoid an optional "press any key" prompt
                elif relative.name == "winpe.wim":
                    target = self.media / "sources/boot.wim"
                else:
                    target = self.media / "Boot/etfsboot.com"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)

    def customize(self):
        """Only the startup script: the console comes from EMS/SAC."""
        overlay = self.work / "overlay"
        system32 = overlay / "Windows/System32"
        system32.mkdir(parents=True)
        startup = (Path(__file__).resolve().parent.parent / "guest/startnet.cmd").read_bytes()
        startup = startup.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        (system32 / "startnet.cmd").write_bytes(startup.replace(b"\n", b"\r\n"))
        wim = self.media / "sources/boot.wim"
        tool = which("wimlib-imagex")
        listing = self.run([tool, "dir", wim, "1"], text=True, capture_output=True).stdout.lower()
        for needed in ("/windows/system32/cmd.exe", "/windows/system32/chkdsk.exe",
                       "/windows/system32/drivers/sacdrv.sys", "/windows/system32/wpeinit.exe"):
            if needed not in listing:
                raise SystemExit("required base WinPE component is missing: " + needed)
        self.run([tool, "update", wim, "1", "--check", "--threads=2",
                  "--command=add " + json.dumps(str(overlay)) + " /"])
        self.run([tool, "info", wim, "1", "--boot"])
        return {"boot_wim_sha256": digester(wim)}

    def enable_ems(self, bcd):
        """Enable EMS/SAC on COM1 so the console needs no guest program."""
        elements = "\\Objects\\{}\\Elements".format
        script = [
            f"cd {elements(EMSSETTINGS)}",
            "cd 16000020", "setval 1", "Element", "hex:3:01",
            f"cd {elements(EMSSETTINGS)}",
            "add 15000022", "cd 15000022", "setval 1", "Element", f"hex:3:{EMS_PORT:02x}",
            f"cd {elements(EMSSETTINGS)}",
            "add 15000023", "cd 15000023", "setval 1", "Element", "hex:3:00,c2,01",
            f"cd {elements(WINPE_LOADER)}",
            "cd 260000b0", "setval 1", "Element", "hex:3:01",
            "commit", "quit",
        ]
        # -w is required: without it hivexsh silently discards modifications.
        self.run([which("hivexsh"), "-w", bcd], input="\n".join(script) + "\n", text=True)
        dump = self.run([which("hivexml"), bcd], capture_output=True, text=True).stdout
        for value in ('value="AQ=="', 'value="AMIB"'):
            if value not in dump:
                raise SystemExit("EMS value not applied: " + value)
        return {"ems_port": EMS_PORT, "ems_baud": EMS_BAUD}

    def build(self):
        for name in BUNDLES:
            self.bundle(name)
        plan = [r for bundle, key in PACKAGES for r in self.files(bundle, key)]
        (self.work / "file-plan.json").write_text(json.dumps(plan, indent=2) + "\n")
        self.assemble(plan)
        hashes = self.customize()
        hashes.update(self.enable_ems(self.media / "Boot/BCD"))
        for required in ("bootmgr", "Boot/BCD", "Boot/boot.sdi", "Boot/etfsboot.com",
                         "sources/boot.wim"):
            if not (self.media / required).is_file():
                raise SystemExit("required boot file is missing: " + required)
        image = self.work / "winpe.iso"
        self.run([which("xorriso"), "-as", "mkisofs", "-iso-level", "3", "-d", "-N",
                  "-J", "-joliet-long", "-V", "WINPE_TERMINAL",
                  "-b", "Boot/etfsboot.com", "-no-emul-boot", "-boot-load-size", "8",
                  "-o", image, self.media])
        receipt = {
            "schema": 2,
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "installer_version": "10.1.26100.9457",
            "architecture": "amd64",
            "boot_mode": "BIOS",
            "console": "EMS/SAC over COM1",
            "windows_servicing_updates_applied": False,
            "iso_sha256": digester(image),
            **hashes,
            "payloads": self.receipts,
        }
        self.output.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image, self.output / "winpe.iso")
        (self.output / "winpe-build.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print("built:", self.output / "winpe.iso", flush=True)
        print("NOTE: base image is not serviced with cumulative updates.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloads", type=Path, required=True, help="verified download cache")
    parser.add_argument("--build", type=Path, required=True, help="scratch build directory")
    parser.add_argument("--output", type=Path, required=True, help="where the finished image is written")
    parser.add_argument("--force", action="store_true", help="rebuild even if the output exists")
    parser.add_argument("--accept-microsoft-license", action="store_true",
                        help="the operator has read and accepts the pinned ADK/WinPE terms")
    args = parser.parse_args()
    if not args.accept_microsoft_license:
        parser.error("Microsoft licence acceptance is required; run this through 'winpe_terminal prepare'.")
    if os.geteuid() == 0:
        parser.error("downloading and building must not run as root")
    try:
        Builder(args.downloads, args.build, args.output).build()
    except (OSError, ValueError, StopIteration, subprocess.SubprocessError) as exc:
        print("WinPE build failed:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
