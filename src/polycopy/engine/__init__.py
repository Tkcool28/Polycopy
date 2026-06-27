"""Engine package — orchestrates discovery, trade detection, and copyability scoring."""

from polycopy.engine.evaluate import evaluate_wallet
from polycopy.scoring.engine import score_wallet
from polycopy.domain.copyability import DataQuality

__all__ = [
    "evaluate_wallet",
    "score_wallet",
    "DataQuality",
]
