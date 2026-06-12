"""Strategy registry. Add a new strategy = add a module here + a config block."""

from strategies import (sweep, breakout, trend_pullback, rsi_reversion,
                        vol_squeeze, zscore_reversion)

REGISTRY = {m.NAME: m for m in (sweep, breakout, trend_pullback,
                                rsi_reversion, vol_squeeze, zscore_reversion)}


def enabled_strategies(cfg: dict) -> list[tuple]:
    """Yield (module, params, bracket, shadow) for enabled strategies, by
    priority. shadow=True means the full pipeline runs but the executor
    SIMULATES the order (paper trade) — used to vet strategies live before
    they touch real capital."""
    out = []
    for name, scfg in cfg.get("strategies", {}).items():
        if not scfg.get("enabled"):
            continue
        if name not in REGISTRY:
            raise KeyError(f"Strategy '{name}' enabled in config but not in REGISTRY")
        out.append((scfg.get("priority", 99), REGISTRY[name], scfg.get("params", {}),
                    scfg.get("bracket", {}), bool(scfg.get("shadow", False))))
    return [(m, p, b, s) for _, m, p, b, s in sorted(out, key=lambda t: t[0])]
