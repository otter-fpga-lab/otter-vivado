# 在空闲的空会话中设置新目录 __otter_demo_dir 和已安装/有许可的 __otter_demo_part。
# 只创建原生磁盘工程，导入样例源和约束；不启动 run、不连接设备。
if {[catch {
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
    foreach __otter_demo_input {demo.v demo.xdc} {
        if {![file isfile [file join $__otter_demo_source $__otter_demo_input]]} {
            error "Missing demo input: $__otter_demo_input"
        }
    }
    create_project otter_progress_demo $__otter_demo_dir -part $__otter_demo_part
    # 消费工程持有自己的输入，GUI 重开不依赖 Otter 安装位置；不复制工具/Skill。
    import_files -fileset sources_1 -flat [list [file join $__otter_demo_source demo.v]]
    import_files -fileset constrs_1 -flat [list [file join $__otter_demo_source demo.xdc]]
    set_property top demo [get_filesets sources_1]
    update_compile_order -fileset sources_1
    # GUI 与 Tcl 创建模式的实际目录可能不同，以 Vivado 返回的当前工程为准。
    set __otter_demo_project [current_project]
    set __otter_demo_actual_dir [get_property DIRECTORY $__otter_demo_project]
    set __otter_demo_xpr [file normalize [file join $__otter_demo_actual_dir \
        "[get_property NAME $__otter_demo_project].xpr"]]
    if {![file isfile $__otter_demo_xpr]} {
        error "Native project file was not found; inspect the current project before retrying"
    }
    puts "VMCP_DEMO:created=$__otter_demo_actual_dir"
    puts "VMCP_DEMO:project_file=$__otter_demo_xpr"
    puts "VMCP_DEMO:sources=imported"
} __otter_demo_error]} {
    # 创建中途失败时保留现场，不自动关闭工程或删除部分输出。
    puts "VMCP_DEMO_ERR:$__otter_demo_error"
}
