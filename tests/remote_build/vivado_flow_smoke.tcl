# 用 Tcl 命令替身执行真实 driver，检查阶段控制；不替代 Vivado 综合/时序证据。
if {$argc != 3} {error "Usage: tclsh vivado_flow_smoke.tcl <driver> <case> <temp_dir>"}
set driver [file normalize [lindex $argv 0]]
set test_case [lindex $argv 1]
set test_root [file normalize [lindex $argv 2]]
file mkdir $test_root
set impl_dir [file join $test_root impl]
file mkdir $impl_dir
set artifact_dir [file join $test_root artifacts]
set project_file [file join $test_root demo.xpr]
close [open $project_file w]
set expected_target [expr {$test_case in {reference synth_failure} ? ($test_case eq "reference" ? "bitstream" : "synth") : $test_case}]
set ::env(FPGA_REMOTE_TARGET) $expected_target
set ::env(FPGA_REMOTE_IP_CACHE) [file join $test_root ip-cache]
unset -nocomplain ::env(FPGA_REMOTE_REFERENCE)
if {$test_case eq "reference"} {
    set ::env(FPGA_REMOTE_REFERENCE) [file join $test_root reference.dcp]
    close [open $::env(FPGA_REMOTE_REFERENCE) w]
}
set launches {}
set reference_applied 0
set cache_applied 0
proc assert {condition message} {if {![uplevel 1 [list expr $condition]]} {error $message}}
proc open_project {path} {}
proc close_project {} {}
proc current_project {} {return project}
proc version {args} {return 2024.2}
proc get_filesets {name} {return $name}
proc get_runs {name} {return $name}
proc get_property {property object} {
    switch -- $property {
        PART {return xczu7ev-ffvc1156-2-i}
        TOP {return top}
        STATUS {return "$object Complete!"}
        PROGRESS {return [expr {$::test_case eq "synth_failure" ? "50%" : "100%"}]}
        DIRECTORY {return $::impl_dir}
        default {error "Unexpected property: $property"}
    }
}
proc list_property {object} {return {AUTO_INCREMENTAL_CHECKPOINT INCREMENTAL_CHECKPOINT}}
proc set_property {property value object} {
    if {$property eq "INCREMENTAL_CHECKPOINT" && $value ne ""} {
        assert {$object eq "impl_1"} "Reference applied to wrong run"
        assert {$value eq $::env(FPGA_REMOTE_REFERENCE)} "Reference path changed"
        set ::reference_applied 1
    }
}
proc config_ip_cache {option path} {
    assert {$option eq "-use_cache_location"} "Unexpected cache option"
    assert {[file isdirectory $path]} "Cache directory not created"
    set ::cache_applied 1
}
proc add_files {args} {}
proc get_files {args} {return {}}
proc update_compile_order {args} {}
proc reset_run {run} {}
proc launch_runs {run args} {
    lappend ::launches [linsert $args 0 $run]
    if {$run eq "impl_1" && [lindex $args 1] eq "write_bitstream"} {
        puts [set f [open [file join $::impl_dir top.bit] w]] "fake bitstream"
        close $f
    }
}
proc wait_on_run {run} {}
proc open_run {run} {}
proc write_report {args} {
    set index [lsearch -exact $args -file]
    assert {$index >= 0} "Missing report file"
    set f [open [lindex $args [expr {$index + 1}]] w]
    puts $f "fixture report"
    close $f
}
foreach command {report_timing_summary report_utilization report_drc report_methodology report_clock_interaction report_cdc report_route_status report_incremental_reuse} {
    interp alias {} $command {} write_report
}
proc write_checkpoint {args} {close [open [lindex $args end] w]}
rename exit real_exit
proc exit {code} {return -code error "DRIVER_EXIT=$code"}
set argv [list $project_file $artifact_dir 1 xczu7ev-ffvc1156-2-i top fixture-source]
set argc [llength $argv]
catch {source $driver} outcome
if {$test_case eq "synth_failure"} {
    assert {$outcome eq "DRIVER_EXIT=1"} "Failed synthesis was accepted: $outcome"
    assert {[llength $launches] == 1} "Implementation launched after synthesis failure"
    assert {[file isfile [file join $artifact_dir build_failure.txt]]} "Failure report missing"
} else {
    assert {$outcome eq "DRIVER_EXIT=0"} "Driver failed: $outcome"
    assert {$cache_applied} "IP cache was not configured"
    assert {$reference_applied == ($test_case eq "reference")} "Incremental reference routing mismatch"
    set f [open [file join $artifact_dir build_info.txt] r]
    set metadata [read $f]
    close $f
    assert {[string first "build_target=$expected_target" $metadata] >= 0} "Wrong target metadata"
    if {$expected_target eq "synth"} {
        assert {[llength $launches] == 1} "Synthesis target launched implementation"
        assert {![file exists [file join $artifact_dir post_route.dcp]]} "Synthesis claimed routed checkpoint"
        assert {[file exists [file join $artifact_dir post_synth_timing.rpt]]} "Missing synthesis estimate"
        assert {[string first "implementation=NOT_RUN" $metadata] >= 0} "Synthesis claimed implementation"
    } else {
        set expected_step [expr {$expected_target eq "route" ? "route_design" : "write_bitstream"}]
        assert {[lindex $launches 1] eq [list impl_1 -to_step $expected_step -jobs 1]} "Wrong implementation stop step"
        assert {[file exists [file join $artifact_dir post_route.dcp]]} "Missing routed checkpoint"
    }
    assert {[file exists [file join $artifact_dir top.bit]] == ($expected_target eq "bitstream")} "Wrong bitstream delivery"
    assert {[file exists [file join $artifact_dir incremental_reuse.rpt]] == ($test_case eq "reference")} "Wrong reuse report delivery"
}
puts "VIVADO_FLOW_SMOKE=$test_case PASS"
real_exit 0
