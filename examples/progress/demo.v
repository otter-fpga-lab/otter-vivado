// Tiny observation-only demo; no board-specific pinout or programming step.
module demo(input wire clk, output wire led);
    reg [23:0] counter = 0;
    always @(posedge clk) counter <= counter + 1'b1;
    assign led = counter[23];
endmodule
