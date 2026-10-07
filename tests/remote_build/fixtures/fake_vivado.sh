#!/usr/bin/env bash
set -eu

if [[ $# -lt 7 || $1 != -mode || $3 != -source || $5 != -tclargs ]]; then
    echo "Unexpected fake Vivado arguments" >&2
    exit 2
fi

artifact_dir=$7
mkdir -p "$artifact_dir"
printf 'fake-bitstream\n' > "$artifact_dir/fake.bit"
printf 'REMOTE_BUILD_COMPLETE=1\n'
exit 0
