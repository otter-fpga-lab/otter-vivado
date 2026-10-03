# Explicitly set __otter_demo_dir (new consumer directory) and __otter_demo_part
# (an installed, licensed device part), then source this file in an idle session.
# This script creates a project only. It does not launch runs or program a device.
if {![info exists __otter_demo_dir] || ![info exists __otter_demo_part]} {
    error "Set __otter_demo_dir and __otter_demo_part before sourcing create_demo.tcl"
}
if {[llength [get_projects -quiet]] > 0} {
    error "Use an empty Vivado session; an existing project will not be closed"
}
if {[file exists $__otter_demo_dir]} {
    error "Output already exists; choose a new consumer directory"
}
if {[llength [get_parts -quiet $__otter_demo_part]] != 1} {
    error "Select one exact installed Vivado part"
}
set __otter_demo_source [file dirname [file normalize [info script]]]
create_project otter_progress_demo $__otter_demo_dir -part $__otter_demo_part
add_files [file join $__otter_demo_source demo.v]
add_files -fileset constrs_1 [file join $__otter_demo_source demo.xdc]
set_property top demo [get_filesets sources_1]
update_compile_order -fileset sources_1
puts "VMCP_DEMO:created=$__otter_demo_dir"
