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
pCpu = Plot[cpuTime[k], {k, 4, 64},
   PlotRange -> All,
   PlotStyle -> Directive[Thick, Blue],
   AxesLabel -> {"k (Bucket Capacity)", "CPU Time (ms)"},
   PlotLabel -> "Total CPU Time vs Bucket Capacity",
   GridLines -> Automatic,
   Epilog -> {
     PointSize[Large], Green, 
     Point[{32, cpuTime[32]}], 
     Text["k=32", {32, cpuTime[32]}, {0, -1}]
   }
];

pNet = Plot[netPayload[k]/1000, {k, 4, 64},
   PlotRange -> All,
   PlotStyle -> Directive[Thick, Red],
   AxesLabel -> {"k (Bucket Capacity)", "Network Payload (kB)"},
   PlotLabel -> "Network Payload vs Bucket Capacity",
   GridLines -> Automatic
];

plot = GraphicsColumn[{pCpu, pNet}, PlotLabel -> "Protocol Trade-offs at Constant Aggregate Capacity (4096)"];

res = Export["tradeoff.pdf", plot];
If[res === $Failed,
  Print["Error exporting plot to tradeoff.pdf."]; Exit[1],
  Print["Plot exported to tradeoff.pdf."]
];
