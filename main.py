from proxy_core import TfmProxy


HOST = "0.0.0.0"
MAIN_PORT = 11801
SATELLITE_PORT = 12801


def main():
    print("=" * 42)
    print(" TFM V1.11 - RESPAWN-SAFE RECORD / SERVER PLAY / AUTOLEARN")
    print("=" * 42)
    print(f"[LISTEN] MAIN      {HOST}:{MAIN_PORT}")
    print(f"[LISTEN] SATELLITE {HOST}:{SATELLITE_PORT}")
    print("[DB] robot_records.db")
    print("[COMMAND] /help")
    print("[COMMAND] /timelist [@mapCode]")
    print("[COMMAND] /timedelete [@mapCode|all]")
    print("[COMMAND] /record on | /record off")
    print("[COMMAND] /play on   | /play off")
    print("[COMMAND] /recordplayer Nick#0000 | /recordplayer off")
    print("[COMMAND] /playplayer Nick#0000   | /playplayer off")
    print()

    proxy = TfmProxy(
        host_address=HOST,
        host_main_port=MAIN_PORT,
        host_satellite_port=SATELLITE_PORT,
        host_socket_policy_port=None,
        expected_address="127.0.0.1",

        # Backend is supplied dynamically by the normal Proxy Loader.
        main_server_address=None,
        main_server_ports=None,
    )

    proxy.run()


if __name__ == "__main__":
    main()
