if {$argc != 6} {
    puts stderr "Usage: vivado -mode batch -source vivado_project_build.tcl -tclargs <project.xpr> <artifact_dir> <jobs> <expected_part> <expected_top> <source_hash>"
    exit 2
}

set project_file  [file normalize [lindex $argv 0]]
set artifact_dir  [file normalize [lindex $argv 1]]
set jobs          [lindex $argv 2]
set expected_part [lindex $argv 3]
set expected_top  [lindex $argv 4]
set source_hash   [lindex $argv 5]
set start_time    [clock seconds]
set build_target bitstream
set reference_checkpoint ""
set ip_cache ""
if {[info exists ::env(FPGA_REMOTE_TARGET)]} {set build_target $::env(FPGA_REMOTE_TARGET)}
if {[info exists ::env(FPGA_REMOTE_REFERENCE)]} {set reference_checkpoint $::env(FPGA_REMOTE_REFERENCE)}
if {[info exists ::env(FPGA_REMOTE_IP_CACHE)]} {set ip_cache $::env(FPGA_REMOTE_IP_CACHE)}
if {$build_target ni {synth route bitstream}} {
    puts stderr "Unknown build target: $build_target"
    exit 2
}

if {![file isfile $project_file]} {
    puts stderr "Project file not found: $project_file"
    exit 2
}
if {![string is integer -strict $jobs] || $jobs < 1} {
    puts stderr "Invalid jobs value: $jobs"
    exit 2
}

file mkdir $artifact_dir

if {[catch {
    open_project $project_file

    set actual_part [get_property PART [current_project]]
    set actual_top  [get_property TOP [get_filesets sources_1]]
    puts "REMOTE_BUILD_PART=$actual_part"
    puts "REMOTE_BUILD_TOP=$actual_top"

    if {$actual_part ne $expected_part} {
        error "Unexpected part: expected $expected_part, got $actual_part"
    }
    if {$actual_top ne $expected_top} {
        error "Unexpected top: expected $expected_top, got $actual_top"
    }
    if {$ip_cache ne ""} {
        file mkdir $ip_cache
        config_ip_cache -use_cache_location $ip_cache
        puts "REMOTE_BUILD_IP_CACHE=$ip_cache"
    }

    set synth_run [get_runs synth_1]
    set impl_run  [get_runs impl_1]
    if {[llength $synth_run] != 1 || [llength $impl_run] != 1} {
        error "Expected exactly one synth_1 and one impl_1 run"
    }

    foreach run_object [list $synth_run $impl_run] {
        set run_properties [list_property $run_object]
        if {[lsearch -exact $run_properties AUTO_INCREMENTAL_CHECKPOINT] >= 0} {
            set_property AUTO_INCREMENTAL_CHECKPOINT 0 $run_object
        }
        if {[lsearch -exact $run_properties INCREMENTAL_CHECKPOINT] >= 0} {
            set_property INCREMENTAL_CHECKPOINT {} $run_object
        }
    }
    if {$reference_checkpoint ne ""} {
        if {$build_target eq "synth"} {error "Incremental reference requires implementation"}
        add_files -fileset utils_1 -norecurse $reference_checkpoint
        set_property INCREMENTAL_CHECKPOINT $reference_checkpoint $impl_run
        puts "REMOTE_BUILD_REFERENCE=$reference_checkpoint"
    }

    set bd_files [get_files -quiet *.bd]
    foreach bd_file $bd_files {
        puts "REMOTE_BUILD_GENERATE_BD=$bd_file"
        generate_target all $bd_file
        set wrapper_files [make_wrapper -files [list $bd_file] -top -import]
        puts "REMOTE_BUILD_BD_WRAPPER=[join $wrapper_files ,]"
    }
    update_compile_order -fileset sources_1

    reset_run $synth_run
    launch_runs $synth_run -jobs $jobs
    wait_on_run $synth_run

    set synth_status   [get_property STATUS $synth_run]
    set synth_progress [get_property PROGRESS $synth_run]
    puts "REMOTE_BUILD_SYNTH_STATUS=$synth_status"
    puts "REMOTE_BUILD_SYNTH_PROGRESS=$synth_progress"
    if {$synth_progress ne "100%"} {
        error "Synthesis did not complete: $synth_status ($synth_progress)"
    }

    set impl_status NOT_RUN
    if {$build_target ne "synth"} {
        set last_step [expr {$build_target eq "route" ? "route_design" : "write_bitstream"}]
        launch_runs $impl_run -to_step $last_step -jobs $jobs
        wait_on_run $impl_run

        set impl_status   [get_property STATUS $impl_run]
        set impl_progress [get_property PROGRESS $impl_run]
        puts "REMOTE_BUILD_IMPL_STATUS=$impl_status"
        puts "REMOTE_BUILD_IMPL_PROGRESS=$impl_progress"
        if {$impl_progress ne "100%"} {
            error "Implementation did not complete: $impl_status ($impl_progress)"
        }
        open_run $impl_run
        set timing_report timing_summary.rpt
    } else {
        open_run $synth_run
        set timing_report post_synth_timing.rpt
    }

    report_timing_summary -delay_type min_max -max_paths 20 -report_unconstrained \
        -file [file join $artifact_dir $timing_report]
    report_utilization -hierarchical -file [file join $artifact_dir utilization.rpt]
    report_drc -file [file join $artifact_dir drc.rpt]
    report_methodology -file [file join $artifact_dir methodology.rpt]
    report_clock_interaction -file [file join $artifact_dir clock_interaction.rpt]
    catch {report_cdc -file [file join $artifact_dir cdc.rpt]} cdc_message
    if {$build_target ne "synth"} {
        report_route_status -file [file join $artifact_dir route_status.rpt]
        write_checkpoint -force [file join $artifact_dir post_route.dcp]
        if {$reference_checkpoint ne ""} {
            report_incremental_reuse -file [file join $artifact_dir incremental_reuse.rpt]
        }
    }

    if {$build_target eq "bitstream"} {
        set impl_dir [get_property DIRECTORY $impl_run]
        set bit_files [glob -nocomplain -directory $impl_dir *.bit]
        if {[llength $bit_files] == 0} {
            error "No bitstream found in $impl_dir"
        }
        foreach source_file $bit_files {
            file copy -force $source_file [file join $artifact_dir [file tail $source_file]]
        }
        foreach source_file [glob -nocomplain -directory $impl_dir *.ltx] {
            file copy -force $source_file [file join $artifact_dir [file tail $source_file]]
        }

        if {[llength $bd_files] > 0} {
            if {[catch {
                write_hw_platform -fixed -include_bit -force \
                    [file join $artifact_dir hardware_platform.xsa]
            } xsa_message]} {
                puts "REMOTE_BUILD_XSA_WARNING=$xsa_message"
            }
        } else {
            puts "REMOTE_BUILD_XSA_SKIPPED=no_block_design"
        }
    }

    set info_file [open [file join $artifact_dir build_info.txt] w]
    puts $info_file "vivado_version=[version -short]"
    puts $info_file "project=$project_file"
    puts $info_file "part=$actual_part"
    puts $info_file "top=$actual_top"
    puts $info_file "jobs=$jobs"
    puts $info_file "build_target=$build_target"
    puts $info_file "synthesis=PASS"
    puts $info_file "implementation=[expr {$build_target ne "synth" ? "PASS" : "NOT_RUN"}]"
    puts $info_file "bitstream=[expr {$build_target eq "bitstream" ? "PASS" : "NOT_RUN"}]"
    puts $info_file "reference_checkpoint=$reference_checkpoint"
    puts $info_file "ip_cache=$ip_cache"
    puts $info_file "synth_status=$synth_status"
    puts $info_file "impl_status=$impl_status"
    puts $info_file "elapsed_seconds=[expr {[clock seconds] - $start_time}]"
    puts $info_file "source_tree_sha256=$source_hash"
    close $info_file

    close_project
} build_error build_options]} {
    set failure_file [open [file join $artifact_dir build_failure.txt] w]
    puts $failure_file $build_error
    if {[dict exists $build_options -errorinfo]} {
        puts $failure_file [dict get $build_options -errorinfo]
    }
    close $failure_file
    puts stderr "REMOTE_BUILD_FAILED=$build_error"
    exit 1
}

puts "REMOTE_BUILD_COMPLETE=1"
exit 0
