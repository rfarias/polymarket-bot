from dataclasses import dataclass
from typing import Optional, Literal

TIMEFRAME_SECONDS = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
}

Action = Literal[
    "idle",
    "post_both_limits",
    "keep_waiting_for_fills",
    "cancel_both_and_rotate_next2",
    "hold_locked_arb_until_resolution",
    "cancel_other_and_place_profit_exit_limit",
    "cancel_other_and_place_scratch_exit_limit",
    "wait_scratch_fill_after_open",
    "stop_remaining_position",
    "abort_small_partial",
]


@dataclass
class ArbConfig:
    per_leg_target_qty: float = 10.0
    min_partial_qty: float = 5.0
    rotate_to_next2_if_no_fills_secs_to_open: int = 45
    final_single_leg_management_secs_to_open: int = 30
    scratch_wait_after_open_secs: int = 5
    stop_ticks_after_open: int = 4
    tick_size: int = 1


@dataclass
class ArbSlot:
    title: str
    slug: str
    timeframe: str
    seconds_to_end: int

    @property
    def seconds_to_open(self) -> int:
        duration = TIMEFRAME_SECONDS[self.timeframe]
        return int(self.seconds_to_end - duration)


@dataclass
class ArbSnapshot:
    slot: ArbSlot
    yes_filled_qty: float = 0.0
    no_filled_qty: float = 0.0
    other_order_cancelled: bool = False
    exit_order_live: bool = False
    seconds_since_open: int = 0
    entry_price_open_leg: Optional[int] = None
    current_executable_exit_price: Optional[int] = None
    next2_has_liquidity: bool = False

    @property
    def total_filled_legs(self) -> int:
        legs = 0
        if self.yes_filled_qty > 0:
            legs += 1
        if self.no_filled_qty > 0:
            legs += 1
        return legs

    @property
    def one_leg_qty(self) -> float:
        return max(self.yes_filled_qty, self.no_filled_qty)


@dataclass
class ArbDecision:
    action: Action
    reason: str


def decide_preopen_arb(snapshot: ArbSnapshot, config: ArbConfig) -> ArbDecision:
    secs_to_open = snapshot.slot.seconds_to_open
    filled_legs = snapshot.total_filled_legs

    # 2 pernas executadas = arbitragem travada
    if filled_legs == 2:
        return ArbDecision(
            action="hold_locked_arb_until_resolution",
            reason="both legs filled; arbitrage is locked",
        )

    # parcial muito pequeno nao compensa a gestao
    if filled_legs == 1 and snapshot.one_leg_qty < config.min_partial_qty:
        return ArbDecision(
            action="abort_small_partial",
            reason="single-leg fill is below useful minimum size",
        )

    # nenhuma executou: aos 45s gira para o next_2, se houver liquidez
    if filled_legs == 0 and secs_to_open <= config.rotate_to_next2_if_no_fills_secs_to_open:
        if snapshot.next2_has_liquidity:
            return ArbDecision(
                action="cancel_both_and_rotate_next2",
                reason="no fills by 45s-to-open; rotate to next_2",
            )
        return ArbDecision(
            action="idle",
            reason="no fills by 45s-to-open, but next_2 has no usable liquidity",
        )

    # nenhuma executou mas ainda ha tempo
    if filled_legs == 0:
        return ArbDecision(
            action="keep_waiting_for_fills",
            reason="both orders still pending and there is time before open",
        )

    # uma perna executou: aos 30s finais cancela a oposta e muda a gestao
    if filled_legs == 1 and secs_to_open <= config.final_single_leg_management_secs_to_open and not snapshot.other_order_cancelled:
        if snapshot.current_executable_exit_price is not None and snapshot.entry_price_open_leg is not None:
            if snapshot.current_executable_exit_price >= snapshot.entry_price_open_leg:
                return ArbDecision(
                    action="cancel_other_and_place_profit_exit_limit",
                    reason="single leg in profit inside final 30s window",
                )
        return ArbDecision(
            action="cancel_other_and_place_scratch_exit_limit",
            reason="single leg not locked by final 30s; try scratch at entry price",
        )

    # uma perna executou e ainda estamos antes da janela final
    if filled_legs == 1 and secs_to_open > config.final_single_leg_management_secs_to_open:
        return ArbDecision(
            action="keep_waiting_for_fills",
            reason="single leg filled; still trying to lock second leg before final 30s",
        )

    # mercado abriu e estamos esperando o scratch/profit fill
    if filled_legs == 1 and snapshot.exit_order_live and secs_to_open <= 0 and snapshot.seconds_since_open <= config.scratch_wait_after_open_secs:
        return ArbDecision(
            action="wait_scratch_fill_after_open",
            reason="market opened; still inside scratch wait window",
        )

    # mercado abriu, ordem nao executou e ja passou a janela -> stop
    if filled_legs == 1 and snapshot.exit_order_live and secs_to_open <= 0 and snapshot.seconds_since_open > config.scratch_wait_after_open_secs:
        if snapshot.entry_price_open_leg is not None and snapshot.current_executable_exit_price is not None:
            if snapshot.current_executable_exit_price <= snapshot.entry_price_open_leg - config.stop_ticks_after_open * config.tick_size:
                return ArbDecision(
                    action="stop_remaining_position",
                    reason="scratch failed after open and stop threshold reached",
                )
        return ArbDecision(
            action="wait_scratch_fill_after_open",
            reason="scratch failed after open, but stop threshold not reached yet",
        )

    return ArbDecision(action="idle", reason="no rule matched")
