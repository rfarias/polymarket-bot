from pprint import pprint

from market.broker_env import load_broker_env
from market.live_guarded_config import load_live_guarded_config


def main():
    broker_status = load_broker_env()
    cfg = load_live_guarded_config()

    print("[TEST] Starting live guarded preflight...")
    print("[BROKER_ENV]")
    pprint(broker_status.as_dict())
    print("[LIVE_GUARDED_CONFIG]")
    pprint(cfg.as_dict())

    if not cfg.enabled:
        print("[RESULT] Guarded runner is disabled. Set POLY_GUARDED_ENABLED=true to arm it.")
        return

    if not broker_status.ready_for_real_smoke:
        print("[RESULT] Broker env is not ready for real-shadow/live. Fix broker env first.")
        return

    if not cfg.shadow_only and not cfg.real_posts_enabled:
        print("[RESULT] Refusing to run with real posts because POLY_GUARDED_REAL_POSTS_ENABLED is false.")
        return

    if not cfg.shadow_only and cfg.real_posts_enabled:
        print("[RESULT] Real posting remains intentionally blocked in v1 until live fill reconciliation + real flatten are implemented.")
        return

    print("[RESULT] Ready for guarded real-shadow monitoring.")


if __name__ == "__main__":
    main()
