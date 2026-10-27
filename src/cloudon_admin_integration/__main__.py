import sys

USAGE = "usage: python -m cloudon_admin_integration agent"


def main(argv: list[str]) -> None:
    if argv[:1] == ["agent"]:
        from cloudon_admin_integration.agent import main as agent_main

        agent_main()
        return
    raise SystemExit(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
