from proxy_core import TfmProxy


HOST = "0.0.0.0"
MAIN_PORT = 11801
SATELLITE_PORT = 12801


def main():
    print("=" * 62)
    print(" emirhankarakoc v1.10 - RECORD / RACING / AUTOLEARN")
    print("=" * 62)
    print(f"[LISTEN] MAIN      {HOST}:{MAIN_PORT}")
    print(f"[LISTEN] SATELLITE {HOST}:{SATELLITE_PORT}")
    print("[DB] robot_records.db")
    print("[RULE] BEST only")
    print("[RULE] minimum record = 8.000s")
    print()
    print("[COMMAND] /help")
    print("[COMMAND] /record on | off")
    print("[COMMAND] /play on | off")
    print("[COMMAND] /sismanlattrambolin on [W] [H] [gorunmezacik|gorunmezkapali] | off")
    print("[COMMAND] /sismanlatlav on [W] [H] [gorunmezacik|gorunmezkapali] | off")
    print("[COMMAND] /debuglogs on | off")
    print("[COMMAND] /chatafterfirst on | off | [message]")
    print("[COMMAND] /afkfarming on | off")
    print("[COMMAND] /recordplayer Nick#0000 | off")
    print("[COMMAND] /playplayer Nick#0000 | off")
    print("[COMMAND] /timelist             (all maps, MIRRORED YES/NO)")
    print("[COMMAND] /timelist @mapCode    (one map, MIRRORED YES/NO)")
    print("[COMMAND] /timedelete [ID|@mapCode|all]")
    print("[COMMAND] /timeowner @mapCode Nick#0000")
    print("[COMMAND] /blacklist add/remove/list/clear [Nick#0000]")
    print()

    proxy = TfmProxy(
        host_address=HOST,
        host_main_port=MAIN_PORT,
        host_satellite_port=SATELLITE_PORT,
        host_socket_policy_port=None,
        expected_address="127.0.0.1",
        main_server_address=None,
        main_server_ports=None,
    )

    proxy.run()


if __name__ == "__main__":
    main()
