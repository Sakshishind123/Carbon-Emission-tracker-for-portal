# """
# carbon_tracker.py  (v2 — full schema)
# ──────────────────────────────────────
# Tracks CPU + RAM + GPU carbon emissions and outputs a CSV that matches
# the codecarbon schema exactly:

#   timestamp, project_name, run_id, experiment_id, duration, emissions,
#   emissions_rate, cpu_power, gpu_power, ram_power, cpu_energy, gpu_energy,
#   ram_energy, energy_consumed, country_name, country_iso_code, region,
#   cloud_provider, cloud_region, os, python_version, codecarbon_version,
#   cpu_count, cpu_model, gpu_count, gpu_model, longitude, latitude,
#   ram_total_size, tracking_mode, on_cloud, pue

# No codecarbon library — built from psutil + stdlib only.

# Dependencies:
#   pip install psutil requests      (core)
#   pip install gputil               (optional — NVIDIA GPU stats)

# Usage:
#   python carbon_tracker.py --project "MyProject" --duration 60
#   python carbon_tracker.py --project "Training" --duration 300 --carbon-intensity 708 --pue 1.2
# """

# import argparse
# import csv
# import datetime
# import os
# import platform
# import subprocess
# import sys
# import time
# import uuid

# try:
#     import psutil
# except ImportError:
#     sys.exit("❌  Install psutil:  pip install psutil")

# try:
#     import GPUtil
#     GPU_AVAILABLE = True
# except ImportError:
#     GPU_AVAILABLE = False

# try:
#     import requests as _req
#     REQUESTS_AVAILABLE = True
# except ImportError:
#     REQUESTS_AVAILABLE = False


# # ─── Constants ────────────────────────────────────────────────────────────────

# TOOL_VERSION         = "2.0.0"
# FALLBACK_CPU_TDP_W   = 65.0
# RAM_WATTS_PER_GB     = 0.375
# DEFAULT_PUE          = 1.58
# DEFAULT_CI           = 475.0   # gCO₂/kWh  (IEA global average)

# CSV_FIELDS = [
#     "timestamp", "project_name", "run_id", "experiment_id",
#     "duration", "emissions", "emissions_rate",
#     "cpu_power", "gpu_power", "ram_power",
#     "cpu_energy", "gpu_energy", "ram_energy", "energy_consumed",
#     "country_name", "country_iso_code", "region",
#     "cloud_provider", "cloud_region",
#     "os", "python_version", "codecarbon_version",
#     "cpu_count", "cpu_model",
#     "gpu_count", "gpu_model",
#     "longitude", "latitude",
#     "ram_total_size", "tracking_mode", "on_cloud", "pue",
# ]


# # ─── Hardware helpers ─────────────────────────────────────────────────────────

# def get_cpu_model() -> str:
#     system = platform.system()
#     try:
#         if system == "Linux":
#             with open("/proc/cpuinfo") as f:
#                 for line in f:
#                     if line.startswith("model name"):
#                         return line.split(":", 1)[1].strip()
#         elif system == "Darwin":
#             out = subprocess.check_output(
#                 ["sysctl", "-n", "machdep.cpu.brand_string"], text=True
#             )
#             return out.strip()
#         elif system == "Windows":
#             out = subprocess.check_output(
#                 ["wmic", "cpu", "get", "Name"], text=True
#             )
#             lines = [l.strip() for l in out.splitlines()
#                      if l.strip() and l.strip() != "Name"]
#             if lines:
#                 return lines[0]
#     except Exception:
#         pass
#     return platform.processor() or "Unknown CPU"


# def get_cpu_tdp_watts() -> float:
#     if platform.system() == "Linux":
#         rapl = "/sys/class/powercap/intel-rapl/intel-rapl:0/constraint_0_power_limit_uw"
#         if os.path.exists(rapl):
#             try:
#                 with open(rapl) as f:
#                     return int(f.read().strip()) / 1_000_000
#             except Exception:
#                 pass
#     return FALLBACK_CPU_TDP_W


# def get_gpu_info() -> tuple:
#     """Returns (count:int, model_str:str, power_watts:float)."""
#     if not GPU_AVAILABLE:
#         return 0, "N/A", 0.0
#     try:
#         gpus = GPUtil.getGPUs()
#         if not gpus:
#             return 0, "N/A", 0.0
#         names = ", ".join(g.name for g in gpus)
#         power = sum(150.0 * (g.load or 0.0) for g in gpus)
#         return len(gpus), names, round(power, 4)
#     except Exception:
#         return 0, "N/A", 0.0


# # ─── Geo / cloud detection ────────────────────────────────────────────────────

# def detect_cloud() -> tuple:
#     """Returns (provider:str, region:str) or ('N/A', 'N/A')."""
#     if not REQUESTS_AVAILABLE:
#         return "N/A", "N/A"
#     checks = [
#         ("aws",   "http://169.254.169.254/latest/meta-data/placement/region", {}),
#         ("gcp",   "http://metadata.google.internal/computeMetadata/v1/instance/zone",
#                   {"Metadata-Flavor": "Google"}),
#         ("azure", ("http://169.254.169.254/metadata/instance/compute/"
#                    "location?api-version=2021-02-01"),
#                   {"Metadata": "true"}),
#     ]
#     for provider, url, headers in checks:
#         try:
#             r = _req.get(url, timeout=2, headers=headers)
#             if r.status_code == 200:
#                 region = r.text.strip().split("/")[-1]
#                 return provider, region
#         except Exception:
#             pass
#     return "N/A", "N/A"


# def geo_lookup() -> dict:
#     """Use ip-api.com (free, no key) for country/region/lat/lon."""
#     if not REQUESTS_AVAILABLE:
#         return {}
#     try:
#         r = _req.get("http://ip-api.com/json/", timeout=5)
#         if r.status_code == 200:
#             return r.json()
#     except Exception:
#         pass
#     return {}


# # ─── Power model ─────────────────────────────────────────────────────────────

# def cpu_power_w(cpu_pct: float) -> float:
#     tdp = get_cpu_tdp_watts()
#     return tdp * (0.30 + 0.70 * cpu_pct / 100.0)


# def ram_power_w(ram_used_gb: float) -> float:
#     return ram_used_gb * RAM_WATTS_PER_GB


# def to_kwh(watts: float, seconds: float) -> float:
#     return watts * seconds / 3_600_000


# # ─── Tracker class ────────────────────────────────────────────────────────────

# class CarbonTracker:
#     def __init__(
#         self,
#         project_name:     str   = "default",
#         experiment_id:    str   = "",
#         duration_seconds: float = 60.0,
#         interval_seconds: float = 5.0,
#         output_csv:       str   = "emissions.csv",
#         carbon_intensity: float = DEFAULT_CI,
#         pue:              float = DEFAULT_PUE,
#         tracking_mode:    str   = "process",
#     ):
#         self.project_name    = project_name
#         self.run_id          = str(uuid.uuid4())
#         self.experiment_id   = experiment_id or str(uuid.uuid4())
#         self.duration        = duration_seconds
#         self.interval        = interval_seconds
#         self.output_csv      = output_csv
#         self.carbon_intensity = carbon_intensity
#         self.pue             = pue
#         self.tracking_mode   = tracking_mode
#         self._samples: list  = []
#         self._start: float   = 0.0
#         self._sys            = {}

#     # ── One-time system snapshot ─────────────────────────────────────────────

#     def _collect_sys(self) -> dict:
#         print("  🔍  Collecting system info …", end=" ", flush=True)
#         geo = geo_lookup()
#         cloud_provider, cloud_region = detect_cloud()
#         gpu_count, gpu_model, _      = get_gpu_info()
#         on_cloud                     = "Y" if cloud_provider != "N/A" else "N"
#         print("done")
#         return {
#             "os":                 f"{platform.system()} {platform.release()} ({platform.machine()})",
#             "python_version":     platform.python_version(),
#             "codecarbon_version": TOOL_VERSION,
#             "cpu_count":          psutil.cpu_count(logical=True),
#             "cpu_model":          get_cpu_model(),
#             "gpu_count":          gpu_count,
#             "gpu_model":          gpu_model,
#             "ram_total_size":     round(psutil.virtual_memory().total / 1024**3, 3),
#             "country_name":       geo.get("country", "Unknown"),
#             "country_iso_code":   geo.get("countryCode", "N/A"),
#             "region":             geo.get("regionName", "Unknown"),
#             "longitude":          geo.get("lon", 0.0),
#             "latitude":           geo.get("lat", 0.0),
#             "cloud_provider":     cloud_provider,
#             "cloud_region":       cloud_region,
#             "on_cloud":           on_cloud,
#             "pue":                self.pue,
#             "tracking_mode":      self.tracking_mode,
#         }

#     # ── Sample ───────────────────────────────────────────────────────────────

#     def _sample(self, elapsed: float) -> dict:
#         cpu_pct       = psutil.cpu_percent(interval=None)
#         ram_used_gb   = psutil.virtual_memory().used / 1024**3
#         _, _, gpu_pw  = get_gpu_info()

#         cp = cpu_power_w(cpu_pct)
#         rp = ram_power_w(ram_used_gb)
#         gp = gpu_pw

#         total_w       = (cp + rp + gp) * self.pue
#         cpu_e         = to_kwh(cp,    self.interval)
#         ram_e         = to_kwh(rp,    self.interval)
#         gpu_e         = to_kwh(gp,    self.interval)
#         energy        = to_kwh(total_w, self.interval)

#         emissions_kg  = energy * self.carbon_intensity / 1000  # g→kg
#         rate_kg_s     = emissions_kg / max(self.interval, 1)

#         row = {
#             "timestamp":          datetime.datetime.now().isoformat(timespec="seconds"),
#             "project_name":       self.project_name,
#             "run_id":             self.run_id,
#             "experiment_id":      self.experiment_id,
#             "duration":           round(elapsed + self.interval, 2),
#             "emissions":          round(emissions_kg, 12),
#             "emissions_rate":     round(rate_kg_s, 12),
#             "cpu_power":          round(cp, 4),
#             "gpu_power":          round(gp, 4),
#             "ram_power":          round(rp, 4),
#             "cpu_energy":         round(cpu_e, 12),
#             "gpu_energy":         round(gpu_e, 12),
#             "ram_energy":         round(ram_e, 12),
#             "energy_consumed":    round(energy, 12),
#         }
#         row.update(self._sys)
#         return row

#     # ── Run loop ─────────────────────────────────────────────────────────────

#     def run(self) -> None:
#         self._sys   = self._collect_sys()
#         self._start = time.time()
#         self._samples = []
#         self._print_header()
#         elapsed = 0.0
#         try:
#             while elapsed < self.duration:
#                 s = self._sample(elapsed)
#                 self._samples.append(s)
#                 self._print_row(s)
#                 time.sleep(self.interval)
#                 elapsed = time.time() - self._start
#         except KeyboardInterrupt:
#             print("\n  ⚠️  Interrupted — saving …")
#         self._write_csv()
#         self._print_summary()

#     # ── Context manager (wrap a block of code) ───────────────────────────────

#     def __enter__(self):
#         self._sys     = self._collect_sys()
#         self._start   = time.time()
#         self._samples = []
#         self._print_header()
#         return self

#     def __exit__(self, *_):
#         elapsed = time.time() - self._start
#         s = self._sample(elapsed)
#         self._samples.append(s)
#         self._print_row(s)
#         self._write_csv()
#         self._print_summary()

#     # ── CSV ───────────────────────────────────────────────────────────────────

#     def _write_csv(self) -> None:
#         if not self._samples:
#             return
#         write_header = not os.path.exists(self.output_csv)
#         with open(self.output_csv, "a", newline="", encoding="utf-8") as f:
#             w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
#             if write_header:
#                 w.writeheader()
#             w.writerows(self._samples)
#         print(f"\n  💾  Saved → {self.output_csv}")

#     # ── Print helpers ─────────────────────────────────────────────────────────

#     def _print_header(self) -> None:
#         s = self._sys
#         print()
#         print("=" * 70)
#         print("  🌍  Carbon Tracker  —  full codecarbon-compatible schema")
#         print("=" * 70)
#         print(f"  Project        : {self.project_name}")
#         print(f"  Run ID         : {self.run_id}")
#         print(f"  Experiment ID  : {self.experiment_id}")
#         print(f"  Duration       : {self.duration}s   Interval: {self.interval}s")
#         print(f"  CPU            : {s['cpu_model']}  ({s['cpu_count']} logical cores)")
#         print(f"  GPU            : {s['gpu_count']}× {s['gpu_model']}")
#         print(f"  RAM total      : {s['ram_total_size']} GB")
#         print(f"  OS             : {s['os']}")
#         print(f"  Python         : {s['python_version']}")
#         print(f"  Location       : {s['country_name']} ({s['country_iso_code']})  "
#               f"{s['region']}  [{s['latitude']}, {s['longitude']}]")
#         print(f"  Cloud          : on_cloud={s['on_cloud']}  "
#               f"provider={s['cloud_provider']}  region={s['cloud_region']}")
#         print(f"  PUE            : {self.pue}   Intensity: {self.carbon_intensity} gCO₂/kWh")
#         print(f"  Tracking mode  : {self.tracking_mode}")
#         print("=" * 70)
#         print(f"  {'Timestamp':<22} {'CPUw':>7} {'GPUw':>7} {'RAMw':>7} "
#               f"{'kWh':>13} {'CO₂(µg)':>12}")
#         print("  " + "─" * 68)

#     def _print_row(self, s: dict) -> None:
#         print(
#             f"  {s['timestamp']:<22} "
#             f"{s['cpu_power']:>7.2f} "
#             f"{s['gpu_power']:>7.2f} "
#             f"{s['ram_power']:>7.2f} "
#             f"{s['energy_consumed']:>13.9f} "
#             f"{s['emissions'] * 1e9:>10.4f}"
#         )

#     def _print_summary(self) -> None:
#         if not self._samples:
#             return
#         te   = sum(s["energy_consumed"] for s in self._samples)
#         tco2 = sum(s["emissions"]       for s in self._samples)
#         n    = len(self._samples)
#         print()
#         print("=" * 70)
#         print("  📊  Run Summary")
#         print("=" * 70)
#         print(f"  Samples          : {n}")
#         print(f"  Total energy     : {te:.8f} kWh  ({te*1000:.5f} Wh)")
#         print(f"  Total CO₂        : {tco2*1000:.6f} g  ({tco2:.8f} kg)")
#         print(f"  Avg CPU power    : {sum(s['cpu_power'] for s in self._samples)/n:.2f} W")
#         print(f"  Avg GPU power    : {sum(s['gpu_power'] for s in self._samples)/n:.2f} W")
#         print(f"  Avg RAM power    : {sum(s['ram_power'] for s in self._samples)/n:.2f} W")
#         print(f"  Emissions rate   : {(tco2/max(n*self.interval,1))*1e6:.4f} µg/s")
#         print(f"  Output CSV       : {self.output_csv}")
#         print("=" * 70)
#         print()


# # ─── CLI ─────────────────────────────────────────────────────────────────────

# if __name__ == "__main__":
#     p = argparse.ArgumentParser(description="Carbon tracker — codecarbon-compatible CSV schema")
#     p.add_argument("--project",           default="default")
#     p.add_argument("--experiment-id",     default="")
#     p.add_argument("--duration",          type=float, default=60)
#     p.add_argument("--interval",          type=float, default=5)
#     p.add_argument("--output",            default="emissions.csv")
#     p.add_argument("--carbon-intensity",  type=float, default=DEFAULT_CI,
#                    help=f"gCO₂/kWh  (default {DEFAULT_CI} = IEA global avg)")
#     p.add_argument("--pue",               type=float, default=DEFAULT_PUE)
#     p.add_argument("--tracking-mode",     default="process")
#     args = p.parse_args()

#     CarbonTracker(
#         project_name     = args.project,
#         experiment_id    = args.experiment_id,
#         duration_seconds = args.duration,
#         interval_seconds = args.interval,
#         output_csv       = args.output,
#         carbon_intensity = args.carbon_intensity,
#         pue              = args.pue,
#         tracking_mode    = args.tracking_mode,
#     ).run()









import psutil, time, platform, os

TDP_W = 65
RAM_W_PER_GB = 0.375
PUE = 1.58
CI = 475  # gCO₂/kWh — change to match your grid

def cpu_power(pct): return TDP_W * (0.30 + 0.70 * pct / 100) * PUE
def ram_power(mb):  return (mb / 1024) * RAM_W_PER_GB * PUE
def co2_rate(w):    return w / 3_600_000 * CI * 1e6  # µg/s

print(f"{'Process':<22} {'PID':>6} {'CPU%':>6} {'RAM MB':>8} {'CPUw':>7} {'RAMw':>7} {'CO₂ µg/s':>10}")
print("-" * 70)

procs = []
for p in psutil.process_iter(['pid','name','memory_info']):
    try:
        p.cpu_percent()  # prime the counter
        procs.append(p)
    except: pass

time.sleep(1)  # let CPU % settle

rows = []
for p in procs:
    try:
        cpu = p.cpu_percent()
        ram = p.memory_info().rss / 1024**2
        cw  = cpu_power(cpu)
        rw  = ram_power(ram)
        rows.append((p.info['name'], p.pid, cpu, ram, cw, rw, co2_rate(cw+rw)))
    except: pass

for r in sorted(rows, key=lambda x: -x[6])[:20]:
    print(f"{r[0]:<22} {r[1]:>6} {r[2]:>6.1f} {r[3]:>8.1f} {r[4]:>7.2f} {r[5]:>7.3f} {r[6]:>10.4f}")