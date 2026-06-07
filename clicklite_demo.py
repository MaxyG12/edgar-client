"""
ClickLite demo — exercises commands, groups, types, and flags.
Run with: python clicklite_demo.py <subcommand> [options]
"""

from clicklite import Choice, INT, STRING, command, group, option, run


# ── Standalone command ────────────────────────────────────────────────────────

@command
@option("--name", "-n", default="world", help="Who to greet.")
@option("--count", "-c", type=INT, default=1, help="How many times.")
@option("--shout", is_flag=True, help="Uppercase the greeting.")
def greet(name, count, shout):
    """Print a greeting."""
    msg = f"Hello, {name}!"
    if shout:
        msg = msg.upper()
    for _ in range(count):
        print(msg)


# ── Group with subcommands ────────────────────────────────────────────────────

@group
@option("--verbose", "-v", is_flag=True, help="Enable verbose output.")
def db(verbose):
    """Database management commands."""
    if verbose:
        print("[verbose mode on]")


@db.command
@option("--host", default="localhost", help="DB host.")
@option("--port", "-p", type=INT, default=5432, help="DB port.")
def connect(host, port):
    """Connect to the database."""
    print(f"Connecting to {host}:{port}")


@db.command
@option("--table", required=True, help="Table to query.")
@option("--limit", type=INT, default=10, help="Max rows to return.")
def query(table, limit):
    """Run a query against the database."""
    print(f"SELECT * FROM {table} LIMIT {limit}")


# ── Nested group ──────────────────────────────────────────────────────────────

@group
def cli(verbose=False):
    """Top-level CLI with nested commands."""


@cli.command
@option("--format", type=Choice(["json", "csv", "table"]), default="table",
        help="Output format.")
@option("--output", "-o", default="-", help="Output file path.")
def export(format, output):
    """Export data to a file."""
    dest = "stdout" if output == "-" else output
    print(f"Exporting as {format} to {dest}")


cli.add_command(greet)   # Reuse the standalone command in a group
cli.add_command(db)      # Nest the db group inside cli


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Allow: python clicklite_demo.py greet --help
    #        python clicklite_demo.py db connect --host mydb
    #        python clicklite_demo.py export --format json
    #        python clicklite_demo.py greet --name Alice --count 3 --shout

    if len(sys.argv) < 2:
        print("Demo commands: greet | db | cli")
        print("  python clicklite_demo.py greet --name Alice --shout")
        print("  python clicklite_demo.py db --verbose connect --host mydb")
        print("  python clicklite_demo.py cli export --format json")
        sys.exit(0)

    top = sys.argv[1]
    rest = sys.argv[2:]

    commands = {"greet": greet, "db": db, "cli": cli}
    if top not in commands:
        print(f"Unknown top-level command: {top!r}. Choose from: {list(commands)}")
        sys.exit(1)

    run(commands[top], rest, prog_name=top)
