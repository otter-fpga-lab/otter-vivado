#!/usr/bin/env bash
set -eu

tool_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
test_root=$(mktemp -d "${TMPDIR:-/tmp}/fpga-remote-runner-smoke.XXXXXX")
case "$test_root" in
    "${TMPDIR:-/tmp}"/fpga-remote-runner-smoke.*) ;;
    *) echo "Unsafe temporary directory: $test_root" >&2; exit 2 ;;
esac
cleanup() {
    rm -rf -- "$test_root"
}
trap cleanup EXIT

build_root="$test_root/build"
fake_root="$test_root/fake-vivado"
mkdir -p "$build_root/source" "$build_root/work" "$build_root/artifacts" "$build_root/logs" "$fake_root/bin"
install -m 755 "$tool_root/tests/remote_build/fixtures/fake_vivado.sh" "$fake_root/bin/vivado"
cp "$tool_root/src/vivado_mcp/remote_build/assets/vivado_project_build.tcl" "$build_root/source/vivado_project_build.tcl"
touch "$build_root/work/demo.xpr"

bash "$tool_root/src/vivado_mcp/remote_build/assets/run_vivado_build.sh" \
    "$build_root" \
    "$fake_root" \
    "$build_root/work/demo.xpr" \
    1 \
    fake-part \
    fake-top \
    0000000000000000000000000000000000000000000000000000000000000000 \
    demo \
    20260822T000000

test "$(cat "$build_root/logs/state")" = SUCCEEDED
test "$(cat "$build_root/logs/build.result")" = SUCCEEDED
test "$(cat "$build_root/logs/build.exit")" = 0
test -f "$build_root/demo-20260822T000000-remote-build.tar.gz"
test -f "$build_root/demo-20260822T000000-remote-build.tar.gz.sha256"
(
    cd "$build_root"
    sha256sum -c demo-20260822T000000-remote-build.tar.gz.sha256
)
tar -tzf "$build_root/demo-20260822T000000-remote-build.tar.gz" | grep -q '^artifacts/fake.bit$'
printf 'RUNNER_SMOKE=PASS\n'

build_root="$test_root/shared-build"
mkdir -p "$build_root/source" "$build_root/work" "$build_root/artifacts" "$build_root/logs"
cp "$tool_root/src/vivado_mcp/remote_build/assets/vivado_project_build.tcl" "$build_root/source/vivado_project_build.tcl"
touch "$build_root/work/demo.xpr"
run_shared() {
    bash "$tool_root/src/vivado_mcp/remote_build/assets/run_vivado_build.sh" \
        "$build_root" "$fake_root" "$build_root/work/demo.xpr" 1 fake-part fake-top \
        0000000000000000000000000000000000000000000000000000000000000000 \
        demo 20260822T000001
}
export FPGA_REMOTE_RESULT_ROOT="$test_root/results"
mkdir "$FPGA_REMOTE_RESULT_ROOT"
run_shared
test "$(cat "$build_root/logs/state")" = SUCCEEDED
test "$(cat "$FPGA_REMOTE_RESULT_ROOT/delivery.complete")" = SUCCEEDED
test -f "$FPGA_REMOTE_RESULT_ROOT/artifacts/fake.bit"
test ! -e "$build_root/demo-20260822T000001-remote-build.tar.gz"

export FPGA_REMOTE_RESULT_ROOT="$test_root/broken-results"
mkdir "$FPGA_REMOTE_RESULT_ROOT"
touch "$FPGA_REMOTE_RESULT_ROOT/artifacts"
set +e
run_shared
shared_rc=$?
set -e
test "$shared_rc" = 3
test "$(cat "$build_root/logs/state")" = PACKAGING_FAILED
test ! -e "$FPGA_REMOTE_RESULT_ROOT/delivery.complete"
test -f "$build_root/artifacts/fake.bit"
printf 'SHARED_RUNNER_SMOKE=PASS\n'
