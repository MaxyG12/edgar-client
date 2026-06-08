# ClickLite — Simple Python CLI Framework

## What Problem Does It Solve?

Writing command-line interfaces in Python normally means manually parsing arguments, checking types, and routing to different functions. ClickLite solves this by letting you **describe what your CLI looks like using decorators**, then it handles all the mechanics: parsing `sys.argv`, converting strings to the right types, printing help text, and routing commands. You write a normal Python function; ClickLite turns it into a professional CLI with almost no extra code.

---

## Installation

Copy `clicklite_v2.py` into your project:

```bash
cp clicklite_v2.py /path/to/your/project/
```

Then import:

```python
from clicklite_v2 import command, option, group, middleware
```

---

## Quick Start — 5 Lines of Code

```python
from clicklite_v2 import command, option, run

@command
@option("--name", default="world", help="Who to greet")
def hello(name):
    print(f"Hello, {name}!")

if __name__ == "__main__":
    run(hello)
```

Run it:
```bash
$ python hello.py --name Alice
Hello, Alice!

$ python hello.py --help
Hello, world!

Options:
  --name TEXT    Who to greet.
  -h, --help     Show this message and exit.
```

---

## Full API Reference

### @command — Turn a function into a CLI command

```python
from clicklite_v2 import command, option, run

@command
@option("--count", type=INT, default=1, help="How many times")
def greet(count):
    for _ in range(count):
        print("Hello!")

run(greet)
```

**Parameters:**
- `name` (optional): Custom command name. Default: function name with underscores → dashes.
- `help`: Help text. Default: the function's docstring.

**How it works:** Converts a function with `@option` decorators into a `Command` object that can parse arguments and run.

---

### @option — Add a named parameter to a command

```python
@command
@option("--name", "-n", default="Alice", help="Your name")
@option("--age", type=INT, default=18, help="Your age")
@option("--verbose", is_flag=True, help="Show more details")
def profile(name, age, verbose):
    print(f"Name: {name}, Age: {age}")
    if verbose:
        print(f"  (verbose mode)")

run(profile)
```

**Parameters:**
- `*names`: One or more names for this option, e.g., `"--name"`, `"-n"`, or both `"--name", "-n"`.
- `type`: Type to convert the string to. Built-in: `STRING`, `INT`, `Choice(["a", "b"])`. Default: `STRING`.
- `default`: Value if not provided. If `None`, the option is required.
- `required`: Force the user to supply it (usually you use `default=None` instead).
- `is_flag`: If `True`, this is a boolean flag (no value needed). `--verbose` sets it to `True`.
- `validate`: A function that returns `True` or `False`. Checked after type conversion.
- `error`: Message shown if `validate` returns `False`.
- `help`: Help text shown in `--help`.

**How it works:** Each `@option` decorator stacks metadata on the function. When `@command` runs, it collects all of them and builds the parameter list.

---

### @group — Nest commands under a parent

```python
from clicklite_v2 import group, command

@group
@option("--verbose", is_flag=True, help="Show details")
def cli(verbose):
    if verbose:
        print("[Verbose mode on]")

@cli.command
@option("--name", default="world")
def greet(name):
    print(f"Hello, {name}!")

@cli.command
@option("--target", required=True)
def deploy(target):
    print(f"Deploying to {target}...")

run(cli)
```

Run:
```bash
$ python app.py greet --name Alice
Hello, Alice!

$ python app.py deploy --target prod
Deploying to prod...

$ python app.py --help
Usage: app.py [OPTIONS] COMMAND [ARGS]...

Options:
  --verbose    Show details
  -h, --help   Show this message and exit.

Commands:
  greet      
  deploy     
```

**How it works:** A `Group` is like a command, but it has sub-commands. The group's callback runs *before* the sub-command. It's the right place for setup logic (like checking credentials, loading config, enabling logging).

**Registering commands:**
- Use the `@group.command` decorator (inline registration, as above).
- Or call `group.add_command(cmd)` to register an already-built command.

```python
greet_cmd = command(lambda name="world": print(f"Hello, {name}!"))(None)
cli.add_command(greet_cmd, name="greet")
```

---

### @middleware — Wrap a command with cross-cutting logic

Middleware is a function that takes the callback and returns a modified version. Use it for logging, timing, error recovery, or any logic that should run around the command.

```python
def timing_middleware(next_fn):
    """Measure how long the command takes."""
    def wrapper(**kwargs):
        import time
        start = time.time()
        result = next_fn(**kwargs)
        elapsed = time.time() - start
        print(f"  [took {elapsed:.2f}s]")
        return result
    return wrapper

@command
@middleware(timing_middleware)
@option("--sleep", type=INT, default=1)
def slow_task(sleep):
    import time
    time.sleep(sleep)
    print("Done!")

run(slow_task)
```

Run:
```bash
$ python app.py --sleep 2
Done!
  [took 2.05s]
```

**How it works:** When the command runs, middleware wraps the callback. You can attach multiple middlewares; they nest from outermost (added last) to innermost.

**Signature:** `middleware_fn(next_fn: Callable) -> Callable`

The middleware receives the actual callback as `next_fn`, and returns a new function that can do work before and after calling `next_fn(**kwargs)`.

---

### apply_options — Reuse option sets across commands

Instead of repeating the same `@option` decorators on every command, bundle them and apply once:

```python
auth_options = [
    OptionDef(["--token", "-t"], required=True, help="API token"),
    OptionDef(["--user", "-u"], required=True, help="Username"),
]

@command
@apply_options(auth_options)
@option("--format", type=Choice(["json", "csv"]), default="json")
def export(token, user, format):
    print(f"[{user}] exporting as {format}...")

@command
@apply_options(auth_options)
@option("--limit", type=INT, default=10)
def list_items(token, user, limit):
    print(f"[{user}] listing {limit} items...")

run(export)  # or run(list_items)
```

Both commands get `--token` and `--user` without repetition.

---

## How the Decorator Pattern Works

When you write:

```python
@command
@option("--age", type=INT)
@option("--name")
def profile(name, age):
    print(f"{name} is {age}")
```

Python executes them **bottom-up**:

1. `@option("--name")` runs first. It doesn't change the function — it **attaches metadata** to it. The function now has an invisible attribute: `__clicklite_params__ = [OptionDef("--name", ...)]`.

2. `@option("--age", type=INT)` runs next. It **appends** another `OptionDef` to the same list: `__clicklite_params__ = [OptionDef("--name", ...), OptionDef("--age", ...)]`.

3. `@command` runs last. It **reads** that list, **reverses it** to restore declaration order, **removes** the attribute from the function, and **creates a `Command` object** with the parameters baked in.

After `@command` runs, the variable `profile` is no longer a function — it's a `Command` object that knows how to parse `--name` and `--age` and call the original function.

**Why reverse?** Decorators are applied bottom-up, so options are accumulated in reverse order. Reversing puts them back in the order you wrote them, which is what you want for help text.

**Key insight:** The function stays callable (it's just stored inside the `Command`), and the metadata travels with it. You can pass it around, import it, inspect it — and the `Command` doesn't need a global registry to work.

---

## How to Extend ClickLite

### Add custom middleware

Middleware is just a function. Write one that makes sense for your app:

```python
def require_admin(next_fn):
    """Check that the user is an admin before running."""
    def wrapper(**kwargs):
        import os
        if os.getenv("ADMIN") != "true":
            print("ERROR: Admin access required.")
            return
        return next_fn(**kwargs)
    return wrapper

@command
@middleware(require_admin)
def delete_all_data():
    print("Deleting...")
```

### Create custom types

Extend `ParamType` to handle domain-specific values:

```python
from clicklite_v2 import ParamType, CLError

class EmailType(ParamType):
    name = "EMAIL"
    
    def convert(self, value):
        if "@" not in value or "." not in value:
            self.fail(f"{value!r} is not a valid email.")
        return value

EMAIL = EmailType()

@command
@option("--contact", type=EMAIL, help="Email address")
def notify(contact):
    print(f"Notifying {contact}...")

run(notify)
```

### Compose option bundles

Organize options into reusable groups:

```python
from clicklite_v2 import OptionDef, STRING, INT

database_options = [
    OptionDef(["--db-host"], default="localhost", help="Database host"),
    OptionDef(["--db-port"], type=INT, default=5432, help="Database port"),
    OptionDef(["--db-user"], required=True, help="DB username"),
]

@command
@apply_options(database_options)
@option("--table", required=True)
def migrate(db_host, db_port, db_user, table):
    print(f"Migrating {table}@{db_host}:{db_port}...")

run(migrate)
```

---

## ClickLite v1 vs v2 — What Changed and Why

### v1 — Simple but rigid

**What it had:**
- `@command`, `@option`, `@group` decorators
- Basic type conversion
- Help text generation

**Limitation:** The parsing logic and command dispatch were intertwined with the CLI layer. If you wanted to use the same decorators for something other than CLIs (e.g., financial models, HTTP APIs), you'd have to copy and re-invent half the framework.

### v2 — Extensible architecture

**What changed:**

| Concern | v1 | v2 |
|---------|----|----|
| **Decorator accumulation** | `_make_command()` function | `Registry` class — reusable in any framework |
| **Execution model** | `invoke(argv)` hardcoded | Split into `invoke()` (input-specific) + `execute(**kwargs)` (stable seam) |
| **Type system** | `ParamType` | Same, but usable anywhere |
| **Error handling** | Flat `CLError` | Structured hierarchy: `UsageError`, `ValidationErrors`, with context fields |
| **Parsing** | Duplicated in `Command` and `Group` | Extracted into `Parser` class — one algorithm, one place to fix bugs |
| **Middleware** | Not present | Pluggable pipeline: `mw(next_fn) -> fn` |
| **Extension point** | Limited | `Handler.execute()` is the stable seam — any framework can sit below it |

**Why it matters:**

V2 's architecture lets you **build financial models, HTTP APIs, and other systems on top of the same core**. The `Registry`, `Handler`, `Executor`, and `middleware` pattern are completely framework-agnostic. A FinanceFramework can reuse them verbatim, swapping only the input source (dict instead of argv) and domain types.

**Code reuse:**
- **ClickLite v1** → CLI only
- **ClickLite v2** → CLI + FinanceFramework + any other domain that needs decorated functions, type coercion, and validation

---

## Examples

### Example 1: A simple database CLI

```python
from clicklite_v2 import group, command, option, run, INT, STRING

@group
def db():
    """Database utilities."""

@db.command
@option("--host", default="localhost")
@option("--port", type=INT, default=5432)
def connect(host, port):
    """Connect to the database."""
    print(f"Connecting to {host}:{port}...")

@db.command
@option("--table", required=True)
@option("--limit", type=INT, default=10)
def query(table, limit):
    """Run a query."""
    print(f"SELECT * FROM {table} LIMIT {limit}")

if __name__ == "__main__":
    run(db)
```

### Example 2: Multiple commands with shared options

```python
from clicklite_v2 import command, option, apply_options, OptionDef, STRING, run

auth = [
    OptionDef(["--api-key"], required=True, help="API key"),
    OptionDef(["--api-secret"], required=True, help="API secret"),
]

@command
@apply_options(auth)
def upload(api_key, api_secret):
    print(f"Uploading with key={api_key}")

@command
@apply_options(auth)
def download(api_key, api_secret):
    print(f"Downloading with key={api_key}")

# Run either one
if __name__ == "__main__":
    import sys
    cmd = upload if "upload" in sys.argv else download
    run(cmd)
```

### Example 3: Middleware for logging

```python
from clicklite_v2 import command, option, middleware, run
from datetime import datetime

def log_calls(next_fn):
    def wrapper(**kwargs):
        print(f"[{datetime.now().isoformat()}] Calling with {kwargs}")
        result = next_fn(**kwargs)
        print(f"[{datetime.now().isoformat()}] Complete")
        return result
    return wrapper

@command
@middleware(log_calls)
@option("--name")
def hello(name):
    return f"Hello, {name}!"

if __name__ == "__main__":
    run(hello)
```

---

## When to use ClickLite

**Good fit:**
- Small to medium CLIs (10–100 commands)
- Internal tools and scripts
- You want decorators and type safety without heavy dependencies
- You're learning Python and want to understand decorators in practice

**Not ideal for:**
- Very large CLIs with complex subgroup hierarchies (consider Click itself)
- Apps needing advanced features like shell completion or custom command routing

---

## License

ClickLite is a learning framework. Use and modify freely.

---

## Next Steps

- Read `clicklite_v2.py` — the full source is ~700 lines and heavily commented.
- Try `finance_framework.py` — see how ClickLite's core layers extend to financial modelling.
- Run `finance_demo.py` and `acquisition_demo.py` — concrete examples of ClickLite's extensibility.
