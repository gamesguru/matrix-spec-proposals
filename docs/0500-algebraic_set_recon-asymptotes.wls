#!/usr/bin/env wolframscript

(* Constants *)
aggCap = 4096;
(* Header overhead per request in bytes *)
H = 64; 

(* Performance models *)
(* CPU time in ms (based on your benchmarks: T(k) ≈ 0.00094 * k^2) *)
cpuTime[k_] := (aggCap / k) * (0.00094 * k^2);

(* Network overhead in bytes *)
netPayload[k_] := (aggCap / k) * (H + 8 * k);

(* Generate Plots *)
plot = Plot[{cpuTime[k], netPayload[k]/1000}, {k, 4, 64},
   PlotRange -> All,
   PlotStyle -> {Directive[Thick, Blue], Directive[Thick, Red]},
   PlotLegends -> {"Total CPU Time (ms)", "Network Payload (kB)"},
   AxesLabel -> {"k (Bucket Capacity)", "Cost"},
   PlotLabel -> "Protocol Trade-offs at Constant Aggregate Capacity (4096)",
   GridLines -> Automatic,
   Epilog -> {
     PointSize[Large], Green, 
     Point[{32, cpuTime[32]}], 
     Text["k=32", {32, cpuTime[32]}, {0, -1}]
   }
];

Export["tradeoff.pdf", plot];
Print["Plot exported to tradeoff.pdf."];
