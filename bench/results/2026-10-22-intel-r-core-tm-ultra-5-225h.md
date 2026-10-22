# nushell, baseline-stable, intel-r-core-tm-ultra-5-225h

What produced [the results file](2026-10-22-intel-r-core-tm-ultra-5-225h.jsonl), so the numbers can be
checked and repeated. Each JSON record also carries the machine, toolchain and commit.

- Workspace: `nushell/nushell` at commit `b632ef0994e07b5176ec45171e8cee2c127c5563` (nushell 0.116.1).
- Machine: Intel Core Ultra 5 225H, 14 cores, 15.1 GiB RAM, SSD, Linux 7.2.4 (CachyOS), `-j 14`.
- Toolchain: rustc 1.96.1 (31fca3adb 2026-06-26), cargo 1.96.1 (356927216 2026-06-26), linker `cc` (GCC 16.2.1).
- Configuration: `baseline-stable`, 5 runs of each of the seven scenarios (35 records), recorded 2026-10-22.
- Command: `python3 bench/run.py nushell --config baseline-stable --runs 5`
- Report over the file: `python3 bench/analyze.py bench/results/2026-10-22-intel-r-core-tm-ultra-5-225h.jsonl`

Single machine, so these are one machine's measurements, not a general result.
