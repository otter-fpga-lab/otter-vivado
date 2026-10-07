"""Vivado remote builds; migrated from fpga-remote e6ff98c2ec5bea07e440d694fa35602aa32eb74f."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

ASSET_ROOT = Path(__file__).resolve().parent / "assets"
SOURCE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = SOURCE_ROOT / "config" / "remote-hosts.local.json"
HOST_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
BUILD_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}(?:-[0-9a-f]{8})?$")
WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
STATE_RE = re.compile(r"^[A-Z_]+$")
GENERATED_FILE_PREFIXES = (
    "$PGENDIR/",
    "$PRUNDIR/",
    "$PIPUSERFILESDIR/",
    "$PCACHEDIR/",
)


class CliError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProjectInfo:
    root: Path
    xpr: Path
    srcs: Path
    name: str
    vivado_version: str
    part: str
    top: str
    source_file_count: int
    source_tree_sha256: str
    referenced_file_count: int
    generated_reference_count: int


@dataclass(frozen=True)
class TclJobInfo:
    root: Path
    manifest: Path
    name: str
    vivado_version: str
    entry: str
    arguments: tuple[str, ...]
    files: tuple[Path, ...]
    required_outputs: tuple[str, ...]
    reports: dict[str, str]
    source_identity: dict[str, Any]


@dataclass(frozen=True)
class BuildHandle:
    host: str
    project: str
    build_id: str
    workspace: str = ""

    def __str__(self) -> str:
        prefix = f"{self.host}/{self.workspace}" if self.workspace else self.host
        return f"{prefix}/{self.project}/{self.build_id}"


@dataclass(frozen=True)
class BuildRecord:
    handle: BuildHandle
    state: str


@dataclass(frozen=True)
class BuildOptions:
    target: str = "bitstream"
    reference_handle: str = ""
    reference_checkpoint: str = ""


@dataclass(frozen=True)
class BuildSubmission:
    info: ProjectInfo | TclJobInfo
    handle: BuildHandle
    jobs: int
    probe: dict[str, str]
    verify_output: str
    launch_output: str
    options: BuildOptions = BuildOptions()


@dataclass(frozen=True)
class FetchResult:
    package: Path | None
    package_sha256: str
    checksum_file: Path | None
    extract_dir: Path | None


def run_process(
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: int | None = None,
    check: bool = True,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    run_kwargs: dict[str, Any] = {
        "capture_output": not stream,
        "timeout": timeout,
        "check": False,
    }
    if input_text is None:
        run_kwargs.update(text=True, encoding="utf-8", errors="replace")
    else:
        normalized_input = input_text.replace("\r\n", "\n").replace("\r", "\n")
        run_kwargs["input"] = normalized_input.encode("utf-8")

    try:
        raw_result = subprocess.run(args, **run_kwargs)
    except FileNotFoundError as exc:
        raise CliError(f"Executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CliError(f"Command timed out after {timeout}s: {args[0]}") from exc

    stdout = raw_result.stdout
    stderr = raw_result.stderr
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    result = subprocess.CompletedProcess(
        raw_result.args,
        raw_result.returncode,
        stdout=stdout,
        stderr=stderr,
    )

    if check and result.returncode != 0:
        detail = ""
        if not stream:
            combined = "\n".join(
                part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
            )
            if combined:
                detail = "\n" + "\n".join(combined.splitlines()[-40:])
        raise CliError(
            f"Command failed with exit {result.returncode}: {' '.join(args[:4])}{detail}"
        )
    return result


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CliError(
            f"Remote host config not found: {path}. "
            "Prepare config/remote-hosts.local.json from config/remote-hosts.example.json, "
            "or select a config with --config / OTTER_VIVADO_REMOTE_CONFIG. "
            "Offline inspect and report --results do not require host configuration."
        ) from exc
    except json.JSONDecodeError as exc:
        raise CliError(f"Invalid JSON config {path}: {exc}") from exc

    if data.get("schema_version") != 1:
        raise CliError("Unsupported config schema_version")
    hosts = data.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        raise CliError("Config must contain at least one host profile")
    for name, profile in hosts.items():
        validate_host_profile(name, profile)
    return data


def validate_host_profile(name: str, profile: dict[str, Any]) -> None:
    if not HOST_NAME_RE.fullmatch(name):
        raise CliError(f"Invalid host profile name: {name}")
    alias = profile.get("ssh_alias")
    if not isinstance(alias, str) or not HOST_NAME_RE.fullmatch(alias):
        raise CliError(f"Invalid ssh_alias for host {name}")
    work_root = profile.get("work_root")
    if not isinstance(work_root, str) or not work_root.startswith("/") or work_root == "/":
        raise CliError(f"Unsafe work_root for host {name}: {work_root!r}")
    if profile.get("scheduler") != "nohup":
        raise CliError(f"Unsupported scheduler for host {name}")
    shared = profile.get("shared")
    if shared is not None:
        if not isinstance(shared, dict):
            raise CliError(f"Invalid shared mapping for host {name}")
        if not isinstance(shared.get("windows_root"), str) or not shared["windows_root"].startswith(
            "\\\\"
        ):
            raise CliError(f"shared.windows_root must be a UNC path for host {name}")
        linux_root = shared.get("linux_root")
        if not isinstance(linux_root, str) or not linux_root.startswith("/") or linux_root == "/":
            raise CliError(f"Invalid shared.linux_root for host {name}")
        if ".." in PurePosixPath(linux_root).parts:
            raise CliError(f"Unsafe shared.linux_root for host {name}")
        shared_area = PurePosixPath(linux_root) / "fpga-remote"
        work = PurePosixPath(work_root)
        if shared_area == work or work in shared_area.parents or shared_area in work.parents:
            raise CliError("Shared delivery area and work_root must be separate")
    for key in ("max_jobs", "max_parallel_builds", "warn_free_gib", "min_free_gib"):
        value = profile.get(key)
        if not isinstance(value, int) or value < 1:
            raise CliError(f"Invalid {key} for host {name}")
    if profile["max_parallel_builds"] != 1:
        raise CliError("The nohup queue supports one active build per host")
    retention = profile.get("retention", {})
    if not isinstance(retention, dict):
        raise CliError(f"Invalid retention map for host {name}")
    for key, default in (
        ("keep_succeeded", 1),
        ("keep_failed", 3),
        ("keep_other", 1),
    ):
        value = retention.get(key, default)
        if not isinstance(value, int) or value < 0:
            raise CliError(f"Invalid retention.{key} for host {name}")
    tools = profile.get("tools", {})
    for tool_name in ("vivado", "petalinux"):
        versions = tools.get(tool_name, {})
        if not isinstance(versions, dict):
            raise CliError(f"Invalid {tool_name} map for host {name}")
        for version, install_root in versions.items():
            if not re.fullmatch(r"[0-9]{4}\.[0-9]+", version):
                raise CliError(f"Invalid {tool_name} version: {version}")
            if not isinstance(install_root, str) or not install_root.startswith("/"):
                raise CliError(f"Invalid {tool_name} path: {install_root}")


def get_profile(config: dict[str, Any], name: str) -> dict[str, Any]:
    try:
        return config["hosts"][name]
    except KeyError as exc:
        available = ", ".join(sorted(config["hosts"]))
        raise CliError(f"Unknown host {name!r}; available: {available}") from exc


def ssh_bash(profile: dict[str, Any], script: str, *, timeout: int = 60) -> str:
    ssh = shutil.which("ssh")
    if not ssh:
        raise CliError("ssh executable not found")
    alias = profile["ssh_alias"]
    result = run_process(
        [ssh, "-o", "BatchMode=yes", alias, "bash", "-s"],
        input_text=script,
        timeout=timeout,
    )
    return result.stdout


def scp_upload(profile: dict[str, Any], sources: Iterable[Path], remote_dir: str) -> None:
    scp = shutil.which("scp")
    if not scp:
        raise CliError("scp executable not found")
    args = [scp, *(str(path) for path in sources), f"{profile['ssh_alias']}:{remote_dir}/"]
    run_process(args, timeout=300)


def scp_download(profile: dict[str, Any], remote_path: str, local_path: Path) -> None:
    scp = shutil.which("scp")
    if not scp:
        raise CliError("scp executable not found")
    run_process(
        [scp, f"{profile['ssh_alias']}:{remote_path}", str(local_path)],
        timeout=300,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    return bool(checker and checker())


def source_files(info_root: Path, xpr: Path, srcs: Path) -> list[Path]:
    if srcs.is_symlink() or _is_junction(srcs):
        raise CliError(f"Source directory is a link/reparse point: {srcs}")
    files = [xpr]
    for path in sorted(srcs.rglob("*")):
        if path.is_symlink() or _is_junction(path):
            raise CliError(f"Source tree contains a link/reparse point: {path}")
        if path.is_file():
            files.append(path)
    if len(files) < 2:
        raise CliError(f"Source directory is empty: {srcs}")
    for path in files:
        try:
            path.relative_to(info_root)
        except ValueError as exc:
            raise CliError(f"Source escapes project root: {path}") from exc
    return files


def source_tree_hash(root: Path, files: Iterable[Path]) -> str:
    lines: list[str] = []
    for path in sorted(files):
        relative = path.relative_to(root).as_posix()
        lines.append(f"{sha256_file(path)}  {relative}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def inspect_project(project: Path) -> ProjectInfo:
    project = project.expanduser().resolve(strict=True)
    if project.is_dir():
        candidates = sorted(project.glob("*.xpr"))
        if len(candidates) != 1:
            raise CliError(
                f"Expected exactly one top-level .xpr in {project}, found {len(candidates)}"
            )
        xpr = candidates[0]
    elif project.is_file() and project.suffix.lower() == ".xpr":
        xpr = project
    else:
        raise CliError(f"Project must be a directory or .xpr file: {project}")

    root = xpr.parent
    name = xpr.stem
    if not PROJECT_NAME_RE.fullmatch(name):
        raise CliError(f"Project name is not remote-safe: {name}")
    srcs = root / f"{name}.srcs"
    if not srcs.is_dir():
        raise CliError(f"Matching source directory not found: {srcs}")

    text = xpr.read_text(encoding="utf-8")
    version_match = re.search(r"Product Version:\s*Vivado v([0-9]{4}\.[0-9]+)", text)
    if not version_match:
        raise CliError("Vivado product version comment not found in XPR")

    try:
        xml_root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CliError(f"Invalid XPR XML: {exc}") from exc

    part_node = xml_root.find("./Configuration/Option[@Name='Part']")
    source_set = xml_root.find("./FileSets/FileSet[@Name='sources_1']")
    top_node = None if source_set is None else source_set.find("./Config/Option[@Name='TopModule']")
    part = "" if part_node is None else part_node.get("Val", "")
    top = "" if top_node is None else top_node.get("Val", "")
    if not part or not top:
        raise CliError("XPR must define Configuration Part and sources_1 TopModule")

    referenced = 0
    generated_references = 0
    srcs_resolved = srcs.resolve(strict=True)
    for file_node in xml_root.findall("./FileSets/FileSet/File"):
        raw_path = file_node.get("Path", "")
        if not raw_path:
            continue
        if raw_path.startswith("$PSRCDIR/"):
            referenced += 1
            relative = raw_path.removeprefix("$PSRCDIR/")
            actual = (srcs / Path(relative.replace("/", os.sep))).resolve()
            try:
                actual.relative_to(srcs_resolved)
            except ValueError as exc:
                raise CliError(f"Referenced source escapes .srcs: {raw_path}") from exc
            if not actual.is_file():
                raise CliError(f"Referenced source file is missing: {actual}")
        elif raw_path.startswith(GENERATED_FILE_PREFIXES):
            generated_references += 1
        else:
            raise CliError(f"Unsupported external File Path in XPR: {raw_path}")

    files = source_files(root, xpr, srcs)
    return ProjectInfo(
        root=root,
        xpr=xpr,
        srcs=srcs,
        name=name,
        vivado_version=version_match.group(1),
        part=part,
        top=top,
        source_file_count=len(files) - 1,
        source_tree_sha256=source_tree_hash(root, files),
        referenced_file_count=referenced,
        generated_reference_count=generated_references,
    )


def project_info_dict(info: ProjectInfo | TclJobInfo) -> dict[str, Any]:
    if isinstance(info, TclJobInfo):
        return {
            "flow": "tcl",
            "name": info.name,
            "bundle_root": str(info.root),
            "manifest": str(info.manifest),
            "entry": info.entry,
            "arguments": list(info.arguments),
            "vivado_version": info.vivado_version,
            "input_file_count": len(info.files),
            "source_identity": info.source_identity,
        }
    return {
        "project_root": str(info.root),
        "xpr": str(info.xpr),
        "srcs": str(info.srcs),
        "name": info.name,
        "vivado_version": info.vivado_version,
        "part": info.part,
        "top": info.top,
        "source_file_count": info.source_file_count,
        "referenced_file_count": info.referenced_file_count,
        "generated_reference_count": info.generated_reference_count,
        "source_tree_sha256": info.source_tree_sha256,
    }


def portable_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise CliError(f"Expected a portable relative path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise CliError(f"Path escapes job directory: {value}")
    return path


def inspect_tcl_job(manifest: Path) -> TclJobInfo:
    manifest = manifest.expanduser().resolve(strict=True)
    root = manifest.parent
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError(f"Cannot read Tcl job manifest: {manifest}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise CliError("Tcl job manifest requires schema_version 1")
    unknown = set(data) - {
        "schema_version",
        "name",
        "vivado_version",
        "entry",
        "arguments",
        "inputs",
        "required_outputs",
        "reports",
        "source_identity",
    }
    if unknown:
        raise CliError(f"Unknown Tcl job fields: {sorted(unknown)}")
    name, version = data.get("name"), data.get("vivado_version")
    if not isinstance(name, str) or not PROJECT_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise CliError("Tcl job requires a valid name")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]{4}\.[0-9]+", version):
        raise CliError("Tcl job requires vivado_version, e.g. 2024.2")
    entry = portable_relative_path(data.get("entry"))
    arguments = data.get("arguments", [])
    if not isinstance(arguments, list) or any(
        not isinstance(arg, str) or "\x00" in arg for arg in arguments
    ):
        raise CliError("arguments must be a list of strings without NUL characters")
    inputs = data.get("inputs", [])
    required = data.get("required_outputs", [])
    reports = data.get("reports", {})
    source_identity = data.get("source_identity", {})
    if not isinstance(inputs, list) or not isinstance(required, list):
        raise CliError("inputs and required_outputs must be lists of relative paths")
    if not isinstance(reports, dict) or set(reports) - {
        "timing",
        "check_timing",
        "drc",
        "methodology",
        "route",
        "cdc",
        "utilization",
    }:
        raise CliError("Unsupported reports mapping")
    if not isinstance(source_identity, dict):
        raise CliError("source_identity must be an object")
    for value in [*required, *reports.values()]:
        portable_relative_path(value)
    files: set[Path] = set()
    for item in [entry.as_posix(), *inputs]:
        relative = portable_relative_path(item)
        candidate = root.joinpath(*relative.parts)
        for ancestor in (candidate, *candidate.parents):
            if ancestor == root:
                break
            if ancestor.is_symlink() or _is_junction(ancestor):
                raise CliError(f"Linked job input is not supported: {ancestor}")
        if not candidate.exists():
            raise CliError(f"Job input is missing: {candidate}")
        paths = candidate.rglob("*") if candidate.is_dir() else [candidate]
        for path in paths:
            if (
                path.is_symlink()
                or _is_junction(path)
                or not path.resolve(strict=True).is_relative_to(root)
            ):
                raise CliError(f"Job input escapes snapshot or is linked: {path}")
            if path.is_file():
                files.add(path)
    if not root.joinpath(*entry.parts).is_file():
        raise CliError("Tcl entry must be a file")
    return TclJobInfo(
        root,
        manifest,
        name,
        version,
        entry.as_posix(),
        tuple(arguments),
        tuple(sorted(files)),
        tuple(required),
        reports,
        source_identity,
    )


def print_mapping(mapping: dict[str, Any]) -> None:
    width = max(len(key) for key in mapping)
    for key, value in mapping.items():
        print(f"{key.ljust(width)} : {value}")


def parse_key_values(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def probe_host(profile: dict[str, Any], vivado_version: str | None = None) -> dict[str, str]:
    work_root = profile["work_root"]
    if vivado_version is None:
        versions = sorted(profile["tools"]["vivado"])
        vivado_version = versions[-1] if versions else ""
    vivado_root = profile["tools"]["vivado"].get(vivado_version, "")
    script = f"""set -eu
work_root={shlex.quote(work_root)}
vivado_root={shlex.quote(vivado_root)}
printf 'HOST=%s\\n' "$(hostname)"
printf 'USER=%s\\n' "$(id -un)"
printf 'ARCH=%s\\n' "$(uname -m)"
printf 'NPROC=%s\\n' "$(nproc)"
printf 'MEM_KIB=%s\\n' "$(awk '/MemTotal:/ {{print $2}}' /proc/meminfo)"
printf 'WORK_ROOT_EXISTS=%s\\n' "$([ -d "$work_root" ] && echo yes || echo no)"
printf 'WORK_ROOT_WRITABLE=%s\\n' "$([ -d "$work_root" ] && [ -w "$work_root" ] && echo yes || echo no)"
printf 'DISK_AVAILABLE_KIB=%s\\n' "$(df -Pk "${{work_root%/*}}" | awk 'NR==2 {{print $4}}')"
running=0
if [ -d "$work_root" ]; then
    for state_file in "$work_root"/*/*/logs/state "$work_root"/workspaces/*/*/*/logs/state; do
        [ -f "$state_file" ] || continue
        state=$(cat "$state_file")
        if [ "$state" != RUNNING ] && [ "$state" != PACKAGING ]; then
            continue
        fi
        pid_file="${{state_file%/state}}/runner.pid"
        if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" 2>/dev/null; then
            running=$((running + 1))
        fi
    done
fi
printf 'RUNNING_BUILDS=%s\\n' "$running"
if [ -n "$vivado_root" ] && [ -x "$vivado_root/bin/vivado" ]; then
    printf 'VIVADO_ROOT=%s\\n' "$vivado_root"
    printf 'VIVADO_VERSION_LINE=%s\\n' "$($vivado_root/bin/vivado -version | sed -n '1p')"
else
    printf 'VIVADO_ROOT=%s\\n' "$vivado_root"
    printf 'VIVADO_VERSION_LINE=missing\\n'
fi
printf 'TAR=%s\\n' "$(command -v tar || true)"
printf 'SHA256SUM=%s\\n' "$(command -v sha256sum || true)"
"""  # noqa: E501 - preserve native shell/report literal layout
    return parse_key_values(ssh_bash(profile, script, timeout=90))


def parse_handle(value: str) -> BuildHandle:
    parts = value.split("/")
    if len(parts) == 3:
        host, project, build_id = parts
        workspace = ""
    elif len(parts) == 4:
        host, workspace, project, build_id = parts
        require_workspace(workspace)
    else:
        raise CliError(
            "Build handle must be host/workspace/project/build-id "
            "(legacy three-part handles are also accepted)"
        )
    if not HOST_NAME_RE.fullmatch(host):
        raise CliError(f"Invalid host in build handle: {host}")
    if not PROJECT_NAME_RE.fullmatch(project) or project in {".", ".."}:
        raise CliError(f"Invalid project in build handle: {project}")
    if not BUILD_ID_RE.fullmatch(build_id):
        raise CliError(f"Invalid build id: {build_id}")
    return BuildHandle(host, project, build_id, workspace)


def require_workspace(value: str | None) -> str:
    if not value or not WORKSPACE_RE.fullmatch(value):
        raise CliError(
            "Specify --workspace with a name of 1..80 letters/digits/._- "
            "starting with a letter or digit; Codex defaults to CODEX_THREAD_ID"
        )
    return value


def build_relative_path(handle: BuildHandle) -> PurePosixPath:
    root = PurePosixPath("workspaces") / handle.workspace if handle.workspace else PurePosixPath()
    return root / handle.project / handle.build_id


def shared_relative_root(workspace: str) -> PurePosixPath:
    root = PurePosixPath("fpga-remote")
    return root / "workspaces" / workspace if workspace else root


def workspace_roots(profile: dict[str, Any], workspace: str) -> list[str]:
    roots = [str(PurePosixPath(profile["work_root"]) / "workspaces" / workspace)]
    if profile.get("shared"):
        roots.append(
            str(PurePosixPath(profile["shared"]["linux_root"]) / shared_relative_root(workspace))
        )
    return roots


@contextmanager
def workspace_lease(profile: dict[str, Any], workspace: str, *, initialize: bool = False):
    """Keep cleanup excluded for the entire client workflow, including source staging."""
    lock_dir = str(PurePosixPath(profile["work_root"]) / ".locks")
    lock_file = f"{lock_dir}/{workspace}.lock"
    roots = (
        " ".join(shlex.quote(root) for root in workspace_roots(profile, workspace))
        if initialize
        else ""
    )
    inner = f"""set -eu
for root in {roots}; do
    if [ -e "$root" ]; then
        test ! -L "$root"
        test "$(cat "$root/.fpga-remote-workspace")" = fpga-remote-workspace-v1
    else
        mkdir -p "${{root%/*}}"
        mkdir "$root"
        printf 'fpga-remote-workspace-v1\\n' > "$root/.fpga-remote-workspace"
    fi
done
printf 'LOCKED\\n'
cat >/dev/null
"""
    script = (
        f"mkdir -p {shlex.quote(lock_dir)} "
        f"&& exec flock -sn {shlex.quote(lock_file)} bash -c {shlex.quote(inner)}"
    )
    process = subprocess.Popen(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            profile["ssh_alias"],
            "bash",
            "-c",
            shlex.quote(script),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        if process.stdout.readline().strip() != b"LOCKED":
            process.stdin.close()
            detail = process.stderr.read().decode("utf-8", errors="replace").strip()
            raise CliError(f"Cannot lock workspace {workspace}; cleanup may be running. {detail}")
        yield
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def parse_build_records(host: str, output: str) -> list[BuildRecord]:
    records: list[BuildRecord] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) == 3:
            project, build_id, state = fields
            handle = parse_handle(f"{host}/{project}/{build_id}")
        elif len(fields) == 4:
            workspace, project, build_id, state = fields
            handle = parse_handle(f"{host}/{workspace}/{project}/{build_id}")
        else:
            raise CliError(f"Invalid remote build record: {line}")
        if not STATE_RE.fullmatch(state):
            raise CliError(f"Invalid remote build state: {state}")
        records.append(BuildRecord(handle=handle, state=state))
    return records


def get_remote_build_records(profile: dict[str, Any], host: str) -> list[BuildRecord]:
    work_root = profile["work_root"]
    script = f"""set -eu
work_root={shlex.quote(work_root)}
if [ ! -d "$work_root" ]; then exit 0; fi
for marker in "$work_root"/*/*/.fpga-remote-work "$work_root"/workspaces/*/*/*/.fpga-remote-work; do
    [ -f "$marker" ] || continue
    root=${{marker%/.fpga-remote-work}}
    build_id=${{root##*/}}
    project_dir=${{root%/*}}
    project=${{project_dir##*/}}
    state=$(cat "$root/logs/state" 2>/dev/null || echo UNKNOWN)
    relative=${{root#"$work_root"/}}
    case "$relative" in
        workspaces/*) workspace_dir=${{project_dir%/*}}; printf '%s\t' "${{workspace_dir##*/}}" ;;
    esac
    printf '%s\t%s\t%s\n' "$project" "$build_id" "$state"
done
"""
    return parse_build_records(host, ssh_bash(profile, script))


def select_prune_candidates(
    records: Iterable[BuildRecord],
    *,
    keep_succeeded: int,
    keep_failed: int,
    keep_other: int,
) -> list[BuildRecord]:
    for value in (keep_succeeded, keep_failed, keep_other):
        if value < 0:
            raise CliError("Retention counts must be non-negative")

    grouped: dict[tuple[str, str], list[BuildRecord]] = {}
    for record in records:
        if record.state in {"RUNNING", "PACKAGING", "STARTING", "QUEUED"}:
            continue
        if record.state == "SUCCEEDED":
            bucket = "succeeded"
        elif record.state in {"FAILED", "PACKAGING_FAILED", "STALE"}:
            bucket = "failed"
        else:
            bucket = "other"
        grouped.setdefault(
            (f"{record.handle.workspace}/{record.handle.project}", bucket), []
        ).append(record)

    keep_by_bucket = {
        "succeeded": keep_succeeded,
        "failed": keep_failed,
        "other": keep_other,
    }
    candidates: list[BuildRecord] = []
    for (_, bucket), group in grouped.items():
        newest_first = sorted(group, key=lambda record: record.handle.build_id, reverse=True)
        candidates.extend(newest_first[keep_by_bucket[bucket] :])
    return sorted(candidates, key=lambda record: str(record.handle))


def remote_build_root(profile: dict[str, Any], handle: BuildHandle) -> str:
    return str(PurePosixPath(profile["work_root"]) / build_relative_path(handle))


def create_source_archive(info: ProjectInfo, destination: Path) -> None:
    with tarfile.open(destination, "w:gz") as archive:
        archive.add(info.xpr, arcname=info.xpr.name, recursive=False)
        archive.add(info.srcs, arcname=info.srcs.name, recursive=True)


def shared_result_dir(profile: dict[str, Any], handle: BuildHandle) -> Path:
    return (
        Path(profile["shared"]["windows_root"]).joinpath(
            *shared_relative_root(handle.workspace).parts
        )
        / "results"
        / handle.project
        / handle.build_id
    )


def resolve_build_options(
    profile: dict[str, Any],
    info: ProjectInfo,
    host: str,
    target: str,
    reference: str | None,
    workspace: str = "",
) -> BuildOptions:
    if target not in {"synth", "route", "bitstream"}:
        raise CliError(f"Unknown build target: {target}")
    if not reference:
        return BuildOptions(target)
    if target == "synth":
        raise CliError("--reference is for incremental implementation, not synthesis")
    if not profile.get("shared"):
        raise CliError("--reference requires shared results on the selected host")
    handle = parse_handle(reference)
    if handle.host != host or handle.project != info.name or handle.workspace != workspace:
        raise CliError("Reference must belong to the same host, workspace and project")
    result_dir = shared_result_dir(profile, handle)
    try:
        complete = (result_dir / "delivery.complete").read_text(encoding="utf-8").strip()
        metadata = parse_key_values(
            (result_dir / "artifacts/build_info.txt").read_text(encoding="utf-8")
        )
    except OSError as exc:
        raise CliError(f"Reference results are unavailable: {result_dir}") from exc
    if complete != "SUCCEEDED":
        raise CliError("Reference build did not complete successfully")
    for key, expected in (
        ("vivado_version", info.vivado_version),
        ("part", info.part),
        ("top", info.top),
    ):
        if metadata.get(key) != expected:
            raise CliError(
                f"Reference {key} mismatch: expected {expected}, got {metadata.get(key)}"
            )
    checkpoint = result_dir / "artifacts/post_route.dcp"
    if not checkpoint.is_file():
        raise CliError(f"Reference contains no post-route checkpoint: {checkpoint}")
    remote_checkpoint = str(
        PurePosixPath(profile["shared"]["linux_root"])
        / shared_relative_root(workspace)
        / "results"
        / handle.project
        / handle.build_id
        / "artifacts/post_route.dcp"
    )
    return BuildOptions(target, str(handle), remote_checkpoint)


def prepare_shared_build(
    profile: dict[str, Any],
    info: ProjectInfo,
    handle: BuildHandle,
    jobs: int,
    vivado_root: str,
    options: BuildOptions = BuildOptions(),
) -> tuple[str, str]:
    shared = profile["shared"]
    windows_root = Path(shared["windows_root"]).resolve(strict=True)
    relative_stage = (
        shared_relative_root(handle.workspace) / "inbox" / handle.project / handle.build_id
    )
    stage = windows_root.joinpath(*relative_stage.parts)
    stage.mkdir(parents=True, exist_ok=False)
    for name in ("vivado_project_build.tcl", "run_vivado_build.sh"):
        (stage / name).write_text(
            (ASSET_ROOT / name).read_text(encoding="utf-8"),
            encoding="utf-8",
            newline="\n",
        )
    try:
        relative_project = info.root.relative_to(windows_root)
    except ValueError:
        relative_project = None
    if relative_project is None:
        (stage / "work").mkdir()
        shutil.copyfile(info.xpr, stage / "work" / info.xpr.name)
        shutil.copytree(info.srcs, stage / "work" / info.srcs.name)
        source_command = 'mv -- "$stage/work" "$root/work"'
    else:
        linux_project = PurePosixPath(shared["linux_root"]).joinpath(*relative_project.parts)
        source_command = (
            'mkdir "$root/work"\n'
            f"cp -a -- {shlex.quote(str(linux_project / info.xpr.name))} "
            f'{shlex.quote(str(linux_project / info.srcs.name))} "$root/work/"'
        )
    root = remote_build_root(profile, handle)
    result_root = str(
        PurePosixPath(shared["linux_root"])
        / shared_relative_root(handle.workspace)
        / "results"
        / handle.project
        / handle.build_id
    )
    reference_command = (
        f'cp -- {shlex.quote(options.reference_checkpoint)} "$root/source/reference.dcp"'
        if options.reference_checkpoint
        else ""
    )
    script = f"""set -eu
root={shlex.quote(root)}
stage={shlex.quote(str(PurePosixPath(shared["linux_root"]) / relative_stage))}
result={shlex.quote(result_root)}
mkdir -p "${{root%/*}}" "${{result%/*}}"
mkdir "$root" "$result"
mkdir "$root/source" "$root/artifacts" "$root/logs"
printf 'fpga-remote-v1\\n' > "$root/.fpga-remote-work"
printf 'STARTING\\n' > "$root/logs/state"
printf 'shared\\n' > "$root/logs/delivery_mode"
{source_command}
mv -- "$stage/vivado_project_build.tcl" "$stage/run_vivado_build.sh" "$root/source/"
{reference_command}
rmdir -- "$stage"
printf 'SOURCE_TRANSFER=shared\\n'
printf 'RESULT_ROOT=%s\\n' "$result"
"""
    output = ssh_bash(profile, script, timeout=300)
    return output, launch_remote_build(
        profile, info, handle, jobs, vivado_root, result_root=result_root, options=options
    )


def prepare_remote_build(
    profile: dict[str, Any],
    info: ProjectInfo,
    handle: BuildHandle,
    jobs: int,
    vivado_root: str,
    options: BuildOptions = BuildOptions(),
) -> tuple[str, str]:
    if profile.get("shared"):
        return prepare_shared_build(profile, info, handle, jobs, vivado_root, options)
    root = remote_build_root(profile, handle)
    with tempfile.TemporaryDirectory(prefix="fpga-remote-") as temp_name:
        temp = Path(temp_name)
        archive_path = temp / "source.tar.gz"
        create_source_archive(info, archive_path)
        archive_hash = sha256_file(archive_path)
        tcl = ASSET_ROOT / "vivado_project_build.tcl"
        runner = ASSET_ROOT / "run_vivado_build.sh"
        tcl_hash = sha256_file(tcl)
        runner_hash = sha256_file(runner)

        create_script = f"""set -eu
root={shlex.quote(root)}
if [ -e "$root" ]; then
    echo "Remote build root already exists: $root" >&2
    exit 20
fi
mkdir -p "$root/source" "$root/work" "$root/artifacts" "$root/logs"
printf 'fpga-remote-v1\\n' > "$root/.fpga-remote-work"
"""
        ssh_bash(profile, create_script)
        scp_upload(profile, [archive_path, tcl, runner], f"{root}/source")

        verify_script = f"""set -eu
root={shlex.quote(root)}
test "$(sha256sum "$root/source/source.tar.gz" | awk '{{print $1}}')" = {shlex.quote(archive_hash)}
test "$(sha256sum "$root/source/vivado_project_build.tcl" | awk '{{print $1}}')" = {shlex.quote(tcl_hash)}
test "$(sha256sum "$root/source/run_vivado_build.sh" | awk '{{print $1}}')" = {shlex.quote(runner_hash)}
tar -tzf "$root/source/source.tar.gz" >/dev/null
tar -xzf "$root/source/source.tar.gz" -C "$root/work"
printf 'SOURCE_ARCHIVE_SHA256=%s\\n' {shlex.quote(archive_hash)}
printf 'EXTRACTED_FILES=%s\\n' "$(find "$root/work" -type f | wc -l)"
"""  # noqa: E501 - preserve native shell/report literal layout
        verify_output = ssh_bash(profile, verify_script)

    return verify_output, launch_remote_build(
        profile, info, handle, jobs, vivado_root, options=options
    )


def launch_remote_build(
    profile: dict[str, Any],
    info: ProjectInfo | TclJobInfo,
    handle: BuildHandle,
    jobs: int,
    vivado_root: str,
    *,
    result_root: str = "",
    options: BuildOptions = BuildOptions(),
) -> str:
    root = remote_build_root(profile, handle)
    native = isinstance(info, TclJobInfo)
    xpr_remote = f"{root}/work/{info.entry if native else info.xpr.name}"
    reference = f"{root}/source/reference.dcp" if options.reference_checkpoint else ""
    ip_cache = (
        str(
            PurePosixPath(profile["shared"]["linux_root"])
            / shared_relative_root(handle.workspace)
            / "cache/ip"
            / info.vivado_version
            / info.part
        )
        if profile.get("shared") and not native
        else ""
    )
    launch_script = f"""set -eu
root={shlex.quote(root)}
export FPGA_REMOTE_RESULT_ROOT={shlex.quote(result_root)}
export FPGA_REMOTE_TARGET={shlex.quote(options.target)}
export FPGA_REMOTE_REFERENCE={shlex.quote(reference)}
export FPGA_REMOTE_IP_CACHE={shlex.quote(ip_cache)}
export FPGA_REMOTE_FLOW={"tcl" if native else "project"}
mkdir -p {shlex.quote(str(PurePosixPath(profile["work_root"]) / ".locks"))}
printf 'QUEUED\\n' > "$root/logs/state"
nohup flock -s {shlex.quote(str(PurePosixPath(profile["work_root"]) / ".locks" / ((handle.workspace or "legacy") + ".lock")))} \\
    flock {shlex.quote(str(PurePosixPath(profile["work_root"]) / ".locks/vivado.lock"))} \\
    bash "$root/source/run_vivado_build.sh" \\
    "$root" \\
    {shlex.quote(vivado_root)} \\
    {shlex.quote(xpr_remote)} \\
    {jobs} \\
    {shlex.quote("" if native else info.part)} \\
    {shlex.quote("" if native else info.top)} \\
    {shlex.quote("" if native else info.source_tree_sha256)} \\
    {shlex.quote(info.name)} \\
    {shlex.quote(handle.build_id)} \\
    > "$root/logs/runner-launch.log" 2>&1 < /dev/null &
pid=$!
printf '%s\\n' "$pid" > "$root/logs/runner.pid"
printf 'RUNNER_PID=%s\\n' "$pid"
printf 'REMOTE_ROOT=%s\\n' "$root"
"""  # noqa: E501 - preserve native shell/report literal layout
    return ssh_bash(profile, launch_script)


def submit_tcl_job(args: argparse.Namespace, config: dict[str, Any]) -> BuildSubmission:
    profile = get_profile(config, args.host)
    workspace = require_workspace(args.workspace)
    if not profile.get("shared"):
        raise CliError("run-tcl requires a configured shared folder")
    info = inspect_tcl_job(Path(args.manifest))
    windows_root = Path(profile["shared"]["windows_root"]).resolve(strict=True)
    if info.root.is_relative_to(windows_root / "fpga-remote"):
        raise CliError("Keep Tcl input bundles outside the managed fpga-remote directory")
    jobs, vivado_root, probe = check_build_host(profile, args.host, info.vivado_version, args.jobs)
    build_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    handle = BuildHandle(args.host, info.name, build_id, workspace)
    relative_stage = shared_relative_root(workspace) / "inbox" / info.name / build_id
    stage = windows_root.joinpath(*relative_stage.parts)
    stage.mkdir(parents=True, exist_ok=False)
    (stage / "run_vivado_build.sh").write_text(
        (ASSET_ROOT / "run_vivado_build.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
        newline="\n",
    )
    shutil.copyfile(info.manifest, stage / "native-job.json")
    (stage / "native-args.bin").write_bytes(
        b"".join(arg.encode("utf-8") + b"\x00" for arg in info.arguments)
    )
    try:
        relative_bundle = info.root.relative_to(windows_root)
    except ValueError:
        relative_bundle = None
    if relative_bundle is None:
        (stage / "work").mkdir()
        for source in info.files:
            destination = stage / "work" / source.relative_to(info.root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        source_commands = 'mv -- "$stage/work" "$root/work"'
    else:
        remote_bundle = PurePosixPath(profile["shared"]["linux_root"]).joinpath(
            *relative_bundle.parts
        )
        lines = ['mkdir "$root/work"']
        for source in info.files:
            relative = PurePosixPath(source.relative_to(info.root).as_posix())
            lines.append(f'mkdir -p "$root/work/"{shlex.quote(str(relative.parent))}')
            lines.append(
                f"cp -p -- {shlex.quote(str(remote_bundle / relative))} "
                f'"$root/work/"{shlex.quote(str(relative))}'
            )
        source_commands = "\n".join(lines)
    remote_root = PurePosixPath(profile["shared"]["linux_root"])
    result_root = str(
        remote_root / shared_relative_root(workspace) / "results" / info.name / build_id
    )
    script = f"""set -eu
root={shlex.quote(remote_build_root(profile, handle))}
stage={shlex.quote(str(remote_root / relative_stage))}
result={shlex.quote(result_root)}
mkdir -p "${{root%/*}}" "${{result%/*}}"
mkdir "$root" "$result"
mkdir "$root/source" "$root/artifacts" "$root/logs"
printf 'fpga-remote-v1\\n' > "$root/.fpga-remote-work"
printf 'STARTING\\n' > "$root/logs/state"
printf 'shared\\n' > "$root/logs/delivery_mode"
{source_commands}
mv -- "$stage/run_vivado_build.sh" "$stage/native-job.json" "$stage/native-args.bin" "$root/source/"
rmdir -- "$stage"
printf 'SOURCE_TRANSFER=native_tcl_shared\\n'
printf 'RESULT_ROOT=%s\\n' "$result"
"""
    transfer = ssh_bash(profile, script, timeout=300)
    options = BuildOptions("script")
    launched = launch_remote_build(
        profile, info, handle, jobs, vivado_root, result_root=result_root, options=options
    )
    return BuildSubmission(info, handle, jobs, probe, transfer, launched, options)


def get_build_status(
    profile: dict[str, Any], handle: BuildHandle, *, include_size: bool = True
) -> dict[str, str]:
    root = remote_build_root(profile, handle)
    size_command = "du -sh \"$root\" | awk '{print $1}'" if include_size else "printf not_scanned"
    script = f"""set -eu
root={shlex.quote(root)}
test -f "$root/.fpga-remote-work"
state=$(cat "$root/logs/state" 2>/dev/null || echo STARTING)
pid=$(cat "$root/logs/runner.pid" 2>/dev/null || echo '')
if [ "$state" = RUNNING ] || [ "$state" = PACKAGING ] || [ "$state" = QUEUED ]; then
    if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
        state=STALE
    fi
fi
printf 'STATE=%s\\n' "$state"
printf 'RUNNER_PID=%s\\n' "$pid"
printf 'EXIT_CODE=%s\\n' "$(cat "$root/logs/build.exit" 2>/dev/null || echo '')"
printf 'BUILD_RESULT=%s\\n' "$(cat "$root/logs/build.result" 2>/dev/null || echo '')"
printf 'STARTED_AT=%s\\n' "$(cat "$root/logs/started_at" 2>/dev/null || echo '')"
printf 'FINISHED_AT=%s\\n' "$(cat "$root/logs/finished_at" 2>/dev/null || echo '')"
printf 'SIZE=%s\\n' "$({size_command})"
marker=$(grep -E '^REMOTE_BUILD_' "$root/logs/vivado-driver.log" 2>/dev/null | tail -1 || true)
printf 'LAST_MARKER=%s\\n' "$marker"
"""
    return parse_key_values(ssh_bash(profile, script))


def wait_for_build(
    profile: dict[str, Any],
    handle: BuildHandle,
    *,
    poll_seconds: int,
    timeout_seconds: int,
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    previous = ""
    while True:
        status = get_build_status(profile, handle, include_size=False)
        summary = f"{status.get('STATE')} {status.get('LAST_MARKER', '')}".strip()
        if summary != previous:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {summary}", flush=True)
            previous = summary
        state = status.get("STATE")
        if state == "SUCCEEDED":
            return status
        if state in {"FAILED", "STALE", "PACKAGING_FAILED"}:
            raise CliError(f"Build {handle} ended with state {state}")
        if time.monotonic() >= deadline:
            raise CliError(f"Timed out waiting for build {handle}")
        time.sleep(poll_seconds)


def command_probe(args: argparse.Namespace, config: dict[str, Any]) -> int:
    profile = get_profile(config, args.host)
    data = probe_host(profile, args.vivado_version)
    data = {"profile": args.host, **data}
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_mapping(data)
    return 0


def command_inspect(args: argparse.Namespace, _: dict[str, Any]) -> int:
    info = inspect_project(Path(args.project))
    data = project_info_dict(info)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_mapping(data)
    return 0


def check_build_host(
    profile: dict[str, Any],
    host: str,
    selected_version: str,
    jobs: int | None,
) -> tuple[int, str, dict[str, str]]:
    try:
        vivado_root = profile["tools"]["vivado"][selected_version]
    except KeyError as exc:
        raise CliError(f"Host {host} has no Vivado {selected_version} profile") from exc

    selected_jobs = jobs or profile["max_jobs"]
    if selected_jobs < 1 or selected_jobs > profile["max_jobs"]:
        raise CliError(f"jobs must be 1..{profile['max_jobs']} for host {host}")

    probe = probe_host(profile, selected_version)
    if probe.get("WORK_ROOT_EXISTS") != "yes" or probe.get("WORK_ROOT_WRITABLE") != "yes":
        raise CliError(f"Remote work_root is not ready: {profile['work_root']}")
    version_line = probe.get("VIVADO_VERSION_LINE", "missing")
    if version_line == "missing":
        raise CliError(f"Vivado {selected_version} executable is missing on {host}")
    if f"v{selected_version}" not in version_line:
        raise CliError(
            f"Vivado version mismatch on {host}: expected {selected_version}, got {version_line}"
        )
    available_gib = int(probe["DISK_AVAILABLE_KIB"]) / 1024 / 1024
    if available_gib < profile["min_free_gib"]:
        raise CliError(
            f"Host {host} has only {available_gib:.1f} GiB free; "
            f"minimum is {profile['min_free_gib']} GiB"
        )
    if available_gib < profile["warn_free_gib"]:
        print(f"WARNING: host has only {available_gib:.1f} GiB free", file=sys.stderr)

    return selected_jobs, vivado_root, probe


def submit_remote_build(
    config: dict[str, Any],
    *,
    host: str,
    project: Path,
    vivado_version: str | None,
    jobs: int | None,
    target: str = "bitstream",
    reference: str | None = None,
    workspace: str | None = None,
) -> BuildSubmission:
    profile = get_profile(config, host)
    workspace = require_workspace(workspace or os.environ.get("CODEX_THREAD_ID"))
    info = inspect_project(project)
    if profile.get("shared"):
        managed_root = (Path(profile["shared"]["windows_root"]) / "fpga-remote").resolve()
        if info.root.is_relative_to(managed_root):
            raise CliError(
                "Keep source projects outside the managed fpga-remote directory; "
                "workspace cleanup owns that generated area"
            )
    options = resolve_build_options(profile, info, host, target, reference, workspace)
    selected_version = vivado_version or info.vivado_version
    if selected_version != info.vivado_version:
        raise CliError(f"Project uses Vivado {info.vivado_version}, requested {selected_version}")
    selected_jobs, vivado_root, probe = check_build_host(profile, host, selected_version, jobs)

    build_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    handle = BuildHandle(host, info.name, build_id, workspace)
    verify_output, launch_output = prepare_remote_build(
        profile, info, handle, selected_jobs, vivado_root, options
    )
    return BuildSubmission(
        info=info,
        handle=handle,
        jobs=selected_jobs,
        probe=probe,
        verify_output=verify_output,
        launch_output=launch_output,
        options=options,
    )


def command_build(args: argparse.Namespace, config: dict[str, Any]) -> int:
    submission = submit_remote_build(
        config,
        host=args.host,
        project=Path(args.project),
        vivado_version=args.vivado_version,
        jobs=args.jobs,
        target=args.target,
        reference=args.reference,
        workspace=args.workspace,
    )
    profile = get_profile(config, args.host)
    handle = submission.handle
    print(f"BUILD_HANDLE={handle}")
    print(submission.verify_output.strip())
    print(submission.launch_output.strip())
    if args.wait:
        wait_for_build(
            profile,
            handle,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout,
        )
    return 0


def command_status(args: argparse.Namespace, config: dict[str, Any]) -> int:
    handle = parse_handle(args.handle)
    profile = get_profile(config, handle.host)
    data = {"handle": str(handle), **get_build_status(profile, handle)}
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print_mapping(data)
    return 1 if data.get("STATE") in {"FAILED", "STALE", "PACKAGING_FAILED"} else 0


def command_logs(args: argparse.Namespace, config: dict[str, Any]) -> int:
    handle = parse_handle(args.handle)
    profile = get_profile(config, handle.host)
    root = remote_build_root(profile, handle)
    log_path = f"{root}/logs/vivado-driver.log"
    ssh = shutil.which("ssh")
    if not ssh:
        raise CliError("ssh executable not found")
    if args.follow:
        result = run_process(
            [
                ssh,
                "-o",
                "BatchMode=yes",
                profile["ssh_alias"],
                f"tail -n {args.tail} -F {shlex.quote(log_path)}",
            ],
            stream=True,
            check=False,
        )
        return result.returncode
    script = f"tail -n {args.tail} {shlex.quote(log_path)}"
    print(ssh_bash(profile, script), end="")
    return 0


def fetch_remote_build(
    profile: dict[str, Any],
    handle: BuildHandle,
    *,
    output: Path,
    extract: bool,
) -> FetchResult:
    if profile.get("shared"):
        result_dir = shared_result_dir(profile, handle)
        if result_dir.exists():
            complete = result_dir / "delivery.complete"
            if not complete.is_file() or complete.read_text(encoding="utf-8").strip() not in {
                "SUCCEEDED",
                "FAILED",
            }:
                raise CliError(f"Shared results are not fully published: {result_dir}")
            output = output.expanduser().resolve()
            if output == result_dir.resolve():
                destination = result_dir
            else:
                output.mkdir(parents=True, exist_ok=True)
                destination = output / f"{handle.project}-{handle.build_id}"
                shutil.copytree(result_dir, destination)
            print(f"RESULTS={destination}")
            return FetchResult(None, "", None, destination)
    if extract and not hasattr(tarfile, "data_filter"):
        raise CliError(
            "Safe package extraction requires tarfile.data_filter "
            "(Python 3.12+ or an interpreter with its security backport). "
            "Use a supported product environment; unfiltered extraction is not allowed."
        )
    root = remote_build_root(profile, handle)
    package_name = f"{handle.project}-{handle.build_id}-remote-build.tar.gz"
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    local_package = output / package_name
    local_sum = output / f"{package_name}.sha256"
    scp_download(profile, f"{root}/{package_name}", local_package)
    scp_download(profile, f"{root}/{package_name}.sha256", local_sum)

    sum_line = local_sum.read_text(encoding="utf-8").strip()
    expected = sum_line.split()[0] if sum_line else ""
    actual = sha256_file(local_package)
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or actual != expected:
        raise CliError(
            f"Package SHA-256 mismatch: expected {expected or '<missing>'}, got {actual}"
        )
    print(f"PACKAGE={local_package}")
    print(f"PACKAGE_SHA256={actual}")

    extract_dir: Path | None = None
    if extract:
        extract_dir = output / f"{handle.project}-{handle.build_id}"
        if extract_dir.exists():
            raise CliError(f"Extraction target already exists: {extract_dir}")
        extract_dir.mkdir()
        with tarfile.open(local_package, "r:gz") as archive:
            archive.extractall(extract_dir, filter="data")
        print(f"EXTRACTED={extract_dir}")
    return FetchResult(
        package=local_package,
        package_sha256=actual,
        checksum_file=local_sum,
        extract_dir=extract_dir,
    )


def command_fetch(args: argparse.Namespace, config: dict[str, Any]) -> int:
    handle = parse_handle(args.handle)
    profile = get_profile(config, handle.host)
    fetch_remote_build(
        profile,
        handle,
        output=Path(args.output),
        extract=args.extract,
    )
    return 0


def parse_timing_summary(text: str) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "constraints_met": "All user specified timing constraints are met." in text,
        "wns_ns": None,
        "tns_ns": None,
        "whs_ns": None,
        "ths_ns": None,
        "no_input_delay": None,
        "no_output_delay": None,
    }
    number_pattern = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if "WNS(ns)" not in line or "TNS(ns)" not in line:
            continue
        for candidate in lines[index + 1 : index + 5]:
            values = number_pattern.findall(candidate)
            if len(values) >= 12:
                metrics["wns_ns"] = float(values[0])
                metrics["tns_ns"] = float(values[1])
                metrics["whs_ns"] = float(values[4])
                metrics["ths_ns"] = float(values[5])
                break
        if metrics["wns_ns"] is not None:
            break

    for key, check_name in (
        ("no_input_delay", "no_input_delay"),
        ("no_output_delay", "no_output_delay"),
    ):
        match = re.search(rf"checking {check_name} \((\d+)\)", text)
        if match:
            metrics[key] = int(match.group(1))
    return metrics


def summarize_constraints(text: str) -> dict[str, Any]:
    counts: dict[str, int | None] = {}
    issues: list[str] = []
    for key in (
        "no_clock",
        "unconstrained_internal_endpoints",
        "no_input_delay",
        "no_output_delay",
    ):
        match = re.search(rf"checking {key} \((\d+)\)", text)
        counts[key] = int(match.group(1)) if match else None
        if counts[key]:
            issues.append(f"{key}={counts[key]}")
    no_constraints = "There are no user specified timing constraints." in text
    if no_constraints:
        issues.append("No user specified timing constraints")
    recognized = any(value is not None for value in counts.values()) or no_constraints
    return {
        "status": "ISSUES_REPORTED"
        if issues
        else "NO_ISSUES_REPORTED"
        if recognized
        else "NOT_CHECKED",
        **counts,
        "no_user_constraints_reported": no_constraints if text.strip() else None,
        "issues": issues,
    }


def summarize_tcl_artifacts(
    extract_dir: Path, info: TclJobInfo, *, require_success: bool
) -> dict[str, Any]:
    artifacts = extract_dir.resolve(strict=True) / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink() or _is_junction(artifacts):
        raise CliError(f"Native artifact directory is missing or unsafe: {artifacts}")
    inventory = {}
    for path in artifacts.rglob("*"):
        if (
            path.is_symlink()
            or _is_junction(path)
            or not path.resolve(strict=True).is_relative_to(artifacts)
        ):
            raise CliError(f"Native output escapes artifact directory or is linked: {path}")
        if path.is_file():
            inventory[path.relative_to(artifacts).as_posix()] = path
    if require_success:
        missing = [
            name
            for name in info.required_outputs
            if str(portable_relative_path(name)) not in inventory
        ]
        if missing:
            raise CliError(f"Tcl job did not produce required outputs: {missing}")
    texts = {
        kind: inventory[str(portable_relative_path(name))].read_text(
            encoding="utf-8", errors="replace"
        )
        for kind, name in info.reports.items()
        if str(portable_relative_path(name)) in inventory
    }
    summary = {
        "directory": str(artifacts),
        "file_count": len(inventory),
        "files": [
            {"name": name, "size_bytes": path.stat().st_size}
            for name, path in sorted(inventory.items())
        ],
        "build_target": "script",
        "stages": {},
        "quality_status": "NOT_EVALUATED",
        "constraints": summarize_constraints(
            texts.get("timing", "") + "\n" + texts.get("check_timing", "")
        ),
        "report_files": info.reports,
    }
    parsers = {
        "timing": parse_timing_summary,
        "drc": parse_rule_report,
        "methodology": parse_rule_report,
        "route": parse_route_status,
        "cdc": parse_cdc_report,
        "utilization": parse_utilization_report,
    }
    for kind, parser in parsers.items():
        if kind in texts:
            summary[kind] = parser(texts[kind])
    return summary


def parse_rule_report(text: str) -> dict[str, int | None]:
    checks_match = re.search(r"Checks found:\s*(\d+)", text)
    result: dict[str, int | None] = {
        "checks_found": int(checks_match.group(1)) if checks_match else None,
        "errors": 0,
        "critical_warnings": 0,
        "warnings": 0,
    }
    severity_patterns = {
        "errors": "Error",
        "critical_warnings": "Critical Warning",
        "warnings": "Warning",
    }
    for key, severity in severity_patterns.items():
        pattern = re.compile(
            rf"^\|\s*[^|]+\|\s*{re.escape(severity)}\s*\|[^|]*\|\s*(\d+)\s*\|$",
            re.MULTILINE,
        )
        result[key] = sum(int(match.group(1)) for match in pattern.finditer(text))
    return result


def parse_route_status(text: str) -> dict[str, int | None]:
    def match_value(pattern: str) -> int | None:
        match = re.search(pattern, text)
        return int(match.group(1)) if match else None

    return {
        "fully_routed_nets": match_value(r"# of fully routed nets\.+\s*:\s*(\d+)"),
        "routing_errors": match_value(r"# of nets with routing errors\.+\s*:\s*(\d+)"),
    }


def parse_cdc_report(text: str) -> dict[str, Any]:
    keys = ("endpoints", "safe", "unsafe", "unknown", "no_async_reg")
    totals = {"rows": 0, **dict.fromkeys(keys, 0)}
    severities = {"Critical": 0, "Warning": 0, "Info": 0}
    row_pattern = re.compile(
        r"^\s*(Critical|Warning|Info)\s+.+?\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$"
    )
    for line in text.splitlines():
        match = row_pattern.match(line)
        if not match:
            continue
        severity, *values = match.groups()
        severities[severity] += 1
        totals["rows"] += 1
        for key, value in zip(keys, values):
            totals[key] += int(value)
    checks = [
        {
            "id": match[1],
            "severity": match[2],
            "count": int(match[3]),
            "description": match[4].strip(),
        }
        for match in re.finditer(
            r"^\s*(CDC-\d+)\s+(Critical|Warning|Info)\s+(\d+)\s+(.+)$", text, re.MULTILINE
        )
    ]
    summary_known = bool(
        totals["rows"] or re.search(r"Endpoints\s+Safe\s+Unsafe\s+Unknown\s+No ASYNC_REG", text)
    )
    details_known = bool(checks or re.search(r"ID\s+Severity\s+Count\s+Description", text))
    return {
        **totals,
        **({} if summary_known else dict.fromkeys(keys)),
        "format": "summary" if summary_known else "details" if details_known else "unrecognized",
        "clock_pairs_by_severity": severities if summary_known else None,
        "checks": checks if details_known else None,
        "check_counts_by_severity": {
            severity: sum(check["count"] for check in checks if check["severity"] == severity)
            for severity in severities
        }
        if details_known
        else None,
    }


def report_metadata(text: str) -> dict[str, str | None]:
    def field(name: str) -> str | None:
        match = re.search(rf"^\|\s*{re.escape(name)}\s*:\s*(.*?)\s*$", text, re.MULTILINE)
        return match[1] if match else None

    command = field("Command")
    return {
        "command": command.split()[0] if command else None,
        "design": field("Design"),
        "design_state": field("Design State"),
        "tool_version": field("Tool Version"),
    }


def parse_utilization_report(text: str, instance: str | None = None) -> dict[str, Any]:
    columns = {
        "Total LUTs": "total_luts",
        "Logic LUTs": "logic_luts",
        "LUTRAMs": "lutram",
        "SRLs": "srl",
        "FFs": "ffs",
        "RAMB36": "ramb36",
        "RAMB18": "ramb18",
        "DSP Blocks": "dsp",
        "URAM": "uram",
    }
    header: list[str] = []
    stack: list[tuple[int, str]] = []
    rows = []
    for line in text.splitlines():
        if not line.startswith("|") or not line.rstrip().endswith("|"):
            continue
        raw = line.strip().split("|")[1:-1]
        cells = [cell.strip() for cell in raw]
        if cells[:2] == ["Instance", "Module"]:
            header = cells
            stack = []
            continue
        if not header or len(cells) != len(header) or cells[0].startswith("("):
            continue
        values = {
            columns[column]: value.replace(",", "")
            for column, value in zip(header, cells)
            if column in columns
        }
        if not values or not all(value.isdigit() for value in values.values()):
            continue
        indent = len(raw[0]) - len(raw[0].lstrip())
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = (stack[-1][1] + "/" if stack else "") + cells[0]
        stack.append((indent, path))
        rows.append(
            {
                "instance": path,
                "module": cells[1],
                "metrics": {key: int(value) for key, value in values.items()},
            }
        )
    selected = (
        rows[:1]
        if instance is None
        else [
            row
            for row in rows
            if row["instance"] == instance or row["instance"].endswith("/" + instance)
        ]
    )
    status = (
        "PARSED"
        if len(selected) == 1
        else "AMBIGUOUS"
        if selected
        else "NOT_FOUND"
        if rows
        else "UNRECOGNIZED"
    )
    return {
        "status": status,
        "design_state": report_metadata(text)["design_state"],
        "selection": "top" if instance is None else instance,
        "scope": selected[0] if len(selected) == 1 else None,
        "matches": [row["instance"] for row in selected],
    }


def summarize_artifacts(
    extract_dir: Path,
    *,
    require_success: bool,
    expected_target: str | None = None,
) -> dict[str, Any]:
    """Read reports and inventory; transfer integrity is checked by fetch_remote_build."""
    artifacts_dir = extract_dir.resolve(strict=True) / "artifacts"
    if not artifacts_dir.is_dir() or artifacts_dir.is_symlink():
        raise CliError(f"Extracted artifacts directory is missing or unsafe: {artifacts_dir}")
    artifact_paths = {}
    for path in artifacts_dir.iterdir():
        if path.is_symlink() or _is_junction(path) or not path.is_file():
            raise CliError(f"Artifact is not a regular file: {path}")
        if path.name != "SHA256SUMS":  # Legacy packages may contain this manifest.
            artifact_paths[path.name] = path

    def report_text(name: str) -> str:
        path = artifacts_dir / name
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    build_info = parse_key_values(report_text("build_info.txt"))
    target = build_info.get("build_target", "bitstream")
    if target not in {"synth", "route", "bitstream"}:
        raise CliError(f"Unknown artifact build target: {target}")
    if require_success and expected_target and target != expected_target:
        raise CliError(f"Delivered target {target} differs from requested {expected_target}")
    required_reports = {
        "build_info.txt",
        "utilization.rpt",
        "drc.rpt",
        "methodology.rpt",
    }
    required_reports.update(
        {"post_synth_timing.rpt"}
        if target == "synth"
        else {"timing_summary.rpt", "route_status.rpt", "post_route.dcp"}
    )
    if require_success:
        missing = sorted(required_reports - set(artifact_paths))
        if missing:
            raise CliError(f"Successful build package is missing reports: {missing}")
        if target == "bitstream" and not any(name.endswith(".bit") for name in artifact_paths):
            raise CliError("Successful build package contains no .bit file")
    timing_name = "post_synth_timing.rpt" if target == "synth" else "timing_summary.rpt"
    timing = parse_timing_summary(report_text(timing_name)) if timing_name in artifact_paths else {}
    route = (
        parse_route_status(report_text("route_status.rpt"))
        if "route_status.rpt" in artifact_paths
        else {}
    )
    drc = parse_rule_report(report_text("drc.rpt")) if "drc.rpt" in artifact_paths else {}
    methodology = (
        parse_rule_report(report_text("methodology.rpt"))
        if "methodology.rpt" in artifact_paths
        else {}
    )
    cdc = parse_cdc_report(report_text("cdc.rpt")) if "cdc.rpt" in artifact_paths else {}

    quality_status = "UNKNOWN"
    if target == "synth" and require_success:
        quality_status = "SYNTH_PASS" if not drc.get("errors") else "FAIL"
    elif timing and route:
        if timing.get("constraints_met") and route.get("routing_errors") == 0:
            warning_total = sum(
                int(value or 0)
                for value in (
                    drc.get("checks_found"),
                    methodology.get("checks_found"),
                    timing.get("no_input_delay"),
                    timing.get("no_output_delay"),
                )
            )
            quality_status = "PASS_WITH_WARNINGS" if warning_total else "PASS"
        else:
            quality_status = "FAIL"

    files = [
        {
            "name": name,
            "size_bytes": path.stat().st_size,
        }
        for name, path in sorted(artifact_paths.items())
    ]
    return {
        "directory": str(artifacts_dir),
        "file_count": len(files),
        "files": files,
        "build_info": build_info,
        "utilization": parse_utilization_report(report_text("utilization.rpt")),
        "build_target": target,
        "timing_stage": "post_synth_estimate" if target == "synth" else "post_route",
        "stages": {}
        if not require_success
        else {
            "synthesis": "PASS",
            "implementation": "PASS" if target != "synth" else "NOT_RUN",
            "bitstream": "PASS" if target == "bitstream" else "NOT_RUN",
        },
        "timing": timing,
        "constraints": summarize_constraints(report_text(timing_name)),
        "route": route,
        "drc": drc,
        "methodology": methodology,
        "cdc": cdc,
        "quality_status": quality_status,
    }


def inspect_saved_reports(result_dir: Path, instance: str | None = None) -> dict[str, Any]:
    result_dir = result_dir.expanduser().resolve(strict=True)
    artifacts = result_dir if result_dir.name == "artifacts" else result_dir / "artifacts"
    if not artifacts.is_dir() or artifacts.is_symlink() or _is_junction(artifacts):
        raise CliError(
            "Specify a published/extracted result directory containing artifacts, "
            "or the artifacts directory itself"
        )
    reports = {}
    skipped = []
    parsers = {
        "report_cdc": parse_cdc_report,
        "report_timing_summary": parse_timing_summary,
        "check_timing": summarize_constraints,
        "report_drc": parse_rule_report,
        "report_methodology": parse_rule_report,
        "report_route_status": parse_route_status,
    }
    for path in sorted(artifacts.rglob("*.rpt")):
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(artifacts):
            raise CliError(f"Report escapes artifact directory or is linked: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        metadata = report_metadata(text)
        command = metadata["command"]
        name = path.relative_to(artifacts).as_posix()
        if command == "report_utilization":
            parsed = parse_utilization_report(text, instance)
        elif command in parsers:
            parsed = parsers[command](text)
        else:
            skipped.append(name)
            continue
        reports[name] = {"metadata": metadata, "data": parsed}
        if command == "report_timing_summary":
            reports[name]["constraints"] = summarize_constraints(text)
    return {
        "schema_version": 1,
        "source_directory": str(artifacts),
        "instance": instance,
        "reports": reports,
        "unparsed_report_files": skipped,
    }


def command_report(args: argparse.Namespace, config: dict[str, Any]) -> int:
    if bool(args.handle) == bool(args.results):
        raise CliError("Use either a build handle or --results, not both")
    if args.handle:
        handle = parse_handle(args.handle)
        profile = get_profile(config, handle.host)
        if not profile.get("shared"):
            raise CliError(
                "Use --results with previously fetched results for a host without shared storage"
            )
        result_dir = shared_result_dir(profile, handle)
        complete = result_dir / "delivery.complete"
        if not complete.is_file() or complete.read_text(encoding="utf-8").strip() not in {
            "SUCCEEDED",
            "FAILED",
        }:
            raise CliError(f"Shared reports have not finished publishing: {result_dir}")
    else:
        result_dir = Path(args.results)
    result = inspect_saved_reports(result_dir, args.instance)
    result["handle"] = args.handle
    serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        destination = Path(args.output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(serialized)
        print(f"REPORT_JSON={destination}")
    else:
        print(serialized, end="")
    return 0


def command_list(args: argparse.Namespace, config: dict[str, Any]) -> int:
    profile = get_profile(config, args.host)
    workspace = None if args.all_workspaces else require_workspace(args.workspace)
    for record in get_remote_build_records(profile, args.host):
        if workspace is None or record.handle.workspace == workspace:
            print(f"{record.handle} state={record.state}")
    return 0


def command_workspace_clean(args: argparse.Namespace, config: dict[str, Any]) -> int:
    workspace = require_workspace(args.workspace)
    profile = get_profile(config, args.host)
    roots = workspace_roots(profile, workspace)
    for root in roots:
        print(f"WORKSPACE_DELETE_TARGET={root}")
    if not args.yes:
        print(
            "DRY_RUN: --yes permanently deletes this workspace's "
            "intermediates, results, staging and caches."
        )
        return 0
    lock_dir = str(PurePosixPath(profile["work_root"]) / ".locks")
    quoted_roots = " ".join(shlex.quote(root) for root in roots)
    script = f"""set -eu
mkdir -p {shlex.quote(lock_dir)}
exec 9>{shlex.quote(lock_dir + "/" + workspace + ".lock")}
if ! flock -xn 9; then
    echo 'Workspace is in use by a client, queued job or running build' >&2
    exit 21
fi
for root in {quoted_roots}; do
    [ -e "$root" ] || continue
    test ! -L "$root"
    test "$(realpath -e -- "$root")" = "$root"
    test ! -L "$root/.fpga-remote-workspace"
    test "$(cat "$root/.fpga-remote-workspace")" = fpga-remote-workspace-v1
    # Cover the launch handoff before the detached runner acquires its lease.
    for state_file in "$root"/*/*/logs/state; do
        [ -f "$state_file" ] || continue
        state=$(cat "$state_file")
        case "$state" in QUEUED|STARTING|RUNNING|PACKAGING) ;; *) continue ;; esac
        pid=$(cat "${{state_file%/state}}/runner.pid" 2>/dev/null || true)
        if [ "$state" = STARTING ] && [ -z "$pid" ]; then continue; fi
        case "$pid" in
            ''|*[!0-9]*) echo 'Unfinished submission has no valid runner PID; inspect it before cleanup' >&2; exit 22 ;;
        esac
        if kill -0 "$pid" 2>/dev/null; then echo 'Workspace has a live runner' >&2; exit 23; fi
    done
done
for root in {quoted_roots}; do
    [ -e "$root" ] || continue
    rm -rf -- "$root"
    test ! -e "$root"
    printf 'WORKSPACE_DELETED=%s\\n' "$root"
done
"""  # noqa: E501 - preserve native shell/report literal layout
    print(ssh_bash(profile, script, timeout=300), end="")
    return 0


def move_remote_build_to_trash(profile: dict[str, Any], handle: BuildHandle) -> str:
    work_root = profile["work_root"]
    root = remote_build_root(profile, handle)
    trash_relative = (
        str(PurePosixPath("workspaces") / handle.workspace / ".trash")
        if handle.workspace
        else ".trash"
    )
    script = f"""set -eu
work_root={shlex.quote(work_root)}
root={shlex.quote(root)}
work_root_real=$(realpath -e -- "$work_root")
root_real=$(realpath -e -- "$root")
test "$root_real" = "$work_root_real/{build_relative_path(handle)}"
test ! -L "$root"
test -f "$root_real/.fpga-remote-work"
test ! -L "$root_real/.fpga-remote-work"
test "$(cat "$root_real/.fpga-remote-work")" = fpga-remote-v1
state=$(cat "$root_real/logs/state" 2>/dev/null || echo UNKNOWN)
if [ "$state" = RUNNING ] || [ "$state" = PACKAGING ] || [ "$state" = QUEUED ] || [ "$state" = STARTING ]; then
    pid=$(cat "$root_real/logs/runner.pid" 2>/dev/null || true)
    case "$pid" in
        ''|*[!0-9]*) echo "Invalid or missing PID for active build: $root_real" >&2; exit 22 ;;
    esac
    if kill -0 "$pid" 2>/dev/null; then
        echo "Build is still running: $root_real" >&2
        exit 21
    fi
fi
mkdir -p "$work_root_real/{trash_relative}"
trash_real=$(realpath -e -- "$work_root_real/{trash_relative}")
test "$trash_real" = "$work_root_real/{trash_relative}"
destination="$trash_real/{handle.workspace or "legacy"}-{handle.project}-{handle.build_id}-$(date +%s)"
test ! -e "$destination"
mv -- "$root_real" "$destination"
printf 'MOVED_TO=%s\\n' "$destination"
"""  # noqa: E501 - preserve native shell/report literal layout
    return ssh_bash(profile, script)


def purge_remote_build(profile: dict[str, Any], handle: BuildHandle) -> str:
    work_root = profile["work_root"]
    root = remote_build_root(profile, handle)
    script = f"""set -eu
work_root={shlex.quote(work_root)}
root={shlex.quote(root)}
work_root_real=$(realpath -e -- "$work_root")
root_real=$(realpath -e -- "$root")
test "$root_real" = "$work_root_real/{build_relative_path(handle)}"
test ! -L "$root"
test -d "$root_real"
test -f "$root_real/.fpga-remote-work"
test ! -L "$root_real/.fpga-remote-work"
test "$(cat "$root_real/.fpga-remote-work")" = fpga-remote-v1
state=$(cat "$root_real/logs/state" 2>/dev/null || echo UNKNOWN)
case "$state" in
    SUCCEEDED|FAILED|PACKAGING_FAILED|STALE|UNKNOWN) ;;
    *) echo "Refusing to purge build in state $state: $root_real" >&2; exit 21 ;;
esac
pid=$(cat "$root_real/logs/runner.pid" 2>/dev/null || true)
case "$pid" in
    '') ;;
    *[!0-9]*) echo "Invalid runner PID in $root_real: $pid" >&2; exit 23 ;;
    *)
        if kill -0 "$pid" 2>/dev/null; then
            echo "Refusing to purge build with live PID $pid: $root_real" >&2
            exit 22
        fi
        ;;
esac
size=$(du -sh "$root_real" | awk '{{print $1}}')
rm -rf -- "$root_real"
test ! -e "$root_real"
printf 'PURGED=%s\\n' "$root_real"
printf 'PURGED_SIZE=%s\\n' "$size"
"""
    return ssh_bash(profile, script)


def command_clean(args: argparse.Namespace, config: dict[str, Any]) -> int:
    handle = parse_handle(args.handle)
    profile = get_profile(config, handle.host)
    root = remote_build_root(profile, handle)
    if not args.yes:
        print(f"DRY_RUN: move {root} to {profile['work_root']}/.trash")
        print("Re-run with --yes to perform the recoverable move.")
        return 0

    print(move_remote_build_to_trash(profile, handle), end="")
    return 0


def command_purge(args: argparse.Namespace, config: dict[str, Any]) -> int:
    handle = parse_handle(args.handle)
    profile = get_profile(config, handle.host)
    root = remote_build_root(profile, handle)
    if not args.yes:
        status = get_build_status(profile, handle)
        print(
            f"DRY_RUN: permanently delete {root} "
            f"state={status.get('STATE')} size={status.get('SIZE')}"
        )
        print("Re-run with --yes to perform the irreversible deletion.")
        return 0

    print(purge_remote_build(profile, handle), end="")
    return 0


def command_prune(args: argparse.Namespace, config: dict[str, Any]) -> int:
    profile = get_profile(config, args.host)
    workspace = require_workspace(args.workspace)
    retention = profile.get("retention", {})
    keep_succeeded = (
        args.keep_succeeded
        if args.keep_succeeded is not None
        else retention.get("keep_succeeded", 1)
    )
    keep_failed = (
        args.keep_failed if args.keep_failed is not None else retention.get("keep_failed", 3)
    )
    keep_other = args.keep_other if args.keep_other is not None else retention.get("keep_other", 1)
    candidates = select_prune_candidates(
        (
            record
            for record in get_remote_build_records(profile, args.host)
            if record.handle.workspace == workspace
        ),
        keep_succeeded=keep_succeeded,
        keep_failed=keep_failed,
        keep_other=keep_other,
    )
    action = "PURGE" if args.permanent else "MOVE_TO_TRASH"
    for record in candidates:
        print(f"CANDIDATE={record.handle} state={record.state} action={action}")
    print(
        f"PRUNE_CANDIDATES={len(candidates)} "
        f"KEEP_SUCCEEDED={keep_succeeded} KEEP_FAILED={keep_failed} "
        f"KEEP_OTHER={keep_other}"
    )
    if not args.yes:
        print("DRY_RUN: re-run with --yes to apply the listed actions.")
        return 0

    for record in candidates:
        if args.permanent:
            print(purge_remote_build(profile, record.handle), end="")
        else:
            print(move_remote_build_to_trash(profile, record.handle), end="")
    return 0


def write_run_summary(path: Path, summary: dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise CliError(f"Failed to write run summary {path}: {exc}") from exc


def command_run(args: argparse.Namespace, config: dict[str, Any]) -> int:
    profile = get_profile(config, args.host)
    if not args.output and not profile.get("shared"):
        raise CliError("--output is required for hosts without a shared folder")
    if getattr(args, "manifest", None):
        submission = submit_tcl_job(args, config)
    else:
        submission = submit_remote_build(
            config,
            host=args.host,
            project=Path(args.project),
            vivado_version=args.vivado_version,
            jobs=args.jobs,
            target=args.target,
            reference=args.reference,
            workspace=args.workspace,
        )
    handle = submission.handle
    profile = get_profile(config, args.host)
    print(f"RUN_HANDLE={handle}")
    print(submission.verify_output.strip())
    print(submission.launch_output.strip())

    wait_error = ""
    try:
        status = wait_for_build(
            profile,
            handle,
            poll_seconds=args.poll_seconds,
            timeout_seconds=args.timeout,
        )
    except CliError as exc:
        wait_error = str(exc)
        try:
            status = get_build_status(profile, handle)
        except CliError as status_exc:
            status = {"STATE": "UNKNOWN", "STATUS_ERROR": str(status_exc)}

    direct_shared = bool(profile.get("shared") and not args.output)
    run_output = (
        shared_result_dir(profile, handle)
        if direct_shared
        else Path(args.output).expanduser().resolve().joinpath(*build_relative_path(handle).parts)
    )
    try:
        if direct_shared:
            if not run_output.is_dir():
                raise CliError(f"Shared result directory is unavailable: {run_output}")
        else:
            run_output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise CliError(f"Cannot create run output directory {run_output}: {exc}") from exc

    fetch_result: FetchResult | None = None
    artifacts_summary: dict[str, Any] = {}
    delivery_error = ""
    try:
        if status.get("STATE") not in {"SUCCEEDED", "FAILED", "PACKAGING_FAILED"}:
            raise CliError("Build has not finished; results have not been collected")
        fetch_result = fetch_remote_build(
            profile,
            handle,
            output=run_output,
            extract=True,
        )
        if fetch_result.extract_dir is None:
            raise CliError("Run workflow requires extracted results")
        if isinstance(submission.info, TclJobInfo):
            artifacts_summary = summarize_tcl_artifacts(
                fetch_result.extract_dir,
                submission.info,
                require_success=status.get("STATE") == "SUCCEEDED",
            )
        else:
            artifacts_summary = summarize_artifacts(
                fetch_result.extract_dir,
                require_success=status.get("STATE") == "SUCCEEDED",
                expected_target=submission.options.target,
            )
    except (CliError, OSError, tarfile.TarError) as exc:
        delivery_error = str(exc)

    build_succeeded = status.get("STATE") == "SUCCEEDED"
    delivery_verified = bool(fetch_result and artifacts_summary and not delivery_error)
    quality_status = artifacts_summary.get("quality_status", "UNKNOWN")
    quality_acceptable = quality_status in {"PASS", "PASS_WITH_WARNINGS", "SYNTH_PASS"}
    if isinstance(submission.info, TclJobInfo):
        quality_acceptable = True  # Native execution reports no inferred design verdict.
    workflow_success = (
        build_succeeded and delivery_verified and quality_acceptable and not wait_error
    )

    cleanup = {
        "requested": args.remote_cleanup,
        "performed": False,
        "state": "KEEP",
        "detail": "",
    }
    if args.remote_cleanup != "keep":
        cleanup["state"] = "AUTHORIZED_PENDING" if workflow_success else "SKIPPED_UNSAFE"

    summary: dict[str, Any] = {
        "schema_version": 5,
        "flow": "tcl" if isinstance(submission.info, TclJobInfo) else "project",
        "handle": str(handle),
        "workspace": handle.workspace,
        "project": project_info_dict(submission.info),
        "submission": {
            "host": args.host,
            "jobs": submission.jobs,
            "probe": submission.probe,
            "target": submission.options.target,
            "reference_handle": submission.options.reference_handle,
        },
        "build": status,
        "local_delivery": {
            "output_directory": str(run_output),
            "storage": "shared" if direct_shared else "local",
            "verification": (
                "none"
                if fetch_result is None
                else "filesystem"
                if fetch_result.package is None
                else "package_sha256"
            ),
            "package": str(fetch_result.package) if fetch_result and fetch_result.package else "",
            "package_sha256": (fetch_result.package_sha256 if fetch_result else ""),
            "extracted_directory": (
                str(fetch_result.extract_dir) if fetch_result and fetch_result.extract_dir else ""
            ),
            "verified": delivery_verified,
        },
        "artifacts": artifacts_summary,
        "quality_status": quality_status,
        "remote_cleanup": cleanup,
        "errors": {
            "wait": wait_error,
            "delivery": delivery_error,
        },
    }
    summary_path = run_output / "remote-build-summary.json"
    write_run_summary(summary_path, summary)

    cleanup_error = ""
    if workflow_success and args.remote_cleanup != "keep":
        try:
            if args.remote_cleanup == "purge":
                cleanup_output = purge_remote_build(profile, handle)
            else:
                cleanup_output = move_remote_build_to_trash(profile, handle)
            cleanup.update(
                performed=True,
                state="PURGED" if args.remote_cleanup == "purge" else "MOVED_TO_TRASH",
                detail=cleanup_output.strip(),
            )
        except CliError as exc:
            cleanup_error = str(exc)
            cleanup.update(state="FAILED", detail=cleanup_error)
        summary["remote_cleanup"] = cleanup
        summary["errors"]["cleanup"] = cleanup_error
        write_run_summary(summary_path, summary)

    if cleanup_error:
        workflow_success = False
    print(f"RUN_STATE={status.get('STATE', 'UNKNOWN')}")
    print(f"RUN_TARGET={submission.options.target}")
    print(f"RUN_QUALITY={quality_status}")
    constraints = artifacts_summary.get("constraints", {})
    print(f"RUN_CONSTRAINTS={constraints.get('status', 'NOT_CHECKED')}")
    for issue in constraints.get("issues", []):
        print(f"CONSTRAINT_WARNING={issue}")
    cdc = artifacts_summary.get("cdc", {})
    if cdc.get("unsafe"):
        print(f"CDC_WARNING=unsafe_endpoints={cdc['unsafe']}")
    critical = (cdc.get("check_counts_by_severity") or {}).get("Critical")
    if critical:
        print(f"CDC_WARNING=critical_checks={critical}")
    print(f"RUN_OUTPUT={run_output}")
    print(f"RUN_SUMMARY={summary_path}")
    print(f"RUN_REMOTE_CLEANUP={cleanup['state']}")
    print(f"RUN_RESULT={'SUCCESS' if workflow_success else 'FAILED'}")
    return 0 if workflow_success else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vivado-mcp remote")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(
            os.environ.get("OTTER_VIVADO_REMOTE_CONFIG", str(DEFAULT_CONFIG))
        ).expanduser(),
        help="Private host config; default config/remote-hosts.local.json in the source checkout, "
        "or OTTER_VIVADO_REMOTE_CONFIG. Not needed for inspect or report --results.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    hosts = subparsers.add_parser("hosts", help="List configured host profiles")
    hosts.set_defaults(handler=lambda args, config: print("\n".join(sorted(config["hosts"]))) or 0)

    probe = subparsers.add_parser("probe", help="Probe a remote host profile")
    probe.add_argument("--host", required=True)
    probe.add_argument("--vivado-version")
    probe.add_argument("--json", action="store_true")
    probe.set_defaults(handler=command_probe)

    inspect = subparsers.add_parser("inspect", help="Inspect local Vivado project portability")
    inspect.add_argument("--project", required=True)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(handler=command_inspect)

    run = subparsers.add_parser(
        "run", help="Submit, wait, fetch, verify, summarize, and optionally clean"
    )
    run.add_argument("--host", required=True)
    run.add_argument(
        "--workspace",
        default=os.environ.get("CODEX_THREAD_ID"),
        help="Isolation namespace; defaults to current Codex task ID",
    )
    run.add_argument("--project", required=True)
    run.add_argument(
        "--output", help="Optional local archive directory; defaults to shared results"
    )
    run.add_argument("--vivado-version")
    run.add_argument("--jobs", type=int)
    run.add_argument("--target", choices=("synth", "route", "bitstream"), default="bitstream")
    run.add_argument(
        "--reference", help="Prior handle supplying post-route DCP for incremental implementation"
    )
    run.add_argument("--poll-seconds", type=int, default=10)
    run.add_argument("--timeout", type=int, default=14400)
    run.add_argument(
        "--remote-cleanup",
        choices=("keep", "trash", "purge"),
        default="keep",
        help="Clean build intermediates only after successful delivery and summary",
    )
    run.set_defaults(handler=command_run)

    native = subparsers.add_parser(
        "run-tcl",
        help="Run a declared Tcl input bundle without creating or converting a Vivado project",
    )
    native.add_argument("--host", required=True)
    native.add_argument("--workspace", default=os.environ.get("CODEX_THREAD_ID"))
    native.add_argument("--manifest", required=True, type=Path)
    native.add_argument("--output")
    native.add_argument("--jobs", type=int)
    native.add_argument("--poll-seconds", type=int, default=10)
    native.add_argument("--timeout", type=int, default=14400)
    native.add_argument("--remote-cleanup", choices=("keep", "trash", "purge"), default="keep")
    native.set_defaults(handler=command_run)

    report = subparsers.add_parser(
        "report", help="Read existing reports only; no build, hashes, transfer or cleanup"
    )
    report.add_argument("handle", nargs="?")
    report.add_argument("--results", type=Path)
    report.add_argument(
        "--instance",
        help="Exact hierarchy path or unique suffix for utilization; defaults to design top",
    )
    report.add_argument(
        "--output", type=Path, help="Save to a new JSON file; existing files are never overwritten"
    )
    report.set_defaults(handler=command_report)

    build = subparsers.add_parser("build", help="Submit a remote Vivado build")
    build.add_argument("--host", required=True)
    build.add_argument("--workspace", default=os.environ.get("CODEX_THREAD_ID"))
    build.add_argument("--project", required=True)
    build.add_argument("--vivado-version")
    build.add_argument("--jobs", type=int)
    build.add_argument("--target", choices=("synth", "route", "bitstream"), default="bitstream")
    build.add_argument(
        "--reference", help="Prior handle supplying post-route DCP for incremental implementation"
    )
    build.add_argument("--wait", action="store_true")
    build.add_argument("--poll-seconds", type=int, default=5)
    build.add_argument("--timeout", type=int, default=7200)
    build.set_defaults(handler=command_build)

    status = subparsers.add_parser("status", help="Show build status")
    status.add_argument("handle")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=command_status)

    logs = subparsers.add_parser("logs", help="Show or follow Vivado driver log")
    logs.add_argument("handle")
    logs.add_argument("--tail", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    logs.set_defaults(handler=command_logs)

    fetch = subparsers.add_parser("fetch", help="Fetch and verify build package")
    fetch.add_argument("handle")
    fetch.add_argument("--output", required=True)
    fetch.add_argument("--extract", action="store_true")
    fetch.set_defaults(handler=command_fetch)

    list_parser = subparsers.add_parser("list", help="List remote builds")
    list_parser.add_argument("--host", required=True)
    list_parser.add_argument("--workspace", default=os.environ.get("CODEX_THREAD_ID"))
    list_parser.add_argument("--all-workspaces", action="store_true")
    list_parser.set_defaults(handler=command_list)

    clean = subparsers.add_parser("clean", help="Move a build into remote trash")
    clean.add_argument("handle")
    clean.add_argument("--yes", action="store_true")
    clean.set_defaults(handler=command_clean)

    purge = subparsers.add_parser("purge", help="Permanently delete one remote build")
    purge.add_argument("handle")
    purge.add_argument("--yes", action="store_true")
    purge.set_defaults(handler=command_purge)

    prune = subparsers.add_parser("prune", help="Apply per-project retention rules")
    prune.add_argument("--host", required=True)
    prune.add_argument("--workspace", default=os.environ.get("CODEX_THREAD_ID"))
    prune.add_argument("--keep-succeeded", type=int)
    prune.add_argument("--keep-failed", type=int)
    prune.add_argument("--keep-other", type=int)
    prune.add_argument("--permanent", action="store_true")
    prune.add_argument("--yes", action="store_true")
    prune.set_defaults(handler=command_prune)

    workspace_clean = subparsers.add_parser(
        "workspace-clean",
        help="Delete one idle workspace, including results and caches; previews by default",
    )
    workspace_clean.add_argument("--host", required=True)
    workspace_clean.add_argument("--workspace", default=os.environ.get("CODEX_THREAD_ID"))
    workspace_clean.add_argument("--yes", action="store_true")
    workspace_clean.set_defaults(handler=command_workspace_clean)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        offline = args.command == "inspect" or (args.command == "report" and args.results)
        config = {} if offline else load_config(args.config.resolve())
        workspace = ""
        if args.command in {"run", "run-tcl", "build", "prune"}:
            workspace = require_workspace(args.workspace)
            profile = get_profile(config, args.host)
        elif args.command in {"fetch", "purge", "clean"}:
            handle = parse_handle(args.handle)
            workspace = handle.workspace
            profile = get_profile(config, handle.host)
        if workspace:
            with workspace_lease(
                profile, workspace, initialize=args.command in {"run", "run-tcl", "build"}
            ):
                result = args.handler(args, config)
        else:
            result = args.handler(args, config)
        return int(result or 0)
    except (CliError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
