# 只验证原生 Tcl 传输与执行，不创建工程、不运行综合或实现。
set artifacts $::env(FPGA_REMOTE_ARTIFACT_DIR)
file mkdir [file join $artifacts nested]
if {[lindex $argv 1] eq "FAIL"} {
    set f [open [file join $artifacts failure.txt] w]
    puts $f "Intentional native Tcl failure fixture"
    close $f
    error "INTENTIONAL_NATIVE_TCL_FAILURE"
}
if {$argc != 4} {error "Expected four arguments, including an empty string; got $argc: $argv"}
if {[lindex $argv 1] ne {literal $value [command] {braces}}} {error "Literal Tcl argument changed"}
if {[lindex $argv 2] ne ""} {error "Empty Tcl argument changed"}
if {[lindex $argv 3] ne "中文"} {error "UTF-8 argument changed"}
set f [open [file join $artifacts nested arguments.txt] w]
fconfigure $f -encoding utf-8 -translation lf
puts $f "argc=$argc"
puts $f "literal=[lindex $argv 1]"
puts $f "unicode=[lindex $argv 3]"
puts $f "jobs=$::env(FPGA_REMOTE_JOBS)"
close $f
file copy [lindex $argv 0] [file join $artifacts copied.txt]
puts "NATIVE_TCL_SMOKE=PASS no_synthesis=1"
exit 0
