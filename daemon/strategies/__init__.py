"""Strategy registry. Add a new strategy = add a module here + a config block."""

from strategies import (sweep, breakout, trend_pullback, rsi_reversion,
                        vol_squeeze, zscore_reversion)

REGISTRY = {m.NAME: m for m in (sweep, breakout, trend_pullback,
                                rsi_reversion, vol_squeeze, zscore_reversion)}


def enabled_strategies(cfg: dict) -> list[tuple]:
    """Yield (module, params, bracket) for enabled strategies, by priority."""
    out = []
    for name, scfg in cfg.get("strategies", {}).items():
        if not scfg.get("enabled"):
            continue
        if name not in REGISTRY:
            raise KeyError(f"Strategy '{name}' enabled in config but not in REGISTRY")
        out.append((scfg.get("priority", 99), REGISTRY[name], scfg.get("params", {}),
                    scfg.get("bracket", {})))
    return [(m, p, b) for _, m, p, b in sorted(out, key=lambda t: t[0])]
