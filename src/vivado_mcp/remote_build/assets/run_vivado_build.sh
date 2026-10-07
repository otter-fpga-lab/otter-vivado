#!/usr/bin/env bash
set -u

if [[ $# -ne 9 ]]; then
    echo "Usage: run_vivado_build.sh <build_root> <vivado_root> <xpr> <jobs> <part> <top> <source_hash> <project_name> <build_id>" >&2
    exit 2
fi

build_root=$1
vivado_root=$2
xpr=$3
jobs=$4
part=$5
top=$6
source_hash=$7
project_name=$8
build_id=$9

logs_dir="$build_root/logs"
artifacts_dir="$build_root/artifacts"
driver_log="$logs_dir/vivado-driver.log"
state_file="$logs_dir/state"
exit_file="$logs_dir/build.exit"
package="$build_root/${project_name}-${build_id}-remote-build.tar.gz"

mkdir -p "$logs_dir" "$artifacts_dir"
printf 'RUNNING\n' > "$state_file"
date --iso-8601=seconds > "$logs_dir/started_at"

cd "$build_root/work" || exit 2
export FPGA_REMOTE_WORK_DIR="$build_root/work"
export FPGA_REMOTE_ARTIFACT_DIR="$artifacts_dir"
export FPGA_REMOTE_LOG_DIR="$logs_dir"
export FPGA_REMOTE_JOBS="$jobs"
if [[ ${FPGA_REMOTE_FLOW:-project} == tcl ]]; then
    native_args=()
    mapfile -d '' -t native_args < "$build_root/source/native-args.bin"
    vivado_command=("$vivado_root/bin/vivado" -mode batch -nolog -nojournal -notrace -source "$xpr")
    if [[ ${#native_args[@]} -gt 0 ]]; then
        vivado_command+=(-tclargs "${native_args[@]}")
    fi
    provenance="$build_root/source/native-job.json"
else
    vivado_command=("$vivado_root/bin/vivado" -mode batch
        -source "$build_root/source/vivado_project_build.tcl"
        -tclargs "$xpr" "$artifacts_dir" "$jobs" "$part" "$top" "$source_hash")
    provenance="$build_root/source/vivado_project_build.tcl"
fi
set +e
if [[ -x /usr/bin/time ]]; then
    /usr/bin/time -v "${vivado_command[@]}" \
        > "$driver_log" 2>&1
else
    "${vivado_command[@]}" \
        > "$driver_log" 2>&1
fi
vivado_rc=$?
set -e

printf '%s\n' "$vivado_rc" > "$exit_file"
if [[ $vivado_rc -eq 0 ]]; then
    build_result=SUCCEEDED
else
    build_result=FAILED
fi
printf '%s\n' "$build_result" > "$logs_dir/build.result"
date --iso-8601=seconds > "$logs_dir/vivado_finished_at"
printf 'PACKAGING\n' > "$state_file"

if [[ -n ${FPGA_REMOTE_RESULT_ROOT:-} ]]; then
    result_root=$FPGA_REMOTE_RESULT_ROOT
    if cp -a -- "$artifacts_dir" "$logs_dir" "$result_root/" &&
        cp -- "$provenance" "$result_root/" &&
        printf '%s\n' "$build_result" > "$result_root/logs/state" &&
        date --iso-8601=seconds > "$result_root/logs/finished_at" &&
        printf '%s\n' "$build_result" > "$result_root/delivery.complete"; then
        printf '%s\n' "$build_result" > "$state_file"
        date --iso-8601=seconds > "$logs_dir/finished_at"
        exit "$vivado_rc"
    else
        printf 'PACKAGING_FAILED\n' > "$state_file"
        date --iso-8601=seconds > "$logs_dir/finished_at"
        exit 3
    fi
fi

project_base=$(basename "$xpr" .xpr)
package_items=(artifacts logs "${provenance#"$build_root/"}")
if [[ -f "$build_root/work/${project_base}.runs/synth_1/runme.log" ]]; then
    package_items+=("work/${project_base}.runs/synth_1/runme.log")
fi
if [[ -f "$build_root/work/${project_base}.runs/impl_1/runme.log" ]]; then
    package_items+=("work/${project_base}.runs/impl_1/runme.log")
fi

if tar -czf "$package" -C "$build_root" "${package_items[@]}"; then
    (
        cd "$build_root"
        sha256sum "$(basename "$package")" > "$(basename "$package").sha256"
    )
else
    printf 'PACKAGING_FAILED\n' > "$state_file"
    date --iso-8601=seconds > "$logs_dir/finished_at"
    exit 3
fi

printf '%s\n' "$build_result" > "$state_file"
date --iso-8601=seconds > "$logs_dir/finished_at"
exit "$vivado_rc"
