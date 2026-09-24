from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass(frozen=True)
class WalletEvent:
    wallet: str
    token: str
    timestamp: int
    side: str
    amount_usd: float
    tx_hash: str = ""
    token_address: str = ""
    source: str = ""
    chain: str = ""
    token_amount: Optional[float] = None
    price_usd: Optional[float] = None
    confidence: float = 1.0

@dataclass
class WalletStats:
    wallet: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_usd: float = 0.0
    roi: Optional[float] = None
    profit_factor: Optional[float] = None
    max_drawdown: float = 0.0
    pre_pump_entries: int = 0
    pre_pump_successes: int = 0
    first_mover_score: float = 0.0

@dataclass
class Candidate:
    symbol: str
    timestamp: int
    wallet_score: float = 0.0
    project_score: float = 0.0
    volume_score: float = 0.0
    market_score: float = 0.0
    safety_score: float = 0.0
    technical_score: float = 0.0
    safety_blocked: bool = False
    evidence: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def class_name(self) -> str:
        wallet = self.wallet_score >= 60
        project = self.project_score >= 60
        volume = self.volume_score >= 55
        if wallet and (project or volume):
            return "C_CONFLUENCE"
        if wallet:
            return "A_WALLET_LED"
        if project:
            return "B_PROJECT_LED"
        return "WATCH"

    def score(self) -> float:
        if self.safety_blocked:
            return 0.0
        return round(max(0.0, min(100.0,
            self.wallet_score * .40 + self.project_score * .18 +
            self.volume_score * .16 + self.market_score * .10 +
            self.safety_score * .10 + self.technical_score * .06)), 2)
