"""Can this computer run a given local (Ollama) model well enough for circuit design?

Checks before a run (GPU memory, system memory, model size) and a live speed
check during a run. Returns plain explanations the AI window shows to the user.
"""
import ctypes
import json
import os
import re
import shutil
import subprocess
import urllib.request

# below this speed a full design (several thousand tokens, several attempts) takes too long
MIN_TOKENS_PER_S = 2.0
# models smaller than this make many design mistakes
MIN_GOOD_PARAMS_B = 25


def gpus():
    """[(name, vram_GB)] for NVIDIA GPUs (via nvidia-smi). Empty if none found."""
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe"
                                         if os.path.exists(r"C:\Windows\System32\nvidia-smi.exe") else None)
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except Exception:
        return []
    res = []
    for line in out.strip().splitlines():
        try:
            name, mb = [x.strip() for x in line.rsplit(",", 1)]
            res.append((name, round(float(mb) / 1024, 1)))
        except ValueError:
            pass
    return res


def ram_gb():
    try:
        if os.name == "nt":
            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("x", ctypes.c_ulonglong)]
            m = MS(); m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return round(m.total / 2 ** 30, 1)
        with open("/proc/meminfo") as f:
            return round(int(f.readline().split()[1]) / 2 ** 20, 1)
    except Exception:
        return None


def ollama_model_info(base_url, model):
    """{'size_gb', 'params_b', 'moe'} from Ollama, or None."""
    root = base_url.rstrip("/")
    root = root[:-3] if root.endswith("/v1") else root
    try:
        with urllib.request.urlopen(root + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode("utf-8")).get("models", [])
    except Exception:
        return None
    m = next((t for t in tags if t.get("name") == model or t.get("model") == model), None)
    if not m:
        return None
    det = m.get("details", {}) or {}
    ps = str(det.get("parameter_size", ""))
    num = re.match(r"([\d.]+)\s*([BM])", ps.upper())
    params = float(num.group(1)) / (1000 if num and num.group(2) == "M" else 1) if num else None
    fam = (det.get("family", "") + " " + model).lower()
    moe = any(k in fam for k in ("gptoss", "gpt-oss", "moe", "-a3b", "a22b", "mixtral", "qwen3-coder:30b",
                                 "qwen3:30b", "deepseek-v"))
    return {"size_gb": round(m.get("size", 0) / 2 ** 30, 1), "params_b": params, "moe": moe}


def assess(base_url, model):
    """-> (ok: bool, level: 'good'|'slow'|'weak'|'no', title, explanation)"""
    info = ollama_model_info(base_url, model)
    g = gpus()
    vram = max((v for _, v in g), default=0.0)
    gname = g[0][0] if g else "no NVIDIA GPU found"
    ram = ram_gb() or 0.0
    spec = "This computer: %s (%.0f GB graphics memory), %.0f GB RAM." % (gname, vram, ram)
    if info is None:
        return True, "good", "Model size unknown", spec + " Could not read the model's size from Ollama, so it was not checked."
    size, params, moe = info["size_gb"], info["params_b"], info["moe"]
    need = size * 1.15 + 1.5                       # weights + context cache + overhead
    mdesc = "Model %s: %.1f GB%s." % (model, size, (", %.0fB parameters" % params) if params else "")
    on_gpu = min(1.0, vram / need) if need else 1.0

    if need > vram + ram * 0.75:
        return False, "no", "This computer cannot run this model", (
            "%s %s It needs about %.0f GB of memory in total, more than this computer has. "
            "Choose a smaller model, or use an online provider (Claude, OpenAI, DeepSeek) in Settings." % (spec, mdesc, need))
    # mixture-of-experts models only use a small part per token, so they tolerate CPU offload
    min_share = 0.35 if moe else 0.6
    if on_gpu < min_share:
        return False, "slow", "Too slow on this computer", (
            "%s %s Only about %d%% of it fits in graphics memory; the rest would run on the processor, "
            "so one design would take tens of minutes or time out. For local design you need a GPU with "
            "about %.0f GB or more. Otherwise use an online provider (Claude, OpenAI, DeepSeek) in Settings, "
            "or a smaller model (faster, but less accurate)." % (spec, mdesc, on_gpu * 100, need * min_share + 0.5))
    if params is not None and params < MIN_GOOD_PARAMS_B:
        return True, "weak", "Runs, but designs will often be wrong", (
            "%s %s Models below about %dB parameters make many wiring and part mistakes. The checks stop "
            "wrong designs from reaching your schematic, but expect several attempts or a failure. "
            "For reliable designs use a 30B+ model on a 24 GB GPU, or an online provider." % (spec, mdesc, MIN_GOOD_PARAMS_B))
    return True, "good", "This computer can run this model", spec + " " + mdesc


def too_slow(tokens, seconds):
    """Live check: True once we are sure the speed is below the minimum."""
    return seconds > 90 and tokens / max(seconds, 1) < MIN_TOKENS_PER_S
